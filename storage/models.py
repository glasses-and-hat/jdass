"""
SQLModel ORM models — single source of truth for the DB schema.
All JSON columns are stored as TEXT and (de)serialized at the model boundary.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from enum import Enum
from typing import Optional

from sqlmodel import Field, SQLModel


# ── Enums ─────────────────────────────────────────────────────────────────────


class JobStatus(str, Enum):
    DISCOVERED = "DISCOVERED"
    FILTERED_OUT = "FILTERED_OUT"
    QUEUED = "QUEUED"
    APPLIED = "APPLIED"
    FAILED_AUTO_APPLY = "FAILED_AUTO_APPLY"
    MANUAL_REVIEW = "MANUAL_REVIEW"


class ApplicationStatus(str, Enum):
    APPLIED = "APPLIED"
    RECRUITER_CONTACTED = "RECRUITER_CONTACTED"
    INTERVIEW = "INTERVIEW"
    REJECTED = "REJECTED"
    OFFER = "OFFER"
    WITHDRAWN = "WITHDRAWN"


class OutreachStatus(str, Enum):
    PENDING_REVIEW = "PENDING_REVIEW"
    APPROVED = "APPROVED"
    SENT = "SENT"
    DISCARDED = "DISCARDED"


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    DONE = "DONE"
    FAILED = "FAILED"


class TaskType(str, Enum):
    APPLICATION = "application"
    OUTREACH = "outreach"
    MANUAL_REVIEW = "manual_review"


# ── Job ───────────────────────────────────────────────────────────────────────


class Job(SQLModel, table=True):
    """
    A discovered job posting. One row per unique fingerprint.
    JSON array fields are stored as TEXT; use helper properties to access them.
    """

    __tablename__ = "jobs"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    fingerprint: str = Field(unique=True, index=True)
    source: str  # "greenhouse", "lever", "hn_hiring", etc.
    external_id: Optional[str] = None
    company: str = Field(index=True)
    title: str
    location: str
    description: str
    url: str
    posted_at: Optional[datetime] = None
    discovered_at: datetime = Field(default_factory=datetime.utcnow)

    # ── LLM-parsed fields (populated after JD analysis) ──
    key_technologies: Optional[str] = None   # JSON: ["Python", "Kafka", ...]
    frameworks: Optional[str] = None          # JSON: ["FastAPI", "React", ...]
    cloud_platforms: Optional[str] = None     # JSON: ["AWS", "GCP", ...]
    databases: Optional[str] = None           # JSON: ["PostgreSQL", "Redis", ...]
    seniority: Optional[str] = None           # "junior" | "mid" | "senior" | "staff"
    h1b_mentioned: Optional[bool] = None
    remote_eligible: Optional[bool] = None

    # ── Scoring ──
    match_score: Optional[int] = None         # 0–100
    score_breakdown: Optional[str] = None     # JSON

    # ── Semantic dedup ──
    embedding: Optional[str] = None           # JSON float array

    status: str = Field(default=JobStatus.DISCOVERED)

    # ── Convenience helpers ───────────────────────────────────────────────────

    def get_technologies(self) -> list[str]:
        return json.loads(self.key_technologies) if self.key_technologies else []

    def get_frameworks(self) -> list[str]:
        return json.loads(self.frameworks) if self.frameworks else []

    def get_cloud_platforms(self) -> list[str]:
        return json.loads(self.cloud_platforms) if self.cloud_platforms else []

    def get_databases(self) -> list[str]:
        return json.loads(self.databases) if self.databases else []

    def get_embedding(self) -> list[float]:
        return json.loads(self.embedding) if self.embedding else []

    def set_technologies(self, v: list[str]) -> None:
        self.key_technologies = json.dumps(v)

    def set_embedding(self, v: list[float]) -> None:
        self.embedding = json.dumps(v)


# ── Application ───────────────────────────────────────────────────────────────


class Application(SQLModel, table=True):
    """One row per job application submitted (or attempted)."""

    __tablename__ = "applications"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    job_id: str = Field(foreign_key="jobs.id", unique=True, index=True)
    applied_at: Optional[datetime] = None
    resume_path: Optional[str] = None      # path to the tailored PDF
    resume_version: Optional[str] = None   # e.g. "stripe_backend_2026-03-08"
    ats_type: Optional[str] = None         # "greenhouse" | "lever" | "linkedin_easy"
    submission_log: Optional[str] = None   # JSON: fields filled, screenshot paths
    form_guesses: Optional[str] = None     # JSON: [{label, value, field_id, options, confirmed}]
    status: str = Field(default=ApplicationStatus.APPLIED)
    notes: Optional[str] = None


# ── OutreachQueue ─────────────────────────────────────────────────────────────


class OutreachQueue(SQLModel, table=True):
    """Recruiter/hiring-manager messages waiting for user approval."""

    __tablename__ = "outreach_queue"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    job_id: str = Field(foreign_key="jobs.id", index=True)
    application_id: Optional[str] = Field(default=None, foreign_key="applications.id")
    recruiter_name: Optional[str] = None
    recruiter_title: Optional[str] = None
    recruiter_url: Optional[str] = None     # LinkedIn profile URL
    message_text: str
    status: str = Field(default=OutreachStatus.PENDING_REVIEW)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    sent_at: Optional[datetime] = None


# ── TaskQueue ─────────────────────────────────────────────────────────────────


class TaskQueue(SQLModel, table=True):
    """
    Lightweight internal work queue backed by SQLite.
    No external broker (Redis/RabbitMQ) needed for local-only use.
    """

    __tablename__ = "task_queue"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    task_type: str        # TaskType value
    payload: str          # JSON — task-specific data
    status: str = Field(default=TaskStatus.PENDING)
    attempts: int = Field(default=0)
    last_error: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: Optional[datetime] = None

    def get_payload(self) -> dict:
        return json.loads(self.payload)


# ── ResumeVersion ─────────────────────────────────────────────────────────────


class ResumeVersion(SQLModel, table=True):
    """Audit trail of every LLM-tailored resume generated."""

    __tablename__ = "resume_versions"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    job_id: str = Field(foreign_key="jobs.id", index=True)
    version_id: str        # human-readable slug
    file_path: str         # absolute path to the PDF
    bullets_used: Optional[str] = None   # JSON: the 3 generated bullets
    llm_model: Optional[str] = None
    generated_at: datetime = Field(default_factory=datetime.utcnow)
