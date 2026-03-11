"""
Greenhouse ATS form handler.

Greenhouse has two application surfaces:
  1. Embedded job board  — boards.greenhouse.io/{company}/jobs/{id}
  2. Company-hosted board — jobs.company.com (still uses Greenhouse JS)

Both render a largely identical form. This handler fills:
  - Personal info (name, email, phone, LinkedIn, GitHub)
  - Resume upload
  - Location / work auth questions
  - EEO / demographic section
  - Any standard yes/no questions about sponsorship, remote, etc.

DOM selectors were mapped against Stripe, Figma, Vercel, and Datadog boards.
If a selector doesn't match, it's skipped gracefully with a log entry.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Optional

from loguru import logger
from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

from automation.base_handler import ApplyOutcome, ApplyResult, BaseATSHandler


class GreenhouseHandler(BaseATSHandler):
    """Handles job applications on Greenhouse-powered job boards."""

    ats_name = "greenhouse"

    # ── Detection ─────────────────────────────────────────────────────────────

    @classmethod
    def detect(cls, url: str) -> bool:
        return (
            "boards.greenhouse.io" in url
            or "boards-api.greenhouse.io" in url
            or "grnh.se" in url          # Greenhouse short links
            or "gh_jid=" in url          # Custom-domain Greenhouse boards (e.g. careers.datadoghq.com)
            or "gh_src=" in url          # Alternative Greenhouse query param
        )

    # ── Main apply flow ───────────────────────────────────────────────────────

    async def apply(self, page: Page, job_id: str, job_url: str) -> ApplyResult:
        result = ApplyResult(
            outcome=ApplyOutcome.ERROR,
            job_id=job_id,
            ats_type=self.ats_name,
            url=job_url,
        )

        try:
            # Navigate if not already there.
            # Use "domcontentloaded" — many modern SPA career sites (Samsara, etc.)
            # have continuous background network activity that prevents "networkidle".
            if page.url != job_url:
                await page.goto(job_url, wait_until="domcontentloaded", timeout=30_000)
                await asyncio.sleep(2)  # brief pause for JS to render the apply button

            # Check for already-applied state
            if await self._check_already_applied(page):
                result.outcome = ApplyOutcome.ALREADY_APPLIED
                result.log("Already applied — skipping")
                return result

            # Click "Apply for this job" button / navigate to Greenhouse form
            await self._click_apply_button(page, result)

            # Wait for the application form to appear (may be on a new URL after redirect)
            try:
                await page.wait_for_selector(
                    "#application_form, form#application, form.application-form",
                    timeout=15_000,
                )
            except PlaywrightTimeout:
                # Last resort: look for any visible form regardless of URL.
                # Covers boards.greenhouse.io with non-standard selectors AND
                # custom-domain pages (e.g. samsara.com) where the iframe
                # navigation happened but the form id differs.
                current_url = page.url
                try:
                    await page.wait_for_selector("form", timeout=5_000)
                    result.log(f"Found generic form on {current_url}")
                except PlaywrightTimeout:
                    result.log(f"Application form not found (url={current_url})")
                    result.screenshot_path = await self._screenshot(page, f"gh_no_form_{job_id}")
                    result.outcome = ApplyOutcome.UNSUPPORTED_FORM
                    return result

            await self._fill_personal_info(page, result)
            await self._upload_resume_gh(page, result)
            await self._fill_location(page, result)
            await self._fill_work_auth(page, result)
            await self._fill_custom_questions(page, result)
            await self._fill_education(page, result)
            await self._fill_labeled_fields(page, result)   # catch-all: label-text scan
            await self._fill_remaining_with_llm(page, result)  # LLM fallback for unknowns
            await self._handle_eeo_section(page, result)
            await self._accept_consent_checkboxes(page, result)

            # Screenshot before submit for audit
            result.screenshot_path = await self._screenshot(page, f"gh_pre_submit_{job_id}")

            submitted = await self._submit(page, result)
            if submitted:
                result.outcome = ApplyOutcome.SUCCESS
                result.log("Application submitted successfully")
            else:
                result.outcome = ApplyOutcome.ERROR
                result.log("Submit button not found or click failed")
                result.screenshot_path = await self._screenshot(page, f"gh_submit_fail_{job_id}")

        except PlaywrightTimeout as exc:
            result.outcome = ApplyOutcome.ERROR
            result.error_message = f"Timeout: {exc}"
            result.screenshot_path = await self._screenshot(page, f"gh_timeout_{job_id}")
            logger.error("Greenhouse timeout | job={} | {}", job_id, exc)

        except Exception as exc:
            result.outcome = ApplyOutcome.ERROR
            result.error_message = str(exc)
            result.screenshot_path = await self._screenshot(page, f"gh_error_{job_id}")
            logger.error("Greenhouse error | job={} | {}", job_id, exc)

        return result

    # ── Step implementations ──────────────────────────────────────────────────

    async def _click_apply_button(self, page: Page, result: ApplyResult) -> None:
        """
        Navigate to the Greenhouse application form.

        Priority order:
        1. Find a link pointing directly to boards.greenhouse.io and navigate there
           (handles custom-domain career pages like careers.datadoghq.com)
        2. Click standard apply button text variants
        """
        # ── Priority 1: Scan for direct Greenhouse links ──────────────────────
        # Many custom career sites link "Apply" directly to boards.greenhouse.io
        try:
            gh_links = await page.locator(
                "a[href*='boards.greenhouse.io'], a[href*='grnh.se'], a[href*='applications/new']"
            ).all()
            for link in gh_links:
                try:
                    href = await link.get_attribute("href")
                    if href and ("greenhouse.io" in href or "applications/new" in href):
                        await page.goto(href, wait_until="domcontentloaded", timeout=30_000)
                        result.log(f"Navigated to Greenhouse form directly: {href}")
                        return
                except Exception:
                    continue
        except Exception:
            pass

        # ── Priority 2: Standard apply button text variants ───────────────────
        apply_selectors = [
            "a:has-text('Apply for this Job')",
            "a:has-text('Apply for this Position')",
            "a:has-text('Apply Now')",
            "a:has-text('Apply')",
            "button:has-text('Apply for this Job')",
            "button:has-text('Apply Now')",
            "button:has-text('Apply')",
            "#apply_button",
            ".apply-button",
            "[data-mapped='true'] a",   # some Greenhouse embedded boards
        ]
        clicked_sel: str | None = None
        for sel in apply_selectors:
            try:
                el = page.locator(sel).first
                if await el.count() and await el.is_visible():
                    await el.click()
                    await page.wait_for_load_state("domcontentloaded", timeout=10_000)
                    await asyncio.sleep(2)
                    result.log(f"Clicked apply button: {sel!r}")
                    clicked_sel = sel
                    break
            except Exception:
                continue

        # ── Priority 3: Greenhouse iframe (e.g. Samsara modal embed) ──────────
        # Some custom career pages open the Greenhouse form in an iframe modal
        # rather than navigating away. Detect the iframe and navigate to its src.
        try:
            iframe_loc = page.locator(
                "iframe[src*='greenhouse.io'], iframe[src*='boards.greenhouse.io']"
            )
            if await iframe_loc.count():
                src = await iframe_loc.first.get_attribute("src")
                if src and "greenhouse.io" in src:
                    await page.goto(src, wait_until="domcontentloaded", timeout=30_000)
                    result.log(f"Navigated to Greenhouse iframe src: {src}")
                    return
        except Exception:
            pass

        if clicked_sel:
            return  # button was clicked and no iframe found — proceed normally

    async def _fill_personal_info(self, page: Page, result: ApplyResult) -> None:
        p = self._personal
        fields = [
            (["#first_name", "input[name='job_application[first_name]']"],
             p.get("first_name", "")),
            (["#last_name", "input[name='job_application[last_name]']"],
             p.get("last_name", "")),
            (["#email", "input[name='job_application[email]']"],
             p.get("email", "")),
            (["#phone", "input[name='job_application[phone]']"],
             p.get("phone", "")),
            (["#job_application_location", "input[id*='location']"],
             f"{p.get('location_city', '')}, {p.get('location_state', '')}"),
        ]
        for selectors, value in fields:
            if value:
                await self._fill_text(page, selectors, value, result)

        # LinkedIn URL
        linkedin = p.get("linkedin_url", "")
        if linkedin:
            await self._fill_text(page, [
                "input[id*='linkedin']",
                "input[placeholder*='LinkedIn']",
                "input[aria-label*='LinkedIn']",
            ], linkedin, result)

        # GitHub URL
        github = p.get("github_url", "")
        if github:
            await self._fill_text(page, [
                "input[id*='github']",
                "input[placeholder*='GitHub']",
                "input[aria-label*='GitHub']",
            ], github, result)

        # Portfolio / website
        portfolio = p.get("portfolio_url", "")
        if portfolio:
            await self._fill_text(page, [
                "input[id*='website']",
                "input[id*='portfolio']",
                "input[placeholder*='website']",
            ], portfolio, result)

    async def _upload_resume_gh(self, page: Page, result: ApplyResult) -> None:
        await self._upload_resume(page, [
            "#resume_input",
            "input[name='resume']",
            "input[type='file'][id*='resume']",
            "input[type='file']",
        ], result)

    async def _fill_location(self, page: Page, result: ApplyResult) -> None:
        p = self._personal

        # City
        city = p.get("location_city", "")
        if city:
            await self._fill_text(page, [
                "input[id*='city']",
                "input[placeholder*='City']",
            ], city, result)

        # State
        state = p.get("location_state", "")
        if state:
            await self._fill_text(page, [
                "input[id*='state']",
                "input[placeholder*='State']",
            ], state, result)
            await self._select_option(page, [
                "select[id*='state']",
                "select[name*='state']",
            ], state, result)

        # Country — Greenhouse commonly shows a country dropdown
        country = p.get("location_country", "United States")
        if country:
            await self._select_option(page, [
                "select[id*='country']",
                "select[name*='country']",
                "select[aria-label*='Country']",
            ], country, result)
            # Some forms show a text input instead
            await self._fill_text(page, [
                "input[id*='country']",
                "input[placeholder*='Country']",
            ], country, result)

    async def _fill_work_auth(self, page: Page, result: ApplyResult) -> None:
        wa = self._work_auth
        authorized = wa.get("authorized_to_work_in_us", True)
        needs_sponsor = wa.get("require_sponsorship", True)

        # "Are you authorized to work in the US?"
        auth_selectors = [
            "select[id*='authorized']",
            "select[aria-label*='authorized']",
        ]
        auth_value = "Yes" if authorized else "No"
        await self._select_option(page, auth_selectors, auth_value, result)
        await self._answer_yes_no(page, "authorized to work", authorized, result)

        # "Do you require visa sponsorship?"
        sponsor_selectors = [
            "select[id*='sponsor']",
            "select[aria-label*='sponsor']",
        ]
        await self._select_option(page, sponsor_selectors,
                                   "Yes" if needs_sponsor else "No", result)
        await self._answer_yes_no(page, "require.*sponsor", needs_sponsor, result)

    async def _fill_custom_questions(self, page: Page, result: ApplyResult) -> None:
        """
        Handle common custom questions on Greenhouse forms.
        These vary per company but commonly include salary, start date, remote pref.
        """
        prefs = self._prefs
        p = self._personal
        profile = self.profile

        # Salary expectation
        salary_min = prefs.get("desired_salary_min", "")
        if salary_min:
            await self._fill_text(page, [
                "input[id*='salary']",
                "input[placeholder*='salary']",
                "input[placeholder*='compensation']",
                "input[placeholder*='Salary']",
            ], str(salary_min), result)

        # Years of experience
        years = str(prefs.get("years_of_experience", ""))
        if years:
            await self._fill_text(page, [
                "input[id*='years'][type='number']",
                "input[id*='experience'][type='number']",
                "input[placeholder*='years of experience']",
                "input[placeholder*='Years of experience']",
            ], years, result)

        # "Are you willing to relocate?"
        await self._answer_yes_no(page, "willing to relocate",
                                   prefs.get("willing_to_relocate", False), result)

        # "How did you hear about us?"
        await self._fill_text(page, [
            "input[id*='referral']",
            "select[id*='hear_about']",
            "input[placeholder*='hear about']",
        ], "LinkedIn", result)

        # Cover letter — use profile template if provided, else generate minimal one
        cl_template = (profile.get("cover_letter") or {}).get("template", "")
        if not cl_template:
            cl_template = (
                f"I am excited to apply for this position. "
                f"With {years or 'several'} years of software engineering experience, "
                f"I am confident I would be a strong addition to your team. "
                f"Please find my resume attached for your review."
            )
        await self._fill_text(page, [
            "textarea[id*='cover']",
            "textarea[name*='cover']",
            "textarea[placeholder*='cover letter']",
            "textarea[placeholder*='Cover Letter']",
            "textarea[id*='letter']",
        ], cl_template, result)

    async def _fill_education(self, page: Page, result: ApplyResult) -> None:
        """Fill education fields — degree, institution, graduation year."""
        education = self.profile.get("education", [])
        if not education:
            return
        most_recent = education[0]  # profile lists most recent first

        degree = most_recent.get("degree", "")
        institution = most_recent.get("institution", "")
        grad_year = str(most_recent.get("graduation_year", ""))
        field_of_study = most_recent.get("field", "")

        if degree:
            await self._fill_text(page, [
                "input[id*='degree']",
                "input[placeholder*='degree']",
                "input[name*='degree']",
            ], degree, result)
            await self._select_option(page, [
                "select[id*='degree']",
                "select[name*='degree']",
                "select[aria-label*='Degree']",
            ], degree, result)

        if field_of_study:
            await self._fill_text(page, [
                "input[id*='field']",
                "input[id*='major']",
                "input[placeholder*='field of study']",
                "input[placeholder*='Major']",
            ], field_of_study, result)

        if institution:
            await self._fill_text(page, [
                "input[id*='school']",
                "input[id*='institution']",
                "input[id*='university']",
                "input[placeholder*='School']",
                "input[placeholder*='Institution']",
                "input[placeholder*='University']",
            ], institution, result)

        if grad_year:
            await self._fill_text(page, [
                "input[id*='graduation']",
                "input[id*='grad_year']",
                "input[placeholder*='Graduation year']",
                "input[placeholder*='graduation']",
            ], grad_year, result)

    async def _fill_labeled_fields(self, page: Page, result: ApplyResult) -> None:
        """
        Fill Greenhouse custom question fields (question_XXXXXXX IDs) by scanning
        every <label> on the page and matching its text to known patterns.

        This handles the long tail of per-company required questions whose numeric
        IDs differ across forms (sponsorship, experience ranges, consent toggles,
        zip code, current employer, etc.).
        """
        p = self._personal
        wa = self._work_auth
        prefs = self._prefs

        yes_auth = "Yes" if wa.get("authorized_to_work_in_us", True) else "No"
        yes_sponsor = "Yes" if wa.get("require_sponsorship", False) else "No"
        yes_relocate = "Yes" if prefs.get("willing_to_relocate", False) else "No"
        years = int(prefs.get("years_of_experience", 0))

        # (label_regex, value_to_fill)
        # Matched case-insensitively against the label's inner text.
        # ORDER MATTERS: more-specific patterns must come before broad ones.
        RULES: list[tuple[str, str]] = [
            # ── Name variants ─────────────────────────────────────────────────
            (r"preferred.{0,10}first", p.get("first_name", "")),
            (r"preferred.{0,10}last", p.get("last_name", "")),
            (r"pronouns", ""),  # leave blank — no profile field for this
            # ── Location ──────────────────────────────────────────────────────
            (r"zip.?code|postal.?code", p.get("zip_code", "")),
            (r"city.{0,10}state|us city|city and state", f"{p.get('location_city', '')}, {p.get('location_state', '')}"),
            # Country — label wording varies widely; cast a broad net
            (r"country.*reside|reside.*country|currently reside|where do you live"
             r"|country.*where|select.*country|country.*located|country of residence"
             r"|current.*country|country.*current", p.get("location_country", "United States")),
            # ── Work auth ─────────────────────────────────────────────────────
            (r"authorized.{0,25}work|eligible.{0,25}work|currently authorized", yes_auth),
            (r"require.{0,15}(visa|sponsor)|sponsor.{0,15}required|immigration.{0,15}sponsor|visa.{0,10}sponsor|work.{0,10}permit.{0,10}sponsor", yes_sponsor),
            (r"reside.{0,25}(us|usa|united states|canada)", "Yes"),
            # ── Employment — MUST come before the previous-employment boolean rule ──
            # These are text fields asking for the employer/title name.
            (r"current.{0,10}employer|current.{0,5}/?.{0,5}previous.{0,10}employer|employer.{0,10}name", p.get("current_employer", "")),
            (r"current.{0,10}title|current.{0,5}/?.{0,5}previous.{0,10}title|job.{0,10}title", "Senior Software Engineer"),
            # ── Previous employment boolean (have you worked HERE before?) ────
            # Must NOT match "current/previous employer" text fields above.
            (r"previously.{0,20}(worked|employed)|ever.{0,15}(been employed|worked).{0,20}(at|for|with|by|here)"
             r"|been.{0,10}(an employee|employed).{0,20}(of|at|by|here|with)|were you.{0,20}(an employee|previously)"
             r"|worked.{0,10}here.{0,10}before|former.{0,10}employee.{0,20}(of|at|here)", "No"),
            # ── Hub / office preference ───────────────────────────────────────
            # Multi-city hub questions — pick "not local, open to relocation" if
            # we're not local; the LLM will refine if it finds a better option.
            (r"hub.{0,20}(availab|prefer|locat|option)|are you local|local to the",
             "Not local" if p.get("location_city", "") not in ("New York", "NYC", "San Francisco", "SF") else "Local"),
            # ── Referral ──────────────────────────────────────────────────────
            (r"hear.{0,25}about|how.{0,20}find|referral.{0,10}source|how did you", "LinkedIn"),
            # ── Relocation ────────────────────────────────────────────────────
            (r"relocation.{0,20}assist", yes_relocate),
            # ── Salary ────────────────────────────────────────────────────────
            (r"salary.{0,20}range|compensation.{0,20}range|accept.{0,15}salary|salary.{0,10}expectation", "Yes"),
            # ── Consent / policy / agreement ──────────────────────────────────
            (r"processing.{0,20}personal.{0,10}data|data.{0,10}processing|gdpr|privacy.{0,10}polic", "Yes"),
            (r"ai.{0,15}(polic|agree|usage|term)", "Yes"),
            (r"sms.{0,10}(consent|opt|messag)", "No"),
        ]

        try:
            labels = await page.locator("label").all()
        except Exception:
            return

        for label in labels:
            try:
                label_text = (await label.inner_text()).strip()
                for_id = await label.get_attribute("for") or ""
                if not for_id:
                    continue

                # Match label text against rules
                matched_value: str | None = None
                for pattern, value in RULES:
                    if re.search(pattern, label_text, re.IGNORECASE):
                        matched_value = value
                        break

                # Experience year-range selects — special handling
                if matched_value is None and re.search(
                    r"experience.{0,25}year|year.{0,25}experience|years.{0,10}of.{0,10}experience",
                    label_text, re.IGNORECASE
                ):
                    matched_value = await self._best_years_option(page, f"#{for_id}", years)

                if matched_value is None:
                    continue  # no rule matched — leave it to other fill methods

                field = page.locator(f"#{for_id}").first
                if not await field.count():
                    continue

                tag = (await field.evaluate("el => el.tagName")).lower()
                role = (await field.get_attribute("role") or "").lower()
                input_type = (await field.get_attribute("type") or "text").lower()

                if tag == "select":
                    if matched_value:
                        await self._set_select_field(page, for_id, matched_value, result)
                elif tag == "input" and role in ("combobox", "listbox"):
                    # React Select / Greenhouse new embed — click to open, then pick option
                    if matched_value:
                        await self._fill_combobox(page, for_id, matched_value, result)
                elif tag == "input" and input_type not in ("hidden", "file", "submit", "checkbox", "radio"):
                    current = await field.input_value()
                    if not current and matched_value:
                        await self._fill_text(page, [f"#{for_id}"], matched_value, result)
                elif tag == "textarea":
                    current = await field.input_value()
                    if not current and matched_value:
                        await self._fill_text(page, [f"#{for_id}"], matched_value, result)
                else:
                    # Div/span/other custom component — treat as select
                    if matched_value:
                        await self._set_select_field(page, for_id, matched_value, result)
            except Exception:
                continue

    async def _fill_combobox(
        self, page: Page, field_id: str, value: str, result: ApplyResult
    ) -> bool:
        """
        Fill a React Select / custom combobox by:
          1. Clicking the input to open the dropdown menu.
          2. Optionally typing to filter options.
          3. Clicking the matching option in the opened menu.

        Used for Greenhouse's new embed form (job-boards.greenhouse.io) which
        renders single-select custom questions as React Select inputs with
        role="combobox", unlike the classic boards.greenhouse.io which uses
        native <select> elements.
        """
        try:
            field = page.locator(f"#{field_id}").first
            if not await field.count():
                return False

            # Click to open the dropdown
            await field.click()
            await asyncio.sleep(0.4)

            # Find the matching option in the dropdown menu
            option = page.locator(
                f"[class*='option']:has-text('{value}'), "
                f"[role='option']:has-text('{value}'), "
                f"[class*='menu'] li:has-text('{value}'), "
                f"[class*='dropdown'] li:has-text('{value}')"
            ).first
            if await option.count() and await option.is_visible():
                await option.click()
                result.log(f"Combobox #{field_id} = {value!r}")
                return True

            # Fallback: type the value to filter, then pick the first remaining option
            await field.fill(value)
            await asyncio.sleep(0.4)
            option = page.locator(
                f"[class*='option']:has-text('{value}'), "
                f"[role='option']:has-text('{value}')"
            ).first
            if await option.count() and await option.is_visible():
                await option.click()
                result.log(f"Combobox #{field_id} = {value!r} (typed filter)")
                return True

        except Exception:
            pass
        return False

    async def _set_select_field(
        self, page: Page, field_id: str, value: str, result: ApplyResult
    ) -> bool:
        """
        Robustly set a select/dropdown field, handling three common patterns:

        1. Native visible <select> — Playwright select_option() works directly.
        2. Native hidden <select> backed by Chosen/Select2 — select_option() still
           works on the hidden element even though is_visible() returns False.
        3. Custom div/React dropdown — click the trigger sibling, then click the
           option text inside the opened menu.

        Returns True if a value was successfully set.
        """
        sel = f"#{field_id}"

        # ── Strategy 1 & 2: native <select> (visible or hidden) ──────────────
        try:
            el = page.locator(sel).first
            if await el.count():
                tag = (await el.evaluate("el => el.tagName")).lower()
                if tag == "select":
                    # Try by label text first, then by value
                    try:
                        await el.select_option(label=value)
                        result.log(f"Selected #{field_id} = {value!r} (native label)")
                        return True
                    except Exception:
                        pass
                    try:
                        await el.select_option(value=value)
                        result.log(f"Selected #{field_id} = {value!r} (native value)")
                        return True
                    except Exception:
                        pass
        except Exception:
            pass

        # ── Strategy 3: custom dropdown (Chosen / Select2 / React Select) ────
        # Pattern: the <label for="field_id"> points to a hidden input; the
        # visible trigger is a sibling div/button. Click it, then pick the option.
        try:
            # Find the wrapper element containing both the hidden input and the trigger
            wrapper = page.locator(f"#{field_id}").locator("xpath=..").first
            if not await wrapper.count():
                return False

            # Click the visible trigger inside the wrapper (span, div, button)
            trigger = wrapper.locator(
                "div.chosen-single, div.select2-selection, "
                "[role='combobox'], [role='button'], "
                "div[class*='select']:not(select), span[class*='select']"
            ).first
            if await trigger.count() and await trigger.is_visible():
                await trigger.click()
                await asyncio.sleep(0.5)
            else:
                # Fallback: click the wrapper itself
                await wrapper.click()
                await asyncio.sleep(0.5)

            # Look for the option text in the now-open dropdown list
            option_loc = page.locator(
                f"li:has-text('{value}'), "
                f"div[role='option']:has-text('{value}'), "
                f"span[role='option']:has-text('{value}'), "
                f".chosen-results li:has-text('{value}'), "
                f".select2-results li:has-text('{value}')"
            ).first
            if await option_loc.count() and await option_loc.is_visible():
                await option_loc.click()
                result.log(f"Selected #{field_id} = {value!r} (custom dropdown)")
                return True
        except Exception:
            pass

        result.log(f"Could not select #{field_id} = {value!r} — no strategy worked")
        return False

    async def _best_years_option(self, page: Page, selector: str, years: int) -> str:
        """
        Pick the <select> option label that best covers `years`.
        Handles ranges like '3-5 years', '10+ years', '0-1 years'.
        Falls back to the last (highest) option if nothing matches.
        """
        try:
            options = await page.locator(f"{selector} option").all()
            last_valid = ""
            for opt in options:
                text = (await opt.inner_text()).strip()
                if not text or text.lower() in ("select", "please select", "—", ""):
                    continue
                nums = re.findall(r"\d+", text)
                if not nums:
                    continue
                low = int(nums[0])
                high = int(nums[1]) if len(nums) > 1 else 999
                last_valid = text
                if low <= years <= high:
                    return text
            return last_valid  # fallback: highest range
        except Exception:
            return str(years)

    async def _fill_remaining_with_llm(self, page: Page, result: ApplyResult) -> None:
        """
        After all rule-based fills, scan for still-empty required fields and
        ask the LLM to provide best-guess answers. Results are saved to
        configs/form_answers.yaml (unconfirmed) and recorded in result.llm_guesses.
        """
        import json as _json
        from core import form_answers as fa
        from pathlib import Path as _Path

        # ── Collect empty required fields ─────────────────────────────────────
        empty_fields: list[dict] = []
        try:
            required_inputs = await page.locator(
                # HTML required attribute
                "input[required]:not([type='hidden']):not([type='file']):not([type='submit']),"
                "select[required],"
                "textarea[required],"
                # aria-required (used by Greenhouse new embed + React components)
                "input[aria-required='true']:not([type='hidden']):not([type='file']):not([type='submit']),"
                "select[aria-required='true'],"
                "textarea[aria-required='true']"
            ).all()
        except Exception:
            return

        for inp in required_inputs:
            try:
                field_id = await inp.get_attribute("id") or ""
                if not field_id:
                    continue

                tag = (await inp.evaluate("el => el.tagName")).lower()
                input_type = (await inp.get_attribute("type") or "text").lower()
                role = (await inp.get_attribute("role") or "").lower()
                # Classify field type for LLM prompt and fill strategy
                if tag == "select":
                    field_type = "select"
                elif role in ("combobox", "listbox"):
                    field_type = "combobox"
                elif tag == "textarea":
                    field_type = "textarea"
                else:
                    field_type = input_type  # text, email, number, etc.

                # Check if already has a value
                if tag == "select":
                    selected_val = await inp.evaluate(
                        "el => el.options[el.selectedIndex] ? el.options[el.selectedIndex].value : ''"
                    )
                    if selected_val and selected_val not in ("", "0", "none", "null"):
                        continue  # already filled
                else:
                    try:
                        current = await inp.input_value()
                        if current:
                            continue  # already filled
                    except Exception:
                        continue  # can't read value — skip

                # Get associated label text
                label_text = ""
                try:
                    label_el = page.locator(f"label[for='{field_id}']")
                    if await label_el.count():
                        label_text = (await label_el.first.inner_text()).strip()
                except Exception:
                    pass
                if not label_text:
                    continue  # can't identify field without label

                # Check form_answers.yaml first
                cached = fa.get(label_text)
                if cached is not None:
                    # For combobox/select: open dropdown to verify the cached value
                    # exists as an exact option (handles case where value was stored
                    # as "True"/"False" or a partial match from a previous run)
                    if field_type == "combobox":
                        live_opts = await self._get_combobox_options(page, field_id)
                        if live_opts:
                            opts_lower = [o.lower() for o in live_opts]
                            # Normalize True/False to Yes/No
                            if cached.lower() in ("true", "false") and "yes" in opts_lower:
                                cached = live_opts[opts_lower.index("yes")] if cached.lower() == "true" else live_opts[opts_lower.index("no")]
                            # If cached value not in options, fall through to LLM
                            if cached not in live_opts and cached.lower() not in opts_lower:
                                empty_fields.append({
                                    "id": field_id, "label": label_text,
                                    "type": field_type, "options": live_opts, "required": True,
                                })
                                continue
                    if field_type == "select":
                        await self._set_select_field(page, field_id, cached, result)
                    elif field_type == "combobox":
                        await self._fill_combobox(page, field_id, cached, result)
                    else:
                        await self._fill_text(page, [f"#{field_id}"], cached, result)
                    result.log(f"[form_answers] Filled '{label_text}' = {cached!r}")
                    result.llm_guesses.append({
                        "label": label_text, "value": cached,
                        "field_id": field_id, "source": "form_answers",
                        "confirmed": fa.get(label_text, confirmed_only=True) is not None,
                    })
                    continue

                # Gather options for select/combobox fields
                options: list[str] = []
                if field_type in ("select", "combobox"):
                    try:
                        # Native <select> options
                        opt_els = await page.locator(f"#{field_id} option").all()
                        for o in opt_els:
                            t = (await o.inner_text()).strip()
                            v = await o.get_attribute("value") or ""
                            if t and v not in ("", "0"):
                                options.append(t)
                    except Exception:
                        pass

                    if not options and field_type == "combobox":
                        # React Select: open the dropdown to read options, then close
                        options = await self._get_combobox_options(page, field_id)

                empty_fields.append({
                    "id": field_id,
                    "label": label_text,
                    "type": field_type,
                    "options": options,
                    "required": True,
                })
            except Exception:
                continue

        if not empty_fields:
            return

        # ── Call LLM ──────────────────────────────────────────────────────────
        try:
            from llm.client import get_llm_client
            prompt_tpl = (_Path("llm/prompts/form_field.txt")).read_text()
            p = self._personal
            wa = self._work_auth
            prefs = self._prefs
            profile_summary = (
                f"Name: {p.get('first_name')} {p.get('last_name')}\n"
                f"Location: {p.get('location_city')}, {p.get('location_state')}, {p.get('location_country')}\n"
                f"Zip: {p.get('zip_code', '')}\n"
                f"Current employer: {p.get('current_employer', '')}\n"
                f"Years of experience: {prefs.get('years_of_experience', '')}\n"
                f"Authorized to work in US: {wa.get('authorized_to_work_in_us', True)}\n"
                f"Requires visa sponsorship: {wa.get('require_sponsorship', False)}\n"
                f"Willing to relocate: {prefs.get('willing_to_relocate', False)}\n"
                f"LinkedIn: {p.get('linkedin_url', '')}\n"
                f"GitHub: {p.get('github_url', '')}\n"
            )
            # Use replace() — not .format() — to avoid KeyError when the
            # JSON payload contains curly braces that Python misreads as
            # format placeholders.
            prompt = (
                prompt_tpl
                .replace("{profile}", profile_summary)
                .replace("{fields}", _json.dumps(empty_fields, indent=2))
            )
            llm = get_llm_client()
            raw = llm.generate(prompt, fast=True, temperature=0.1)

            # Extract JSON from response (may be wrapped in ```json ... ```)
            json_match = re.search(r"\{.*\}", raw, re.DOTALL)
            if not json_match:
                result.log(f"[LLM] Could not parse JSON from response")
                return
            answers: dict = _json.loads(json_match.group())
        except Exception as exc:
            result.log(f"[LLM] form-field call failed: {exc}")
            return

        # ── Apply LLM answers ─────────────────────────────────────────────────
        field_map = {f["id"]: f for f in empty_fields}
        for field_id, value in answers.items():
            if not value or field_id not in field_map:
                continue
            finfo = field_map[field_id]
            label = finfo["label"]
            ftype = finfo["type"]
            options = finfo.get("options", [])

            # Normalize boolean strings: LLM sometimes returns "True"/"False"
            # instead of "Yes"/"No" when the field has yes/no options.
            if value.lower() in ("true", "false") and options:
                opts_lower = [o.lower() for o in options]
                is_true = value.lower() == "true"
                if "yes" in opts_lower and "no" in opts_lower:
                    value = options[opts_lower.index("yes")] if is_true else options[opts_lower.index("no")]
                elif is_true and any(o.lower().startswith("yes") for o in options):
                    value = next(o for o in options if o.lower().startswith("yes"))
                elif not is_true and any(o.lower().startswith("no") for o in options):
                    value = next(o for o in options if o.lower().startswith("no"))

            if ftype == "select":
                await self._set_select_field(page, field_id, value, result)
            elif ftype == "combobox":
                await self._fill_combobox(page, field_id, value, result)
            else:
                await self._fill_text(page, [f"#{field_id}"], value, result)

            result.log(f"[LLM] Filled '{label}' = {value!r}")

            # Save to form_answers.yaml as unconfirmed
            fa.save(label, value, confirmed=False, source="llm")

            result.llm_guesses.append({
                "label": label,
                "value": value,
                "field_id": field_id,
                "options": options,
                "source": "llm",
                "confirmed": False,
            })

    async def _get_combobox_options(self, page: Page, field_id: str) -> list[str]:
        """
        Click a React Select combobox to open its menu, collect all option texts,
        then close without selecting anything. Used to populate the options list
        before sending to the LLM so it can return an exact match.
        """
        options: list[str] = []
        try:
            field = page.locator(f"#{field_id}").first
            if not await field.count():
                return options

            await field.click()
            await asyncio.sleep(0.3)

            # Collect option texts from the open menu
            option_els = await page.locator(
                "[class*='option']:not([class*='--is-disabled']), [role='option']"
            ).all()
            for o in option_els:
                try:
                    text = (await o.inner_text()).strip()
                    if text:
                        options.append(text)
                except Exception:
                    continue

            # Close without selecting
            await field.press("Escape")
            await asyncio.sleep(0.2)
        except Exception:
            pass
        return options

    async def _accept_consent_checkboxes(self, page: Page, result: ApplyResult) -> None:
        """Check any consent / agreement checkboxes that are required to submit."""
        consent_selectors = [
            "input[type='checkbox'][id*='agree']",
            "input[type='checkbox'][id*='consent']",
            "input[type='checkbox'][id*='terms']",
            "input[type='checkbox'][name*='agree']",
            "input[type='checkbox'][name*='consent']",
            "input[type='checkbox'][name*='terms']",
        ]
        for sel in consent_selectors:
            try:
                checkboxes = await page.locator(sel).all()
                for cb in checkboxes:
                    if await cb.count() and not await cb.is_checked():
                        await cb.check()
                        result.log(f"Checked consent checkbox: {sel!r}")
            except Exception:
                continue

    async def _submit(self, page: Page, result: ApplyResult) -> bool:
        submit_selectors = [
            "button[type='submit']#submit_app",
            "input[type='submit'][value*='Submit']",
            "button:has-text('Submit Application')",
            "button:has-text('Submit')",
            "#submit_app",
        ]
        for sel in submit_selectors:
            try:
                el = page.locator(sel).first
                if await el.count() and await el.is_visible():
                    await el.click()
                    # Wait for confirmation page or URL change
                    await asyncio.sleep(2)
                    await page.wait_for_load_state("networkidle", timeout=15_000)
                    result.log(f"Clicked submit: {sel!r}")
                    return True
            except Exception:
                continue
        return False
