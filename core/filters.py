"""
Job filtering rules.

A job must PASS all enabled filters before being saved to the DB.
Filters are configured via configs/settings.yaml and passed as a FilterConfig.

Adding a new filter:
  1. Add a method `_check_<name>(job) -> bool` to JobFilter
  2. Add the check to the `passes()` method
  3. Add the config field to FilterConfig
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from loguru import logger

from scrapers.base import RawJob


# ── Filter configuration ──────────────────────────────────────────────────────


@dataclass
class FilterConfig:
    # Location
    remote_ok: bool = True
    allowed_locations: list[str] = field(
        default_factory=lambda: ["chicago", "remote", "anywhere"]
    )

    # Seniority — roles must mention at least one of these
    target_seniority: list[str] = field(
        default_factory=lambda: ["mid", "senior", "staff", "principal", "lead"]
    )
    # If True, roles with NO seniority signal are kept (ambiguous titles)
    keep_unseniored: bool = True

    # Recency — reject jobs older than this many days (None = no limit)
    max_age_days: Optional[int] = None
    # If True, keep jobs with no posted_at date (unknown age)
    keep_undated: bool = True

    # H1B — if True, only keep roles that explicitly mention sponsorship
    require_h1b: bool = False
    # If True, only reject roles that explicitly say "no sponsorship"
    reject_no_sponsorship: bool = True

    # Minimum title match — role title must contain at least one target keyword
    target_role_keywords: list[str] = field(
        default_factory=lambda: [
            "software engineer", "software developer", "swe", "sde",
            "backend engineer", "backend developer",
            "full stack", "fullstack",
            "platform engineer", "infrastructure engineer",
            "site reliability", "sre",
        ]
    )


# ── Compiled pattern helpers ──────────────────────────────────────────────────


def _compile(keywords: list[str], flags: int = re.IGNORECASE) -> re.Pattern:
    escaped = [re.escape(k) for k in keywords]
    return re.compile(r"\b(?:" + "|".join(escaped) + r")\b", flags)


# Patterns for remote / location detection
_REMOTE_PAT = re.compile(
    r"\b(remote|wfh|work from home|fully.remote|distributed|anywhere)\b",
    re.IGNORECASE,
)

# Chicago-area location signals
_CHICAGO_PAT = re.compile(
    r"\b(chicago|chicagoland|il\b|illinois|oak.?park|evanston|naperville|"
    r"schaumburg|aurora|joliet|rockford)\b",
    re.IGNORECASE,
)

# No-sponsorship rejection signals
_NO_SPONSORSHIP_PAT = re.compile(
    r"\b(no\s+(?:h.?1.?b|visa)\s+sponsor|"
    r"not\s+(?:able|eligible)\s+to\s+sponsor|"
    r"cannot\s+sponsor|"
    r"sponsorship\s+not\s+(?:available|provided|offered))\b",
    re.IGNORECASE,
)

# H1B sponsorship acceptance signals
_H1B_PAT = re.compile(
    r"\b(h.?1.?b|visa\s+sponsor(?:ship)?|we\s+(?:do\s+)?sponsor|"
    r"open\s+to\s+sponsorship|sponsorship\s+available)\b",
    re.IGNORECASE,
)

# Seniority patterns
_SENIORITY_SIGNALS: dict[str, re.Pattern] = {
    "junior": re.compile(r"\b(junior|jr\.?|entry.level|new.?grad|associate)\b", re.IGNORECASE),
    "mid": re.compile(r"\b(mid.level|mid\s+level|midlevel|software\s+engineer\s+ii|swe\s+ii)\b", re.IGNORECASE),
    "senior": re.compile(r"\b(senior|sr\.?)\b", re.IGNORECASE),
    "staff": re.compile(r"\b(staff|principal)\b", re.IGNORECASE),
    "lead": re.compile(r"\b(tech\s+lead|lead\s+engineer|engineering\s+lead)\b", re.IGNORECASE),
    "manager": re.compile(r"\b(engineering\s+manager|em\b|manager)\b", re.IGNORECASE),
}

# Exclusion: intern / co-op / contract (skip by default)
_EXCLUDE_PAT = re.compile(
    r"\b(intern(?:ship)?|co.?op|contractor|contract.to.hire|freelance)\b",
    re.IGNORECASE,
)


# ── JobFilter class ───────────────────────────────────────────────────────────


class JobFilter:
    """
    Applies a chain of boolean filters to a RawJob.
    Returns True (pass) or False (reject) with a logged reason.
    """

    def __init__(self, config: FilterConfig):
        self.config = config
        self._role_pat = _compile(config.target_role_keywords)

    def passes(self, job: RawJob) -> tuple[bool, str]:
        """
        Run all filters. Returns (True, "ok") or (False, reason).

        The `reason` string is used for logging / DB annotation.
        """
        checks = [
            self._check_recency,
            self._check_excluded,
            self._check_role_keyword,
            self._check_location,
            self._check_seniority,
            self._check_sponsorship,
        ]
        for check in checks:
            ok, reason = check(job)
            if not ok:
                logger.debug(
                    "Filter REJECT | {} | company={} title={} reason={}",
                    check.__name__, job.company, job.title, reason,
                )
                return False, reason

        return True, "ok"

    # ── Individual checks ─────────────────────────────────────────────────────

    def _check_recency(self, job: RawJob) -> tuple[bool, str]:
        """Reject jobs older than max_age_days. Keeps jobs with unknown date if keep_undated."""
        if self.config.max_age_days is None:
            return True, ""
        if job.posted_at is None:
            if self.config.keep_undated:
                return True, ""
            return False, "no_posted_date"
        posted = job.posted_at
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - posted).days
        if age_days > self.config.max_age_days:
            return False, f"too_old: {age_days}d > {self.config.max_age_days}d"
        return True, ""

    def _check_excluded(self, job: RawJob) -> tuple[bool, str]:
        """Skip internships, co-ops, and contract roles."""
        haystack = f"{job.title} {job.description[:300]}"
        if _EXCLUDE_PAT.search(haystack):
            return False, "excluded_role_type"
        return True, ""

    def _check_role_keyword(self, job: RawJob) -> tuple[bool, str]:
        """Title must contain at least one target role keyword."""
        if not self._role_pat.search(job.title):
            return False, f"title_mismatch: {job.title!r}"
        return True, ""

    def _check_location(self, job: RawJob) -> tuple[bool, str]:
        """Job must be remote OR in the Chicago metro area."""
        loc_text = f"{job.location} {job.description[:500]}".lower()

        if self.config.remote_ok and _REMOTE_PAT.search(loc_text):
            return True, ""

        if _CHICAGO_PAT.search(loc_text):
            return True, ""

        # Check configured allowed locations
        for allowed in self.config.allowed_locations:
            if allowed.lower() in loc_text:
                return True, ""

        return False, f"location_mismatch: {job.location!r}"

    def _check_seniority(self, job: RawJob) -> tuple[bool, str]:
        """
        Reject if the role is explicitly junior-only or a manager role.
        If no seniority signal is found, respect `keep_unseniored`.
        """
        haystack = f"{job.title} {job.description[:600]}"

        # Hard-reject pure manager / director roles
        if _SENIORITY_SIGNALS["manager"].search(job.title):
            return False, "manager_role"

        # Reject junior-only if not in targets
        is_junior = bool(_SENIORITY_SIGNALS["junior"].search(haystack))
        if is_junior and "junior" not in self.config.target_seniority:
            return False, "junior_role"

        # Check whether any target seniority matches
        for level in self.config.target_seniority:
            pat = _SENIORITY_SIGNALS.get(level)
            if pat and pat.search(haystack):
                return True, ""

        # No seniority signal at all
        if self.config.keep_unseniored:
            return True, ""

        return False, "seniority_not_matched"

    def _check_sponsorship(self, job: RawJob) -> tuple[bool, str]:
        """
        Optionally require H1B sponsorship OR reject explicit no-sponsorship.
        """
        desc = job.description[:1000]

        if self.config.reject_no_sponsorship and _NO_SPONSORSHIP_PAT.search(desc):
            return False, "explicit_no_sponsorship"

        if self.config.require_h1b:
            if not _H1B_PAT.search(desc):
                return False, "h1b_not_mentioned"

        return True, ""


# ── Convenience helpers ───────────────────────────────────────────────────────


def detect_remote(job: RawJob) -> bool:
    return bool(_REMOTE_PAT.search(f"{job.location} {job.description[:500]}"))


def detect_h1b(job: RawJob) -> bool:
    return bool(_H1B_PAT.search(job.description[:1000]))


def detect_seniority(job: RawJob) -> str:
    """Return best-guess seniority string for the job."""
    haystack = f"{job.title} {job.description[:600]}"
    for level in ("staff", "lead", "senior", "mid", "junior"):
        if _SENIORITY_SIGNALS[level].search(haystack):
            return level
    return "unknown"
