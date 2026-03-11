"""Unit tests for JobScorer — no DB or LLM required."""

from datetime import datetime, timedelta, timezone

import pytest

from core.jd_parser import ParsedJD
from core.scorer import JobScorer, ScoreBreakdown


@pytest.fixture
def scorer():
    return JobScorer()


def make_parsed(**kwargs) -> ParsedJD:
    defaults = dict(
        key_technologies=[],
        frameworks=[],
        cloud_platforms=[],
        databases=[],
        important_skills=[],
        seniority="unknown",
        remote_eligible=False,
        h1b_mentioned=False,
    )
    defaults.update(kwargs)
    return ParsedJD(**defaults)


# ── Title match ───────────────────────────────────────────────────────────────


def test_title_match_full_credit(scorer):
    bd = scorer.score(make_parsed(), title="Senior Software Engineer", location="Remote")
    assert bd.title_match == 25


def test_title_match_sre(scorer):
    bd = scorer.score(make_parsed(), title="Site Reliability Engineer")
    assert bd.title_match == 25


def test_title_match_no_match(scorer):
    bd = scorer.score(make_parsed(), title="Product Manager")
    assert bd.title_match == 0


def test_title_match_empty(scorer):
    bd = scorer.score(make_parsed(), title="")
    assert bd.title_match == 10  # partial credit for unknown


# ── Tech overlap ──────────────────────────────────────────────────────────────


def test_tech_overlap_full_match(scorer):
    parsed = make_parsed(key_technologies=["Python", "Go"])
    bd = scorer.score(parsed)
    # Both are core languages — should score near max
    assert bd.tech_overlap > 25


def test_tech_overlap_no_match(scorer):
    parsed = make_parsed(key_technologies=["COBOL", "Fortran", "Pascal"])
    bd = scorer.score(parsed)
    assert bd.tech_overlap == 0


def test_tech_overlap_partial(scorer):
    parsed = make_parsed(
        key_technologies=["Python"],   # candidate knows this
        frameworks=["Spring Boot"],    # candidate doesn't know this
    )
    bd = scorer.score(parsed)
    assert 0 < bd.tech_overlap < 35


def test_tech_overlap_matched_tech_populated(scorer):
    parsed = make_parsed(key_technologies=["Python", "Go"])
    bd = scorer.score(parsed)
    assert len(bd.matched_tech) > 0


def test_tech_overlap_empty_jd(scorer):
    parsed = make_parsed()
    bd = scorer.score(parsed)
    assert bd.tech_overlap == 15  # partial credit for no tech listed


# ── Seniority ─────────────────────────────────────────────────────────────────


def test_seniority_senior_full(scorer):
    bd = scorer.score(make_parsed(seniority="senior"))
    assert bd.seniority_match == 15


def test_seniority_staff_full(scorer):
    bd = scorer.score(make_parsed(seniority="staff"))
    assert bd.seniority_match == 15


def test_seniority_junior_zero(scorer):
    bd = scorer.score(make_parsed(seniority="junior"))
    assert bd.seniority_match == 0


def test_seniority_unknown_partial(scorer):
    bd = scorer.score(make_parsed(seniority="unknown"))
    assert bd.seniority_match == 8


# ── Location ──────────────────────────────────────────────────────────────────


def test_location_remote_full(scorer):
    bd = scorer.score(make_parsed(remote_eligible=True), location="Remote")
    assert bd.location_bonus == 10


def test_location_chicago_bonus(scorer):
    bd = scorer.score(make_parsed(), location="Chicago, IL")
    assert bd.location_bonus == 8


def test_location_unknown_zero(scorer):
    bd = scorer.score(make_parsed(), location="Timbuktu")
    assert bd.location_bonus == 0


# ── H1B ───────────────────────────────────────────────────────────────────────


def test_h1b_bonus(scorer):
    bd = scorer.score(make_parsed(h1b_mentioned=True))
    assert bd.h1b_bonus == 10


def test_no_h1b_zero(scorer):
    bd = scorer.score(make_parsed(h1b_mentioned=False))
    assert bd.h1b_bonus == 0


# ── Recency ───────────────────────────────────────────────────────────────────


def test_recency_fresh(scorer):
    recent = datetime.now(tz=timezone.utc) - timedelta(days=3)
    bd = scorer.score(make_parsed(), posted_at=recent)
    assert bd.recency_bonus == 5


def test_recency_month_old(scorer):
    old = datetime.now(tz=timezone.utc) - timedelta(days=20)
    bd = scorer.score(make_parsed(), posted_at=old)
    assert bd.recency_bonus == 3


def test_recency_very_old(scorer):
    very_old = datetime.now(tz=timezone.utc) - timedelta(days=60)
    bd = scorer.score(make_parsed(), posted_at=very_old)
    assert bd.recency_bonus == 0


def test_recency_unknown(scorer):
    bd = scorer.score(make_parsed(), posted_at=None)
    assert bd.recency_bonus == 2


# ── Total score ───────────────────────────────────────────────────────────────


def test_total_score_is_sum(scorer):
    bd = scorer.score(
        make_parsed(seniority="senior", remote_eligible=True, h1b_mentioned=True,
                    key_technologies=["Python"]),
        title="Senior Software Engineer",
        location="Remote",
        posted_at=datetime.now(tz=timezone.utc),
    )
    assert bd.total == (
        bd.title_match + bd.tech_overlap + bd.seniority_match
        + bd.location_bonus + bd.h1b_bonus + bd.recency_bonus
    )


def test_total_score_max_100(scorer):
    """Score should never exceed 100."""
    bd = scorer.score(
        make_parsed(
            seniority="senior", remote_eligible=True, h1b_mentioned=True,
            key_technologies=["Python", "Go", "TypeScript"],
            frameworks=["FastAPI", "React"],
            cloud_platforms=["AWS", "GCP"],
            databases=["PostgreSQL", "Redis"],
        ),
        title="Senior Software Engineer",
        location="Remote",
        posted_at=datetime.now(tz=timezone.utc),
    )
    assert bd.total <= 100


def test_score_breakdown_to_json(scorer):
    import json
    bd = scorer.score(make_parsed(seniority="senior"), title="Senior SWE")
    json_str = bd.to_json()
    data = json.loads(json_str)
    assert "total" in data
    assert "matched_tech" in data
    assert data["total"] == bd.total
