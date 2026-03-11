"""
Tests for Phase 4 automation layer.

Covers:
  - RateLimiter: can_apply, is_score_eligible, seconds_until_slot
  - Handler detection: _get_handler returns correct class for each URL
  - ApplyResult: outcome helpers, log accumulation
  - ApplicationRunner: dry_run path, handler registry

No Playwright, no DB, no Ollama required.
Playwright Page objects are mocked where needed.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from automation.base_handler import ApplyOutcome, ApplyResult
from automation.greenhouse_handler import GreenhouseHandler
from automation.lever_handler import LeverHandler
from automation.linkedin_handler import LinkedInHandler
from automation.application_runner import _get_handler


# ── ApplyResult ────────────────────────────────────────────────────────────────


class TestApplyResult:
    def _make(self, outcome=ApplyOutcome.ERROR) -> ApplyResult:
        return ApplyResult(
            outcome=outcome,
            job_id="job-1",
            ats_type="greenhouse",
            url="https://boards.greenhouse.io/stripe/jobs/123",
        )

    def test_succeeded_true_on_success(self):
        r = self._make(ApplyOutcome.SUCCESS)
        assert r.succeeded is True

    def test_succeeded_false_on_error(self):
        r = self._make(ApplyOutcome.ERROR)
        assert r.succeeded is False

    def test_succeeded_false_on_blocked(self):
        r = self._make(ApplyOutcome.BLOCKED)
        assert r.succeeded is False

    def test_log_accumulates(self):
        r = self._make()
        r.log("step one")
        r.log("step two")
        assert len(r.submission_log) == 2
        assert r.submission_log[0] == "step one"
        assert r.submission_log[1] == "step two"

    def test_default_fields(self):
        r = self._make()
        assert r.screenshot_path is None
        assert r.error_message is None
        assert r.fields_filled == {}
        assert r.submission_log == []

    def test_outcome_enum_values(self):
        assert ApplyOutcome.SUCCESS == "success"
        assert ApplyOutcome.ALREADY_APPLIED == "already_applied"
        assert ApplyOutcome.REQUIRES_ACCOUNT == "requires_account"
        assert ApplyOutcome.UNSUPPORTED_FORM == "unsupported_form"
        assert ApplyOutcome.BLOCKED == "blocked"
        assert ApplyOutcome.ERROR == "error"


# ── Handler detection ──────────────────────────────────────────────────────────


class TestHandlerDetection:
    _profile = {"personal": {}, "work_authorization": {}, "preferences": {}, "demographics": {}}
    _resume = Path("resumes/master_resume.docx")

    def test_greenhouse_detected(self):
        handler = _get_handler("https://boards.greenhouse.io/stripe/jobs/123", self._profile, self._resume)
        assert isinstance(handler, GreenhouseHandler)

    def test_greenhouse_boards_url(self):
        handler = _get_handler("https://job-boards.greenhouse.io/stripe", self._profile, self._resume)
        assert isinstance(handler, GreenhouseHandler)

    def test_lever_detected(self):
        handler = _get_handler("https://jobs.lever.co/netflix/abc123", self._profile, self._resume)
        assert isinstance(handler, LeverHandler)

    def test_linkedin_detected(self):
        handler = _get_handler("https://www.linkedin.com/jobs/view/12345", self._profile, self._resume)
        assert isinstance(handler, LinkedInHandler)

    def test_unknown_url_returns_none(self):
        handler = _get_handler("https://jobs.workday.com/company/12345", self._profile, self._resume)
        assert handler is None

    def test_handler_receives_profile(self):
        profile = {"personal": {"email": "test@test.com"}, "work_authorization": {}, "preferences": {}, "demographics": {}}
        handler = _get_handler("https://boards.greenhouse.io/stripe/jobs/1", profile, self._resume)
        assert handler._personal["email"] == "test@test.com"

    def test_handler_receives_resume_path(self):
        handler = _get_handler("https://jobs.lever.co/modal/xyz", self._profile, self._resume)
        assert handler.resume_path == self._resume


# ── GreenhouseHandler.detect ───────────────────────────────────────────────────


class TestGreenhouseHandlerDetect:
    def test_boards_greenhouse_io(self):
        assert GreenhouseHandler.detect("https://boards.greenhouse.io/stripe/jobs/1") is True

    def test_job_boards_greenhouse_io(self):
        assert GreenhouseHandler.detect("https://job-boards.greenhouse.io/stripe") is True

    def test_custom_domain_gh_jid(self):
        assert GreenhouseHandler.detect("https://careers.datadoghq.com/detail/5049733/?gh_jid=5049733") is True

    def test_custom_domain_gh_src(self):
        assert GreenhouseHandler.detect("https://jobs.stripe.com/listing/123?gh_src=redsols5us") is True

    def test_non_greenhouse_url(self):
        assert GreenhouseHandler.detect("https://jobs.lever.co/netflix") is False

    def test_linkedin_url_not_greenhouse(self):
        assert GreenhouseHandler.detect("https://linkedin.com/jobs/view/123") is False


# ── LeverHandler.detect ───────────────────────────────────────────────────────


class TestLeverHandlerDetect:
    def test_jobs_lever_co(self):
        assert LeverHandler.detect("https://jobs.lever.co/figma/abc") is True

    def test_lever_co(self):
        assert LeverHandler.detect("https://lever.co/company") is True

    def test_greenhouse_not_lever(self):
        assert LeverHandler.detect("https://boards.greenhouse.io/stripe") is False


# ── LinkedInHandler.detect ────────────────────────────────────────────────────


class TestLinkedInHandlerDetect:
    def test_linkedin_jobs_view(self):
        assert LinkedInHandler.detect("https://www.linkedin.com/jobs/view/12345") is True

    def test_linkedin_jobs_search(self):
        assert LinkedInHandler.detect("https://linkedin.com/jobs/search?keywords=swe") is True

    def test_greenhouse_not_linkedin(self):
        assert LinkedInHandler.detect("https://boards.greenhouse.io/stripe") is False


# ── RateLimiter ───────────────────────────────────────────────────────────────


class TestRateLimiter:
    """
    Tests the RateLimiter logic without touching a real DB.
    We patch _applied_in_last_hour to control the count.
    """

    def _make(self, max_per_hour: int = 10, min_score: int = 70):
        from automation.rate_limiter import RateLimiter
        rl = RateLimiter.__new__(RateLimiter)
        rl.max_per_hour = max_per_hour
        rl.min_score = min_score
        return rl

    def test_can_apply_when_below_limit(self):
        rl = self._make(max_per_hour=10)
        with patch.object(rl, "_applied_in_last_hour", return_value=5):
            assert rl.can_apply() is True

    def test_cannot_apply_when_at_limit(self):
        rl = self._make(max_per_hour=10)
        with patch.object(rl, "_applied_in_last_hour", return_value=10):
            assert rl.can_apply() is False

    def test_cannot_apply_when_over_limit(self):
        rl = self._make(max_per_hour=5)
        with patch.object(rl, "_applied_in_last_hour", return_value=7):
            assert rl.can_apply() is False

    def test_is_score_eligible_at_threshold(self):
        rl = self._make(min_score=70)
        assert rl.is_score_eligible(70) is True

    def test_is_score_eligible_above_threshold(self):
        rl = self._make(min_score=70)
        assert rl.is_score_eligible(95) is True

    def test_is_score_not_eligible_below_threshold(self):
        rl = self._make(min_score=70)
        assert rl.is_score_eligible(65) is False

    def test_is_score_not_eligible_none(self):
        rl = self._make(min_score=70)
        assert rl.is_score_eligible(None) is False

    def test_seconds_until_slot_is_zero_when_available(self):
        rl = self._make(max_per_hour=10)
        with patch.object(rl, "_applied_in_last_hour", return_value=5):
            assert rl.seconds_until_slot() == 0

    def test_seconds_until_slot_positive_when_at_limit(self):
        rl = self._make(max_per_hour=2)
        oldest = datetime.utcnow() - timedelta(minutes=30)
        with patch.object(rl, "_applied_in_last_hour", return_value=2):
            with patch.object(rl, "_oldest_recent_application", return_value=oldest):
                secs = rl.seconds_until_slot()
                # 30 min left until slot opens
                assert 1700 < secs < 1900

    def test_applied_this_hour_delegates(self):
        rl = self._make()
        with patch.object(rl, "_applied_in_last_hour", return_value=3):
            assert rl.applied_this_hour() == 3


# ── BaseATSHandler helpers (unit, no Playwright) ──────────────────────────────


class TestBaseATSHandlerHelpers:
    """Test the shared form helper logic without running a browser."""

    def _make_handler(self):
        profile = {
            "personal": {"email": "test@example.com", "first_name": "Test"},
            "work_authorization": {"authorized_to_work_in_us": True, "require_sponsorship": True},
            "preferences": {},
            "demographics": {},
        }
        return GreenhouseHandler(profile=profile, resume_path=Path("resumes/master_resume.docx"))

    def test_personal_data_accessible(self):
        h = self._make_handler()
        assert h._personal["email"] == "test@example.com"

    def test_work_auth_accessible(self):
        h = self._make_handler()
        assert h._work_auth["authorized_to_work_in_us"] is True
        assert h._work_auth["require_sponsorship"] is True

    def test_ats_name_set(self):
        h = self._make_handler()
        assert h.ats_name == "greenhouse"

    @pytest.mark.asyncio
    async def test_check_already_applied_detects_signal(self):
        h = self._make_handler()
        page = AsyncMock()
        page.content = AsyncMock(return_value="<html>you have already applied to this job</html>")
        result = await h._check_already_applied(page)
        assert result is True

    @pytest.mark.asyncio
    async def test_check_already_applied_false_when_clean(self):
        h = self._make_handler()
        page = AsyncMock()
        page.content = AsyncMock(return_value="<html>Apply now!</html>")
        result = await h._check_already_applied(page)
        assert result is False

    @pytest.mark.asyncio
    async def test_upload_resume_skips_missing_file(self):
        h = self._make_handler()
        h.resume_path = Path("/nonexistent/resume.docx")
        result = ApplyResult(outcome=ApplyOutcome.ERROR, job_id="j1", ats_type="greenhouse", url="https://example.com")
        page = AsyncMock()
        ok = await h._upload_resume(page, ["input[type='file']"], result)
        assert ok is False
        assert "Resume not found" in result.submission_log[0]
