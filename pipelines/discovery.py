"""
Discovery pipeline.

Orchestrates: scrape → filter → deduplicate → save → parse JD → score → enqueue

Run manually:
    python -m pipelines.discovery             # fast mode: regex parsing only
    python -m pipelines.discovery --llm       # LLM JD parsing (requires Ollama)
    python -m pipelines.discovery --semantic  # semantic dedup (requires Ollama)

Or import and call run() from the scheduler.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml
from loguru import logger

from core.deduplicator import Deduplicator
from core.filters import FilterConfig, JobFilter, detect_h1b, detect_remote, detect_seniority
from core.jd_parser import JDParser
from core.notifier import get_notifier
from core.scorer import JobScorer
from scrapers.base import RawJob
from scrapers.greenhouse import GreenhouseScraper
from scrapers.hn_hiring import HNHiringScraper
from scrapers.lever import LeverScraper
from storage import database as db
from storage.models import Job, JobStatus, TaskType


# ── Config loading ────────────────────────────────────────────────────────────

_CONFIG_DIR = Path(__file__).parent.parent / "configs"


def _load_yaml(name: str) -> dict:
    path = _CONFIG_DIR / name
    if not path.exists():
        logger.warning("Config file not found: {}", path)
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


# ── Pipeline result ───────────────────────────────────────────────────────────


@dataclass
class DiscoveryResult:
    total_scraped: int = 0
    filtered_out: int = 0
    duplicates: int = 0
    saved: int = 0
    parsed: int = 0
    errors: int = 0

    def summary(self) -> str:
        return (
            f"scraped={self.total_scraped} "
            f"saved={self.saved} "
            f"parsed={self.parsed} "
            f"filtered={self.filtered_out} "
            f"dupes={self.duplicates} "
            f"errors={self.errors}"
        )


# ── Pipeline ──────────────────────────────────────────────────────────────────


class DiscoveryPipeline:
    """
    Runs all configured scrapers and persists new, relevant jobs to the DB.

    Usage:
        pipeline = DiscoveryPipeline()
        result = pipeline.run()
        print(result.summary())
    """

    def __init__(
        self,
        settings_file: str = "settings.yaml",
        sources_file: str = "sources.yaml",
        semantic_dedup: bool = False,
        use_llm: bool = False,
        filter_overrides: Optional[dict] = None,
    ):
        self._settings = _load_yaml(settings_file)
        self._sources = _load_yaml(sources_file)
        self._filter_cfg = self._build_filter_config(filter_overrides or {})
        self._job_filter = JobFilter(self._filter_cfg)
        self._dedup = Deduplicator(semantic_enabled=semantic_dedup)
        self._parser = JDParser(use_llm=use_llm)
        self._scorer = JobScorer()
        self._notifier = get_notifier()

    # ── Public ────────────────────────────────────────────────────────────────

    def run(self) -> DiscoveryResult:
        logger.info("=== Discovery pipeline starting ===")
        t0 = time.perf_counter()

        db.init_db()
        result = DiscoveryResult()

        raw_jobs = self._scrape_all()
        result.total_scraped = len(raw_jobs)
        logger.info("Total scraped: {}", result.total_scraped)

        top_score: Optional[int] = None
        for raw in raw_jobs:
            try:
                score = self._process_job(raw, result)
                if score is not None and (top_score is None or score > top_score):
                    top_score = score
            except Exception as exc:
                logger.error("Unhandled error processing job | company={} title={} | {}", raw.company, raw.title, exc)
                result.errors += 1

        elapsed = time.perf_counter() - t0
        logger.info(
            "=== Discovery pipeline done | {} | elapsed={:.1f}s ===",
            result.summary(), elapsed,
        )
        # Notify user if new jobs were found
        self._notifier.discovery_complete(result.saved, top_score)
        return result

    # ── Scraping ──────────────────────────────────────────────────────────────

    def _scrape_all(self) -> list[RawJob]:
        all_jobs: list[RawJob] = []
        sources = self._sources.get("sources", {})

        # Greenhouse
        gh_slugs = sources.get("greenhouse", {}).get("company_slugs", [])
        if gh_slugs:
            logger.info("Greenhouse | companies={}", gh_slugs)
            try:
                scraper = GreenhouseScraper(
                    request_delay=sources.get("greenhouse", {}).get("request_delay", 2.0)
                )
                all_jobs.extend(scraper.fetch_jobs(company_slugs=gh_slugs))
            except Exception as exc:
                logger.error("Greenhouse scraper failed: {}", exc)

        # Lever
        lv_slugs = sources.get("lever", {}).get("company_slugs", [])
        if lv_slugs:
            logger.info("Lever | companies={}", lv_slugs)
            try:
                scraper = LeverScraper(
                    request_delay=sources.get("lever", {}).get("request_delay", 2.0)
                )
                all_jobs.extend(scraper.fetch_jobs(company_slugs=lv_slugs))
            except Exception as exc:
                logger.error("Lever scraper failed: {}", exc)

        # HN Hiring
        if sources.get("hn_hiring", {}).get("enabled", False):
            logger.info("HN Hiring | fetching current month thread")
            try:
                scraper = HNHiringScraper()
                all_jobs.extend(scraper.fetch_jobs(months_back=0))
            except Exception as exc:
                logger.error("HN Hiring scraper failed: {}", exc)

        return all_jobs

    # ── Per-job processing ────────────────────────────────────────────────────

    def _process_job(self, raw: RawJob, result: DiscoveryResult) -> Optional[int]:
        # 1. Filter
        passes, reason = self._job_filter.passes(raw)
        if not passes:
            result.filtered_out += 1
            return None

        # 2. Deduplicate
        fingerprint = self._dedup.fingerprint(raw)
        if self._dedup.is_duplicate(raw):
            result.duplicates += 1
            return None

        # 3. Build DB model
        job = Job(
            fingerprint=fingerprint,
            source=raw.source,
            external_id=raw.external_id or None,
            company=raw.company,
            title=raw.title,
            location=raw.location,
            description=raw.description,
            url=raw.url,
            posted_at=raw.posted_at,
            # Populate simple signals immediately (no LLM needed)
            remote_eligible=detect_remote(raw),
            h1b_mentioned=detect_h1b(raw),
            seniority=detect_seniority(raw),
            status=JobStatus.DISCOVERED,
        )

        # 4. Save (fast path — no LLM yet)
        saved = db.save_job(job)
        result.saved += 1

        # 5. Parse JD (LLM or regex) + compute score
        score: Optional[int] = None
        try:
            parsed = self._parser.parse(raw.description, title=raw.title)
            breakdown = self._scorer.score(
                parsed,
                title=raw.title,
                location=raw.location,
                posted_at=raw.posted_at,
            )
            # Persist parsed fields + score
            db.update_job_parsed_fields(saved.id, parsed.to_db_fields())
            db.update_job_score(saved.id, breakdown.total, breakdown.to_json())
            result.parsed += 1
            score = breakdown.total
            logger.info(
                "NEW JOB | score={} | {} | {} | {} | {} | tech={}",
                score, raw.company, raw.title, raw.location,
                raw.source, breakdown.matched_tech[:4],
            )
            # Notify immediately for high-score jobs
            self._notifier.job_found(raw.company, raw.title, score=score)
        except Exception as exc:
            logger.warning("JD parse/score failed for {} {}: {}", raw.company, raw.title, exc)
            logger.info(
                "NEW JOB | score=? | {} | {} | {} | {}",
                raw.company, raw.title, raw.location, raw.source,
            )

        # 6. Generate and store embedding (for future semantic dedup)
        embed_text = f"{raw.title} {raw.location} {raw.description[:400]}"
        self._dedup.compute_and_store_embedding(saved.id, embed_text)

        # 7. Enqueue for application pipeline (Phase 4)
        db.enqueue_task(
            task_type=TaskType.APPLICATION,
            payload={"job_id": saved.id},
        )
        return score

    # ── Config helpers ────────────────────────────────────────────────────────

    def _build_filter_config(self, overrides: dict | None = None) -> FilterConfig:
        filt = dict(self._settings.get("filters", {}))
        if overrides:
            filt.update(overrides)
        return FilterConfig(
            remote_ok=filt.get("remote_ok", True),
            allowed_locations=filt.get("allowed_locations", ["chicago", "remote"]),
            target_seniority=filt.get("target_seniority", ["mid", "senior", "staff", "principal", "lead"]),
            keep_unseniored=filt.get("keep_unseniored", True),
            max_age_days=filt.get("max_age_days", None),
            keep_undated=filt.get("keep_undated", True),
            require_h1b=filt.get("require_h1b", False),
            reject_no_sponsorship=filt.get("reject_no_sponsorship", True),
            target_role_keywords=filt.get("target_role_keywords", [
                "software engineer", "software developer", "swe", "sde",
                "backend engineer", "fullstack", "full stack",
                "platform engineer", "infrastructure engineer",
                "site reliability", "sre",
            ]),
        )


# ── CLI entry point ───────────────────────────────────────────────────────────


def main() -> None:
    """
    Entry point for `python -m pipelines.discovery` and `jdass-discover`.

    Modes:
      (default)          Regex JD parsing — fast, no Ollama required
      --llm              LLM JD parsing via Ollama (richer extraction)
      --semantic         Semantic dedup via Ollama embeddings
    """
    import argparse

    parser = argparse.ArgumentParser(description="JDASS Discovery Pipeline")
    parser.add_argument(
        "--llm",
        action="store_true",
        default=False,
        help="Use LLM for JD parsing (requires Ollama running)",
    )
    parser.add_argument(
        "--semantic",
        action="store_true",
        default=False,
        help="Enable semantic deduplication via Ollama embeddings (requires Ollama)",
    )
    parser.add_argument(
        "--no-semantic",
        action="store_true",
        default=False,
        help="Force-disable semantic deduplication",
    )
    parser.add_argument(
        "--max-age-days",
        type=float,
        default=None,
        metavar="N",
        help="Only keep jobs posted within the last N days (overrides settings.yaml)",
    )
    parser.add_argument(
        "--require-h1b",
        action="store_true",
        default=False,
        help="Only keep jobs that explicitly mention H1B sponsorship",
    )
    parser.add_argument(
        "--no-reject-no-sponsorship",
        action="store_true",
        default=False,
        help="Do NOT reject jobs that say 'no sponsorship' (overrides settings.yaml)",
    )
    parser.add_argument(
        "--locations",
        type=str,
        default=None,
        metavar="LOC1,LOC2",
        help="Comma-separated location keywords to allow (overrides settings.yaml)",
    )
    args = parser.parse_args()

    use_semantic = args.semantic and not args.no_semantic

    # Build filter overrides from CLI args
    filter_overrides: dict = {}
    if args.max_age_days is not None:
        filter_overrides["max_age_days"] = args.max_age_days
    if args.require_h1b:
        filter_overrides["require_h1b"] = True
    if args.no_reject_no_sponsorship:
        filter_overrides["reject_no_sponsorship"] = False
    if args.locations:
        filter_overrides["allowed_locations"] = [loc.strip() for loc in args.locations.split(",")]

    # Set up logging to file + stderr
    _setup_logging()

    pipeline = DiscoveryPipeline(
        semantic_dedup=use_semantic,
        use_llm=args.llm,
        filter_overrides=filter_overrides or None,
    )
    result = pipeline.run()

    print(f"\n{'='*50}")
    print(f"  Discovery complete")
    print(f"  Scraped : {result.total_scraped}")
    print(f"  Saved   : {result.saved}")
    print(f"  Parsed  : {result.parsed}")
    print(f"  Filtered: {result.filtered_out}")
    print(f"  Dupes   : {result.duplicates}")
    print(f"  Errors  : {result.errors}")
    print(f"{'='*50}\n")


def _setup_logging() -> None:
    from loguru import logger
    import sys

    logger.remove()
    logger.add(sys.stderr, level="INFO", colorize=True,
               format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}")
    logger.add(
        "logs/discovery_{time:YYYY-MM-DD}.log",
        rotation="1 day",
        retention="30 days",
        level="DEBUG",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {name}:{line} | {message}",
    )


if __name__ == "__main__":
    main()
