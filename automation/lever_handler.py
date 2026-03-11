"""
Lever ATS form handler.

Lever job applications live at:
    https://jobs.lever.co/{company}/{job_id}/apply

The form is a single-page React app with predictable field IDs:
  name, email, phone, org (current company), urls (LinkedIn/GitHub/other),
  resume upload, then custom questions.

Verified against: Netflix, Figma, Replit, Modal, Railway.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from loguru import logger
from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

from automation.base_handler import ApplyOutcome, ApplyResult, BaseATSHandler


class LeverHandler(BaseATSHandler):
    """Handles job applications on Lever-powered job boards."""

    ats_name = "lever"

    @classmethod
    def detect(cls, url: str) -> bool:
        return "jobs.lever.co" in url or "lever.co" in url

    # ── Main flow ──────────────────────────────────────────────────────────────

    async def apply(self, page: Page, job_id: str, job_url: str) -> ApplyResult:
        result = ApplyResult(
            outcome=ApplyOutcome.ERROR,
            job_id=job_id,
            ats_type=self.ats_name,
            url=job_url,
        )

        # Lever apply URL: append /apply to the posting URL
        apply_url = job_url.rstrip("/") + "/apply"

        try:
            await page.goto(apply_url, wait_until="domcontentloaded", timeout=30_000)
            await asyncio.sleep(1)  # let SPA finish rendering

            if await self._check_already_applied(page):
                result.outcome = ApplyOutcome.ALREADY_APPLIED
                result.log("Already applied")
                return result

            # Lever forms have a consistent wrapper
            try:
                await page.wait_for_selector(".application-form, form.application", timeout=10_000)
            except PlaywrightTimeout:
                result.log("Lever application form not found")
                result.screenshot_path = await self._screenshot(page, f"lv_no_form_{job_id}")
                result.outcome = ApplyOutcome.UNSUPPORTED_FORM
                return result

            await self._fill_personal_info(page, result)
            await self._upload_resume_lv(page, result)
            await self._fill_links(page, result)
            await self._fill_additional_info(page, result)
            await self._fill_work_auth(page, result)
            await self._fill_custom_questions(page, result)

            result.screenshot_path = await self._screenshot(page, f"lv_pre_submit_{job_id}")

            submitted = await self._submit(page, result)
            if submitted:
                result.outcome = ApplyOutcome.SUCCESS
                result.log("Application submitted")
            else:
                result.outcome = ApplyOutcome.ERROR
                result.screenshot_path = await self._screenshot(page, f"lv_submit_fail_{job_id}")

        except PlaywrightTimeout as exc:
            result.outcome = ApplyOutcome.ERROR
            result.error_message = f"Timeout: {exc}"
            result.screenshot_path = await self._screenshot(page, f"lv_timeout_{job_id}")
            logger.error("Lever timeout | job={} | {}", job_id, exc)

        except Exception as exc:
            result.outcome = ApplyOutcome.ERROR
            result.error_message = str(exc)
            result.screenshot_path = await self._screenshot(page, f"lv_error_{job_id}")
            logger.error("Lever error | job={} | {}", job_id, exc)

        return result

    # ── Step implementations ──────────────────────────────────────────────────

    async def _fill_personal_info(self, page: Page, result: ApplyResult) -> None:
        p = self._personal
        full_name = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()

        fields = [
            (["input[name='name']", "#name"], full_name),
            (["input[name='email']", "#email"], p.get("email", "")),
            (["input[name='phone']", "#phone"], p.get("phone", "")),
            # "Current company" — leave blank or use a placeholder
            (["input[name='org']", "#org"], ""),
        ]
        for selectors, value in fields:
            if value:
                await self._fill_text(page, selectors, value, result)

    async def _upload_resume_lv(self, page: Page, result: ApplyResult) -> None:
        await self._upload_resume(page, [
            "input[type='file'][name='resume']",
            "input[type='file'][id*='resume']",
            "input[type='file']",
        ], result)

    async def _fill_links(self, page: Page, result: ApplyResult) -> None:
        """Lever has a 'urls' section with LinkedIn, GitHub, Portfolio, Other."""
        p = self._personal
        link_map = [
            (["input[name='urls[LinkedIn]']", "input[placeholder*='LinkedIn']"],
             p.get("linkedin_url", "")),
            (["input[name='urls[GitHub]']", "input[placeholder*='GitHub']"],
             p.get("github_url", "")),
            (["input[name='urls[Portfolio]']", "input[placeholder*='Portfolio']",
              "input[placeholder*='website']"],
             p.get("portfolio_url", "")),
        ]
        for selectors, value in link_map:
            if value:
                await self._fill_text(page, selectors, value, result)

    async def _fill_additional_info(self, page: Page, result: ApplyResult) -> None:
        """Fill the free-text 'Additional information' textarea if present."""
        # Leave blank — don't put anything here without a specific message
        pass

    async def _fill_work_auth(self, page: Page, result: ApplyResult) -> None:
        wa = self._work_auth
        authorized = wa.get("authorized_to_work_in_us", True)
        needs_sponsor = wa.get("require_sponsorship", True)

        # Lever typically uses radio buttons for these
        if authorized:
            await self._click_radio_or_checkbox(page, "Yes.*authorized|authorized.*Yes", result)
        else:
            await self._click_radio_or_checkbox(page, "No.*authorized|authorized.*No", result)

        if needs_sponsor:
            await self._click_radio_or_checkbox(page, "Yes.*sponsor|require.*sponsor", result)
        else:
            await self._click_radio_or_checkbox(page, "No.*sponsor", result)

    async def _fill_custom_questions(self, page: Page, result: ApplyResult) -> None:
        """
        Lever custom questions are free-form. Handle common patterns:
        - "How did you hear about this role?" → LinkedIn
        - Yes/No radio questions → handled by _click_radio_or_checkbox
        """
        await self._fill_text(page, [
            "textarea[name*='hear']",
            "input[name*='hear']",
        ], "LinkedIn", result)

    async def _submit(self, page: Page, result: ApplyResult) -> bool:
        submit_selectors = [
            "button[type='submit']:has-text('Submit Application')",
            "button[type='submit']:has-text('Submit')",
            "button.submit-app-btn",
            "input[type='submit']",
        ]
        for sel in submit_selectors:
            try:
                el = page.locator(sel).first
                if await el.count() and await el.is_visible():
                    await el.click()
                    await asyncio.sleep(2)
                    await page.wait_for_load_state("networkidle", timeout=15_000)
                    result.log(f"Clicked submit: {sel!r}")
                    return True
            except Exception:
                continue
        return False
