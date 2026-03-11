"""Unit tests for job filters — no DB or LLM required."""

import pytest

from core.filters import FilterConfig, JobFilter, detect_h1b, detect_remote, detect_seniority
from scrapers.base import RawJob


def make_job(**kwargs) -> RawJob:
    defaults = dict(
        source="test",
        company="TestCorp",
        title="Software Engineer",
        location="Remote",
        description="We are looking for a software engineer.",
        url="https://example.com",
    )
    defaults.update(kwargs)
    return RawJob(**defaults)


@pytest.fixture
def default_filter():
    return JobFilter(FilterConfig())


# ── Role keyword filter ───────────────────────────────────────────────────────


def test_matching_role_passes(default_filter):
    job = make_job(title="Senior Software Engineer")
    ok, _ = default_filter.passes(job)
    assert ok


def test_non_matching_role_rejected(default_filter):
    job = make_job(title="Product Manager")
    ok, reason = default_filter.passes(job)
    assert not ok
    assert "title_mismatch" in reason


def test_sre_title_passes(default_filter):
    job = make_job(title="Site Reliability Engineer")
    ok, _ = default_filter.passes(job)
    assert ok


# ── Location filter ───────────────────────────────────────────────────────────


def test_remote_location_passes(default_filter):
    job = make_job(location="Remote")
    ok, _ = default_filter.passes(job)
    assert ok


def test_chicago_location_passes(default_filter):
    job = make_job(location="Chicago, IL")
    ok, _ = default_filter.passes(job)
    assert ok


def test_new_york_only_rejected(default_filter):
    job = make_job(location="New York, NY")
    ok, reason = default_filter.passes(job)
    assert not ok
    assert "location_mismatch" in reason


def test_remote_in_description_passes(default_filter):
    job = make_job(location="New York", description="This is a fully remote position.")
    ok, _ = default_filter.passes(job)
    assert ok


# ── Sponsorship filter ────────────────────────────────────────────────────────


def test_no_sponsorship_text_rejected(default_filter):
    job = make_job(description="We cannot sponsor H1B visas at this time.")
    ok, reason = default_filter.passes(job)
    assert not ok
    assert "no_sponsorship" in reason


def test_sponsorship_available_passes(default_filter):
    job = make_job(description="We offer H1B sponsorship for qualified candidates.")
    ok, _ = default_filter.passes(job)
    assert ok


# ── Exclusion filter ──────────────────────────────────────────────────────────


def test_internship_rejected(default_filter):
    job = make_job(title="Software Engineer Internship")
    ok, _ = default_filter.passes(job)
    assert not ok


def test_contract_rejected(default_filter):
    job = make_job(title="Software Engineer (Contractor)")
    ok, _ = default_filter.passes(job)
    assert not ok


# ── Helper functions ──────────────────────────────────────────────────────────


def test_detect_remote_true():
    job = make_job(location="Remote", description="Fully remote role.")
    assert detect_remote(job) is True


def test_detect_remote_false():
    job = make_job(location="Chicago, IL", description="Onsite required.")
    assert detect_remote(job) is False


def test_detect_h1b_true():
    job = make_job(description="We do sponsor H1B visas.")
    assert detect_h1b(job) is True


def test_detect_h1b_false():
    job = make_job(description="No mention of visas.")
    assert detect_h1b(job) is False


def test_detect_seniority_senior():
    job = make_job(title="Senior Software Engineer")
    assert detect_seniority(job) == "senior"


def test_detect_seniority_staff():
    job = make_job(title="Staff Engineer")
    assert detect_seniority(job) == "staff"


def test_detect_seniority_unknown():
    job = make_job(title="Software Engineer")
    assert detect_seniority(job) == "unknown"
