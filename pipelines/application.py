"""
Application pipeline — drains PENDING application tasks from the task queue.

For each task:
  1. Load the Job record
  2. Check rate limit (max_per_hour from settings.yaml)
  3. Check minimum match score
  4. Tailor resume (LLM bullet generation + DOCX + PDF)
  5. Run ApplicationRunner.run() (Playwright form-fill + submit)
  6. Mark task DONE or FAILED

Run modes:
  • make apply-queue        — process all queued tasks
  • make apply-job JOB_ID=x — apply to one specific job by ID
  • make apply-dry-run      — open browser, don't submit

Usage:
    .venv/bin/python -m pipelines.application
    .venv/bin/python -m pipelines.application --job-id <uuid>
    .venv/bin/python -m pipelines.application --dry-run
    .venv/bin/python -m pipelines.application --limit 5
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from loguru import logger

from automation.application_runner import ApplicationRunner, dry_run
from automation.rate_limiter import RateLimiter
from automation.base_handler import ApplyOutcome
from core.jd_parser import JDParser
from core.notifier import get_notifier
from core.resume_tailor import ResumeTailor
from storage.database import (
    claim_next_task,
    complete_task,
    fail_task,
    get_job_by_id,
    get_session,
    save_resume_version,
    update_job_status,
    init_db,
)
from storage.models import Job, JobStatus, TaskType


# ── Logging setup ─────────────────────────────────────────────────────────────


def _setup_logging() -> None:
    from datetime import date
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / f"application_{date.today().isoformat()}.log"
    logger.add(log_file, rotation="00:00", retention="14 days", level="DEBUG")


# ── Single-job application ─────────────────────────────────────────────────────


async def apply_one(
    job: Job,
    *,
    use_llm: bool = True,
    headless: bool = False,
    dry: bool = False,
) -> bool:
    """
    Tailor resume + apply for a single job.
    Returns True on success, False on failure.
    """
    # ── 1. Parse JD if not already done ──────────────────────────────────────
    parser = JDParser(use_llm=use_llm)
    parsed = parser.parse(job.description, title=job.title)

    # ── 2. Tailor resume ──────────────────────────────────────────────────────
    tailor = ResumeTailor(use_llm=use_llm)
    result = tailor.tailor(job, parsed)

    if result is None:
        logger.error("Resume tailoring failed | job={} company={}", job.id, job.company)
        update_job_status(job.id, JobStatus.FAILED_AUTO_APPLY)
        return False

    # Save resume version to DB
    save_resume_version(result.to_db_record())

    resume_path = result.pdf_path
    if not resume_path.exists():
        logger.error("Tailored PDF not found: {} | job={}", resume_path, job.id)
        update_job_status(job.id, JobStatus.FAILED_AUTO_APPLY)
        return False

    # ── 3. Dry run ────────────────────────────────────────────────────────────
    if dry:
        logger.info("[DRY RUN] Would apply | company={} url={}", job.company, job.url)
        await dry_run(job, resume_path)
        return True

    # ── 4. Apply ──────────────────────────────────────────────────────────────
    notifier = get_notifier()
    runner = ApplicationRunner(headless=headless)
    apply_result = await runner.run(job, resume_path)

    succeeded = apply_result.outcome in (
        ApplyOutcome.SUCCESS,
        ApplyOutcome.ALREADY_APPLIED,
    )
    if succeeded:
        notifier.application_submitted(job.company, job.title)
    else:
        notifier.application_failed(job.company, job.title, apply_result.error_message or apply_result.outcome)
    return succeeded


# ── Interactive approval prompt ────────────────────────────────────────────────

_SEP = "─" * 62


def _print_job_details(job: Job) -> None:
    tech = json.loads(job.key_technologies or "[]") + json.loads(job.frameworks or "[]")
    print(f"\n{_SEP}")
    print(f"  {job.title}")
    print(f"  {job.company}  |  {job.location or '—'}")
    print(f"  Score : {job.match_score or '?'}   Seniority: {job.seniority or '?'}")
    print(f"  Tech  : {', '.join(tech[:8]) or '—'}")
    print(f"  URL   : {job.url or '—'}")
    print(f"{_SEP}")


def _prompt(dry: bool) -> str:
    """Return 'a'pply / 'd'ry-run / 's'kip / 'q'uit."""
    if dry:
        return "d"
    while True:
        try:
            c = input("  Action: [a]pply  [d]ry-run  [s]kip  [q]uit → ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return "q"
        if c in ("a", "d", "s", "q"):
            return c
        if c == "":
            return "s"


# ── Queue drain ───────────────────────────────────────────────────────────────


async def run_queue(
    limit: int = 50,
    use_llm: bool = True,
    headless: bool = False,
    dry: bool = False,
    interactive: bool = True,
) -> None:
    """Drain PENDING application tasks from the task queue up to `limit`."""
    rate_limiter = RateLimiter()
    processed = 0
    skipped_rate = 0
    skipped_score = 0
    skipped_user = 0
    succeeded = 0
    failed = 0

    logger.info("Starting application queue run | limit={} llm={} interactive={}", limit, use_llm, interactive)

    while processed < limit:
        # Check rate limit before claiming a task
        if not rate_limiter.can_apply():
            wait = rate_limiter.seconds_until_slot()
            logger.warning(
                "Rate limit hit. {} applied this hour (max={}). "
                "Next slot in {}s.",
                rate_limiter.applied_this_hour(),
                rate_limiter.max_per_hour,
                wait,
            )
            break

        task = claim_next_task(TaskType.APPLICATION)
        if task is None:
            logger.info("Task queue empty — done.")
            break

        payload = task.get_payload()
        job_id = payload.get("job_id")

        if not job_id:
            fail_task(task.id, "Missing job_id in payload")
            continue

        job = get_job_by_id(job_id)
        if not job:
            fail_task(task.id, f"Job {job_id} not found in DB")
            logger.warning("Task {} references missing job {}", task.id, job_id)
            continue

        # Check minimum match score
        if not rate_limiter.is_score_eligible(job.match_score):
            fail_task(
                task.id,
                f"Score {job.match_score} below min {rate_limiter.min_score}",
            )
            logger.info(
                "Skipping low-score job | company={} score={} min={}",
                job.company, job.match_score, rate_limiter.min_score,
            )
            skipped_score += 1
            continue

        # ── Per-job approval prompt ───────────────────────────────────────────
        if interactive:
            _print_job_details(job)
            choice = _prompt(dry)
        else:
            choice = "d" if dry else "a"

        if choice == "q":
            print("\nExiting early.")
            # Return the claimed task to PENDING so it isn't lost
            with get_session() as s:
                t = s.get(type(task), task.id)
                if t:
                    from storage.models import TaskStatus
                    t.status = TaskStatus.PENDING
                    s.add(t)
            break

        if choice == "s":
            print("  → Skipped.")
            with get_session() as s:
                t = s.get(type(task), task.id)
                if t:
                    from storage.models import TaskStatus
                    t.status = TaskStatus.PENDING
                    s.add(t)
            skipped_user += 1
            continue

        run_dry = choice == "d"
        logger.info(
            "Processing task {} | company={} title={} score={} dry={}",
            task.id, job.company, job.title, job.match_score, run_dry,
        )

        try:
            ok = await apply_one(job, use_llm=use_llm, headless=headless, dry=run_dry)
            if ok:
                complete_task(task.id)
                succeeded += 1
            else:
                fail_task(task.id, "apply_one returned False")
                failed += 1
        except Exception as exc:
            fail_task(task.id, str(exc))
            logger.error("Exception processing task {} | {}", task.id, exc)
            failed += 1

        processed += 1

    logger.info(
        "Queue run complete | processed={} succeeded={} failed={} "
        "skipped_rate={} skipped_score={} skipped_user={}",
        processed, succeeded, failed, skipped_rate, skipped_score, skipped_user,
    )

    if processed > 0 or skipped_user > 0:
        _print_summary(succeeded, failed, skipped_score, skipped_user)


def _print_summary(succeeded: int, failed: int, skipped_score: int, skipped_user: int = 0) -> None:
    print("\n" + "=" * 50)
    print("  Application Pipeline Summary")
    print("=" * 50)
    print(f"  Applied    : {succeeded}")
    print(f"  Failed     : {failed}")
    print(f"  Skipped    : {skipped_user}  (you skipped)")
    print(f"  Filtered   : {skipped_score}  (score below threshold)")
    print("=" * 50 + "\n")


# ── Resume-only (tailor without applying) ─────────────────────────────────────


def tailor_only(job: Job, *, use_llm: bool = True) -> Path | None:
    """
    Parse the JD and build a tailored PDF resume without submitting any application.
    Returns the path to the generated PDF (or DOCX fallback), or None on failure.
    Prints the PDF path to stdout so the dashboard subprocess can capture it.
    """
    parser = JDParser(use_llm=use_llm)
    parsed = parser.parse(job.description, title=job.title)

    tailor = ResumeTailor(use_llm=use_llm)
    result = tailor.tailor(job, parsed)

    if result is None:
        logger.error("Resume tailoring failed | job={} company={}", job.id, job.company)
        return None

    save_resume_version(result.to_db_record())

    pdf = result.pdf_path
    if not pdf.exists():
        # Fall back to DOCX if PDF export failed
        docx = result.pdf_path.with_suffix(".docx")
        if docx.exists():
            pdf = docx
        else:
            logger.error("Tailored file not found: {}", pdf)
            return None

    logger.info("Resume tailored | company={} path={}", job.company, pdf)
    # Print path for subprocess capture
    print(f"RESUME_PATH:{pdf}")
    return pdf


# ── CLI ────────────────────────────────────────────────────────────────────────


async def main_async(args: argparse.Namespace) -> None:
    init_db()
    _setup_logging()

    if args.job_id:
        # Single job mode — always show details + prompt unless --auto
        job = get_job_by_id(args.job_id)
        if job is None:
            logger.error("Job {} not found", args.job_id)
            sys.exit(1)
        logger.info(
            "Single-job mode | company={} title={} score={}",
            job.company, job.title, job.match_score,
        )

        if args.resume_only:
            path = tailor_only(job, use_llm=not args.no_llm)
            sys.exit(0 if path else 1)

        if not args.auto:
            _print_job_details(job)
            choice = _prompt(args.dry_run)
            if choice == "q" or choice == "s":
                print("Cancelled.")
                sys.exit(0)
            run_dry = choice == "d"
        else:
            run_dry = args.dry_run

        ok = await apply_one(
            job,
            use_llm=not args.no_llm,
            headless=args.headless,
            dry=run_dry,
        )
        sys.exit(0 if ok else 1)
    else:
        # Queue mode
        await run_queue(
            limit=args.limit,
            use_llm=not args.no_llm,
            headless=args.headless,
            dry=args.dry_run,
            interactive=not args.auto,
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="JDASS Application Pipeline — submit queued job applications"
    )
    parser.add_argument(
        "--job-id",
        help="Apply to a single job by ID (skips the queue)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Maximum number of queue tasks to process (default: 50)",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Disable LLM (use regex parsing + template resume bullets)",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run Playwright in headless mode (default: visible browser)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Open browser and fill form but do NOT click submit",
    )
    parser.add_argument(
        "--resume-only",
        action="store_true",
        help="Tailor and generate resume PDF only — do not open browser or apply",
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Skip per-job approval prompts and apply to all eligible jobs automatically",
    )
    args = parser.parse_args()

    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
