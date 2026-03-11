"""
Outreach pipeline — generates recruiter messages for recently applied jobs.

Flow per job:
  1. Find recruiter / hiring-manager candidates (DuckDuckGo → LinkedIn URLs)
  2. Generate personalised message with LLM
  3. Save to outreach_queue with status PENDING_REVIEW
  4. User reviews and approves/discards from the dashboard

Nothing is EVER sent automatically. Every message requires explicit
user approval from the Outreach tab in the dashboard.

Usage:
    make outreach               Run for all jobs applied in last 7 days
    make outreach DAYS=3        Run for jobs applied in last 3 days
    .venv/bin/python -m pipelines.outreach --days 7
    .venv/bin/python -m pipelines.outreach --job-id <uuid>
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

from loguru import logger

from automation.base_handler import load_applicant_profile
from core.message_generator import MessageGenerator
from core.notifier import get_notifier
from core.recruiter_finder import RecruiterFinder
from storage.database import get_session, init_db
from storage.models import Application, ApplicationStatus, Job, OutreachQueue, OutreachStatus


# ── Logging ───────────────────────────────────────────────────────────────────


def _setup_logging() -> None:
    from datetime import date
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / f"outreach_{date.today().isoformat()}.log"
    logger.add(log_file, rotation="00:00", retention="14 days", level="DEBUG")


# ── DB helpers ────────────────────────────────────────────────────────────────


def _get_recently_applied_jobs(days: int) -> list[tuple[Application, Job]]:
    """Return (Application, Job) pairs for apps submitted within the last `days` days."""
    from sqlmodel import select
    cutoff = datetime.utcnow() - timedelta(days=days)
    with get_session() as session:
        rows = list(session.exec(
            select(Application, Job)
            .join(Job, Application.job_id == Job.id)
            .where(Application.applied_at >= cutoff)
            .where(Application.status == ApplicationStatus.APPLIED)
            .order_by(Application.applied_at.desc())
        ).all())
    return rows


def _get_job_by_id_with_app(job_id: str) -> tuple[Application, Job] | None:
    from sqlmodel import select
    with get_session() as session:
        row = session.exec(
            select(Application, Job)
            .join(Job, Application.job_id == Job.id)
            .where(Job.id == job_id)
        ).first()
    return row


def _outreach_already_queued(job_id: str) -> bool:
    """Return True if any outreach has already been generated for this job."""
    from sqlmodel import select
    with get_session() as session:
        existing = session.exec(
            select(OutreachQueue).where(OutreachQueue.job_id == job_id)
        ).first()
    return existing is not None


def _save_outreach_items(items: list[OutreachQueue]) -> int:
    """Insert OutreachQueue items into DB. Returns count saved."""
    with get_session() as session:
        for item in items:
            session.add(item)
    return len(items)


# ── Core per-job logic ────────────────────────────────────────────────────────


def run_outreach_for_job(
    job: Job,
    app: Application,
    finder: RecruiterFinder,
    generator: MessageGenerator,
    max_candidates: int = 3,
) -> int:
    """
    Run the full outreach flow for one job.
    Returns number of messages queued.
    """
    if _outreach_already_queued(job.id):
        logger.info(
            "Outreach already queued | company={} job={}",
            job.company, job.id,
        )
        return 0

    # 1. Find recruiter candidates
    candidates = finder.find(job.company, job.title, max_results=max_candidates)
    if not candidates:
        logger.info("No recruiter candidates found | company={}", job.company)
        return 0

    # 2. Generate messages
    items = generator.generate_for_job(
        job, candidates, application_id=app.id
    )
    if not items:
        return 0

    # 3. Save to DB
    saved = _save_outreach_items(items)
    logger.info(
        "Queued {} outreach messages | company={} job={}",
        saved, job.company, job.id,
    )
    return saved


# ── Pipeline entry points ─────────────────────────────────────────────────────


def run_pipeline(
    days: int = 7,
    job_id: str | None = None,
    use_llm: bool = True,
    max_candidates: int = 3,
    profile_path: str = "configs/applicant.yaml",
) -> None:
    init_db()
    _setup_logging()

    profile = load_applicant_profile(profile_path)
    finder = RecruiterFinder()
    generator = MessageGenerator(profile=profile, use_llm=use_llm)

    total_queued = 0
    total_jobs = 0

    try:
        if job_id:
            # Single job mode
            row = _get_job_by_id_with_app(job_id)
            if row is None:
                logger.error("No applied application found for job {}", job_id)
                sys.exit(1)
            app, job = row
            queued = run_outreach_for_job(job, app, finder, generator, max_candidates)
            total_queued += queued
            total_jobs = 1
        else:
            # Batch mode — all recently applied jobs
            rows = _get_recently_applied_jobs(days)
            if not rows:
                logger.info("No applied jobs in the last {} days", days)
                _print_summary(0, 0)
                return

            logger.info(
                "Running outreach for {} recently applied jobs (last {} days)",
                len(rows), days,
            )
            for app, job in rows:
                try:
                    queued = run_outreach_for_job(job, app, finder, generator, max_candidates)
                    total_queued += queued
                    total_jobs += 1
                except Exception as exc:
                    logger.error(
                        "Outreach failed | company={} job={} error={}",
                        job.company, job.id, exc,
                    )
    finally:
        finder.close()

    get_notifier().outreach_ready(total_queued)
    _print_summary(total_jobs, total_queued)


def _print_summary(jobs: int, queued: int) -> None:
    print("\n" + "=" * 50)
    print("  Outreach Pipeline Summary")
    print("=" * 50)
    print(f"  Jobs processed  : {jobs}")
    print(f"  Messages queued : {queued}  (status: PENDING_REVIEW)")
    print("  Review at: http://localhost:8000  (Outreach tab)")
    print("=" * 50 + "\n")


# ── CLI ────────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="JDASS Outreach Pipeline — generate recruiter messages for applied jobs"
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Generate outreach for jobs applied in the last N days (default: 7)",
    )
    parser.add_argument(
        "--job-id",
        help="Generate outreach for a single specific job by ID",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Use template messages instead of LLM generation",
    )
    parser.add_argument(
        "--max-candidates",
        type=int,
        default=3,
        help="Maximum recruiter candidates to contact per job (default: 3)",
    )
    args = parser.parse_args()

    run_pipeline(
        days=args.days,
        job_id=args.job_id,
        use_llm=not args.no_llm,
        max_candidates=args.max_candidates,
    )


if __name__ == "__main__":
    main()
