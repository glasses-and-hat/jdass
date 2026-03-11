"""
Tests for Phase 5 outreach components.

Covers:
  - RecruiterFinder: confidence scoring, name extraction, URL parsing
  - MessageGenerator: template message, LLM path (mocked)
  - RecruiterCandidate: is_recruiter heuristic

No internet requests, no LLM, no DB required.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.recruiter_finder import RecruiterCandidate, RecruiterFinder
from core.message_generator import MessageGenerator
from storage.models import Job


# ── Fixtures ───────────────────────────────────────────────────────────────────


def _make_job(**kwargs) -> Job:
    defaults = dict(
        id="job-1",
        fingerprint="fp-1",
        source="greenhouse",
        company="Stripe",
        title="Senior Backend Engineer",
        location="Remote",
        description="We use Python, Go, PostgreSQL, Kafka, and AWS.",
        url="https://boards.greenhouse.io/stripe/jobs/123",
        key_technologies='["Python", "Go", "PostgreSQL"]',
        frameworks='["FastAPI"]',
        cloud_platforms='["AWS"]',
        databases='["PostgreSQL"]',
        match_score=88,
    )
    defaults.update(kwargs)
    return Job(**defaults)


def _make_profile(**kwargs) -> dict:
    return {
        "personal": {
            "first_name": "Rahul",
            "location_city": "Chicago",
            **kwargs.get("personal", {}),
        },
        "work_authorization": {
            "require_sponsorship": True,
            **kwargs.get("work_authorization", {}),
        },
        "preferences": {
            "years_of_experience": 5,
            **kwargs.get("preferences", {}),
        },
        "demographics": {},
    }


# ── RecruiterCandidate ────────────────────────────────────────────────────────


class TestRecruiterCandidate:
    def test_is_recruiter_for_recruiter_title(self):
        c = RecruiterCandidate(
            name="Jane Smith",
            title="Technical Recruiter",
            linkedin_url="https://linkedin.com/in/janesmith",
            company="Stripe",
        )
        assert c.is_recruiter() is True

    def test_is_recruiter_for_talent_title(self):
        c = RecruiterCandidate(
            name="Bob Lee",
            title="Talent Acquisition Manager",
            linkedin_url="https://linkedin.com/in/boblee",
            company="Stripe",
        )
        assert c.is_recruiter() is True

    def test_is_recruiter_false_for_engineer(self):
        c = RecruiterCandidate(
            name="Alice Wu",
            title="Senior Software Engineer",
            linkedin_url="https://linkedin.com/in/alicewu",
            company="Stripe",
        )
        assert c.is_recruiter() is False

    def test_is_recruiter_for_hr(self):
        c = RecruiterCandidate(
            name="Carol Kim",
            title="HR Business Partner",
            linkedin_url="https://linkedin.com/in/carolkim",
            company="Stripe",
        )
        assert c.is_recruiter() is True

    def test_is_recruiter_for_sourcer(self):
        c = RecruiterCandidate(
            name="Dan Park",
            title="Sourcer, Engineering",
            linkedin_url="https://linkedin.com/in/danpark",
            company="Stripe",
        )
        assert c.is_recruiter() is True

    def test_confidence_defaults_to_zero(self):
        c = RecruiterCandidate(
            name="Test", title="Unknown", linkedin_url="https://linkedin.com/in/x", company="X"
        )
        assert c.confidence == 0.0


# ── RecruiterFinder._score_confidence ─────────────────────────────────────────


class TestRecruiterFinderScoring:
    def test_high_confidence_for_recruiter_at_company(self):
        score = RecruiterFinder._score_confidence(
            "Technical Recruiter", "Stripe",
            "Jane Smith works as a Technical Recruiter at Stripe, hiring engineers"
        )
        assert score >= 0.7

    def test_low_confidence_for_engineer(self):
        score = RecruiterFinder._score_confidence(
            "Senior Software Engineer", "Stripe",
            "Alice Wu is a Senior Software Engineer at Stripe building payment systems"
        )
        assert score < 0.4

    def test_confidence_bounded_0_to_1(self):
        score = RecruiterFinder._score_confidence(
            "Recruiter Talent Acquisition Staffing", "Company",
            "recruiter talent staffing hiring company"
        )
        assert 0.0 <= score <= 1.0

    def test_company_mention_boosts_score(self):
        score_with = RecruiterFinder._score_confidence(
            "Recruiter", "Stripe", "recruiter at Stripe"
        )
        score_without = RecruiterFinder._score_confidence(
            "Recruiter", "Stripe", "recruiter at some company"
        )
        assert score_with > score_without


# ── RecruiterFinder._extract_name ────────────────────────────────────────────


class TestRecruiterFinderExtractName:
    def test_extracts_full_name(self):
        finder = RecruiterFinder.__new__(RecruiterFinder)
        name = finder._extract_name("Jane Smith - Recruiter at Stripe | LinkedIn")
        assert "Jane Smith" in name

    def test_handles_empty_title(self):
        finder = RecruiterFinder.__new__(RecruiterFinder)
        name = finder._extract_name("")
        assert isinstance(name, str)

    def test_strips_linkedin_suffix(self):
        finder = RecruiterFinder.__new__(RecruiterFinder)
        name = finder._extract_name("Alice Wu | LinkedIn")
        assert "LinkedIn" not in name


# ── RecruiterFinder._extract_real_url ────────────────────────────────────────


class TestExtractRealUrl:
    def test_passthrough_for_normal_url(self):
        url = "https://www.linkedin.com/in/janesmith"
        assert RecruiterFinder._extract_real_url(url) == url

    def test_unwraps_ddg_redirect(self):
        wrapped = "https://duckduckgo.com/?uddg=https%3A%2F%2Fwww.linkedin.com%2Fin%2Fjanesmith"
        result = RecruiterFinder._extract_real_url(wrapped)
        assert "linkedin.com/in/janesmith" in result

    def test_returns_href_as_is_without_uddg(self):
        href = "https://example.com/page"
        assert RecruiterFinder._extract_real_url(href) == href


# ── RecruiterFinder._build_queries ────────────────────────────────────────────


class TestBuildQueries:
    def test_returns_multiple_queries(self):
        finder = RecruiterFinder.__new__(RecruiterFinder)
        queries = finder._build_queries("Stripe", "backend engineer")
        assert len(queries) >= 2

    def test_queries_contain_company(self):
        finder = RecruiterFinder.__new__(RecruiterFinder)
        queries = finder._build_queries("Stripe", "backend engineer")
        assert all("Stripe" in q for q in queries)

    def test_queries_target_linkedin(self):
        finder = RecruiterFinder.__new__(RecruiterFinder)
        queries = finder._build_queries("Figma", "sre")
        assert all("linkedin.com" in q for q in queries)


# ── MessageGenerator._template_message ────────────────────────────────────────


class TestTemplateMessage:
    def _make_gen(self, **profile_kwargs) -> MessageGenerator:
        profile = _make_profile(**profile_kwargs)
        return MessageGenerator(profile=profile, use_llm=False)

    def test_template_includes_company(self):
        gen = self._make_gen()
        job = _make_job()
        candidate = RecruiterCandidate(
            name="Jane Smith", title="Recruiter",
            linkedin_url="https://linkedin.com/in/janesmith", company="Stripe"
        )
        msg = gen._template_message(job, candidate)
        assert "Stripe" in msg

    def test_template_includes_role(self):
        gen = self._make_gen()
        job = _make_job()
        candidate = RecruiterCandidate(
            name="Bob Lee", title="Recruiter",
            linkedin_url="https://linkedin.com/in/boblee", company="Stripe"
        )
        msg = gen._template_message(job, candidate)
        assert "Senior Backend Engineer" in msg or "engineer" in msg.lower()

    def test_template_uses_first_name(self):
        gen = self._make_gen()
        job = _make_job()
        candidate = RecruiterCandidate(
            name="Jane Smith", title="Recruiter",
            linkedin_url="https://linkedin.com/in/janesmith", company="Stripe"
        )
        msg = gen._template_message(job, candidate)
        assert "Jane" in msg

    def test_template_within_300_chars(self):
        gen = self._make_gen()
        job = _make_job()
        candidate = RecruiterCandidate(
            name="A" * 30, title="Recruiter",
            linkedin_url="https://linkedin.com/in/a", company="Stripe"
        )
        msg = gen._template_message(job, candidate)
        assert len(msg) <= 300

    def test_template_includes_applicant_first_name(self):
        gen = self._make_gen()
        job = _make_job()
        candidate = RecruiterCandidate(
            name="Bob Lee", title="Recruiter",
            linkedin_url="https://linkedin.com/in/boblee", company="Stripe"
        )
        msg = gen._template_message(job, candidate)
        assert "Rahul" in msg


# ── MessageGenerator.generate_for_job ────────────────────────────────────────


class TestGenerateForJob:
    def test_empty_candidates_returns_empty(self):
        profile = _make_profile()
        gen = MessageGenerator(profile=profile, use_llm=False)
        job = _make_job()
        items = gen.generate_for_job(job, candidates=[])
        assert items == []

    def test_returns_one_item_per_candidate(self):
        profile = _make_profile()
        gen = MessageGenerator(profile=profile, use_llm=False)
        job = _make_job()
        candidates = [
            RecruiterCandidate("Alice", "Recruiter", "https://linkedin.com/in/alice", "Stripe"),
            RecruiterCandidate("Bob", "Talent", "https://linkedin.com/in/bob", "Stripe"),
        ]
        items = gen.generate_for_job(job, candidates=candidates)
        assert len(items) == 2

    def test_items_have_correct_job_id(self):
        profile = _make_profile()
        gen = MessageGenerator(profile=profile, use_llm=False)
        job = _make_job(id="job-xyz")
        candidates = [
            RecruiterCandidate("Alice", "Recruiter", "https://linkedin.com/in/alice", "Stripe"),
        ]
        items = gen.generate_for_job(job, candidates=candidates)
        assert items[0].job_id == "job-xyz"

    def test_items_have_recruiter_info(self):
        profile = _make_profile()
        gen = MessageGenerator(profile=profile, use_llm=False)
        job = _make_job()
        candidates = [
            RecruiterCandidate(
                name="Alice Wu",
                title="Senior Recruiter",
                linkedin_url="https://linkedin.com/in/alicewu",
                company="Stripe",
            ),
        ]
        items = gen.generate_for_job(job, candidates=candidates)
        assert items[0].recruiter_name == "Alice Wu"
        assert items[0].recruiter_title == "Senior Recruiter"
        assert items[0].recruiter_url == "https://linkedin.com/in/alicewu"

    def test_llm_fallback_to_template_on_failure(self):
        profile = _make_profile()
        gen = MessageGenerator(profile=profile, use_llm=True)

        # Mock the LLM to fail
        mock_llm = MagicMock()
        mock_llm.generate.side_effect = Exception("Ollama not running")
        gen._llm = mock_llm

        job = _make_job()
        candidates = [
            RecruiterCandidate("Alice", "Recruiter", "https://linkedin.com/in/alice", "Stripe"),
        ]
        items = gen.generate_for_job(job, candidates=candidates)
        # Should still produce a message via template fallback
        assert len(items) == 1
        assert len(items[0].message_text) > 10


# ── Notifier (unit) ───────────────────────────────────────────────────────────


class TestNotifier:
    def _make(self, enabled: bool = True, threshold: int = 80):
        from core.notifier import Notifier
        n = Notifier.__new__(Notifier)
        n.enabled = enabled
        n.score_threshold = threshold
        n._is_mac = True
        return n

    def test_job_found_below_threshold_does_not_notify(self):
        n = self._make(threshold=80)
        with patch.object(n, "_send") as mock_send:
            n.job_found("Stripe", "Engineer", score=75)
            mock_send.assert_not_called()

    def test_job_found_at_threshold_notifies(self):
        n = self._make(threshold=80)
        with patch.object(n, "_send") as mock_send:
            n.job_found("Stripe", "Engineer", score=80)
            mock_send.assert_called_once()

    def test_job_found_above_threshold_notifies(self):
        n = self._make(threshold=80)
        with patch.object(n, "_send") as mock_send:
            n.job_found("Stripe", "Engineer", score=95)
            mock_send.assert_called_once()

    def test_disabled_notifier_does_not_send(self):
        n = self._make(enabled=False)
        with patch("subprocess.run") as mock_run:
            n._send("title", "message")
            mock_run.assert_not_called()

    def test_outreach_ready_zero_does_not_notify(self):
        n = self._make()
        with patch.object(n, "_send") as mock_send:
            n.outreach_ready(0)
            mock_send.assert_not_called()

    def test_outreach_ready_nonzero_notifies(self):
        n = self._make()
        with patch.object(n, "_send") as mock_send:
            n.outreach_ready(3)
            mock_send.assert_called_once()

    def test_discovery_complete_zero_jobs_no_notify(self):
        n = self._make()
        with patch.object(n, "_send") as mock_send:
            n.discovery_complete(0, top_score=None)
            mock_send.assert_not_called()

    def test_discovery_complete_sends_notification(self):
        n = self._make()
        with patch.object(n, "_send") as mock_send:
            n.discovery_complete(5, top_score=91)
            mock_send.assert_called_once()

    def test_non_mac_does_not_call_osascript(self):
        n = self._make()
        n._is_mac = False
        with patch("subprocess.run") as mock_run:
            n._send("title", "message")
            mock_run.assert_not_called()
