"""
Lever scraper — uses Lever's 100% public JSON API.
No authentication required. Very reliable.

API docs: https://hire.lever.co/developer/postings
GET https://api.lever.co/v0/postings/{company}?mode=json
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

from bs4 import BeautifulSoup
from loguru import logger

from scrapers.base import BaseScraper, RawJob


class LeverScraper(BaseScraper):
    """
    Scrapes job listings for a list of company slugs via the Lever Postings API.

    Company slug is the identifier used in Lever URLs, e.g.:
        https://jobs.lever.co/netflix  →  slug = "netflix"
    """

    source_name = "lever"
    BASE_URL = "https://api.lever.co/v0/postings/{slug}"

    def fetch_jobs(self, company_slugs: list[str], **kwargs) -> list[RawJob]:
        """
        Fetch all open job postings for the given company slugs.

        Args:
            company_slugs: List of Lever company slugs, e.g. ["netflix", "figma"]
        """
        jobs: list[RawJob] = []
        for slug in company_slugs:
            fetched = self._fetch_company(slug)
            logger.info("Lever | slug={} jobs_found={}", slug, len(fetched))
            jobs.extend(fetched)
        return jobs

    def _fetch_company(self, slug: str) -> list[RawJob]:
        url = self.BASE_URL.format(slug=slug)
        resp = self._safe_fetch(url, params={"mode": "json", "limit": 500})
        if resp is None:
            return []

        try:
            items = resp.json()
        except Exception as exc:
            logger.warning("Lever JSON parse failed | slug={} error={}", slug, exc)
            return []

        if not isinstance(items, list):
            logger.warning("Lever unexpected response shape | slug={}", slug)
            return []

        results: list[RawJob] = []
        for item in items:
            try:
                job = self._parse_job(item, slug)
                if job:
                    results.append(job)
            except Exception as exc:
                logger.warning(
                    "Lever parse error | slug={} job_id={} error={}",
                    slug, item.get("id"), exc,
                )
        return results

    def _parse_job(self, item: dict, slug: str) -> Optional[RawJob]:
        job_id = item.get("id", "")
        title = (item.get("text") or "").strip()
        if not title:
            return None

        company = slug.replace("-", " ").title()
        location = self._extract_location(item)
        description = self._extract_description(item)
        url = item.get("hostedUrl") or f"https://jobs.lever.co/{slug}/{job_id}"
        posted_at = self._parse_timestamp(item.get("createdAt"))

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
                "team": item.get("categories", {}).get("team"),
                "commitment": item.get("categories", {}).get("commitment"),
                "department": item.get("categories", {}).get("department"),
                "tags": item.get("tags", []),
            },
        )

    # ── Parsing helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _extract_location(item: dict) -> str:
        categories = item.get("categories") or {}
        location = categories.get("location") or ""
        # Also check workplaceType for remote info
        workplace = item.get("workplaceType") or ""
        if workplace.lower() == "remote" and "remote" not in location.lower():
            location = f"{location} (Remote)".strip(" ()")
            location = f"Remote - {location}" if location else "Remote"
        return location or "Unknown"

    @staticmethod
    def _extract_description(item: dict) -> str:
        """Combine Lever description sections into plain text."""
        parts: list[str] = []

        # Lever structures description into sections
        description_html = item.get("descriptionPlain") or item.get("description") or ""
        if description_html:
            if "<" in description_html:
                soup = BeautifulSoup(description_html, "html.parser")
                parts.append(soup.get_text(separator="\n"))
            else:
                parts.append(description_html)

        # Additional sections (lists, requirements, etc.)
        for section in item.get("lists", []):
            heading = section.get("text", "")
            content_html = section.get("content", "")
            if heading:
                parts.append(f"\n{heading}")
            if content_html:
                soup = BeautifulSoup(content_html, "html.parser")
                parts.append(soup.get_text(separator="\n"))

        additional = item.get("additional") or ""
        if additional:
            if "<" in additional:
                soup = BeautifulSoup(additional, "html.parser")
                parts.append(soup.get_text(separator="\n"))
            else:
                parts.append(additional)

        text = "\n\n".join(p.strip() for p in parts if p.strip())
        return re.sub(r"\n{3,}", "\n\n", text).strip()

    @staticmethod
    def _parse_timestamp(ms_timestamp: Optional[int]) -> Optional[datetime]:
        """Lever uses millisecond UNIX timestamps."""
        if not ms_timestamp:
            return None
        try:
            return datetime.fromtimestamp(ms_timestamp / 1000, tz=timezone.utc)
        except Exception:
            return None
