"""
Standalone JD parsing + scoring pipeline.

Runs against already-discovered (unscored) jobs in the DB.
Use this to (re)parse jobs with the LLM after discovery, without re-scraping.

Usage:
    python -m pipelines.parse_jobs              # regex mode — fast
    python -m pipelines.parse_jobs --llm        # LLM mode (requires Ollama)
    python -m pipelines.parse_jobs --limit 20   # process only 20 jobs
"""

from __future__ import annotations

import argparse
import sys
import time

from loguru import logger

from core.jd_parser import JDParser
from core.scorer import JobScorer
from storage import database as db


def run(use_llm: bool = False, limit: int = 100) -> None:
    db.init_db()

    jobs = db.get_unscored_jobs(limit=limit)
    if not jobs:
        logger.info("No unscored jobs found.")
        return

    parser = JDParser(use_llm=use_llm)
    scorer = JobScorer()

    logger.info("Parsing {} jobs | llm={}", len(jobs), use_llm)
    t0 = time.perf_counter()
    success = errors = 0

    for job in jobs:
        try:
            parsed = parser.parse(job.description, title=job.title)
            breakdown = scorer.score(
                parsed,
                title=job.title,
                location=job.location,
                posted_at=job.posted_at,
            )
            db.update_job_parsed_fields(job.id, parsed.to_db_fields())
            db.update_job_score(job.id, breakdown.total, breakdown.to_json())
            success += 1
            logger.info(
                "Parsed | score={:3d} | {:30s} | {} | tech={}",
                breakdown.total,
                f"{job.company[:20]} / {job.title[:20]}",
                job.source,
                breakdown.matched_tech[:4],
            )
        except Exception as exc:
            logger.error("Parse failed | {} {} | {}", job.company, job.title, exc)
            errors += 1

    elapsed = time.perf_counter() - t0
    print(f"\n{'='*50}")
    print(f"  Parse complete | llm={use_llm}")
    print(f"  Processed : {len(jobs)}")
    print(f"  Success   : {success}")
    print(f"  Errors    : {errors}")
    print(f"  Elapsed   : {elapsed:.1f}s")
    print(f"{'='*50}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="JDASS JD Parsing Pipeline")
    parser.add_argument("--llm", action="store_true", help="Use LLM (requires Ollama)")
    parser.add_argument("--limit", type=int, default=100, help="Max jobs to process")
    args = parser.parse_args()

    logger.remove()
    logger.add(sys.stderr, level="INFO", colorize=True,
               format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}")
    logger.add(
        "logs/parse_jobs_{time:YYYY-MM-DD}.log",
        rotation="1 day", retention="14 days", level="DEBUG",
    )

    run(use_llm=args.llm, limit=args.limit)


if __name__ == "__main__":
    main()
