"""
Base scraper interface. Every job source implements this ABC.

New scrapers only need to:
1. Subclass BaseScraper
2. Implement fetch_jobs()
3. Register in configs/sources.yaml
"""

from __future__ import annotations

import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import httpx
from loguru import logger


# ── Raw job dataclass ─────────────────────────────────────────────────────────


@dataclass
class RawJob:
    """
    Normalised job record produced by every scraper.
    All fields are plain strings / primitives — no DB types here.
    """

    source: str                       # "greenhouse" | "lever" | "hn_hiring" | ...
    company: str
    title: str
    location: str
    description: str
    url: str
    external_id: str = ""             # source-native job ID (best-effort)
    posted_at: Optional[datetime] = None
    raw_metadata: dict = field(default_factory=dict)  # source-specific extras

    def __post_init__(self):
        # Normalise whitespace in description
        self.description = re.sub(r"\n{3,}", "\n\n", self.description).strip()
        self.title = self.title.strip()
        self.company = self.company.strip()
        self.location = self.location.strip()


# ── Base scraper ──────────────────────────────────────────────────────────────


class BaseScraper(ABC):
    """
    Abstract base class for all job source scrapers.

    Subclasses must implement `fetch_jobs`. They may use `self._get` for
    rate-limited HTTP requests.
    """

    source_name: str = "unknown"     # override in subclass

    def __init__(
        self,
        request_delay: float = 2.0,
        timeout: float = 30.0,
        user_agent: str = (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    ):
        self.request_delay = request_delay
        self._last_request_at: float = 0.0
        self._http = httpx.Client(
            timeout=timeout,
            headers={"User-Agent": user_agent},
            follow_redirects=True,
        )

    # ── Abstract API ──────────────────────────────────────────────────────────

    @abstractmethod
    def fetch_jobs(self, **kwargs) -> list[RawJob]:
        """
        Fetch and return raw job listings from this source.

        Implementations should be idempotent and return an empty list
        on non-fatal errors (log and continue) rather than raising.
        """
        ...

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _get(self, url: str, **kwargs) -> httpx.Response:
        """Rate-limited GET with logging."""
        elapsed = time.time() - self._last_request_at
        if elapsed < self.request_delay:
            time.sleep(self.request_delay - elapsed)

        logger.debug("GET {} | source={}", url, self.source_name)
        resp = self._http.get(url, **kwargs)
        self._last_request_at = time.time()
        resp.raise_for_status()
        return resp

    def _safe_fetch(self, url: str, **kwargs) -> Optional[httpx.Response]:
        """Like _get but returns None instead of raising on error."""
        try:
            return self._get(url, **kwargs)
        except Exception as exc:
            logger.warning("Failed to fetch {} | {}", url, exc)
            return None

    def __del__(self):
        try:
            self._http.close()
        except Exception:
            pass
