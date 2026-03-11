"""
JDASS Streamlit Dashboard.

Reads directly from the SQLite DB (no API server required).
Three pages:
  1. Jobs        — browse discovered jobs, view JD + score breakdown
  2. Applications — track applied jobs, update status, view resumes
  3. Stats        — charts: score distribution, tech frequency, timeline

Run:
    make dashboard
    # or
    .venv/bin/streamlit run dashboard/ui.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import streamlit as st
from sqlmodel import select

# ── Bootstrap path so imports work when run from project root ─────────────────
ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from storage.database import get_session, init_db, enqueue_task, update_job_status as _db_update_job_status
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

# ── Page config (must be first Streamlit call) ────────────────────────────────
st.set_page_config(
    page_title="JDASS",
    page_icon="💼",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Init DB on startup ────────────────────────────────────────────────────────
init_db()


# ── Shared helpers ─────────────────────────────────────────────────────────────

STATUS_COLORS = {
    "DISCOVERED":           "#6c757d",
    "QUEUED":               "#0d6efd",
    "APPLIED":              "#198754",
    "FAILED_AUTO_APPLY":    "#dc3545",
    "MANUAL_REVIEW":        "#fd7e14",
    "FILTERED_OUT":         "#adb5bd",
    ApplicationStatus.APPLIED:              "#198754",
    ApplicationStatus.RECRUITER_CONTACTED:  "#0dcaf0",
    ApplicationStatus.INTERVIEW:            "#0d6efd",
    ApplicationStatus.REJECTED:             "#dc3545",
    ApplicationStatus.OFFER:               "#ffc107",
    ApplicationStatus.WITHDRAWN:           "#6c757d",
}

SCORE_EMOJI = {
    range(0, 50):   "🔴",
    range(50, 70):  "🟡",
    range(70, 85):  "🟢",
    range(85, 101): "⭐",
}


def score_badge(score: Optional[int]) -> str:
    if score is None:
        return "—"
    for r, emoji in SCORE_EMOJI.items():
        if score in r:
            return f"{emoji} {score}"
    return str(score)


@st.cache_data(ttl=30)
def load_jobs(
    min_score: int = 0,
    sources: Optional[list[str]] = None,
    remote_only: bool = False,
    h1b_only: bool = False,
    status_filter: Optional[str] = None,
    search: str = "",
    limit: int = 500,
) -> pd.DataFrame:
    with get_session() as session:
        q = select(Job).order_by(Job.match_score.desc().nullslast())  # type: ignore
        if min_score:
            q = q.where(Job.match_score >= min_score)
        if remote_only:
            q = q.where(Job.remote_eligible == True)  # noqa: E712
        if h1b_only:
            q = q.where(Job.h1b_mentioned == True)  # noqa: E712
        if status_filter and status_filter != "All":
            q = q.where(Job.status == status_filter)
        q = q.limit(limit)
        jobs = list(session.exec(q).all())

    rows = []
    for j in jobs:
        tech = j.get_technologies()
        if sources and j.source not in sources:
            continue
        title_lower = j.title.lower()
        company_lower = j.company.lower()
        if search and search.lower() not in title_lower and search.lower() not in company_lower:
            continue
        bd = json.loads(j.score_breakdown) if j.score_breakdown else {}
        rows.append({
            "id": j.id,
            "Company": j.company,
            "Title": j.title,
            "Location": j.location,
            "Source": j.source,
            "Score": j.match_score,
            "Seniority": j.seniority or "—",
            "Remote": "✓" if j.remote_eligible else "",
            "H1B": "✓" if j.h1b_mentioned else "",
            "Tech": ", ".join(tech[:5]),
            "Matched Tech": ", ".join(bd.get("matched_tech", [])[:5]),
            "Status": j.status,
            "Discovered": j.discovered_at.strftime("%m/%d") if j.discovered_at else "",
            "URL": j.url,
            "Description": j.description[:300] + "...",
            "_posted_at": j.posted_at,
        })
    return pd.DataFrame(rows)


@st.cache_data(ttl=30)
def load_applications() -> pd.DataFrame:
    with get_session() as session:
        rows_raw = list(session.exec(
            select(Application, Job).join(Job, Application.job_id == Job.id)
            .order_by(Application.applied_at.desc())
        ).all())

    rows = []
    for app, job in rows_raw:
        rows.append({
            "id": app.id,
            "job_id": app.job_id,
            "Company": job.company,
            "Title": job.title,
            "Location": job.location,
            "Score": job.match_score,
            "Applied": app.applied_at.strftime("%Y-%m-%d") if app.applied_at else "—",
            "Status": app.status,
            "ATS": app.ats_type or "—",
            "Resume": app.resume_version or "—",
            "Notes": app.notes or "",
            "URL": job.url,
            "resume_path": app.resume_path,
        })
    return pd.DataFrame(rows)


@st.cache_data(ttl=60)
def load_stats() -> dict:
    with get_session() as session:
        all_jobs = list(session.exec(select(Job)).all())
        all_apps = list(session.exec(select(Application)).all())

    scored = [j.match_score for j in all_jobs if j.match_score is not None]
    tech_counter: dict[str, int] = {}
    for j in all_jobs:
        for t in j.get_technologies():
            tech_counter[t] = tech_counter.get(t, 0) + 1

    # Applications by week
    app_by_week: dict[str, int] = {}
    for a in all_apps:
        if a.applied_at:
            week = a.applied_at.strftime("%Y-W%W")
            app_by_week[week] = app_by_week.get(week, 0) + 1

    source_counts: dict[str, int] = {}
    for j in all_jobs:
        source_counts[j.source] = source_counts.get(j.source, 0) + 1

    app_status_counts: dict[str, int] = {}
    for a in all_apps:
        app_status_counts[a.status] = app_status_counts.get(a.status, 0) + 1

    return {
        "total_jobs": len(all_jobs),
        "total_apps": len(all_apps),
        "avg_score": round(sum(scored) / len(scored), 1) if scored else 0,
        "top_score": max(scored) if scored else 0,
        "scores": scored,
        "tech_counter": dict(sorted(tech_counter.items(), key=lambda x: x[1], reverse=True)[:20]),
        "source_counts": source_counts,
        "app_status_counts": app_status_counts,
        "app_by_week": app_by_week,
        "interviews": app_status_counts.get(ApplicationStatus.INTERVIEW, 0),
        "offers": app_status_counts.get(ApplicationStatus.OFFER, 0),
    }


# ── Apply helpers ─────────────────────────────────────────────────────────────

def queue_job_for_apply(job_id: str) -> bool:
    """Add job to task queue and set status to QUEUED. Returns True if newly queued."""
    from sqlmodel import select
    with get_session() as session:
        # Check if already pending in queue
        existing = session.exec(
            select(TaskQueue)
            .where(TaskQueue.task_type == TaskType.APPLICATION)
            .where(TaskQueue.status == TaskStatus.PENDING)
        ).all()
        for t in existing:
            import json as _json
            try:
                if _json.loads(t.payload).get("job_id") == job_id:
                    return False  # already queued
            except Exception:
                pass

    enqueue_task(TaskType.APPLICATION, {"job_id": job_id})
    _db_update_job_status(job_id, JobStatus.QUEUED)
    st.cache_data.clear()
    return True


def run_apply_subprocess(job_id: str, dry_run: bool = False) -> tuple[int, str]:
    """Run the application pipeline as a subprocess. Returns (returncode, combined_output)."""
    cmd = [sys.executable, "-m", "pipelines.application", "--job-id", job_id]
    if dry_run:
        cmd.append("--dry-run")
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(ROOT),
        )
        output = (result.stdout or "") + (result.stderr or "")
        return result.returncode, output.strip()
    except subprocess.TimeoutExpired:
        return 1, "Timed out after 5 minutes."
    except Exception as exc:
        return 1, str(exc)


def _is_manual_only_url(url: str) -> bool:
    """Return True for sources that can't be auto-applied (e.g. HN threads)."""
    return "news.ycombinator.com" in url or "ycombinator.com/item" in url


def run_tailor_subprocess(job_id: str) -> tuple[int, str, Optional[Path]]:
    """
    Run resume-only tailoring as a subprocess.
    Returns (returncode, combined_output, pdf_path_or_None).
    """
    cmd = [sys.executable, "-m", "pipelines.application", "--job-id", job_id, "--resume-only"]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(ROOT),
        )
        output = (result.stdout or "") + (result.stderr or "")
        # Extract the PDF path printed by tailor_only()
        pdf_path: Optional[Path] = None
        for line in output.splitlines():
            if line.startswith("RESUME_PATH:"):
                pdf_path = Path(line.split("RESUME_PATH:", 1)[1].strip())
                break
        return result.returncode, output.strip(), pdf_path
    except subprocess.TimeoutExpired:
        return 1, "Timed out after 5 minutes.", None
    except Exception as exc:
        return 1, str(exc), None


def _render_resume_only_panel(job_id: str) -> None:
    """Generate-resume panel for manual-apply jobs (HN, etc.)."""
    output_key = f"tailor_output_{job_id}"
    pdf_key = f"tailor_pdf_{job_id}"

    if st.button(
        "📄 Generate Tailored Resume",
        key=f"tailor_{job_id}",
        type="primary",
        use_container_width=True,
        help="Tailor resume to this job's JD and produce a PDF — no browser opened",
    ):
        with st.spinner("Tailoring resume… (may take ~30 s with LLM)"):
            rc, output, pdf_path = run_tailor_subprocess(job_id)
        st.session_state[output_key] = output
        st.session_state[pdf_key] = str(pdf_path) if pdf_path else None
        if rc == 0 and pdf_path:
            st.success("Resume generated!")
        else:
            st.error("Tailoring failed — see output below.")

    # ── Show download button if resume exists ─────────────────────────────────
    saved_pdf = st.session_state.get(pdf_key)
    if saved_pdf:
        pdf_file = Path(saved_pdf)
        if pdf_file.exists():
            with open(pdf_file, "rb") as fh:
                st.download_button(
                    label=f"⬇ Download {pdf_file.name}",
                    data=fh.read(),
                    file_name=pdf_file.name,
                    mime="application/pdf" if pdf_file.suffix == ".pdf" else "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    key=f"dl_{job_id}",
                    use_container_width=True,
                )

    # ── Pipeline output log ───────────────────────────────────────────────────
    if output_key in st.session_state and st.session_state[output_key]:
        with st.expander("Pipeline output", expanded=False):
            st.code(st.session_state[output_key], language="text")


def _render_apply_panel(job_id: str, job_status: str, job_url: str) -> None:
    """Render the Apply Actions panel inside the job detail view."""
    st.write("**Apply Actions**")

    # Current status badge
    color = STATUS_COLORS.get(job_status, "#6c757d")
    st.markdown(
        f'<span style="background:{color};color:#fff;padding:2px 8px;'
        f'border-radius:4px;font-size:0.8em">{job_status}</span>',
        unsafe_allow_html=True,
    )
    st.write("")

    # HN / unsupported sources — show resume generator instead of apply buttons
    if _is_manual_only_url(job_url):
        st.info(
            "This job was sourced from Hacker News. Auto-apply is not supported — "
            "apply directly on the company's site.",
            icon="ℹ️",
        )
        _render_resume_only_panel(job_id)
        return

    btn_col1, btn_col2, btn_col3 = st.columns(3)

    # ── Queue for Apply ───────────────────────────────────────────────────────
    with btn_col1:
        already_applied = job_status in ("APPLIED",)
        if st.button(
            "➕ Queue",
            key=f"queue_{job_id}",
            use_container_width=True,
            disabled=already_applied,
            help="Add to task queue — processed by `make apply-queue`",
        ):
            added = queue_job_for_apply(job_id)
            if added:
                st.success("Queued!")
                st.rerun()
            else:
                st.info("Already queued.")

    # ── Apply Now ─────────────────────────────────────────────────────────────
    with btn_col2:
        if st.button(
            "▶ Apply Now",
            key=f"apply_{job_id}",
            use_container_width=True,
            disabled=already_applied,
            type="primary",
            help="Tailor resume and submit application immediately",
        ):
            with st.spinner("Applying… (may take up to 5 min)"):
                rc, output = run_apply_subprocess(job_id)
            if rc == 0:
                st.success("Application submitted!")
                st.cache_data.clear()
                st.rerun()
            else:
                st.error("Application failed — see output below.")
            st.session_state[f"apply_output_{job_id}"] = output

    # ── Dry Run ───────────────────────────────────────────────────────────────
    with btn_col3:
        if st.button(
            "🔍 Dry Run",
            key=f"dry_{job_id}",
            use_container_width=True,
            help="Open browser and fill form but do NOT submit",
        ):
            with st.spinner("Opening browser for dry run…"):
                rc, output = run_apply_subprocess(job_id, dry_run=True)
            st.info("Dry run complete." if rc == 0 else "Dry run ended.")
            st.session_state[f"apply_output_{job_id}"] = output

    # ── Output log ────────────────────────────────────────────────────────────
    output_key = f"apply_output_{job_id}"
    if output_key in st.session_state and st.session_state[output_key]:
        with st.expander("Pipeline output", expanded=True):
            st.code(st.session_state[output_key], language="text")

    # ── Resume generator (available for all jobs) ─────────────────────────────
    st.divider()
    _render_resume_only_panel(job_id)


def update_application_status(app_id: str, new_status: str) -> None:
    with get_session() as session:
        application = session.get(Application, app_id)
        if application:
            application.status = new_status
            session.add(application)
    st.cache_data.clear()


def update_application_notes(app_id: str, notes: str) -> None:
    with get_session() as session:
        application = session.get(Application, app_id)
        if application:
            application.notes = notes
            session.add(application)
    st.cache_data.clear()


# ── Sidebar navigation ────────────────────────────────────────────────────────

def render_sidebar() -> str:
    with st.sidebar:
        st.title("💼 JDASS")
        st.caption("Job Discovery & Application System")
        st.divider()
        page = st.radio(
            "Navigate",
            ["🔍 Jobs", "📋 Applications", "📊 Stats", "✅ Review"],
            label_visibility="collapsed",
        )
        st.divider()

        # Quick stats in sidebar
        stats = load_stats()
        col1, col2 = st.columns(2)
        col1.metric("Jobs", stats["total_jobs"])
        col2.metric("Applied", stats["total_apps"])
        col1.metric("Interviews", stats["interviews"])
        col2.metric("Offers", stats["offers"])

        st.divider()
        if st.button("🔄 Refresh data", use_container_width=True):
            st.cache_data.clear()
            st.rerun()

        st.caption("Avg score: **{}**  |  Top: **{}**".format(
            stats["avg_score"], stats["top_score"]
        ))

    return page


# ── Page 1: Jobs ──────────────────────────────────────────────────────────────

def page_jobs():
    st.header("🔍 Discovered Jobs")

    # ── Filters bar ──────────────────────────────────────────────────────────
    col1, col2, col3, col4, col5 = st.columns([2, 1, 1, 1, 1])
    search     = col1.text_input("Search company / title", placeholder="e.g. Stripe, Backend", label_visibility="collapsed")
    min_score  = col2.slider("Min score", 0, 100, 50, step=5)
    remote_chk = col3.checkbox("Remote only")
    h1b_chk    = col4.checkbox("H1B only")
    status_sel = col5.selectbox("Status", ["All", "DISCOVERED", "QUEUED", "APPLIED", "FILTERED_OUT"])

    df = load_jobs(
        min_score=min_score,
        remote_only=remote_chk,
        h1b_only=h1b_chk,
        status_filter=status_sel if status_sel != "All" else None,
        search=search,
    )

    if df.empty:
        st.info("No jobs match your filters.")
        return

    st.caption(f"Showing **{len(df)}** jobs")

    # ── Job table ─────────────────────────────────────────────────────────────
    display_cols = ["Score", "Company", "Title", "Location", "Seniority", "Remote", "H1B", "Tech", "Source", "Status", "Discovered"]
    display_df = df[display_cols].copy()
    display_df["Score"] = display_df["Score"].apply(lambda s: score_badge(s) if pd.notna(s) else "—")

    selected = st.dataframe(
        display_df,
        use_container_width=True,
        height=400,
        selection_mode="single-row",
        on_select="rerun",
        hide_index=True,
    )

    # ── Job detail panel ──────────────────────────────────────────────────────
    sel_rows = selected.get("selection", {}).get("rows", [])
    if sel_rows:
        idx = sel_rows[0]
        job_row = df.iloc[idx]

        st.divider()
        col_left, col_right = st.columns([2, 1])

        with col_left:
            st.subheader(f"{job_row['Company']} — {job_row['Title']}")
            st.caption(f"📍 {job_row['Location']}  |  🏷 {job_row['Source']}  |  🔗 [{job_row['URL']}]({job_row['URL']})")

            with st.expander("📄 Job Description", expanded=False):
                # Load full description
                with get_session() as session:
                    full_job = session.get(Job, job_row["id"])
                st.text(full_job.description if full_job else job_row["Description"])

        with col_right:
            # Score breakdown
            with get_session() as session:
                full_job = session.get(Job, job_row["id"])

            if full_job and full_job.score_breakdown:
                bd = json.loads(full_job.score_breakdown)
                st.metric("Match Score", bd.get("total", "—"))
                st.write("**Score Breakdown**")
                breakdown_items = {
                    "Title match":    bd.get("title_match", 0),
                    "Tech overlap":   bd.get("tech_overlap", 0),
                    "Seniority":      bd.get("seniority_match", 0),
                    "Location":       bd.get("location_bonus", 0),
                    "H1B":            bd.get("h1b_bonus", 0),
                    "Recency":        bd.get("recency_bonus", 0),
                }
                for label, val in breakdown_items.items():
                    st.progress(val / 35, text=f"{label}: {val}")

                if bd.get("matched_tech"):
                    st.write("**Matched tech:** " + ", ".join(f"`{t}`" for t in bd["matched_tech"]))

            st.write("**Technologies**")
            if full_job:
                all_tech = full_job.get_technologies() + full_job.get_frameworks() + full_job.get_cloud_platforms() + full_job.get_databases()
                if all_tech:
                    st.write(" ".join(f"`{t}`" for t in all_tech[:12]))
                else:
                    st.caption("None extracted")

            st.link_button("Open Job Posting ↗", job_row["URL"], use_container_width=True)

            st.divider()
            _render_apply_panel(job_row["id"], job_row["Status"], job_row["URL"])


# ── Page 2: Applications ──────────────────────────────────────────────────────

def page_applications():
    st.header("📋 Applications")

    df = load_applications()

    if df.empty:
        st.info("No applications yet. Run the application pipeline to get started.")
        return

    # ── Status filter tabs ────────────────────────────────────────────────────
    all_statuses = ["All"] + sorted(df["Status"].unique().tolist())
    tabs = st.tabs(all_statuses)

    for tab, status in zip(tabs, all_statuses):
        with tab:
            filtered = df if status == "All" else df[df["Status"] == status]
            st.caption(f"{len(filtered)} applications")

            if filtered.empty:
                st.info(f"No applications with status '{status}'.")
                continue

            display_cols = ["Score", "Company", "Title", "Location", "Applied", "Status", "ATS", "Resume"]
            display_df = filtered[display_cols].copy()
            display_df["Score"] = display_df["Score"].apply(lambda s: score_badge(s) if pd.notna(s) else "—")

            selected = st.dataframe(
                display_df,
                use_container_width=True,
                height=300,
                selection_mode="single-row",
                on_select="rerun",
                hide_index=True,
                key=f"apps_table_{status}",
            )

            sel_rows = selected.get("selection", {}).get("rows", [])
            if sel_rows:
                idx = sel_rows[0]
                app_row = filtered.iloc[idx]

                st.divider()
                col1, col2, col3 = st.columns([2, 1, 1])

                with col1:
                    st.subheader(f"{app_row['Company']} — {app_row['Title']}")
                    st.caption(f"📍 {app_row['Location']}  |  Applied: {app_row['Applied']}")
                    st.link_button("Open Job Posting ↗", app_row["URL"])

                with col2:
                    st.write("**Update Status**")
                    new_status = st.selectbox(
                        "Status",
                        [s.value for s in ApplicationStatus],
                        index=[s.value for s in ApplicationStatus].index(app_row["Status"])
                        if app_row["Status"] in [s.value for s in ApplicationStatus] else 0,
                        key=f"status_sel_{app_row['id']}",
                        label_visibility="collapsed",
                    )
                    if st.button("Save status", key=f"save_status_{app_row['id']}", use_container_width=True):
                        update_application_status(app_row["id"], new_status)
                        st.success("Status updated!")
                        st.rerun()

                with col3:
                    st.write("**Notes**")
                    notes = st.text_area(
                        "Notes",
                        value=app_row["Notes"],
                        key=f"notes_{app_row['id']}",
                        height=80,
                        label_visibility="collapsed",
                    )
                    if st.button("Save notes", key=f"save_notes_{app_row['id']}", use_container_width=True):
                        update_application_notes(app_row["id"], notes)
                        st.success("Notes saved!")

                # Resume viewer
                if app_row.get("resume_path") and Path(str(app_row["resume_path"])).exists():
                    st.write("**Tailored Resume**")
                    resume_path = Path(str(app_row["resume_path"]))
                    with open(resume_path, "rb") as f:
                        st.download_button(
                            "⬇ Download Resume PDF",
                            data=f,
                            file_name=resume_path.name,
                            mime="application/pdf",
                            use_container_width=True,
                        )


# ── Page 3: Stats ─────────────────────────────────────────────────────────────

def page_stats():
    import plotly.express as px
    import plotly.graph_objects as go

    st.header("📊 Stats & Insights")
    stats = load_stats()

    # ── Top metrics ───────────────────────────────────────────────────────────
    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Jobs Discovered", stats["total_jobs"])
    col2.metric("Applications",    stats["total_apps"])
    col3.metric("Interviews",      stats["interviews"])
    col4.metric("Offers",          stats["offers"])
    col5.metric("Avg Score",       stats["avg_score"])

    st.divider()

    row1_col1, row1_col2 = st.columns(2)

    # ── Score distribution ────────────────────────────────────────────────────
    with row1_col1:
        st.subheader("Score Distribution")
        if stats["scores"]:
            fig = px.histogram(
                x=stats["scores"],
                nbins=20,
                labels={"x": "Match Score", "y": "Jobs"},
                color_discrete_sequence=["#0d6efd"],
            )
            fig.update_layout(
                margin=dict(l=0, r=0, t=20, b=0),
                height=280,
                showlegend=False,
            )
            fig.add_vline(x=stats["avg_score"], line_dash="dash", line_color="red",
                          annotation_text=f"Avg {stats['avg_score']}")
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("No scored jobs yet.")

    # ── Source breakdown ──────────────────────────────────────────────────────
    with row1_col2:
        st.subheader("Jobs by Source")
        if stats["source_counts"]:
            fig = px.pie(
                names=list(stats["source_counts"].keys()),
                values=list(stats["source_counts"].values()),
                color_discrete_sequence=px.colors.qualitative.Set2,
                hole=0.4,
            )
            fig.update_layout(margin=dict(l=0, r=0, t=20, b=0), height=280)
            st.plotly_chart(fig, use_container_width=True)

    row2_col1, row2_col2 = st.columns(2)

    # ── Tech frequency ────────────────────────────────────────────────────────
    with row2_col1:
        st.subheader("Top Technologies in Job Postings")
        if stats["tech_counter"]:
            tech_df = pd.DataFrame(
                list(stats["tech_counter"].items()),
                columns=["Technology", "Count"],
            ).sort_values("Count", ascending=True).tail(15)
            fig = px.bar(
                tech_df,
                x="Count",
                y="Technology",
                orientation="h",
                color="Count",
                color_continuous_scale="Blues",
            )
            fig.update_layout(
                margin=dict(l=0, r=0, t=20, b=0),
                height=380,
                showlegend=False,
                coloraxis_showscale=False,
            )
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("No tech data yet. Run `make parse-jobs`.")

    # ── Application status funnel ─────────────────────────────────────────────
    with row2_col2:
        st.subheader("Application Pipeline")
        if stats["app_status_counts"]:
            funnel_order = [
                ApplicationStatus.APPLIED,
                ApplicationStatus.RECRUITER_CONTACTED,
                ApplicationStatus.INTERVIEW,
                ApplicationStatus.OFFER,
            ]
            funnel_data = [
                {"Stage": s, "Count": stats["app_status_counts"].get(s, 0)}
                for s in funnel_order
            ]
            fig = go.Figure(go.Funnel(
                y=[d["Stage"] for d in funnel_data],
                x=[d["Count"] for d in funnel_data],
                textinfo="value+percent initial",
                marker_color=["#198754", "#0dcaf0", "#0d6efd", "#ffc107"],
            ))
            fig.update_layout(margin=dict(l=0, r=0, t=20, b=0), height=280)
            st.plotly_chart(fig, use_container_width=True)

            # Rejection rate
            rejected = stats["app_status_counts"].get(ApplicationStatus.REJECTED, 0)
            total = stats["total_apps"]
            if total:
                st.caption(f"Rejection rate: **{rejected}/{total}** ({round(rejected/total*100)}%)")
        else:
            st.info("No applications yet.")

    # ── Applications over time ────────────────────────────────────────────────
    if stats["app_by_week"]:
        st.subheader("Applications by Week")
        week_df = pd.DataFrame(
            sorted(stats["app_by_week"].items()),
            columns=["Week", "Applications"],
        )
        fig = px.bar(week_df, x="Week", y="Applications", color_discrete_sequence=["#198754"])
        fig.update_layout(margin=dict(l=0, r=0, t=20, b=0), height=200)
        st.plotly_chart(fig, use_container_width=True)


# ── Main router ────────────────────────────────────────────────────────────────

def page_review():
    """
    Review page — shows LLM-guessed form field answers from past applications.
    User can edit values and confirm them; confirmed answers are written to
    configs/form_answers.yaml so future applications reuse them automatically.
    """
    st.header("✅ Form Answer Review")
    st.caption(
        "These answers were filled by the LLM during automation. "
        "Review, edit, and confirm them — confirmed answers will be reused for future applications."
    )

    # ── Import form_answers helper ─────────────────────────────────────────────
    import sys as _sys
    if str(ROOT) not in _sys.path:
        _sys.path.insert(0, str(ROOT))
    from core import form_answers as fa

    # ── Tab 1: Application-level review ───────────────────────────────────────
    tab_apps, tab_global = st.tabs(["By Application", "All Saved Answers"])

    with tab_apps:
        # Load applications that have LLM guesses
        with get_session() as session:
            apps_with_guesses = session.exec(
                select(Application).where(Application.form_guesses.isnot(None))
            ).all()

        if not apps_with_guesses:
            st.info("No applications with LLM-guessed answers yet. Run the automation to populate this page.")
        else:
            # Build display list with job info
            rows = []
            for app in apps_with_guesses:
                guesses = json.loads(app.form_guesses or "[]")
                unconfirmed = sum(1 for g in guesses if not g.get("confirmed"))
                with get_session() as session:
                    job = session.get(Job, app.job_id)
                rows.append({
                    "app_id": app.id,
                    "Company": job.company if job else "—",
                    "Title": job.title if job else "—",
                    "Applied": app.applied_at.strftime("%Y-%m-%d") if app.applied_at else "—",
                    "Guesses": len(guesses),
                    "Pending": unconfirmed,
                    "guesses": guesses,
                })

            # Show selector
            options = [
                f"{r['Company']} — {r['Title']} ({r['Applied']})  [{r['Pending']} pending]"
                for r in rows
            ]
            selected_idx = st.selectbox(
                "Select application to review",
                range(len(options)),
                format_func=lambda i: options[i],
            )
            selected_row = rows[selected_idx]
            guesses = selected_row["guesses"]

            if not guesses:
                st.info("No guesses recorded for this application.")
            else:
                st.divider()
                st.subheader(f"{selected_row['Company']} — {selected_row['Title']}")

                # Build editable form for each guess
                edited: list[dict] = []
                for i, guess in enumerate(guesses):
                    label = guess.get("label", "")
                    current_value = guess.get("value", "")
                    options_list = guess.get("options", [])
                    source = guess.get("source", "llm")
                    confirmed = guess.get("confirmed", False)

                    col_label, col_input, col_status = st.columns([3, 3, 1])
                    with col_label:
                        st.markdown(f"**{label}**")
                        st.caption(f"source: {source}")

                    with col_input:
                        if options_list:
                            # Select field — show dropdown with known options
                            all_opts = options_list if current_value in options_list else [current_value] + options_list
                            try:
                                default_i = all_opts.index(current_value)
                            except ValueError:
                                default_i = 0
                            new_val = st.selectbox(
                                "value",
                                all_opts,
                                index=default_i,
                                key=f"review_sel_{selected_row['app_id']}_{i}",
                                label_visibility="collapsed",
                            )
                        else:
                            new_val = st.text_input(
                                "value",
                                value=current_value,
                                key=f"review_txt_{selected_row['app_id']}_{i}",
                                label_visibility="collapsed",
                            )

                    with col_status:
                        if confirmed:
                            st.markdown("✅")
                        else:
                            st.markdown("⏳")

                    edited.append({"label": label, "value": new_val})

                st.divider()
                if st.button("✅ Confirm All & Save", type="primary", key=f"confirm_{selected_row['app_id']}"):
                    fa.confirm_many(edited)
                    # Update the Application record — mark all guesses as confirmed
                    confirmed_guesses = [
                        {**g, "value": e["value"], "confirmed": True}
                        for g, e in zip(guesses, edited)
                    ]
                    with get_session() as session:
                        app_rec = session.get(Application, selected_row["app_id"])
                        if app_rec:
                            app_rec.form_guesses = json.dumps(confirmed_guesses)
                            session.add(app_rec)
                    st.success(f"Saved {len(edited)} answers to configs/form_answers.yaml")
                    st.rerun()

    with tab_global:
        st.subheader("All Saved Answers")
        st.caption("Edit any value and click Save to update configs/form_answers.yaml.")

        all_answers = fa.load()
        if not all_answers:
            st.info("No answers saved yet.")
        else:
            edited_global: list[dict] = []
            for label, entry in all_answers.items():
                val = entry.get("value", "")
                confirmed = entry.get("confirmed", False)
                source = entry.get("source", "")

                col1, col2, col3 = st.columns([3, 3, 1])
                with col1:
                    st.markdown(f"**{label}**")
                    st.caption(f"{'✅ confirmed' if confirmed else '⏳ pending'}  ·  {source}")
                with col2:
                    new_val = st.text_input(
                        "val",
                        value=val,
                        key=f"global_{label}",
                        label_visibility="collapsed",
                    )
                with col3:
                    st.write("")  # spacer
                edited_global.append({"label": label, "value": new_val})

            if st.button("💾 Save All Changes", key="save_global"):
                fa.confirm_many(edited_global)
                st.success("Saved.")
                st.rerun()


def main():
    page = render_sidebar()
    if page == "🔍 Jobs":
        page_jobs()
    elif page == "📋 Applications":
        page_applications()
    elif page == "📊 Stats":
        page_stats()
    elif page == "✅ Review":
        page_review()


# Streamlit executes the module top-to-bottom; this is the entrypoint.
main()
