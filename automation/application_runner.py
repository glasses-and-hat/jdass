"""
ApplicationRunner — orchestrates a single end-to-end job application.

Flow:
  1. Load applicant profile + tailored resume path
  2. Detect ATS handler from job URL
  3. Launch Playwright (visible by default for debugging)
  4. Call handler.apply() → ApplyResult
  5. Retry up to MAX_RETRIES times on transient ERROR outcomes
  6. Persist Application record to DB on success
  7. Update job status (APPLIED or FAILED_AUTO_APPLY)
  8. Return final ApplyResult
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from loguru import logger
from playwright.async_api import async_playwright

from automation.base_handler import ApplyOutcome, ApplyResult, BaseATSHandler, load_applicant_profile
from automation.greenhouse_handler import GreenhouseHandler
from automation.lever_handler import LeverHandler
from automation.linkedin_handler import LinkedInHandler
from storage.database import get_session, update_job_status
from storage.models import Application, ApplicationStatus, Job, JobStatus, ResumeVersion


# ── Constants ─────────────────────────────────────────────────────────────────

MAX_RETRIES = 2
RETRY_DELAY_BASE = 5   # seconds; doubled each retry


# ── Handler registry ──────────────────────────────────────────────────────────

_HANDLER_CLASSES: list[type[BaseATSHandler]] = [
    GreenhouseHandler,
    LeverHandler,
    LinkedInHandler,   # checked last — requires saved session
]


def _get_handler(url: str, profile: dict, resume_path: Path) -> Optional[BaseATSHandler]:
    for cls in _HANDLER_CLASSES:
        if cls.detect(url):
            return cls(profile=profile, resume_path=resume_path)
    return None


# ── ApplicationRunner ─────────────────────────────────────────────────────────


class ApplicationRunner:
    """
    Runs a single job application end-to-end.

    Usage:
        runner = ApplicationRunner(headless=False)
        result = asyncio.run(runner.run(job, resume_path))
    """

    def __init__(
        self,
        profile_path: str = "configs/applicant.yaml",
        headless: bool = False,
        slow_mo: int = 200,          # ms between Playwright actions — helps avoid bot detection
        viewport_width: int = 1280,
        viewport_height: int = 900,
    ):
        self.profile = load_applicant_profile(profile_path)
        self.headless = headless
        self.slow_mo = slow_mo
        self.viewport = {"width": viewport_width, "height": viewport_height}

    # ── Main entry point ──────────────────────────────────────────────────────

    async def run(self, job: Job, resume_path: Path) -> ApplyResult:
        """
        Apply to `job` using the tailored resume at `resume_path`.
        Saves an Application record on success; updates job status.
        Returns the final ApplyResult (possibly after retries).
        """
        handler = _get_handler(job.url, self.profile, resume_path)
        if handler is None:
            logger.warning("No ATS handler found for URL: {}", job.url)
            result = ApplyResult(
                outcome=ApplyOutcome.UNSUPPORTED_FORM,
                job_id=job.id,
                ats_type="unknown",
                url=job.url,
            )
            result.log(f"No handler for URL: {job.url}")
            update_job_status(job.id, JobStatus.MANUAL_REVIEW)
            return result

        logger.info(
            "Applying | job={} company={} ats={} url={}",
            job.id, job.company, handler.ats_name, job.url,
        )

        result = await self._run_with_retries(handler, job)

        if result.outcome == ApplyOutcome.SUCCESS:
            await self._save_application(job, result, resume_path)
            update_job_status(job.id, JobStatus.APPLIED)
            logger.info(
                "Applied successfully | job={} company={} ats={}",
                job.id, job.company, handler.ats_name,
            )
        elif result.outcome == ApplyOutcome.ALREADY_APPLIED:
            update_job_status(job.id, JobStatus.APPLIED)
            logger.info("Already applied | job={} company={}", job.id, job.company)
        else:
            update_job_status(job.id, JobStatus.FAILED_AUTO_APPLY)
            logger.error(
                "Application failed | job={} company={} outcome={} error={}",
                job.id, job.company, result.outcome, result.error_message,
            )

        return result

    # ── Playwright lifecycle ──────────────────────────────────────────────────

    async def _run_with_retries(
        self,
        handler: BaseATSHandler,
        job: Job,
    ) -> ApplyResult:
        """Launch a browser, call handler.apply(), retry on transient errors."""
        last_result: Optional[ApplyResult] = None

        for attempt in range(MAX_RETRIES + 1):
            if attempt > 0:
                delay = RETRY_DELAY_BASE * (2 ** (attempt - 1))
                logger.info("Retrying in {}s (attempt {}/{})", delay, attempt + 1, MAX_RETRIES + 1)
                await asyncio.sleep(delay)

            try:
                last_result = await self._run_once(handler, job)
            except Exception as exc:
                logger.error("Playwright crash on attempt {}: {}", attempt + 1, exc)
                last_result = ApplyResult(
                    outcome=ApplyOutcome.ERROR,
                    job_id=job.id,
                    ats_type=handler.ats_name,
                    url=job.url,
                    error_message=str(exc),
                )

            # Don't retry on these terminal outcomes
            if last_result.outcome not in (ApplyOutcome.ERROR,):
                break

        return last_result  # type: ignore[return-value]

    async def _run_once(self, handler: BaseATSHandler, job: Job) -> ApplyResult:
        """Open a fresh browser context, navigate to the job URL, call handler.apply()."""
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=self.headless,
                slow_mo=self.slow_mo,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                ],
            )
            context = await browser.new_context(
                viewport=self.viewport,
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
                locale="en-US",
                timezone_id="America/Chicago",
            )
            page = await context.new_page()

            try:
                result = await handler.apply(page, job.id, job.url)
            finally:
                await context.close()
                await browser.close()

        return result

    # ── Persistence ───────────────────────────────────────────────────────────

    async def _save_application(
        self,
        job: Job,
        result: ApplyResult,
        resume_path: Path,
    ) -> None:
        """Write an Application row to the database."""
        app = Application(
            job_id=job.id,
            applied_at=datetime.utcnow(),
            resume_path=str(resume_path),
            resume_version=resume_path.stem,
            ats_type=result.ats_type,
            submission_log=json.dumps(result.submission_log),
            form_guesses=json.dumps(result.llm_guesses) if result.llm_guesses else None,
            status=ApplicationStatus.APPLIED,
        )
        with get_session() as session:
            # Check for an existing row (idempotent)
            from sqlmodel import select
            from storage.models import Application as AppModel
            existing = session.exec(
                select(AppModel).where(AppModel.job_id == job.id)
            ).first()
            if not existing:
                session.add(app)
                logger.debug("Saved Application record | job={}", job.id)
            else:
                logger.debug("Application record already exists | job={}", job.id)


# ── Dry-run helper ────────────────────────────────────────────────────────────


async def dry_run(job: Job, resume_path: Path, profile_path: str = "configs/applicant.yaml") -> None:
    """
    Fill the application form in a visible browser but do NOT submit.

    Runs the full handler.apply() flow so every field gets filled, then keeps
    the browser open for inspection. Submission is skipped by replacing the
    handler's _submit method with a no-op before calling apply().
    """
    profile = load_applicant_profile(profile_path)
    handler = _get_handler(job.url, profile, resume_path)
    if handler is None:
        print(f"[dry-run] No ATS handler found for: {job.url}")
        print("[dry-run] Supported: boards.greenhouse.io, jobs.lever.co, linkedin.com/jobs")
        return

    print(f"[dry-run] ATS detected: {handler.ats_name!r} | URL: {job.url}")

    # Replace _submit with a no-op so the form gets filled but never submitted
    async def _skip_submit(page: "Page", result: "ApplyResult") -> bool:  # type: ignore[name-defined]
        result.log("[DRY RUN] Submit skipped")
        return True

    handler._submit = _skip_submit  # type: ignore[method-assign]

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,
            slow_mo=300,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="America/Chicago",
        )
        page = await context.new_page()

        try:
            result = await handler.apply(page, job.id, job.url)
            print(f"\n[dry-run] Form fill complete ({len(result.submission_log)} actions):")
            for entry in result.submission_log:
                print(f"  • {entry}")
            print(f"\n[dry-run] Outcome: {result.outcome}")
            print("[dry-run] Browser held open — close the window or press Ctrl+C to exit.\n")
            try:
                await asyncio.sleep(300)
            except KeyboardInterrupt:
                pass
        finally:
            await browser.close()
