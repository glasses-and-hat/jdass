"""
Greenhouse scraper — uses Greenhouse's 100% public JSON API.
No authentication required. Very reliable.

API docs: https://developers.greenhouse.io/job-board.html
GET https://boards-api.greenhouse.io/v1/boards/{company_slug}/jobs?content=true
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from bs4 import BeautifulSoup
from loguru import logger

from scrapers.base import BaseScraper, RawJob


class GreenhouseScraper(BaseScraper):
    """
    Scrapes job listings for a list of company slugs via the Greenhouse Job Board API.

    Company slug is the identifier used in Greenhouse URLs, e.g.:
        https://boards.greenhouse.io/stripe  →  slug = "stripe"
    """

    source_name = "greenhouse"
    BASE_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"

    def fetch_jobs(self, company_slugs: list[str], **kwargs) -> list[RawJob]:
        """
        Fetch all open jobs for the given company slugs.

        Args:
            company_slugs: List of Greenhouse board slugs, e.g. ["stripe", "notion"]
        """
        jobs: list[RawJob] = []
        for slug in company_slugs:
            fetched = self._fetch_company(slug)
            logger.info(
                "Greenhouse | slug={} jobs_found={}", slug, len(fetched)
            )
            jobs.extend(fetched)
        return jobs

    def _fetch_company(self, slug: str) -> list[RawJob]:
        url = self.BASE_URL.format(slug=slug)
        resp = self._safe_fetch(url, params={"content": "true"})
        if resp is None:
            return []

        try:
            data = resp.json()
        except Exception as exc:
            logger.warning("Greenhouse JSON parse failed | slug={} error={}", slug, exc)
            return []

        raw_jobs = data.get("jobs", [])
        company_name = data.get("company", {}).get("name") or slug.title()

        results: list[RawJob] = []
        for item in raw_jobs:
            try:
                job = self._parse_job(item, company_name, slug)
                if job:
                    results.append(job)
            except Exception as exc:
                logger.warning(
                    "Greenhouse parse error | slug={} job_id={} error={}",
                    slug, item.get("id"), exc,
                )
        return results

    def _parse_job(self, item: dict, company: str, slug: str) -> Optional[RawJob]:
        job_id = str(item.get("id", ""))
        title = item.get("title", "").strip()
        if not title:
            return None

        location = self._extract_location(item)
        description = self._extract_description(item)
        url = item.get("absolute_url") or f"https://boards.greenhouse.io/{slug}/jobs/{job_id}"
        posted_at = self._parse_date(item.get("updated_at") or item.get("first_published_at"))

        return RawJob(
            source=self.source_name,
            external_id=job_id,
            company=company,
            title=title,
            location=location,
            description=description,
            url=url,
            posted_at=posted_at,
            raw_metadata={
                "slug": slug,
                "departments": [d.get("name") for d in item.get("departments", [])],
                "offices": [o.get("name") for o in item.get("offices", [])],
            },
        )

    # ── Parsing helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _extract_location(item: dict) -> str:
        # Greenhouse can have multiple locations — join them
        locations = item.get("locations") or []
        if locations:
            return ", ".join(loc.get("name", "") for loc in locations if loc.get("name"))
        # Fall back to top-level location field
        loc = item.get("location", {})
        return loc.get("name", "Unknown") if isinstance(loc, dict) else str(loc)

    @staticmethod
    def _extract_description(item: dict) -> str:
        """Extract and clean HTML description to plain text."""
        content = item.get("content") or ""
        if not content:
            return ""
        soup = BeautifulSoup(content, "html.parser")
        text = soup.get_text(separator="\n")
        # Collapse 3+ blank lines to 2
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @staticmethod
    def _parse_date(date_str: Optional[str]) -> Optional[datetime]:
        if not date_str:
            return None
        # Greenhouse format: "2024-05-01T12:00:00-04:00"
        try:
            return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        except Exception:
            return None
