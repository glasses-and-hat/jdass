"""
Database setup, session management, and repository helpers.
All DB access goes through the functions in this module — nothing else
imports SQLModel engine/session directly.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime
from typing import Generator, Optional

from loguru import logger
from sqlmodel import Session, SQLModel, create_engine, select

from storage.models import Job, JobStatus, ResumeVersion, TaskQueue, TaskStatus, TaskType


# ── Engine ────────────────────────────────────────────────────────────────────

_engine = None


def get_engine(db_url: str = "sqlite:///./jdass.db"):
    global _engine
    if _engine is None:
        # check_same_thread=False is safe here because we use one session per call
        _engine = create_engine(
            db_url,
            echo=False,
            connect_args={"check_same_thread": False},
        )
    return _engine


def init_db(db_url: str = "sqlite:///./jdass.db") -> None:
    """Create all tables. Safe to call multiple times (CREATE TABLE IF NOT EXISTS)."""
    engine = get_engine(db_url)
    SQLModel.metadata.create_all(engine)
    logger.info("Database initialised at {}", db_url)


@contextmanager
def get_session() -> Generator[Session, None, None]:
    engine = get_engine()
    # expire_on_commit=False keeps objects usable after the session closes,
    # preventing "Instance is not bound to a Session" errors.
    with Session(engine, expire_on_commit=False) as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise


# ── Job repository ────────────────────────────────────────────────────────────


def save_job(job: Job) -> Job:
    """Insert or ignore a job (fingerprint is unique)."""
    with get_session() as session:
        existing = session.exec(
            select(Job).where(Job.fingerprint == job.fingerprint)
        ).first()
        if existing:
            logger.debug("Job already exists: {}", job.fingerprint)
            return existing
        session.add(job)
        logger.info(
            "Saved new job | company={} title={} source={}",
            job.company, job.title, job.source,
        )
    return job


def get_job_by_fingerprint(fingerprint: str) -> Optional[Job]:
    with get_session() as session:
        return session.exec(
            select(Job).where(Job.fingerprint == fingerprint)
        ).first()


def job_fingerprint_exists(fingerprint: str) -> bool:
    return get_job_by_fingerprint(fingerprint) is not None


def get_jobs(
    status: Optional[str] = None,
    min_score: Optional[int] = None,
    limit: int = 100,
    offset: int = 0,
) -> list[Job]:
    with get_session() as session:
        q = select(Job)
        if status:
            q = q.where(Job.status == status)
        if min_score is not None:
            q = q.where(Job.match_score >= min_score)
        q = q.order_by(Job.discovered_at.desc()).offset(offset).limit(limit)
        return list(session.exec(q).all())


def update_job_status(job_id: str, status: JobStatus) -> None:
    with get_session() as session:
        job = session.get(Job, job_id)
        if job:
            job.status = status
            session.add(job)


def get_recent_company_jobs(company: str, days: int = 90) -> list[Job]:
    """Return jobs from `company` discovered within the last `days` days."""
    from datetime import timedelta
    cutoff = datetime.utcnow() - timedelta(days=days)
    with get_session() as session:
        return list(session.exec(
            select(Job)
            .where(Job.company == company)
            .where(Job.discovered_at >= cutoff)
        ).all())


# ── Task queue repository ─────────────────────────────────────────────────────


def enqueue_task(task_type: TaskType, payload: dict) -> TaskQueue:
    task = TaskQueue(task_type=task_type, payload=json.dumps(payload))
    with get_session() as session:
        session.add(task)
    logger.debug("Enqueued task | type={} payload={}", task_type, payload)
    return task


def claim_next_task(task_type: Optional[TaskType] = None) -> Optional[TaskQueue]:
    """
    Atomically claim the next PENDING task.
    Returns None if the queue is empty.
    """
    with get_session() as session:
        q = select(TaskQueue).where(TaskQueue.status == TaskStatus.PENDING)
        if task_type:
            q = q.where(TaskQueue.task_type == task_type)
        q = q.order_by(TaskQueue.created_at).limit(1)
        task = session.exec(q).first()
        if task:
            task.status = TaskStatus.PROCESSING
            task.updated_at = datetime.utcnow()
            session.add(task)
    return task


def complete_task(task_id: str) -> None:
    with get_session() as session:
        task = session.get(TaskQueue, task_id)
        if task:
            task.status = TaskStatus.DONE
            task.updated_at = datetime.utcnow()
            session.add(task)


def fail_task(task_id: str, error: str) -> None:
    with get_session() as session:
        task = session.get(TaskQueue, task_id)
        if task:
            task.status = TaskStatus.FAILED
            task.last_error = error
            task.attempts += 1
            task.updated_at = datetime.utcnow()
            session.add(task)


def update_job_parsed_fields(job_id: str, fields: dict) -> None:
    """Persist LLM-parsed fields (tech stack, seniority, etc.) onto an existing Job."""
    with get_session() as session:
        job = session.get(Job, job_id)
        if not job:
            logger.warning("update_job_parsed_fields: job {} not found", job_id)
            return
        for key, value in fields.items():
            if hasattr(job, key):
                setattr(job, key, value)
        session.add(job)


def update_job_score(job_id: str, score: int, breakdown_json: str) -> None:
    """Persist the match score and breakdown onto an existing Job."""
    with get_session() as session:
        job = session.get(Job, job_id)
        if job:
            job.match_score = score
            job.score_breakdown = breakdown_json
            session.add(job)


def get_unscored_jobs(limit: int = 50) -> list[Job]:
    """Return DISCOVERED jobs that haven't been scored yet."""
    with get_session() as session:
        return list(session.exec(
            select(Job)
            .where(Job.status == JobStatus.DISCOVERED)
            .where(Job.match_score == None)  # noqa: E711
            .order_by(Job.discovered_at.desc())
            .limit(limit)
        ).all())


def get_job_by_id(job_id: str) -> Optional[Job]:
    with get_session() as session:
        return session.get(Job, job_id)


def save_resume_version(rv: ResumeVersion) -> ResumeVersion:
    with get_session() as session:
        session.add(rv)
    return rv


def retry_failed_tasks() -> int:
    """Reset all FAILED tasks to PENDING so they can be retried. Returns count."""
    with get_session() as session:
        tasks = list(session.exec(
            select(TaskQueue).where(TaskQueue.status == TaskStatus.FAILED)
        ).all())
        for t in tasks:
            t.status = TaskStatus.PENDING
            t.updated_at = datetime.utcnow()
            session.add(t)
    logger.info("Re-queued {} failed tasks", len(tasks))
    return len(tasks)
