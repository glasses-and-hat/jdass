"""
Job deduplication engine.

Two-stage strategy:
  1. Fingerprint hash  — fast O(1) exact lookup via SQLite unique index
  2. Semantic similarity — catches reposts with different titles/descriptions
     (uses embeddings from nomic-embed-text via Ollama)

The fingerprint encodes: normalized(company) + normalized(title) + normalized(location)
  + md5(first 500 chars of description). This is stable across minor description edits
  while still catching meaningfully different postings from the same company.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Optional

from loguru import logger

from scrapers.base import RawJob
from storage import database as db


# ── Text normalisation ────────────────────────────────────────────────────────

# Title aliases that should be treated as equivalent
_TITLE_ALIASES: dict[str, str] = {
    "swe": "software engineer",
    "sde": "software engineer",
    "software development engineer": "software engineer",
    "software developer": "software engineer",
    "software engineer ii": "software engineer 2",
    "software engineer iii": "software engineer 3",
    "sr ": "senior ",
    "sr.": "senior",
    "principal engineer": "staff engineer",  # loose equivalence
}


def normalize_text(text: str) -> str:
    """Lowercase, remove punctuation, collapse whitespace, replace underscores."""
    text = text.lower().strip()
    for alias, replacement in _TITLE_ALIASES.items():
        text = text.replace(alias, replacement)
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(r"\s+", "_", text.strip())
    return text


def _desc_hash(description: str) -> str:
    """MD5 of first 500 normalised characters of the description."""
    sample = normalize_text(description[:500])
    return hashlib.md5(sample.encode()).hexdigest()[:8]


# ── Fingerprint ───────────────────────────────────────────────────────────────


def make_fingerprint(company: str, title: str, location: str, description: str) -> str:
    """
    Build a stable, human-readable job fingerprint.

    Format: {company}_{title}_{location}_{desc_hash}
    Example: stripe_senior_software_engineer_remote_82fd882a
    """
    parts = [
        normalize_text(company),
        normalize_text(title),
        normalize_text(location),
        _desc_hash(description),
    ]
    return "_".join(p for p in parts if p)


def fingerprint_for_raw(job: RawJob) -> str:
    return make_fingerprint(job.company, job.title, job.location, job.description)


# ── Cosine similarity ─────────────────────────────────────────────────────────


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Pure-Python cosine similarity. Returns 0.0 if either vector is zero."""
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(y * y for y in b))
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


# ── Deduplicator ──────────────────────────────────────────────────────────────


class Deduplicator:
    """
    Stateless deduplication helper. Reads from and writes to the DB.

    Usage:
        dedup = Deduplicator()
        fp = dedup.fingerprint(raw_job)
        if dedup.is_duplicate(raw_job):
            continue
    """

    def __init__(
        self,
        semantic_threshold: float = 0.92,
        semantic_enabled: bool = True,
        llm_client=None,
    ):
        """
        Args:
            semantic_threshold: Cosine similarity above which a job is near-duplicate.
            semantic_enabled:   Disable for speed (fingerprint-only mode).
            llm_client:         OllamaClient instance (lazy-imported if None).
        """
        self.semantic_threshold = semantic_threshold
        self.semantic_enabled = semantic_enabled
        self._llm = llm_client  # injected or fetched lazily

    @property
    def llm(self):
        if self._llm is None:
            from llm.client import get_llm_client
            self._llm = get_llm_client()
        return self._llm

    # ── Public API ────────────────────────────────────────────────────────────

    def fingerprint(self, job: RawJob) -> str:
        return fingerprint_for_raw(job)

    def is_duplicate(self, job: RawJob) -> bool:
        """
        Return True if this job is already in the DB.

        Checks (in order):
          1. Exact fingerprint match  (fast)
          2. Semantic similarity      (slower, optional)
        """
        fp = self.fingerprint(job)

        # Stage 1: exact fingerprint
        if db.job_fingerprint_exists(fp):
            logger.debug("Dedup[fingerprint] | DUPLICATE | {}", fp)
            return True

        # Stage 2: semantic similarity against same company's recent jobs
        if self.semantic_enabled:
            if self._is_semantic_duplicate(job):
                logger.info(
                    "Dedup[semantic] | NEAR-DUPLICATE | company={} title={}",
                    job.company, job.title,
                )
                return True

        return False

    def _is_semantic_duplicate(self, job: RawJob) -> bool:
        """
        Embed the incoming job description and compare against recent jobs
        from the same company. Returns True if similarity > threshold.
        """
        recent = db.get_recent_company_jobs(company=job.company, days=90)
        if not recent:
            return False

        # Only compute embedding if there are candidates
        try:
            new_embedding = self.llm.embed(
                f"{job.title} {job.location} {job.description[:400]}"
            )
        except Exception as exc:
            logger.warning("Embedding failed — skipping semantic dedup: {}", exc)
            return False

        for existing in recent:
            stored_vec = existing.get_embedding()
            if not stored_vec:
                continue
            sim = cosine_similarity(new_embedding, stored_vec)
            if sim >= self.semantic_threshold:
                logger.debug(
                    "Semantic sim={:.3f} threshold={} | company={} existing_title={}",
                    sim, self.semantic_threshold, job.company, existing.title,
                )
                return True

        return False

    def compute_and_store_embedding(self, job_id: str, text: str) -> Optional[list[float]]:
        """
        Generate an embedding for a newly saved job and persist it.
        Call this after saving the job to the DB.
        """
        try:
            vec = self.llm.embed(text)
        except Exception as exc:
            logger.warning("Could not generate embedding for job {}: {}", job_id, exc)
            return None

        try:
            with db.get_session() as session:
                from storage.models import Job
                from sqlmodel import select
                job = session.exec(select(Job).where(Job.id == job_id)).first()
                if job:
                    job.set_embedding(vec)
                    session.add(job)
        except Exception as exc:
            logger.warning("Could not persist embedding for job {}: {}", job_id, exc)

        return vec
