"""
HackerNews "Who's Hiring" scraper.

Parses the monthly "Ask HN: Who is hiring?" thread via the HN Algolia API.
No authentication required. New threads post on the first of each month.

Strategy:
  1. Find the current month's "Who's Hiring" thread via Algolia search
  2. Fetch all top-level comments (each comment = one job posting)
  3. Parse company, role, location, and remote status from free-text comments
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

from loguru import logger

from scrapers.base import BaseScraper, RawJob

# Algolia HN API endpoints
ALGOLIA_SEARCH = "https://hn.algolia.com/api/v1/search"
ALGOLIA_ITEM = "https://hn.algolia.com/api/v1/items/{item_id}"

# Keywords that signal a job post vs. meta-comments
JOB_KEYWORDS = re.compile(
    r"\b(engineer|developer|devops|sre|data scientist|ml|backend|frontend|"
    r"fullstack|full.stack|software|platform|infrastructure|security|mobile)\b",
    re.IGNORECASE,
)

# Remote signal
REMOTE_RE = re.compile(r"\b(remote|wfh|work from home|distributed)\b", re.IGNORECASE)

# H1B signal
H1B_RE = re.compile(r"\b(h1b|h-1b|visa sponsor|sponsorship)\b", re.IGNORECASE)


class HNHiringScraper(BaseScraper):
    """
    Parses the current (or specified) month's HN "Who's Hiring" thread.
    """

    source_name = "hn_hiring"

    def fetch_jobs(self, months_back: int = 0, **kwargs) -> list[RawJob]:
        """
        Fetch jobs from HN Who's Hiring thread.

        Args:
            months_back: 0 = current month, 1 = last month, etc.
        """
        thread_id = self._find_thread_id(months_back)
        if not thread_id:
            logger.warning("HN Hiring: could not find thread for months_back={}", months_back)
            return []

        logger.info("HN Hiring | thread_id={}", thread_id)
        comments = self._fetch_comments(thread_id)
        logger.info("HN Hiring | raw_comments={}", len(comments))

        jobs = [j for c in comments for j in [self._parse_comment(c)] if j]
        logger.info("HN Hiring | parsed_jobs={}", len(jobs))
        return jobs

    # ── Thread discovery ──────────────────────────────────────────────────────

    def _find_thread_id(self, months_back: int = 0) -> Optional[str]:
        """Find the HN thread ID for the target month's hiring post."""
        from datetime import date
        from dateutil.relativedelta import relativedelta  # type: ignore

        target = date.today().replace(day=1)
        if months_back:
            target = target - relativedelta(months=months_back)

        month_name = target.strftime("%B %Y")  # e.g. "March 2026"
        query = f"Ask HN: Who is hiring? ({month_name})"

        resp = self._safe_fetch(
            ALGOLIA_SEARCH,
            params={
                "query": query,
                "tags": "ask_hn",
                "hitsPerPage": 5,
            },
        )
        if resp is None:
            return None

        try:
            hits = resp.json().get("hits", [])
        except Exception:
            return None

        for hit in hits:
            title = hit.get("title", "")
            if "who is hiring" in title.lower() and month_name.split()[0].lower() in title.lower():
                return str(hit.get("objectID"))

        # Fallback: first result
        if hits:
            return str(hits[0].get("objectID"))
        return None

    # ── Comment fetching ──────────────────────────────────────────────────────

    def _fetch_comments(self, thread_id: str) -> list[dict]:
        """Fetch all top-level comments from the thread using the Algolia items API."""
        resp = self._safe_fetch(ALGOLIA_ITEM.format(item_id=thread_id))
        if resp is None:
            return []
        try:
            data = resp.json()
        except Exception:
            return []

        # Top-level children are the job postings
        children = data.get("children", [])
        return [c for c in children if c.get("text")]

    # ── Comment parsing ───────────────────────────────────────────────────────

    def _parse_comment(self, comment: dict) -> Optional[RawJob]:
        text_html = comment.get("text") or ""
        if not text_html:
            return None

        # Strip HTML tags for plain text
        from bs4 import BeautifulSoup
        text = BeautifulSoup(text_html, "html.parser").get_text(separator="\n").strip()

        # Filter: must look like a job posting
        if not JOB_KEYWORDS.search(text):
            return None

        # HN posts follow a loose convention:
        #   "Company Name | Role | Location | Remote/Onsite | ..."
        first_line = text.split("\n")[0].strip()
        parts = [p.strip() for p in first_line.split("|")]

        company = parts[0] if parts else "Unknown"
        title = parts[1] if len(parts) > 1 else self._guess_title(text)
        location = parts[2] if len(parts) > 2 else self._guess_location(text)

        # Clean up company name — remove common noise
        company = re.sub(r"\s*\(.*?\)", "", company).strip()
        if not company or len(company) > 80:
            company = "Unknown"

        if not title:
            return None

        posted_at = self._parse_timestamp(comment.get("created_at"))
        url = f"https://news.ycombinator.com/item?id={comment.get('id', '')}"

        return RawJob(
            source=self.source_name,
            external_id=str(comment.get("id", "")),
            company=company,
            title=title,
            location=location,
            description=text,
            url=url,
            posted_at=posted_at,
            raw_metadata={
                "remote": bool(REMOTE_RE.search(text)),
                "h1b_mentioned": bool(H1B_RE.search(text)),
                "hn_comment_id": comment.get("id"),
            },
        )

    # ── Text extraction helpers ───────────────────────────────────────────────

    @staticmethod
    def _guess_title(text: str) -> str:
        """Best-effort title extraction when pipe-separated format isn't used."""
        # Look for common role patterns in the first 200 chars
        snippet = text[:200]
        patterns = [
            r"(senior|staff|principal|lead|junior|mid.level)?\s*"
            r"(software|backend|frontend|fullstack|platform|data|ml|devops|sre|mobile)\s+"
            r"engineer(?:ing)?",
            r"(senior|staff|principal|lead)?\s*developer",
        ]
        for pat in patterns:
            m = re.search(pat, snippet, re.IGNORECASE)
            if m:
                return m.group(0).strip().title()
        return "Software Engineer"

    @staticmethod
    def _guess_location(text: str) -> str:
        """Guess location from text when not pipe-separated."""
        if REMOTE_RE.search(text):
            return "Remote"
        # Look for city names (rudimentary)
        cities = re.search(
            r"\b(New York|San Francisco|Chicago|Austin|Seattle|Boston|"
            r"Los Angeles|Denver|Atlanta|NYC|SF|LA)\b",
            text, re.IGNORECASE,
        )
        if cities:
            return cities.group(0)
        return "Unknown"

    @staticmethod
    def _parse_timestamp(ts: Optional[str]) -> Optional[datetime]:
        if not ts:
            return None
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            return None
