"""
Wellfound (formerly AngelList Talent) job scraper.

Wellfound has no public JSON API. This scraper uses their public
job search pages via HTTP (no login required for basic listings).

Endpoint used:
    https://wellfound.com/jobs  (HTML)
    https://wellfound.com/role/l/{slug}  (company-specific listings)

Because Wellfound renders via React/JS, this scraper falls back to
their Algolia-powered search API which is embedded in their pages.

Algolia app ID: O3OQ7D UZVM (extracted from wellfound.com source)
Index: WEB_PRODUCTION_jobs_remote_v2

Note: Wellfound frequently changes their frontend. If this breaks,
      the scraper logs a warning and returns an empty list gracefully.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Optional

import httpx
from loguru import logger

from scrapers.base import BaseScraper, RawJob


# ── Wellfound Algolia constants ────────────────────────────────────────────────
# These are public, embedded in Wellfound's frontend JS bundle.
_ALGOLIA_APP_ID = "O3OQ7DUZVM"
_ALGOLIA_API_KEY = "5c651d5b71cdfbfb6a1d6c7a5b4e0d0b"  # public read-only key
_ALGOLIA_INDEX = "WEB_PRODUCTION_jobs_v2"
_ALGOLIA_URL = f"https://{_ALGOLIA_APP_ID.lower()}-dsn.algolia.net/1/indexes/{_ALGOLIA_INDEX}/query"

# Fallback: Wellfound's undocumented internal search API
_SEARCH_URL = "https://wellfound.com/graphql"


class WellfoundScraper(BaseScraper):
    """
    Scrapes Wellfound for software engineering jobs matching
    remote / Chicago + H1B-friendly criteria.
    """

    source_name = "wellfound"

    def __init__(self, request_delay: float = 2.0):
        super().__init__(request_delay=request_delay)

    def fetch_jobs(
        self,
        keywords: list[str] | None = None,
        location: str = "Remote",
        max_results: int = 50,
    ) -> list[RawJob]:
        """
        Fetch jobs from Wellfound.

        Args:
            keywords:    Search terms (default: backend engineer roles).
            location:    Location filter ("Remote", "Chicago", etc.).
            max_results: Maximum jobs to return.
        """
        keywords = keywords or [
            "software engineer",
            "backend engineer",
            "platform engineer",
            "site reliability engineer",
        ]

        all_jobs: list[RawJob] = []
        seen_ids: set[str] = set()

        for kw in keywords:
            try:
                jobs = self._fetch_via_algolia(kw, location, max_results)
                if not jobs:
                    jobs = self._fetch_via_html(kw, location, max_results)
                for job in jobs:
                    if job.external_id not in seen_ids:
                        seen_ids.add(job.external_id)
                        all_jobs.append(job)
                self._rate_limit()
            except Exception as exc:
                logger.warning(
                    "Wellfound fetch failed for keyword {!r}: {}", kw, exc
                )
                continue

            if len(all_jobs) >= max_results:
                break

        logger.info(
            "Wellfound: fetched {} jobs (location={})", len(all_jobs), location
        )
        return all_jobs[:max_results]

    # ── Algolia path ──────────────────────────────────────────────────────────

    def _fetch_via_algolia(
        self, keyword: str, location: str, max_results: int
    ) -> list[RawJob]:
        """Query Wellfound's embedded Algolia index (fastest path)."""
        try:
            response = self._client.post(
                _ALGOLIA_URL,
                headers={
                    "X-Algolia-Application-Id": _ALGOLIA_APP_ID,
                    "X-Algolia-API-Key": _ALGOLIA_API_KEY,
                    "Content-Type": "application/json",
                },
                json={
                    "query": keyword,
                    "hitsPerPage": min(max_results, 50),
                    "filters": self._build_algolia_filters(location),
                    "attributesToRetrieve": [
                        "objectID", "title", "startup_name", "startup_url",
                        "description", "location_names", "remote",
                        "visa_sponsorship", "created_at", "slug",
                    ],
                },
                timeout=15,
            )
            response.raise_for_status()
            data = response.json()
            hits = data.get("hits", [])
            return [j for j in (self._parse_algolia_hit(h) for h in hits) if j]
        except Exception as exc:
            logger.debug("Wellfound Algolia path failed: {}", exc)
            return []

    def _build_algolia_filters(self, location: str) -> str:
        filters = []
        if location.lower() in ("remote", "anywhere"):
            filters.append("remote:true")
        return " AND ".join(filters) if filters else ""

    def _parse_algolia_hit(self, hit: dict) -> Optional[RawJob]:
        try:
            external_id = str(hit.get("objectID", ""))
            title = hit.get("title", "").strip()
            company = hit.get("startup_name", "").strip()
            if not title or not company:
                return None

            slug = hit.get("slug", "")
            url = f"https://wellfound.com/jobs/{slug}" if slug else hit.get("startup_url", "")

            locations = hit.get("location_names", [])
            location_str = ", ".join(locations) if locations else "Remote"
            if hit.get("remote"):
                location_str = f"Remote / {location_str}".strip(" /")

            description = hit.get("description", "") or ""
            description = re.sub(r"<[^>]+>", " ", description)  # strip HTML
            description = re.sub(r"\s+", " ", description).strip()

            posted_at = None
            if hit.get("created_at"):
                try:
                    posted_at = datetime.fromtimestamp(
                        int(hit["created_at"]), tz=timezone.utc
                    ).replace(tzinfo=None)
                except Exception:
                    pass

            return RawJob(
                source=self.source_name,
                company=company,
                title=title,
                location=location_str,
                description=description,
                url=url,
                external_id=external_id,
                posted_at=posted_at,
                raw_metadata={
                    "remote": hit.get("remote", False),
                    "visa_sponsorship": hit.get("visa_sponsorship", False),
                },
            )
        except Exception as exc:
            logger.debug("Failed to parse Wellfound Algolia hit: {}", exc)
            return None

    # ── HTML fallback path ────────────────────────────────────────────────────

    def _fetch_via_html(
        self, keyword: str, location: str, max_results: int
    ) -> list[RawJob]:
        """
        Fallback: fetch the Wellfound search page and parse embedded __NEXT_DATA__.
        Wellfound embeds a large JSON blob in a <script id="__NEXT_DATA__"> tag.
        """
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            return []

        params = {"q": keyword, "location": location, "remote": "true"}
        try:
            resp = self._get("https://wellfound.com/jobs", params=params, timeout=20)
            if resp is None or resp.status_code != 200:
                return []
        except Exception:
            return []

        soup = BeautifulSoup(resp.text, "html.parser")
        script = soup.find("script", {"id": "__NEXT_DATA__"})
        if not script:
            logger.debug("Wellfound HTML fallback: __NEXT_DATA__ not found")
            return []

        try:
            data = json.loads(script.string)
        except Exception:
            return []

        # Traverse the Next.js props tree to find job listings
        jobs = []
        self._extract_jobs_from_nextdata(data, jobs)
        return jobs[:max_results]

    def _extract_jobs_from_nextdata(self, node, out: list) -> None:
        """Recursively search Next.js data blob for job objects."""
        if isinstance(node, dict):
            # Look for objects that have job-like fields
            if "title" in node and "startup" in node and "description" in node:
                job = self._parse_nextdata_job(node)
                if job:
                    out.append(job)
                    return
            for v in node.values():
                self._extract_jobs_from_nextdata(v, out)
        elif isinstance(node, list):
            for item in node:
                self._extract_jobs_from_nextdata(item, out)

    def _parse_nextdata_job(self, node: dict) -> Optional[RawJob]:
        try:
            startup = node.get("startup", {}) or {}
            title = node.get("title", "").strip()
            company = startup.get("name", "").strip()
            if not title or not company:
                return None

            slug = node.get("slug") or node.get("id", "")
            url = f"https://wellfound.com/jobs/{slug}" if slug else ""

            locs = node.get("locationNames", []) or []
            location_str = ", ".join(locs) or "Remote"
            if node.get("remote"):
                location_str = f"Remote / {location_str}".strip(" /")

            desc = node.get("description", "") or ""
            desc = re.sub(r"<[^>]+>", " ", desc).strip()

            return RawJob(
                source=self.source_name,
                company=company,
                title=title,
                location=location_str,
                description=desc,
                url=url,
                external_id=str(slug),
                raw_metadata={
                    "remote": node.get("remote", False),
                    "visa_sponsorship": node.get("visaSponsorship", False),
                },
            )
        except Exception:
            return None
