"""
LinkedIn Easy Apply handler.

LinkedIn requires authentication — the system uses a saved browser session
(Playwright storage_state JSON) so you only log in once manually.

Setup (one-time):
    make linkedin-login
    # → opens a visible Chrome window
    # → log in to LinkedIn normally
    # → close the browser when done
    # → session saved to .linkedin_session.json

After setup, `make apply-job JOB_ID=xxx` works for LinkedIn URLs automatically.

Supported URL patterns:
    https://www.linkedin.com/jobs/view/{job_id}
    https://linkedin.com/jobs/view/{job_id}

Limitations:
  • Multi-step forms vary greatly — the handler fills what it can and
    logs any unsupported steps as UNSUPPORTED_FORM.
  • LinkedIn aggressively detects bots. slow_mo=300 is recommended.
  • If a CAPTCHA appears, the handler returns ApplyOutcome.BLOCKED.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from loguru import logger
from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

from automation.base_handler import ApplyOutcome, ApplyResult, BaseATSHandler

# Path where the saved LinkedIn session cookies are stored
SESSION_FILE = Path(".linkedin_session.json")


class LinkedInHandler(BaseATSHandler):
    """Handles LinkedIn Easy Apply job applications."""

    ats_name = "linkedin"

    @classmethod
    def detect(cls, url: str) -> bool:
        return "linkedin.com/jobs" in url

    # ── Main flow ──────────────────────────────────────────────────────────────

    async def apply(self, page: Page, job_id: str, job_url: str) -> ApplyResult:
        result = ApplyResult(
            outcome=ApplyOutcome.ERROR,
            job_id=job_id,
            ats_type=self.ats_name,
            url=job_url,
        )

        # LinkedIn requires a saved session — check it exists
        if not SESSION_FILE.exists():
            result.outcome = ApplyOutcome.REQUIRES_ACCOUNT
            result.error_message = (
                f"LinkedIn session not found at {SESSION_FILE}. "
                "Run: make linkedin-login"
            )
            result.log("No LinkedIn session file — run: make linkedin-login")
            logger.warning("LinkedIn session missing: {}", SESSION_FILE)
            return result

        try:
            # Inject saved cookies/storage before navigating
            await page.context.add_cookies(self._load_cookies())
        except Exception as exc:
            logger.warning("Could not load LinkedIn cookies: {}", exc)

        try:
            await page.goto(job_url, wait_until="domcontentloaded", timeout=30_000)

            # Check for login wall
            if await self._is_logged_out(page):
                result.outcome = ApplyOutcome.REQUIRES_ACCOUNT
                result.error_message = "LinkedIn session expired — run: make linkedin-login"
                result.log("Session expired")
                return result

            # Check CAPTCHA / bot challenge
            if await self._is_blocked(page):
                result.outcome = ApplyOutcome.BLOCKED
                result.error_message = "LinkedIn CAPTCHA / bot challenge detected"
                result.screenshot_path = await self._screenshot(page, f"li_blocked_{job_id}")
                return result

            # Check already applied
            if await self._check_already_applied(page):
                result.outcome = ApplyOutcome.ALREADY_APPLIED
                result.log("Already applied")
                return result

            # Click the Easy Apply button
            clicked = await self._click_easy_apply(page, result)
            if not clicked:
                result.outcome = ApplyOutcome.UNSUPPORTED_FORM
                result.screenshot_path = await self._screenshot(page, f"li_no_easy_apply_{job_id}")
                return result

            # Fill the multi-step form
            await asyncio.sleep(1.5)  # modal animation
            completed = await self._fill_easy_apply_form(page, result)

            if not completed:
                result.outcome = ApplyOutcome.UNSUPPORTED_FORM
                result.screenshot_path = await self._screenshot(page, f"li_unsupported_{job_id}")
                return result

            # Submit
            result.screenshot_path = await self._screenshot(page, f"li_pre_submit_{job_id}")
            submitted = await self._submit(page, result)
            if submitted:
                result.outcome = ApplyOutcome.SUCCESS
                result.log("Application submitted via LinkedIn Easy Apply")
            else:
                result.outcome = ApplyOutcome.ERROR
                result.screenshot_path = await self._screenshot(page, f"li_submit_fail_{job_id}")

        except PlaywrightTimeout as exc:
            result.outcome = ApplyOutcome.ERROR
            result.error_message = f"Timeout: {exc}"
            result.screenshot_path = await self._screenshot(page, f"li_timeout_{job_id}")
            logger.error("LinkedIn timeout | job={} | {}", job_id, exc)

        except Exception as exc:
            result.outcome = ApplyOutcome.ERROR
            result.error_message = str(exc)
            result.screenshot_path = await self._screenshot(page, f"li_error_{job_id}")
            logger.error("LinkedIn error | job={} | {}", job_id, exc)

        return result

    # ── Session management ─────────────────────────────────────────────────────

    @staticmethod
    def _load_cookies() -> list[dict]:
        """Load cookies from the saved session file."""
        import json
        data = json.loads(SESSION_FILE.read_text())
        # storage_state has {"cookies": [...], "origins": [...]}
        return data.get("cookies", [])

    # ── Page state detection ───────────────────────────────────────────────────

    async def _is_logged_out(self, page: Page) -> bool:
        """Return True if we hit a sign-in wall."""
        try:
            content = (await page.content()).lower()
            signals = ["sign in to linkedin", "join linkedin", "authwall"]
            return any(s in content for s in signals)
        except Exception:
            return False

    async def _is_blocked(self, page: Page) -> bool:
        """Return True if LinkedIn is showing a CAPTCHA or challenge."""
        try:
            content = (await page.content()).lower()
            return "security verification" in content or "captcha" in content
        except Exception:
            return False

    # ── Easy Apply flow ────────────────────────────────────────────────────────

    async def _click_easy_apply(self, page: Page, result: ApplyResult) -> bool:
        """Find and click the Easy Apply button. Returns True on success."""
        selectors = [
            "button.jobs-apply-button:has-text('Easy Apply')",
            "button[aria-label*='Easy Apply']",
            "button:has-text('Easy Apply')",
        ]
        for sel in selectors:
            try:
                el = page.locator(sel).first
                if await el.count() and await el.is_visible():
                    await el.click()
                    result.log(f"Clicked Easy Apply: {sel!r}")
                    return True
            except Exception:
                continue

        result.log("Easy Apply button not found — may require full application or external link")
        return False

    async def _fill_easy_apply_form(self, page: Page, result: ApplyResult) -> bool:
        """
        Navigate through the multi-step Easy Apply modal.

        LinkedIn's Easy Apply modal has 1–10 steps. Common steps:
          1. Contact info (phone, address)
          2. Resume upload
          3. Work experience questions
          4. Custom screening questions
          5. Review

        Returns False if we hit an unsupported step we can't fill.
        """
        max_steps = 10
        for step in range(max_steps):
            # Check if the modal is still open
            modal = page.locator(".jobs-easy-apply-modal, [data-test-modal]").first
            if not await modal.count():
                result.log(f"Modal closed at step {step + 1} — assuming complete")
                return True

            # Check for review/submit page
            if await self._is_review_page(page):
                result.log("Reached review page")
                return True

            # Fill visible fields in this step
            await self._fill_step(page, result)
            await asyncio.sleep(0.5)

            # Try to advance to the next step
            advanced = await self._click_next(page, result)
            if not advanced:
                # Could be the submit button — check
                if await self._is_review_page(page):
                    return True
                result.log(f"Could not advance past step {step + 1}")
                return False

            await asyncio.sleep(1.0)

        result.log("Exceeded max steps — form may be too complex")
        return False

    async def _is_review_page(self, page: Page) -> bool:
        """Return True if we're on the review/summary step."""
        try:
            el = page.locator("button:has-text('Submit application'), button:has-text('Submit Application')").first
            return bool(await el.count() and await el.is_visible())
        except Exception:
            return False

    async def _fill_step(self, page: Page, result: ApplyResult) -> None:
        """Fill all recognisable fields in the current modal step."""
        p = self._personal
        prefs = self._prefs
        wa = self._work_auth

        # Phone
        await self._fill_text(page, [
            "input[id*='phoneNumber']",
            "input[name*='phone']",
            "input[placeholder*='Phone']",
        ], p.get("phone", ""), result)

        # City / location
        await self._fill_text(page, [
            "input[id*='city']",
            "input[placeholder*='City']",
        ], p.get("location_city", "Chicago"), result)

        # Resume upload
        await self._upload_resume(page, [
            "input[type='file'][name*='resume']",
            "input[type='file'][id*='resume']",
            "input[type='file']",
        ], result)

        # Work authorization — select dropdowns
        if wa.get("authorized_to_work_in_us"):
            await self._select_option(page, [
                "select[id*='authorized']",
                "select[name*='authorized']",
            ], "Yes", result)
        else:
            await self._select_option(page, [
                "select[id*='authorized']",
            ], "No", result)

        # Visa / sponsorship
        if wa.get("require_sponsorship"):
            await self._select_option(page, [
                "select[id*='sponsorship']",
                "select[name*='sponsorship']",
            ], "Yes", result)
        else:
            await self._select_option(page, [
                "select[id*='sponsorship']",
            ], "No", result)

        # Salary expectations (text inputs)
        salary_min = str(prefs.get("desired_salary_min", ""))
        if salary_min:
            await self._fill_text(page, [
                "input[id*='salary']",
                "input[placeholder*='salary']",
                "input[placeholder*='Salary']",
            ], salary_min, result)

        # "How did you hear about us" type fields
        await self._fill_text(page, [
            "input[name*='hear']",
            "textarea[name*='hear']",
        ], "LinkedIn", result)

        # Years of experience (if numeric input present)
        years = str(prefs.get("years_of_experience", ""))
        if years:
            await self._fill_text(page, [
                "input[id*='years'][type='number']",
                "input[placeholder*='years of experience']",
            ], years, result)

    async def _click_next(self, page: Page, result: ApplyResult) -> bool:
        """Click Next / Continue / Review button. Returns True if clicked."""
        next_selectors = [
            "button[aria-label='Continue to next step']",
            "button:has-text('Next')",
            "button:has-text('Continue')",
            "button:has-text('Review')",
            "button[aria-label*='next']",
        ]
        for sel in next_selectors:
            try:
                el = page.locator(sel).first
                if await el.count() and await el.is_visible() and await el.is_enabled():
                    await el.click()
                    result.log(f"Advanced step: {sel!r}")
                    return True
            except Exception:
                continue
        return False

    async def _submit(self, page: Page, result: ApplyResult) -> bool:
        """Click the final Submit Application button."""
        submit_selectors = [
            "button[aria-label='Submit application']",
            "button:has-text('Submit application')",
            "button:has-text('Submit Application')",
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


# ── Session login helper ────────────────────────────────────────────────────────


async def linkedin_login(session_file: Path = SESSION_FILE) -> None:
    """
    Open a visible Chrome browser, let the user log in to LinkedIn manually,
    then save the session to disk for future automated runs.

    Called by: make linkedin-login
    """
    from playwright.async_api import async_playwright

    print("\n  LinkedIn Login Setup")
    print("  " + "─" * 40)
    print("  1. A Chrome window will open.")
    print("  2. Log in to LinkedIn normally.")
    print("  3. Once you see your LinkedIn feed, close the browser.")
    print(f"  4. Session will be saved to: {session_file}")
    print()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, slow_mo=100)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()
        await page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
        print("  Browser opened. Log in and then close the window.")

        # Wait for the user to navigate to the feed (logged-in state)
        try:
            await page.wait_for_url(
                re.compile(r"linkedin\.com/feed|linkedin\.com/in/"),
                timeout=300_000,  # 5 min
            )
            print("  Login detected! Saving session...")
            await context.storage_state(path=str(session_file))
            print(f"  Session saved to {session_file}")
        except Exception as exc:
            print(f"  Login timed out or browser was closed: {exc}")
            # Try saving whatever we have
            try:
                await context.storage_state(path=str(session_file))
                print(f"  Partial session saved to {session_file}")
            except Exception:
                pass
        finally:
            await browser.close()


def linkedin_login_sync() -> None:
    """Sync wrapper for the async login flow."""
    asyncio.run(linkedin_login())
