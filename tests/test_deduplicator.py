"""Unit tests for the deduplicator — no DB or LLM required."""

import pytest

from core.deduplicator import cosine_similarity, make_fingerprint, normalize_text
from scrapers.base import RawJob


# ── normalize_text ────────────────────────────────────────────────────────────


def test_normalize_lowercases():
    assert normalize_text("Stripe") == "stripe"


def test_normalize_removes_punctuation():
    assert normalize_text("Sr. Engineer!") == "senior_engineer"


def test_normalize_collapses_spaces():
    assert normalize_text("  backend   engineer  ") == "backend_engineer"


def test_alias_sr():
    result = normalize_text("Sr Software Engineer")
    assert "senior" in result


# ── make_fingerprint ──────────────────────────────────────────────────────────


def test_fingerprint_is_stable():
    fp1 = make_fingerprint("Stripe", "Backend Engineer", "Remote", "We are hiring...")
    fp2 = make_fingerprint("Stripe", "Backend Engineer", "Remote", "We are hiring...")
    assert fp1 == fp2


def test_fingerprint_differs_by_company():
    fp1 = make_fingerprint("Stripe", "Backend Engineer", "Remote", "desc")
    fp2 = make_fingerprint("Notion", "Backend Engineer", "Remote", "desc")
    assert fp1 != fp2


def test_fingerprint_differs_by_description():
    fp1 = make_fingerprint("Stripe", "Backend Engineer", "Remote", "Python Kafka AWS")
    fp2 = make_fingerprint("Stripe", "Backend Engineer", "Remote", "Go gRPC Kubernetes")
    assert fp1 != fp2


def test_fingerprint_format():
    fp = make_fingerprint("Stripe", "Senior SWE", "Remote", "description here")
    assert "_" in fp
    # Should not have spaces
    assert " " not in fp


# ── cosine_similarity ─────────────────────────────────────────────────────────


def test_cosine_identical_vectors():
    v = [1.0, 0.5, 0.3]
    assert abs(cosine_similarity(v, v) - 1.0) < 1e-6


def test_cosine_orthogonal_vectors():
    a = [1.0, 0.0]
    b = [0.0, 1.0]
    assert abs(cosine_similarity(a, b)) < 1e-6


def test_cosine_zero_vector():
    assert cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0


def test_cosine_mismatched_length():
    assert cosine_similarity([1.0, 2.0], [1.0]) == 0.0


# ── RawJob normalisation ──────────────────────────────────────────────────────


def test_rawjob_strips_whitespace():
    job = RawJob(
        source="test",
        company="  Stripe  ",
        title="  Backend Engineer  ",
        location="  Remote  ",
        description="Some description\n\n\n\nwith blank lines",
        url="https://example.com",
    )
    assert job.company == "Stripe"
    assert job.title == "Backend Engineer"
    # Multiple blank lines collapsed to 2
    assert "\n\n\n" not in job.description
