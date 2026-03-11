"""
Job match scorer — produces a 0-100 score for how well a job matches
the candidate's profile.

Scoring is intentionally heuristic and transparent: every sub-score is
logged so you can see exactly why a job scored the way it did.

Score breakdown (weights sum to 100):
  title_match       25  — does the job title match target roles?
  tech_overlap      35  — how many of the candidate's core techs appear?
  seniority_match   15  — is the seniority level in the target range?
  location_bonus    10  — remote or Chicago?
  h1b_bonus         10  — explicit H1B sponsorship mentioned?
  recency_bonus      5  — posted within the last 30 days?

Edit CANDIDATE_PROFILE below to match your actual tech stack.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from loguru import logger

from core.jd_parser import ParsedJD


# ── Candidate profile — EDIT THIS ─────────────────────────────────────────────
# List your actual skills here. Tech overlap score is based on this.

CANDIDATE_PROFILE = {
    # Core languages — highest weight in overlap calc
    "core_languages": ["Python", "JavaScript", "TypeScript", "Go"],

    # Frameworks you've shipped production code with
    "frameworks": ["FastAPI", "React", "Django", "Node.js", "Next.js"],

    # Cloud / infra you're comfortable with
    "cloud": ["AWS", "GCP", "Docker", "Kubernetes", "Terraform"],

    # Databases
    "databases": ["PostgreSQL", "Redis", "MongoDB", "Elasticsearch"],

    # Other tech that's a plus but not core
    "bonus_tech": ["Kafka", "Spark", "GraphQL", "gRPC", "Airflow"],

    # Target role keywords (for title match)
    "target_titles": [
        "software engineer", "backend engineer", "swe", "sde",
        "platform engineer", "infrastructure engineer", "full stack",
        "fullstack", "site reliability", "sre",
    ],

    # Target seniority levels
    "target_seniority": ["mid", "senior", "staff", "principal", "lead"],
}


# ── Score breakdown dataclass ──────────────────────────────────────────────────


@dataclass
class ScoreBreakdown:
    title_match: int = 0       # 0–25
    tech_overlap: int = 0      # 0–35
    seniority_match: int = 0   # 0–15
    location_bonus: int = 0    # 0–10
    h1b_bonus: int = 0         # 0–10
    recency_bonus: int = 0     # 0–5
    matched_tech: list[str] = field(default_factory=list)
    missing_tech: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return (
            self.title_match
            + self.tech_overlap
            + self.seniority_match
            + self.location_bonus
            + self.h1b_bonus
            + self.recency_bonus
        )

    def to_json(self) -> str:
        return json.dumps({
            "total": self.total,
            "title_match": self.title_match,
            "tech_overlap": self.tech_overlap,
            "seniority_match": self.seniority_match,
            "location_bonus": self.location_bonus,
            "h1b_bonus": self.h1b_bonus,
            "recency_bonus": self.recency_bonus,
            "matched_tech": self.matched_tech,
            "missing_tech": self.missing_tech,
        })


# ── Scorer ─────────────────────────────────────────────────────────────────────


class JobScorer:
    """
    Scores a job against the candidate profile.

    Usage:
        scorer = JobScorer()
        breakdown = scorer.score(parsed_jd, title="Senior Backend Engineer",
                                 location="Remote", posted_at=datetime.now())
        print(breakdown.total)   # e.g. 82
    """

    def __init__(self, profile: dict = CANDIDATE_PROFILE):
        self.profile = profile
        # Build a flat set of all candidate tech for O(1) lookup
        self._candidate_tech: set[str] = set()
        for key in ("core_languages", "frameworks", "cloud", "databases", "bonus_tech"):
            self._candidate_tech.update(t.lower() for t in profile.get(key, []))

    def score(
        self,
        parsed: ParsedJD,
        title: str = "",
        location: str = "",
        posted_at: Optional[datetime] = None,
    ) -> ScoreBreakdown:
        bd = ScoreBreakdown()

        bd.title_match = self._score_title(title)
        bd.tech_overlap, bd.matched_tech, bd.missing_tech = self._score_tech(parsed)
        bd.seniority_match = self._score_seniority(parsed.seniority)
        bd.location_bonus = self._score_location(location, parsed.remote_eligible)
        bd.h1b_bonus = self._score_h1b(parsed.h1b_mentioned)
        bd.recency_bonus = self._score_recency(posted_at)

        logger.debug(
            "Score={} | title={} tech={} seniority={} location={} h1b={} | {}",
            bd.total, bd.title_match, bd.tech_overlap, bd.seniority_match,
            bd.location_bonus, bd.h1b_bonus, title,
        )
        return bd

    # ── Sub-scorers ───────────────────────────────────────────────────────────

    def _score_title(self, title: str) -> int:
        """25 pts: title must contain a target role keyword."""
        if not title:
            return 10  # partial credit for unknown title
        title_lower = title.lower()
        for kw in self.profile.get("target_titles", []):
            if kw in title_lower:
                return 25
        return 0

    def _score_tech(self, parsed: ParsedJD) -> tuple[int, list[str], list[str]]:
        """
        35 pts: fraction of job's tech stack that overlaps with candidate's tech.

        Core languages count double toward the score.
        """
        job_tech = [t.lower() for t in parsed.all_tech()]
        if not job_tech:
            return 15, [], []  # no tech listed — partial credit

        core_langs = {t.lower() for t in self.profile.get("core_languages", [])}

        matched = []
        score = 0
        for tech in job_tech:
            if tech in self._candidate_tech:
                matched.append(tech)
                # Core languages worth double
                score += 2 if tech in core_langs else 1

        max_possible = sum(
            2 if t.lower() in core_langs else 1
            for t in job_tech
        )
        if max_possible == 0:
            return 15, matched, []

        ratio = min(score / max_possible, 1.0)
        missing = [t for t in job_tech if t not in self._candidate_tech]

        return round(ratio * 35), matched, missing

    def _score_seniority(self, seniority: str) -> int:
        """15 pts: full if in target range, partial for unknown."""
        targets = self.profile.get("target_seniority", [])
        if seniority in targets:
            return 15
        if seniority == "unknown":
            return 8   # ambiguous — keep but partial credit
        if seniority == "junior":
            return 0
        return 5

    def _score_location(self, location: str, remote_eligible: bool) -> int:
        """10 pts: remote or Chicago area."""
        loc_lower = location.lower()
        if remote_eligible or "remote" in loc_lower or "anywhere" in loc_lower:
            return 10
        chicago_signals = ["chicago", "il,", ", il", "chicagoland", "evanston",
                           "naperville", "oak park", "schaumburg"]
        if any(s in loc_lower for s in chicago_signals):
            return 8
        return 0

    def _score_h1b(self, h1b_mentioned: bool) -> int:
        """10 pts: explicit H1B sponsorship is a strong positive signal."""
        return 10 if h1b_mentioned else 0

    def _score_recency(self, posted_at: Optional[datetime]) -> int:
        """5 pts: prefer freshly-posted jobs."""
        if not posted_at:
            return 2   # unknown — neutral
        # Normalize to UTC for comparison
        now = datetime.now(tz=timezone.utc)
        if posted_at.tzinfo is None:
            posted_at = posted_at.replace(tzinfo=timezone.utc)
        age = now - posted_at
        if age <= timedelta(days=7):
            return 5
        if age <= timedelta(days=30):
            return 3
        return 0
