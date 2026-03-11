"""
SQLite-backed rate limiter for application submissions.

Reads `max_per_hour` from configs/settings.yaml and enforces it by
counting Application rows created within the last rolling 60 minutes.

No external dependencies (Redis / APScheduler not required here).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import yaml
from loguru import logger
from sqlmodel import Session, select

from storage.database import get_engine
from storage.models import Application


# ── Loader ────────────────────────────────────────────────────────────────────


def _load_max_per_hour(settings_path: str = "configs/settings.yaml") -> int:
    try:
        with open(settings_path) as f:
            cfg = yaml.safe_load(f) or {}
        return int(cfg.get("application", {}).get("max_per_hour", 10))
    except Exception as exc:
        logger.warning("Could not read max_per_hour from settings: {} — using 10", exc)
        return 10


def _load_min_score(settings_path: str = "configs/settings.yaml") -> int:
    try:
        with open(settings_path) as f:
            cfg = yaml.safe_load(f) or {}
        return int(cfg.get("application", {}).get("min_score_to_apply", 70))
    except Exception as exc:
        logger.warning("Could not read min_score_to_apply from settings: {} — using 70", exc)
        return 70


# ── RateLimiter ───────────────────────────────────────────────────────────────


class RateLimiter:
    """
    Enforces a per-hour application cap using the applications table as the
    authoritative record (so the limit survives process restarts).

    Usage:
        rl = RateLimiter()
        if rl.can_apply():
            # proceed
        else:
            wait_secs = rl.seconds_until_slot()
    """

    def __init__(self, settings_path: str = "configs/settings.yaml"):
        self.max_per_hour: int = _load_max_per_hour(settings_path)
        self.min_score: int = _load_min_score(settings_path)

    # ── Public API ────────────────────────────────────────────────────────────

    def can_apply(self) -> bool:
        """Return True if we are below the hourly cap."""
        count = self._applied_in_last_hour()
        allowed = count < self.max_per_hour
        if not allowed:
            logger.info(
                "Rate limit reached: {} applications in last hour (max={})",
                count, self.max_per_hour,
            )
        return allowed

    def is_score_eligible(self, score: int | None) -> bool:
        """Return True if the job's match score meets the minimum threshold."""
        if score is None:
            return False
        return score >= self.min_score

    def seconds_until_slot(self) -> int:
        """
        Return how many seconds until the oldest recent application
        is older than 1 hour (freeing up a slot). Returns 0 if a slot
        is already available.
        """
        if self.can_apply():
            return 0
        oldest = self._oldest_recent_application()
        if oldest is None:
            return 0
        opens_at = oldest + timedelta(hours=1)
        remaining = (opens_at - datetime.utcnow()).total_seconds()
        return max(0, int(remaining))

    def applied_this_hour(self) -> int:
        """Return how many applications have been submitted in the last 60 min."""
        return self._applied_in_last_hour()

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _applied_in_last_hour(self) -> int:
        cutoff = datetime.utcnow() - timedelta(hours=1)
        engine = get_engine()
        with Session(engine, expire_on_commit=False) as session:
            apps = list(session.exec(
                select(Application).where(
                    Application.applied_at >= cutoff
                )
            ).all())
        return len(apps)

    def _oldest_recent_application(self) -> datetime | None:
        """Return the applied_at of the oldest application within the last hour."""
        cutoff = datetime.utcnow() - timedelta(hours=1)
        engine = get_engine()
        with Session(engine, expire_on_commit=False) as session:
            apps = list(session.exec(
                select(Application).where(
                    Application.applied_at >= cutoff
                ).order_by(Application.applied_at)  # type: ignore[attr-defined]
            ).all())
        if not apps:
            return None
        return apps[0].applied_at
