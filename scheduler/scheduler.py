"""
JDASS Scheduler — APScheduler-based cron runner.

Runs three daily jobs in sequence:
  08:00  discover        — scrape all sources → filter → score → save
  09:00  apply           — drain application task queue (rate-limited)
  10:00  outreach        — generate recruiter messages for recently applied jobs

Schedule times are configurable in configs/settings.yaml under `scheduler:`.

The scheduler persists its state in a SQLite job store (scheduler_jobs.db)
so it survives restarts.

Run:
    make scheduler          Start (blocks; use Ctrl+C to stop)
    make scheduler-status   Print next run times for each job
"""

from __future__ import annotations

import asyncio
import signal
import sys
from datetime import datetime
from pathlib import Path

import yaml
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.executors.pool import ThreadPoolExecutor
from loguru import logger


# ── Config loader ──────────────────────────────────────────────────────────────


def _load_schedule_config(settings_path: str = "configs/settings.yaml") -> dict:
    defaults = {
        "discover_time": "08:00",
        "apply_time": "09:00",
        "outreach_time": "10:00",
        "enabled": True,
        "apply_days": 7,          # look-back window for outreach
        "use_llm": True,
    }
    try:
        with open(settings_path) as f:
            cfg = yaml.safe_load(f) or {}
        sched = cfg.get("scheduler", {})
        return {**defaults, **sched}
    except Exception as exc:
        logger.warning("Could not read scheduler config: {} — using defaults", exc)
        return defaults


def _parse_time(t: str) -> tuple[int, int]:
    """Parse 'HH:MM' → (hour, minute)."""
    parts = t.split(":")
    return int(parts[0]), int(parts[1])


# ── Logging ───────────────────────────────────────────────────────────────────


def _setup_logging() -> None:
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / "scheduler_{time:YYYY-MM-DD}.log"
    logger.add(
        str(log_file),
        rotation="00:00",
        retention="30 days",
        level="INFO",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}",
    )


# ── Job functions (called by APScheduler) ─────────────────────────────────────


def job_discover(use_llm: bool = True) -> None:
    """Run the discovery pipeline."""
    logger.info("=== Scheduler: starting DISCOVER job ===")
    try:
        from pipelines.discovery import run_pipeline
        run_pipeline(use_llm=use_llm, use_semantic=False)
        logger.info("=== Scheduler: DISCOVER job complete ===")
    except Exception as exc:
        logger.error("Scheduler: DISCOVER job failed: {}", exc)


def job_apply() -> None:
    """Drain the application task queue."""
    logger.info("=== Scheduler: starting APPLY job ===")
    try:
        from pipelines.application import run_queue
        asyncio.run(run_queue(limit=50, use_llm=True, headless=True))
        logger.info("=== Scheduler: APPLY job complete ===")
    except Exception as exc:
        logger.error("Scheduler: APPLY job failed: {}", exc)


def job_outreach(days: int = 7, use_llm: bool = True) -> None:
    """Generate recruiter outreach messages for recently applied jobs."""
    logger.info("=== Scheduler: starting OUTREACH job ===")
    try:
        from pipelines.outreach import run_pipeline
        run_pipeline(days=days, use_llm=use_llm)
        logger.info("=== Scheduler: OUTREACH job complete ===")
    except Exception as exc:
        logger.error("Scheduler: OUTREACH job failed: {}", exc)


# ── Scheduler setup ───────────────────────────────────────────────────────────


def build_scheduler(cfg: dict) -> BlockingScheduler:
    jobstores = {
        "default": SQLAlchemyJobStore(url="sqlite:///./scheduler_jobs.db"),
    }
    executors = {
        "default": ThreadPoolExecutor(max_workers=1),  # one job at a time
    }
    job_defaults = {
        "coalesce": True,          # don't run missed jobs multiple times
        "max_instances": 1,
        "misfire_grace_time": 3600,  # 1 hour grace for missed jobs
    }

    scheduler = BlockingScheduler(
        jobstores=jobstores,
        executors=executors,
        job_defaults=job_defaults,
        timezone="America/Chicago",
    )

    use_llm = cfg.get("use_llm", True)
    apply_days = cfg.get("apply_days", 7)

    # Discover
    dh, dm = _parse_time(cfg.get("discover_time", "08:00"))
    scheduler.add_job(
        job_discover,
        trigger="cron",
        hour=dh, minute=dm,
        id="discover",
        name="Discovery Pipeline",
        replace_existing=True,
        kwargs={"use_llm": use_llm},
    )

    # Apply
    ah, am = _parse_time(cfg.get("apply_time", "09:00"))
    scheduler.add_job(
        job_apply,
        trigger="cron",
        hour=ah, minute=am,
        id="apply",
        name="Application Pipeline",
        replace_existing=True,
    )

    # Outreach
    oh, om = _parse_time(cfg.get("outreach_time", "10:00"))
    scheduler.add_job(
        job_outreach,
        trigger="cron",
        hour=oh, minute=om,
        id="outreach",
        name="Outreach Pipeline",
        replace_existing=True,
        kwargs={"days": apply_days, "use_llm": use_llm},
    )

    return scheduler


# ── Status printer ────────────────────────────────────────────────────────────


def print_status() -> None:
    """Print next scheduled run time for each job."""
    cfg = _load_schedule_config()
    if not cfg.get("enabled", True):
        print("Scheduler is DISABLED (set scheduler.enabled: true in settings.yaml)")
        return

    scheduler = build_scheduler(cfg)
    print("\n  JDASS Scheduler — Next Run Times")
    print("  " + "─" * 45)
    for job in scheduler.get_jobs():
        next_run = job.next_run_time
        print(f"  {job.name:<25} {str(next_run)[:19] if next_run else 'N/A'}")
    print()
    scheduler.shutdown(wait=False)


# ── Main entry point ──────────────────────────────────────────────────────────


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="JDASS Scheduler")
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print next run times and exit",
    )
    parser.add_argument(
        "--run-now",
        choices=["discover", "apply", "outreach"],
        help="Trigger a specific job immediately and exit",
    )
    args = parser.parse_args()

    _setup_logging()
    cfg = _load_schedule_config()

    if not cfg.get("enabled", True):
        logger.error("Scheduler is disabled. Set scheduler.enabled: true in settings.yaml")
        sys.exit(1)

    if args.status:
        print_status()
        return

    if args.run_now:
        logger.info("Manual trigger: {}", args.run_now)
        use_llm = cfg.get("use_llm", True)
        if args.run_now == "discover":
            job_discover(use_llm=use_llm)
        elif args.run_now == "apply":
            job_apply()
        elif args.run_now == "outreach":
            job_outreach(days=cfg.get("apply_days", 7), use_llm=use_llm)
        return

    scheduler = build_scheduler(cfg)

    def _shutdown(signum, frame):
        logger.info("Shutdown signal received — stopping scheduler")
        scheduler.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    logger.info("JDASS Scheduler started (timezone: America/Chicago)")
    for job in scheduler.get_jobs():
        logger.info("  {} → next: {}", job.name, job.next_run_time)

    print("\n  JDASS Scheduler running. Press Ctrl+C to stop.\n")
    scheduler.start()


if __name__ == "__main__":
    main()
