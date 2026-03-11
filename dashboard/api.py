"""
FastAPI backend — serves the dashboard UI and provides a clean REST API
for all JDASS data.

Run:
    .venv/bin/uvicorn dashboard.api:app --reload --port 8000

Or:
    make api
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from loguru import logger
from pydantic import BaseModel
from sqlmodel import Session, select

from storage.database import enqueue_task, get_engine, get_session, init_db
from storage.models import (
    Application,
    ApplicationStatus,
    Job,
    JobStatus,
    OutreachQueue,
    OutreachStatus,
    ResumeVersion,
    TaskQueue,
    TaskStatus,
    TaskType,
)

# ── App setup ─────────────────────────────────────────────────────────────────

app = FastAPI(
    title="JDASS API",
    description="Job Discovery & Application System — local dashboard API",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # local-only, no security risk
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup():
    init_db()
    logger.info("JDASS API started")


# ── Pydantic response schemas ─────────────────────────────────────────────────


class JobSummary(BaseModel):
    id: str
    company: str
    title: str
    location: str
    source: str
    url: str
    match_score: Optional[int]
    seniority: Optional[str]
    remote_eligible: Optional[bool]
    h1b_mentioned: Optional[bool]
    status: str
    discovered_at: datetime
    posted_at: Optional[datetime]
    key_technologies: list[str]
    frameworks: list[str]
    matched_tech: list[str]

    @classmethod
    def from_job(cls, job: Job) -> "JobSummary":
        breakdown = {}
        if job.score_breakdown:
            try:
                breakdown = json.loads(job.score_breakdown)
            except Exception:
                pass
        return cls(
            id=job.id,
            company=job.company,
            title=job.title,
            location=job.location,
            source=job.source,
            url=job.url,
            match_score=job.match_score,
            seniority=job.seniority,
            remote_eligible=job.remote_eligible,
            h1b_mentioned=job.h1b_mentioned,
            status=job.status,
            discovered_at=job.discovered_at,
            posted_at=job.posted_at,
            key_technologies=job.get_technologies(),
            frameworks=job.get_frameworks(),
            matched_tech=breakdown.get("matched_tech", []),
        )


class JobDetail(JobSummary):
    description: str
    cloud_platforms: list[str]
    databases: list[str]
    score_breakdown: Optional[dict]

    @classmethod
    def from_job(cls, job: Job) -> "JobDetail":  # type: ignore[override]
        base = JobSummary.from_job(job)
        breakdown = None
        if job.score_breakdown:
            try:
                breakdown = json.loads(job.score_breakdown)
            except Exception:
                pass
        return cls(
            **base.model_dump(),
            description=job.description,
            cloud_platforms=job.get_cloud_platforms(),
            databases=job.get_databases(),
            score_breakdown=breakdown,
        )


class ApplicationSummary(BaseModel):
    id: str
    job_id: str
    company: str
    title: str
    location: str
    url: str
    match_score: Optional[int]
    applied_at: Optional[datetime]
    status: str
    resume_version: Optional[str]
    resume_path: Optional[str]
    notes: Optional[str]
    ats_type: Optional[str]


class OutreachItem(BaseModel):
    id: str
    job_id: str
    company: str
    title: str
    recruiter_name: Optional[str]
    recruiter_title: Optional[str]
    recruiter_url: Optional[str]
    message_text: str
    status: str
    created_at: datetime


class StatusUpdate(BaseModel):
    status: str


class NotesUpdate(BaseModel):
    notes: str


class DashboardStats(BaseModel):
    total_discovered: int
    total_applied: int
    total_interviews: int
    total_offers: int
    avg_match_score: Optional[float]
    top_sources: dict[str, int]
    applications_by_status: dict[str, int]
    top_technologies: list[dict]
    recent_applications: int  # last 7 days


# ── Jobs endpoints ────────────────────────────────────────────────────────────


@app.get("/api/jobs", response_model=list[JobSummary])
def list_jobs(
    status: Optional[str] = Query(None),
    min_score: Optional[int] = Query(None),
    source: Optional[str] = Query(None),
    remote_only: bool = Query(False),
    h1b_only: bool = Query(False),
    search: Optional[str] = Query(None),
    sort_by: str = Query("match_score"),  # "match_score" | "discovered_at"
    limit: int = Query(50, le=500),
    offset: int = Query(0),
):
    with get_session() as session:
        q = select(Job)
        if status:
            q = q.where(Job.status == status)
        if min_score is not None:
            q = q.where(Job.match_score >= min_score)
        if source:
            q = q.where(Job.source == source)
        if remote_only:
            q = q.where(Job.remote_eligible == True)  # noqa: E712
        if h1b_only:
            q = q.where(Job.h1b_mentioned == True)  # noqa: E712
        if search:
            term = f"%{search.lower()}%"
            q = q.where(
                (Job.company.ilike(term))  # type: ignore[attr-defined]
                | (Job.title.ilike(term))  # type: ignore[attr-defined]
            )
        if sort_by == "match_score":
            q = q.order_by(Job.match_score.desc().nullslast())  # type: ignore[attr-defined]
        else:
            q = q.order_by(Job.discovered_at.desc())
        q = q.offset(offset).limit(limit)
        jobs = list(session.exec(q).all())
    return [JobSummary.from_job(j) for j in jobs]


@app.get("/api/jobs/{job_id}", response_model=JobDetail)
def get_job(job_id: str):
    with get_session() as session:
        job = session.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return JobDetail.from_job(job)


@app.post("/api/jobs/{job_id}/queue")
def queue_job(job_id: str):
    """Add a job to the application task queue (sets status → QUEUED)."""
    import json as _json
    with get_session() as session:
        job = session.get(Job, job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        # Check if already pending
        pending = session.exec(
            select(TaskQueue)
            .where(TaskQueue.task_type == TaskType.APPLICATION)
            .where(TaskQueue.status == TaskStatus.PENDING)
        ).all()
        for t in pending:
            try:
                if _json.loads(t.payload).get("job_id") == job_id:
                    return {"ok": True, "queued": False, "reason": "Already in queue"}
            except Exception:
                pass
        job.status = JobStatus.QUEUED
        session.add(job)

    enqueue_task(TaskType.APPLICATION, {"job_id": job_id})
    return {"ok": True, "queued": True}


@app.put("/api/jobs/{job_id}/status")
def update_job_status(job_id: str, body: StatusUpdate):
    valid = {s.value for s in JobStatus}
    if body.status not in valid:
        raise HTTPException(400, f"Invalid status. Choose from: {sorted(valid)}")
    with get_session() as session:
        job = session.get(Job, job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        job.status = body.status
        session.add(job)
    return {"ok": True}


# ── Applications endpoints ────────────────────────────────────────────────────


@app.get("/api/applications", response_model=list[ApplicationSummary])
def list_applications(
    status: Optional[str] = Query(None),
    limit: int = Query(100, le=500),
    offset: int = Query(0),
):
    with get_session() as session:
        q = (
            select(Application, Job)
            .join(Job, Application.job_id == Job.id)
            .order_by(Application.applied_at.desc())
        )
        if status:
            q = q.where(Application.status == status)
        q = q.offset(offset).limit(limit)
        rows = list(session.exec(q).all())

    results = []
    for app_row, job in rows:
        results.append(ApplicationSummary(
            id=app_row.id,
            job_id=app_row.job_id,
            company=job.company,
            title=job.title,
            location=job.location,
            url=job.url,
            match_score=job.match_score,
            applied_at=app_row.applied_at,
            status=app_row.status,
            resume_version=app_row.resume_version,
            resume_path=app_row.resume_path,
            notes=app_row.notes,
            ats_type=app_row.ats_type,
        ))
    return results


@app.put("/api/applications/{app_id}/status")
def update_application_status(app_id: str, body: StatusUpdate):
    valid = {s.value for s in ApplicationStatus}
    if body.status not in valid:
        raise HTTPException(400, f"Invalid status. Choose from: {sorted(valid)}")
    with get_session() as session:
        application = session.get(Application, app_id)
        if not application:
            raise HTTPException(404, "Application not found")
        application.status = body.status
        session.add(application)
    return {"ok": True}


@app.put("/api/applications/{app_id}/notes")
def update_application_notes(app_id: str, body: NotesUpdate):
    with get_session() as session:
        application = session.get(Application, app_id)
        if not application:
            raise HTTPException(404, "Application not found")
        application.notes = body.notes
        session.add(application)
    return {"ok": True}


@app.get("/api/applications/{app_id}/resume")
def get_resume(app_id: str):
    """Serve the tailored resume PDF for an application."""
    with get_session() as session:
        application = session.get(Application, app_id)
    if not application or not application.resume_path:
        raise HTTPException(404, "Resume not found")
    path = Path(application.resume_path)
    if not path.exists():
        raise HTTPException(404, f"Resume file missing: {path}")
    return FileResponse(str(path), media_type="application/pdf", filename=path.name)


# ── Resume versions ───────────────────────────────────────────────────────────


@app.get("/api/resumes")
def list_resume_versions(job_id: Optional[str] = Query(None)):
    with get_session() as session:
        q = select(ResumeVersion).order_by(ResumeVersion.generated_at.desc())
        if job_id:
            q = q.where(ResumeVersion.job_id == job_id)
        versions = list(session.exec(q).all())
    return [
        {
            "id": v.id,
            "job_id": v.job_id,
            "version_id": v.version_id,
            "file_path": v.file_path,
            "llm_model": v.llm_model,
            "generated_at": v.generated_at,
            "bullets": json.loads(v.bullets_used) if v.bullets_used else [],
        }
        for v in versions
    ]


# ── Outreach queue endpoints ──────────────────────────────────────────────────


@app.get("/api/outreach", response_model=list[OutreachItem])
def list_outreach(
    status: Optional[str] = Query(OutreachStatus.PENDING_REVIEW),
    limit: int = Query(50, le=200),
):
    with get_session() as session:
        q = (
            select(OutreachQueue, Job)
            .join(Job, OutreachQueue.job_id == Job.id)
            .order_by(OutreachQueue.created_at.desc())
        )
        if status:
            q = q.where(OutreachQueue.status == status)
        q = q.limit(limit)
        rows = list(session.exec(q).all())

    return [
        OutreachItem(
            id=oq.id,
            job_id=oq.job_id,
            company=job.company,
            title=job.title,
            recruiter_name=oq.recruiter_name,
            recruiter_title=oq.recruiter_title,
            recruiter_url=oq.recruiter_url,
            message_text=oq.message_text,
            status=oq.status,
            created_at=oq.created_at,
        )
        for oq, job in rows
    ]


@app.post("/api/outreach/{outreach_id}/approve")
def approve_outreach(outreach_id: str):
    with get_session() as session:
        item = session.get(OutreachQueue, outreach_id)
        if not item:
            raise HTTPException(404, "Outreach item not found")
        item.status = OutreachStatus.APPROVED
        session.add(item)
    return {"ok": True}


@app.delete("/api/outreach/{outreach_id}")
def discard_outreach(outreach_id: str):
    with get_session() as session:
        item = session.get(OutreachQueue, outreach_id)
        if not item:
            raise HTTPException(404, "Outreach item not found")
        item.status = OutreachStatus.DISCARDED
        session.add(item)
    return {"ok": True}


# ── Stats endpoint ────────────────────────────────────────────────────────────


@app.get("/api/stats", response_model=DashboardStats)
def get_stats():
    from datetime import timedelta
    from collections import Counter
    import json

    with get_session() as session:
        all_jobs = list(session.exec(select(Job)).all())
        all_apps = list(session.exec(select(Application)).all())

    # Job counts
    total_discovered = len(all_jobs)
    app_status_counts: dict[str, int] = {}
    for app in all_apps:
        app_status_counts[app.status] = app_status_counts.get(app.status, 0) + 1

    total_applied = len(all_apps)
    total_interviews = app_status_counts.get(ApplicationStatus.INTERVIEW, 0)
    total_offers = app_status_counts.get(ApplicationStatus.OFFER, 0)

    # Average score (scored jobs only)
    scored = [j.match_score for j in all_jobs if j.match_score is not None]
    avg_score = round(sum(scored) / len(scored), 1) if scored else None

    # Source breakdown
    source_counts: dict[str, int] = {}
    for job in all_jobs:
        source_counts[job.source] = source_counts.get(job.source, 0) + 1

    # Tech frequency across all jobs
    tech_counter: Counter = Counter()
    for job in all_jobs:
        for tech in job.get_technologies():
            tech_counter[tech.lower()] += 1
    top_tech = [
        {"tech": tech, "count": count}
        for tech, count in tech_counter.most_common(15)
    ]

    # Recent applications (last 7 days)
    cutoff = datetime.utcnow() - timedelta(days=7)
    recent = sum(
        1 for a in all_apps
        if a.applied_at and a.applied_at >= cutoff
    )

    return DashboardStats(
        total_discovered=total_discovered,
        total_applied=total_applied,
        total_interviews=total_interviews,
        total_offers=total_offers,
        avg_match_score=avg_score,
        top_sources=source_counts,
        applications_by_status=app_status_counts,
        top_technologies=top_tech,
        recent_applications=recent,
    )


# ── Task queue endpoints ──────────────────────────────────────────────────────


@app.get("/api/tasks/summary")
def task_summary():
    with get_session() as session:
        tasks = list(session.exec(select(TaskQueue)).all())
    counts: dict[str, int] = {}
    for t in tasks:
        counts[t.status] = counts.get(t.status, 0) + 1
    return counts


@app.post("/api/tasks/retry-failed")
def retry_failed():
    from storage.database import retry_failed_tasks
    n = retry_failed_tasks()
    return {"retried": n}


# ── Health check ──────────────────────────────────────────────────────────────


@app.get("/health")
def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}
