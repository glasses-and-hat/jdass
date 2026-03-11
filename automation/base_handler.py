"""
Base ATS handler interface.

Every ATS-specific handler (Greenhouse, Lever, LinkedIn Easy Apply, etc.)
implements this ABC. The ApplicationRunner calls handler.apply() and only
deals with the result — it doesn't know which ATS it's talking to.

Adding a new ATS:
  1. Subclass BaseATSHandler
  2. Implement detect() and apply()
  3. Register in ApplicationRunner._get_handler()
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

from loguru import logger
from playwright.async_api import Page


# ── Result types ──────────────────────────────────────────────────────────────


class ApplyOutcome(str, Enum):
    SUCCESS = "success"
    ALREADY_APPLIED = "already_applied"
    REQUIRES_ACCOUNT = "requires_account"
    UNSUPPORTED_FORM = "unsupported_form"
    BLOCKED = "blocked"           # CAPTCHA / bot detection
    ERROR = "error"


@dataclass
class ApplyResult:
    outcome: ApplyOutcome
    job_id: str
    ats_type: str
    url: str
    fields_filled: dict = field(default_factory=dict)
    screenshot_path: Optional[Path] = None
    error_message: Optional[str] = None
    submission_log: list[str] = field(default_factory=list)
    # LLM-guessed answers: [{label, value, field_id, options, confirmed, source}]
    llm_guesses: list[dict] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return self.outcome == ApplyOutcome.SUCCESS

    def log(self, msg: str) -> None:
        self.submission_log.append(msg)
        logger.debug("[{}] {}", self.ats_type, msg)


# ── Applicant profile loader ──────────────────────────────────────────────────


def load_applicant_profile(path: str = "configs/applicant.yaml") -> dict:
    import yaml
    with open(path) as f:
        return yaml.safe_load(f) or {}


# ── Base handler ──────────────────────────────────────────────────────────────


class BaseATSHandler(ABC):
    """
    Abstract base for ATS-specific form automation.

    Subclasses receive a Playwright Page already navigated to the job URL.
    They must fill the form and submit it, then return an ApplyResult.
    """

    ats_name: str = "unknown"

    def __init__(self, profile: dict, resume_path: Path):
        self.profile = profile
        self.resume_path = resume_path
        self._personal = profile.get("personal", {})
        self._work_auth = profile.get("work_authorization", {})
        self._prefs = profile.get("preferences", {})
        self._demographics = profile.get("demographics", {})

    @classmethod
    @abstractmethod
    def detect(cls, url: str) -> bool:
        """Return True if this handler can handle the given job URL."""
        ...

    @abstractmethod
    async def apply(self, page: Page, job_id: str, job_url: str) -> ApplyResult:
        """
        Navigate to the application form and submit it.

        The page is already loaded at job_url when this is called.
        """
        ...

    # ── Shared form helpers ───────────────────────────────────────────────────

    async def _fill_text(
        self, page: Page, selectors: list[str], value: str, result: ApplyResult
    ) -> bool:
        """Try each selector until one works. Returns True on success."""
        for sel in selectors:
            try:
                el = page.locator(sel).first
                if await el.count() and await el.is_visible():
                    await el.fill(value)
                    result.log(f"Filled {sel!r} = {repr(value)[:40]}")
                    return True
            except Exception:
                continue
        return False

    async def _select_option(
        self, page: Page, selectors: list[str], value: str, result: ApplyResult
    ) -> bool:
        """Select a <select> option by visible text or value."""
        for sel in selectors:
            try:
                el = page.locator(sel).first
                if await el.count() and await el.is_visible():
                    await el.select_option(label=value)
                    result.log(f"Selected {sel!r} = {value!r}")
                    return True
            except Exception:
                try:
                    await el.select_option(value=value)
                    return True
                except Exception:
                    continue
        return False

    async def _click_radio_or_checkbox(
        self, page: Page, label_text: str, result: ApplyResult
    ) -> bool:
        """Click a radio/checkbox identified by its associated label text."""
        try:
            locator = page.get_by_label(re.compile(label_text, re.IGNORECASE))
            if await locator.count():
                await locator.first.check()
                result.log(f"Checked radio/checkbox: {label_text!r}")
                return True
        except Exception:
            pass
        return False

    async def _upload_resume(
        self, page: Page, selectors: list[str], result: ApplyResult
    ) -> bool:
        """Upload resume file using a file input element."""
        if not self.resume_path.exists():
            result.log(f"Resume not found: {self.resume_path}")
            return False
        for sel in selectors:
            try:
                el = page.locator(sel).first
                if await el.count():
                    await el.set_input_files(str(self.resume_path))
                    result.log(f"Uploaded resume via {sel!r}")
                    return True
            except Exception:
                continue
        return False

    async def _answer_yes_no(
        self,
        page: Page,
        question_pattern: str,
        answer: bool,
        result: ApplyResult,
    ) -> bool:
        """
        Find a yes/no question by its label text and select the appropriate answer.
        Handles radio buttons and select dropdowns.
        """
        answer_text = "Yes" if answer else "No"
        try:
            # Try radio button approach first
            label_loc = page.locator(
                f"text=/{question_pattern}/i"
            ).locator("xpath=../..").get_by_label(answer_text)
            if await label_loc.count():
                await label_loc.first.check()
                result.log(f"Answered '{question_pattern}' → {answer_text}")
                return True
        except Exception:
            pass
        return False

    async def _handle_eeo_section(self, page: Page, result: ApplyResult) -> None:
        """
        Fill in EEO/demographic section using profile demographics config.
        All fields default to "prefer not to say" to keep things simple.
        """
        prefer = "I prefer not to say"
        pna_variants = [
            "Prefer Not to Say", "Prefer not to say", "I don't wish to answer",
            "Decline to self-identify", "I prefer not to self-identify",
        ]
        eeo_selects = [
            "select[id*='gender']", "select[id*='race']", "select[id*='ethnicity']",
            "select[id*='veteran']", "select[id*='disability']",
        ]
        for sel in eeo_selects:
            for pna in pna_variants:
                try:
                    el = page.locator(sel).first
                    if await el.count():
                        await el.select_option(label=pna)
                        result.log(f"Set EEO {sel!r} → {pna!r}")
                        break
                except Exception:
                    continue

    async def _screenshot(self, page: Page, label: str) -> Optional[Path]:
        """Take a debug screenshot and return its path."""
        screenshots_dir = Path("logs") / "screenshots"
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        from datetime import datetime
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        path = screenshots_dir / f"{label}_{ts}.png"
        try:
            await page.screenshot(path=str(path), full_page=True)
            logger.debug("Screenshot: {}", path)
            return path
        except Exception as exc:
            logger.warning("Screenshot failed: {}", exc)
            return None

    async def _check_already_applied(self, page: Page) -> bool:
        """Heuristic: look for 'already applied' language on the page."""
        text = (await page.content()).lower()
        signals = [
            "you have already applied",
            "already submitted an application",
            "duplicate application",
            "you've already applied",
        ]
        return any(s in text for s in signals)
