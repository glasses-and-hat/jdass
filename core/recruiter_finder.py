"""
Recruiter / hiring-manager finder.

Strategy (no LinkedIn API required):
  1. Search DuckDuckGo for "site:linkedin.com/in {role} {company}" type queries.
  2. Parse LinkedIn profile URLs and names from search results.
  3. Return a ranked list of RecruiterCandidate objects for the LLM to use
     when generating outreach messages.

Caveats:
  • DuckDuckGo returns at most ~10 organic results per query.
  • LinkedIn aggressively blocks scrapers — this is best-effort.
  • Results are NOT guaranteed to be current recruiters.

For more reliable results, manually add recruiters via the dashboard's
Outreach tab after you've connected on LinkedIn.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx
from loguru import logger


# ── Data model ────────────────────────────────────────────────────────────────


@dataclass
class RecruiterCandidate:
    name: str
    title: str                       # e.g. "Technical Recruiter"
    linkedin_url: str
    company: str
    confidence: float = 0.0          # 0–1 heuristic score
    extra: dict = field(default_factory=dict)

    def is_recruiter(self) -> bool:
        """Heuristic: does the title look like a recruiter / talent role?"""
        recruiter_keywords = {
            "recruiter", "recruiting", "talent", "acquisition",
            "hr", "human resources", "staffing", "sourcer",
            "hiring manager", "engineering manager",
        }
        title_lower = self.title.lower()
        return any(kw in title_lower for kw in recruiter_keywords)


# ── Finder ─────────────────────────────────────────────────────────────────────


class RecruiterFinder:
    """
    Finds potential recruiters / hiring managers for a given company + role.

    Usage:
        finder = RecruiterFinder()
        candidates = finder.find("Stripe", "backend engineer")
        for c in candidates:
            print(c.name, c.title, c.linkedin_url)
    """

    # DuckDuckGo HTML search endpoint (no API key needed)
    _DDG_URL = "https://html.duckduckgo.com/html/"

    # Patterns to extract LinkedIn profile info from search snippets
    _LI_URL_RE = re.compile(
        r"https?://(?:www\.)?linkedin\.com/in/([\w%-]+)",
        re.IGNORECASE,
    )
    _NAME_RE = re.compile(r"^([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)")

    def __init__(self, request_delay: float = 2.0, timeout: int = 15):
        self._delay = request_delay
        self._timeout = timeout
        self._client = httpx.Client(
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "en-US,en;q=0.9",
            },
            follow_redirects=True,
            timeout=self._timeout,
        )

    # ── Public API ─────────────────────────────────────────────────────────────

    def find(
        self,
        company: str,
        role_title: str,
        max_results: int = 5,
    ) -> list[RecruiterCandidate]:
        """
        Search for recruiters or hiring managers at `company` for `role_title`.
        Returns up to `max_results` candidates, ranked by confidence.
        """
        candidates: list[RecruiterCandidate] = []

        queries = self._build_queries(company, role_title)
        seen_urls: set[str] = set()

        for query in queries:
            try:
                results = self._search_ddg(query)
                for r in results:
                    url = r.get("url", "")
                    if not url or url in seen_urls:
                        continue
                    seen_urls.add(url)
                    candidate = self._parse_result(r, company)
                    if candidate:
                        candidates.append(candidate)
                time.sleep(self._delay)
            except Exception as exc:
                logger.warning("Recruiter search failed for query {!r}: {}", query, exc)
                continue

            if len(candidates) >= max_results:
                break

        candidates.sort(key=lambda c: c.confidence, reverse=True)
        logger.info(
            "Found {} recruiter candidates | company={} role={}",
            len(candidates), company, role_title,
        )
        return candidates[:max_results]

    def close(self) -> None:
        self._client.close()

    # ── Query builder ──────────────────────────────────────────────────────────

    def _build_queries(self, company: str, role_title: str) -> list[str]:
        """Build a ranked list of search queries, most specific first."""
        return [
            f'site:linkedin.com/in recruiter "{company}"',
            f'site:linkedin.com/in "talent acquisition" "{company}"',
            f'site:linkedin.com/in "engineering manager" "{company}" "{role_title}"',
            f'site:linkedin.com/in "hiring manager" "{company}"',
        ]

    # ── DuckDuckGo search ──────────────────────────────────────────────────────

    def _search_ddg(self, query: str) -> list[dict]:
        """
        POST to DuckDuckGo HTML endpoint and parse organic results.
        Returns list of {title, url, snippet} dicts.
        """
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            logger.error("beautifulsoup4 not installed. Run: pip install beautifulsoup4")
            return []

        try:
            response = self._client.post(
                self._DDG_URL,
                data={"q": query, "b": "", "kl": "us-en"},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.debug("DDG request failed: {}", exc)
            return []

        soup = BeautifulSoup(response.text, "html.parser")
        results = []

        for result_div in soup.select(".result"):
            title_el = result_div.select_one(".result__title a")
            snippet_el = result_div.select_one(".result__snippet")
            if not title_el:
                continue

            url = title_el.get("href", "")
            # DDG sometimes wraps URLs in a redirect — extract real URL
            url = self._extract_real_url(url)

            results.append({
                "title": title_el.get_text(strip=True),
                "url": url,
                "snippet": snippet_el.get_text(strip=True) if snippet_el else "",
            })

        return results

    @staticmethod
    def _extract_real_url(href: str) -> str:
        """DuckDuckGo wraps results in uddg= redirect — unwrap if needed."""
        if "uddg=" in href:
            from urllib.parse import parse_qs, urlparse, unquote
            qs = parse_qs(urlparse(href).query)
            if "uddg" in qs:
                return unquote(qs["uddg"][0])
        return href

    # ── Result parser ──────────────────────────────────────────────────────────

    def _parse_result(self, result: dict, company: str) -> Optional[RecruiterCandidate]:
        """
        Extract a RecruiterCandidate from a search result dict.
        Returns None if the result doesn't look like a LinkedIn profile.
        """
        url = result.get("url", "")
        if "linkedin.com/in/" not in url.lower():
            return None

        # Normalise URL
        m = self._LI_URL_RE.search(url)
        if not m:
            return None
        clean_url = f"https://www.linkedin.com/in/{m.group(1)}"

        # Title and snippet become our name + job title source
        page_title = result.get("title", "")
        snippet = result.get("snippet", "")
        combined = f"{page_title} {snippet}"

        name = self._extract_name(page_title)
        title = self._extract_job_title(combined)
        confidence = self._score_confidence(title, company, combined)

        return RecruiterCandidate(
            name=name,
            title=title,
            linkedin_url=clean_url,
            company=company,
            confidence=confidence,
        )

    def _extract_name(self, page_title: str) -> str:
        """LinkedIn page titles are usually 'First Last - Title | LinkedIn'."""
        # Strip ' | LinkedIn' suffix
        name_part = page_title.split("|")[0].split("-")[0].strip()
        m = self._NAME_RE.match(name_part)
        return m.group(1) if m else name_part[:50]

    @staticmethod
    def _extract_job_title(text: str) -> str:
        """Best-effort extraction of job title from search snippet."""
        title_patterns = [
            r"(?:is|as|works as)\s+([A-Za-z\s]+?)\s+at\b",
            r"([A-Za-z\s]*(?:Recruiter|Manager|Director|Engineer|Lead)[A-Za-z\s]*)",
        ]
        for pattern in title_patterns:
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                t = m.group(1).strip()
                if 4 < len(t) < 60:
                    return t
        # Fall back to first dash-separated segment of page title
        parts = text.split(" - ")
        if len(parts) > 1:
            return parts[1].strip()[:60]
        return "Unknown"

    @staticmethod
    def _score_confidence(title: str, company: str, text: str) -> float:
        """Heuristic 0–1 confidence score."""
        score = 0.0
        title_lower = title.lower()
        text_lower = text.lower()

        recruiter_kws = ["recruiter", "talent", "acquisition", "sourcer", "staffing"]
        manager_kws = ["manager", "director", "lead", "head of"]

        if any(kw in title_lower for kw in recruiter_kws):
            score += 0.5
        elif any(kw in title_lower for kw in manager_kws):
            score += 0.3

        if company.lower() in text_lower:
            score += 0.3

        if "hiring" in text_lower:
            score += 0.1
        if "engineer" in title_lower:
            score -= 0.1  # less likely to be a recruiter

        return min(1.0, max(0.0, score))
