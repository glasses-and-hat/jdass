"""
JDASS Streamlit Dashboard — redesigned.

Pages:
  🚀 Discover    — run discovery pipeline with configurable filters
  🔍 Jobs        — browse + filter discovered jobs, view JD + score breakdown
  📋 Applications — track applied jobs, update status, download resumes
  📊 Stats        — score distribution, tech frequency, pipeline funnel
  ✅ Review       — confirm/edit LLM-guessed form answers

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

# ── Custom CSS ────────────────────────────────────────────────────────────────
st.markdown("""
<style>
/* Tighten sidebar padding */
[data-testid="stSidebar"] { padding-top: 1rem; }
/* Metric cards */
[data-testid="stMetricValue"] { font-size: 1.5rem; }
/* Status badge helper */
.badge {
    display: inline-block;
    padding: 2px 10px;
    border-radius: 12px;
    font-size: 0.78em;
    font-weight: 600;
    color: #fff;
}
/* Score tier colors */
.score-red  { background: #dc3545; }
.score-yellow { background: #fd7e14; }
.score-green  { background: #198754; }
.score-star   { background: #0d6efd; }
/* Section dividers */
.section-title {
    font-size: 0.75em;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: #6c757d;
    margin: 0.5rem 0 0.25rem;
}
</style>
""", unsafe_allow_html=True)

# ── Init DB on startup ────────────────────────────────────────────────────────
init_db()


# ── Constants ─────────────────────────────────────────────────────────────────

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

SCORE_TIERS = [
    (85, "⭐", "#0d6efd"),
    (70, "🟢", "#198754"),
    (50, "🟡", "#fd7e14"),
    (0,  "🔴", "#dc3545"),
]


def score_badge(score: Optional[int]) -> str:
    if score is None:
        return "—"
    for threshold, emoji, _ in SCORE_TIERS:
        if score >= threshold:
            return f"{emoji} {score}"
    return str(score)


def status_pill(status: str) -> str:
    color = STATUS_COLORS.get(status, "#6c757d")
    return (
        f'<span class="badge" style="background:{color}">{status}</span>'
    )


# ── Data loaders ──────────────────────────────────────────────────────────────

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


def _load_settings() -> dict:
    """Load configs/settings.yaml — returns empty dict on error."""
    import yaml
    path = ROOT / "configs" / "settings.yaml"
    try:
        return yaml.safe_load(path.read_text()) or {}
    except Exception:
        return {}


# ── Apply helpers ──────────────────────────────────────────────────────────────

def queue_job_for_apply(job_id: str) -> bool:
    """Add job to task queue and set status to QUEUED. Returns True if newly queued."""
    with get_session() as session:
        existing = session.exec(
            select(TaskQueue)
            .where(TaskQueue.task_type == TaskType.APPLICATION)
            .where(TaskQueue.status == TaskStatus.PENDING)
        ).all()
        for t in existing:
            try:
                if json.loads(t.payload).get("job_id") == job_id:
                    return False
            except Exception:
                pass

    enqueue_task(TaskType.APPLICATION, {"job_id": job_id})
    _db_update_job_status(job_id, JobStatus.QUEUED)
    st.cache_data.clear()
    return True


def run_apply_subprocess(job_id: str, dry_run: bool = False) -> tuple[int, str]:
    """Run the application pipeline as a subprocess."""
    cmd = [sys.executable, "-m", "pipelines.application", "--job-id", job_id]
    if dry_run:
        cmd.append("--dry-run")
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300, cwd=str(ROOT),
        )
        output = (result.stdout or "") + (result.stderr or "")
        return result.returncode, output.strip()
    except subprocess.TimeoutExpired:
        return 1, "Timed out after 5 minutes."
    except Exception as exc:
        return 1, str(exc)


def run_tailor_subprocess(job_id: str) -> tuple[int, str, Optional[Path]]:
    """Run resume-only tailoring as a subprocess."""
    cmd = [sys.executable, "-m", "pipelines.application", "--job-id", job_id, "--resume-only"]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300, cwd=str(ROOT),
        )
        output = (result.stdout or "") + (result.stderr or "")
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


def run_discover_subprocess(
    max_age_days: Optional[float],
    require_h1b: bool,
    reject_no_sponsorship: bool,
    locations: list[str],
    use_llm: bool,
) -> tuple[int, str]:
    """Run the discovery pipeline as a subprocess with filter overrides."""
    cmd = [sys.executable, "-m", "pipelines.discovery"]
    if use_llm:
        cmd.append("--llm")
    if max_age_days is not None:
        cmd.extend(["--max-age-days", str(max_age_days)])
    if require_h1b:
        cmd.append("--require-h1b")
    if not reject_no_sponsorship:
        cmd.append("--no-reject-no-sponsorship")
    if locations:
        cmd.extend(["--locations", ",".join(locations)])
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=600, cwd=str(ROOT),
        )
        output = (result.stdout or "") + (result.stderr or "")
        return result.returncode, output.strip()
    except subprocess.TimeoutExpired:
        return 1, "Timed out after 10 minutes."
    except Exception as exc:
        return 1, str(exc)


def _is_manual_only_url(url: str) -> bool:
    return "news.ycombinator.com" in url or "ycombinator.com/item" in url


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


# ── Reusable UI widgets ───────────────────────────────────────────────────────

def _render_resume_only_panel(job_id: str) -> None:
    """Generate-resume panel for any job."""
    output_key = f"tailor_output_{job_id}"
    pdf_key    = f"tailor_pdf_{job_id}"

    if st.button(
        "📄 Generate Tailored Resume",
        key=f"tailor_{job_id}",
        type="secondary",
        use_container_width=True,
        help="Tailor resume to this JD and produce a PDF — no browser opened",
    ):
        with st.spinner("Tailoring resume… (may take ~30 s with LLM)"):
            rc, output, pdf_path = run_tailor_subprocess(job_id)
        st.session_state[output_key] = output
        st.session_state[pdf_key] = str(pdf_path) if pdf_path else None
        if rc == 0 and pdf_path:
            st.success("Resume ready!")
        else:
            st.error("Tailoring failed — see output below.")

    saved_pdf = st.session_state.get(pdf_key)
    if saved_pdf:
        pdf_file = Path(saved_pdf)
        if pdf_file.exists():
            with open(pdf_file, "rb") as fh:
                st.download_button(
                    label=f"⬇ Download {pdf_file.name}",
                    data=fh.read(),
                    file_name=pdf_file.name,
                    mime="application/pdf"
                    if pdf_file.suffix == ".pdf"
                    else "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    key=f"dl_{job_id}",
                    use_container_width=True,
                )

    if output_key in st.session_state and st.session_state[output_key]:
        with st.expander("Pipeline output", expanded=False):
            st.code(st.session_state[output_key], language="text")


def _render_apply_panel(job_id: str, job_status: str, job_url: str) -> None:
    """Apply actions panel inside the job detail view."""
    color = STATUS_COLORS.get(job_status, "#6c757d")
    st.markdown(status_pill(job_status), unsafe_allow_html=True)
    st.write("")

    # HN / unsupported sources
    if _is_manual_only_url(job_url):
        st.info(
            "Sourced from Hacker News — auto-apply not supported. "
            "Apply directly on the company's site.",
            icon="ℹ️",
        )
        _render_resume_only_panel(job_id)
        return

    already_applied = job_status == "APPLIED"
    col1, col2, col3 = st.columns(3)

    with col1:
        if st.button(
            "➕ Queue",
            key=f"queue_{job_id}",
            use_container_width=True,
            disabled=already_applied,
            help="Add to task queue for `make apply-queue`",
        ):
            added = queue_job_for_apply(job_id)
            st.success("Queued!") if added else st.info("Already queued.")
            if added:
                st.rerun()

    with col2:
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

    with col3:
        if st.button(
            "🔍 Dry Run",
            key=f"dry_{job_id}",
            use_container_width=True,
            help="Fill form in browser but do NOT submit",
        ):
            with st.spinner("Opening browser…"):
                rc, output = run_apply_subprocess(job_id, dry_run=True)
            st.info("Dry run complete." if rc == 0 else "Dry run ended.")
            st.session_state[f"apply_output_{job_id}"] = output

    output_key = f"apply_output_{job_id}"
    if output_key in st.session_state and st.session_state[output_key]:
        with st.expander("Pipeline output", expanded=True):
            st.code(st.session_state[output_key], language="text")

    st.divider()
    _render_resume_only_panel(job_id)


# ── Sidebar ────────────────────────────────────────────────────────────────────

def render_sidebar() -> str:
    with st.sidebar:
        st.markdown("## 💼 JDASS")
        st.caption("Job Discovery & Application System")
        st.divider()

        page = st.radio(
            "nav",
            ["🚀 Discover", "🔍 Jobs", "📋 Applications", "📊 Stats", "✅ Review"],
            label_visibility="collapsed",
        )
        st.divider()

        # Quick stats
        stats = load_stats()
        c1, c2 = st.columns(2)
        c1.metric("Jobs", stats["total_jobs"])
        c2.metric("Applied", stats["total_apps"])
        c1.metric("Interviews", stats["interviews"])
        c2.metric("Offers", stats["offers"])

        st.divider()
        if st.button("🔄 Refresh", use_container_width=True):
            st.cache_data.clear()
            st.rerun()

        st.caption(
            f"Avg score: **{stats['avg_score']}**  ·  "
            f"Top: **{stats['top_score']}**"
        )
    return page


# ── Page: Discover ─────────────────────────────────────────────────────────────

def page_discover():
    st.header("🚀 Discover Jobs")
    st.caption("Configure filters and run the discovery pipeline to scrape new job postings.")

    # Load defaults from settings.yaml
    cfg = _load_settings()
    filters = cfg.get("filters", {})

    # ── Filter configuration ───────────────────────────────────────────────────
    st.subheader("Filter Settings")
    st.caption("These override `configs/settings.yaml` for this run only.")

    col1, col2, col3 = st.columns(3)

    with col1:
        st.markdown('<p class="section-title">Recency</p>', unsafe_allow_html=True)
        default_age = filters.get("max_age_days", 3.0)
        age_enabled = st.toggle(
            "Limit by posting age",
            value=default_age is not None and default_age > 0,
            key="disc_age_enabled",
        )
        max_age = None
        if age_enabled:
            max_age = st.slider(
                "Max age (days)",
                min_value=0.5,
                max_value=30.0,
                value=float(default_age) if default_age else 7.0,
                step=0.5,
                format="%.1f days",
                help="Only keep jobs posted within this many days",
            )

        st.markdown('<p class="section-title">Sponsorship</p>', unsafe_allow_html=True)
        require_h1b = st.toggle(
            "Require H1B mention",
            value=filters.get("require_h1b", False),
            help="Only keep jobs that explicitly mention H1B sponsorship",
        )
        reject_no_sponsor = st.toggle(
            "Reject 'no sponsorship' jobs",
            value=filters.get("reject_no_sponsorship", True),
            help="Filter out listings that explicitly say they won't sponsor",
        )

    with col2:
        st.markdown('<p class="section-title">Location Keywords</p>', unsafe_allow_html=True)
        default_locs = filters.get("allowed_locations", ["chicago", "remote", "usa", "anywhere"])
        loc_text = st.text_area(
            "Allowed locations (one per line)",
            value="\n".join(default_locs),
            height=140,
            help="A job passes if its location/description contains any of these keywords (case-insensitive)",
            label_visibility="collapsed",
        )
        locations = [loc.strip() for loc in loc_text.splitlines() if loc.strip()]

    with col3:
        st.markdown('<p class="section-title">Pipeline Options</p>', unsafe_allow_html=True)
        use_llm = st.toggle(
            "Use LLM for JD parsing",
            value=False,
            help="Enables richer tech extraction. Requires Ollama running.",
        )
        st.write("")
        st.info(
            "**Sources** are configured in `configs/sources.yaml`. "
            "Edit that file to add/remove companies.",
            icon="📂",
        )

    st.divider()

    # ── Summary of active filters ──────────────────────────────────────────────
    filter_summary = []
    if max_age:
        filter_summary.append(f"📅 Posted ≤ {max_age:.1f} days ago")
    if require_h1b:
        filter_summary.append("🛂 H1B required")
    if reject_no_sponsor:
        filter_summary.append("✅ Reject 'no sponsorship'")
    if locations:
        filter_summary.append(f"📍 Locations: {', '.join(locations[:4])}")
    if use_llm:
        filter_summary.append("🤖 LLM parsing")

    if filter_summary:
        st.caption("  ·  ".join(filter_summary))

    # ── Run button ─────────────────────────────────────────────────────────────
    run_col, _ = st.columns([1, 3])
    with run_col:
        run_clicked = st.button(
            "🚀 Run Discovery",
            type="primary",
            use_container_width=True,
            help="Scrape all configured sources and save new jobs to the database",
        )

    if run_clicked:
        with st.spinner("Running discovery pipeline… this may take a minute."):
            rc, output = run_discover_subprocess(
                max_age_days=max_age,
                require_h1b=require_h1b,
                reject_no_sponsorship=reject_no_sponsor,
                locations=locations,
                use_llm=use_llm,
            )
        st.cache_data.clear()

        if rc == 0:
            # Parse summary numbers from output
            saved_n = 0
            for line in output.splitlines():
                if "saved=" in line:
                    import re
                    m = re.search(r"saved=(\d+)", line)
                    if m:
                        saved_n = int(m.group(1))
            st.success(f"Discovery complete! **{saved_n}** new jobs saved.")
        else:
            st.error("Discovery pipeline exited with errors — see output below.")

        with st.expander("Pipeline output", expanded=(rc != 0)):
            st.code(output, language="text")


# ── Page: Jobs ─────────────────────────────────────────────────────────────────

def page_jobs():
    st.header("🔍 Jobs")

    # ── Filter bar ────────────────────────────────────────────────────────────
    with st.container():
        fc1, fc2, fc3, fc4, fc5 = st.columns([3, 1, 1, 1, 1])
        search     = fc1.text_input("", placeholder="🔎  Search company or title…", label_visibility="collapsed")
        min_score  = fc2.slider("Min score", 0, 100, 50, step=5, label_visibility="collapsed",
                                help="Minimum match score")
        remote_chk = fc3.checkbox("Remote", help="Remote-eligible only")
        h1b_chk    = fc4.checkbox("H1B", help="Jobs that mention H1B sponsorship")
        status_sel = fc5.selectbox(
            "Status",
            ["All", "DISCOVERED", "QUEUED", "APPLIED", "MANUAL_REVIEW", "FAILED_AUTO_APPLY", "FILTERED_OUT"],
            label_visibility="collapsed",
        )

    df = load_jobs(
        min_score=min_score,
        remote_only=remote_chk,
        h1b_only=h1b_chk,
        status_filter=status_sel if status_sel != "All" else None,
        search=search,
    )

    if df.empty:
        st.info("No jobs match your filters. Try running Discovery or adjusting the filters above.")
        return

    # ── Status summary chips ───────────────────────────────────────────────────
    status_counts = df["Status"].value_counts().to_dict()
    chips = []
    for s, cnt in status_counts.items():
        color = STATUS_COLORS.get(s, "#6c757d")
        chips.append(f'<span class="badge" style="background:{color}">{s} {cnt}</span>')
    st.markdown(
        f"**{len(df)}** jobs  &nbsp;&nbsp; " + "  ".join(chips),
        unsafe_allow_html=True,
    )

    # ── Job table ─────────────────────────────────────────────────────────────
    display_cols = ["Score", "Company", "Title", "Location", "Seniority",
                    "Remote", "H1B", "Source", "Status", "Discovered"]
    display_df = df[display_cols].copy()
    display_df["Score"] = display_df["Score"].apply(
        lambda s: score_badge(s) if pd.notna(s) else "—"
    )

    selected = st.dataframe(
        display_df,
        use_container_width=True,
        height=380,
        selection_mode="single-row",
        on_select="rerun",
        hide_index=True,
    )

    # ── Job detail panel ──────────────────────────────────────────────────────
    sel_rows = selected.get("selection", {}).get("rows", [])
    if not sel_rows:
        st.caption("Select a job to view details and apply.")
        return

    idx = sel_rows[0]
    job_row = df.iloc[idx]

    st.divider()
    detail_left, detail_right = st.columns([3, 2])

    with detail_left:
        score_val = job_row["Score"]
        score_str = score_badge(score_val) if pd.notna(score_val) else "—"
        st.subheader(f"{job_row['Company']} — {job_row['Title']}")
        st.caption(
            f"📍 {job_row['Location']}  ·  "
            f"🏷 {job_row['Source']}  ·  "
            f"Score: **{score_str}**  ·  "
            f"Seniority: **{job_row['Seniority']}**"
        )
        st.link_button("Open Job Posting ↗", job_row["URL"])

        # Full JD
        with st.expander("📄 Job Description", expanded=False):
            with get_session() as session:
                full_job = session.get(Job, job_row["id"])
            st.text(full_job.description if full_job else job_row["Description"])

        # Tech tags
        with get_session() as session:
            full_job = session.get(Job, job_row["id"])
        if full_job:
            all_tech = (
                full_job.get_technologies()
                + full_job.get_frameworks()
                + full_job.get_cloud_platforms()
                + full_job.get_databases()
            )
            if all_tech:
                st.write("**Technologies**")
                st.write("  ".join(f"`{t}`" for t in all_tech[:15]))

    with detail_right:
        # Score breakdown card
        if full_job and full_job.score_breakdown:
            bd = json.loads(full_job.score_breakdown)
            st.metric("Match Score", bd.get("total", "—"))
            breakdown_items = {
                "Title match":  bd.get("title_match", 0),
                "Tech overlap": bd.get("tech_overlap", 0),
                "Seniority":    bd.get("seniority_match", 0),
                "Location":     bd.get("location_bonus", 0),
                "H1B":          bd.get("h1b_bonus", 0),
                "Recency":      bd.get("recency_bonus", 0),
            }
            for label, val in breakdown_items.items():
                st.progress(min(val / 35, 1.0), text=f"{label}: {val}")
            if bd.get("matched_tech"):
                st.caption("Matched: " + ", ".join(f"`{t}`" for t in bd["matched_tech"]))

        st.divider()
        _render_apply_panel(job_row["id"], job_row["Status"], job_row["URL"])


# ── Page: Applications ────────────────────────────────────────────────────────

def page_applications():
    st.header("📋 Applications")

    df = load_applications()
    if df.empty:
        st.info("No applications yet. Use **Apply Now** on a job to get started.")
        return

    # ── Status tabs ───────────────────────────────────────────────────────────
    all_statuses = ["All"] + sorted(df["Status"].unique().tolist())
    tabs = st.tabs(all_statuses)

    for tab, status in zip(tabs, all_statuses):
        with tab:
            filtered = df if status == "All" else df[df["Status"] == status]
            st.caption(f"{len(filtered)} application{'s' if len(filtered) != 1 else ''}")

            if filtered.empty:
                st.info(f"No applications with status '{status}'.")
                continue

            display_cols = ["Score", "Company", "Title", "Location",
                            "Applied", "Status", "ATS", "Resume"]
            display_df = filtered[display_cols].copy()
            display_df["Score"] = display_df["Score"].apply(
                lambda s: score_badge(s) if pd.notna(s) else "—"
            )

            selected = st.dataframe(
                display_df,
                use_container_width=True,
                height=280,
                selection_mode="single-row",
                on_select="rerun",
                hide_index=True,
                key=f"apps_table_{status}",
            )

            sel_rows = selected.get("selection", {}).get("rows", [])
            if not sel_rows:
                continue

            idx = sel_rows[0]
            app_row = filtered.iloc[idx]

            st.divider()
            hdr_col, status_col, notes_col = st.columns([3, 1, 2])

            with hdr_col:
                score_str = score_badge(app_row["Score"]) if pd.notna(app_row["Score"]) else "—"
                st.subheader(f"{app_row['Company']} — {app_row['Title']}")
                st.caption(
                    f"📍 {app_row['Location']}  ·  Applied: {app_row['Applied']}  ·  "
                    f"Score: **{score_str}**  ·  ATS: {app_row['ATS']}"
                )
                st.link_button("Open Job Posting ↗", app_row["URL"])

                if app_row.get("resume_path") and Path(str(app_row["resume_path"])).exists():
                    resume_path = Path(str(app_row["resume_path"]))
                    with open(resume_path, "rb") as f:
                        st.download_button(
                            "⬇ Download Resume PDF",
                            data=f,
                            file_name=resume_path.name,
                            mime="application/pdf",
                        )

            with status_col:
                st.markdown("**Status**")
                status_vals = [s.value for s in ApplicationStatus]
                cur_idx = status_vals.index(app_row["Status"]) if app_row["Status"] in status_vals else 0
                new_status = st.selectbox(
                    "Status",
                    status_vals,
                    index=cur_idx,
                    key=f"status_sel_{app_row['id']}",
                    label_visibility="collapsed",
                )
                if st.button("Save", key=f"save_status_{app_row['id']}", use_container_width=True):
                    update_application_status(app_row["id"], new_status)
                    st.success("Updated!")
                    st.rerun()

            with notes_col:
                st.markdown("**Notes**")
                notes = st.text_area(
                    "Notes",
                    value=app_row["Notes"],
                    key=f"notes_{app_row['id']}",
                    height=100,
                    label_visibility="collapsed",
                )
                if st.button("Save notes", key=f"save_notes_{app_row['id']}", use_container_width=True):
                    update_application_notes(app_row["id"], notes)
                    st.success("Saved!")


# ── Page: Stats ───────────────────────────────────────────────────────────────

def page_stats():
    import plotly.express as px
    import plotly.graph_objects as go

    st.header("📊 Stats & Insights")
    stats = load_stats()

    # ── Top-line metrics ──────────────────────────────────────────────────────
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Jobs Discovered", stats["total_jobs"])
    m2.metric("Applications",    stats["total_apps"])
    m3.metric("Interviews",      stats["interviews"])
    m4.metric("Offers",          stats["offers"])
    m5.metric("Avg Score",       stats["avg_score"])

    st.divider()
    row1_l, row1_r = st.columns(2)

    with row1_l:
        st.subheader("Score Distribution")
        if stats["scores"]:
            fig = px.histogram(
                x=stats["scores"], nbins=20,
                labels={"x": "Match Score", "y": "Jobs"},
                color_discrete_sequence=["#0d6efd"],
            )
            fig.add_vline(
                x=stats["avg_score"], line_dash="dash", line_color="#dc3545",
                annotation_text=f"Avg {stats['avg_score']}",
            )
            fig.update_layout(
                margin=dict(l=0, r=0, t=20, b=0), height=280,
                showlegend=False, plot_bgcolor="rgba(0,0,0,0)",
                paper_bgcolor="rgba(0,0,0,0)",
            )
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("No scored jobs yet. Run `make parse-jobs`.")

    with row1_r:
        st.subheader("Jobs by Source")
        if stats["source_counts"]:
            fig = px.pie(
                names=list(stats["source_counts"].keys()),
                values=list(stats["source_counts"].values()),
                color_discrete_sequence=px.colors.qualitative.Set2,
                hole=0.45,
            )
            fig.update_layout(
                margin=dict(l=0, r=0, t=20, b=0), height=280,
                paper_bgcolor="rgba(0,0,0,0)",
            )
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("No jobs yet.")

    row2_l, row2_r = st.columns(2)

    with row2_l:
        st.subheader("Top Technologies")
        if stats["tech_counter"]:
            tech_df = pd.DataFrame(
                list(stats["tech_counter"].items()),
                columns=["Technology", "Count"],
            ).sort_values("Count", ascending=True).tail(15)
            fig = px.bar(
                tech_df, x="Count", y="Technology",
                orientation="h", color="Count",
                color_continuous_scale="Blues",
            )
            fig.update_layout(
                margin=dict(l=0, r=0, t=20, b=0), height=380,
                showlegend=False, coloraxis_showscale=False,
                plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
            )
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("No tech data yet.")

    with row2_r:
        st.subheader("Application Funnel")
        if stats["app_status_counts"]:
            funnel_order = [
                ApplicationStatus.APPLIED,
                ApplicationStatus.RECRUITER_CONTACTED,
                ApplicationStatus.INTERVIEW,
                ApplicationStatus.OFFER,
            ]
            fig = go.Figure(go.Funnel(
                y=[s for s in funnel_order],
                x=[stats["app_status_counts"].get(s, 0) for s in funnel_order],
                textinfo="value+percent initial",
                marker_color=["#198754", "#0dcaf0", "#0d6efd", "#ffc107"],
            ))
            fig.update_layout(
                margin=dict(l=0, r=0, t=20, b=0), height=280,
                paper_bgcolor="rgba(0,0,0,0)",
            )
            st.plotly_chart(fig, use_container_width=True)

            rejected = stats["app_status_counts"].get(ApplicationStatus.REJECTED, 0)
            total = stats["total_apps"]
            if total:
                st.caption(
                    f"Rejection rate: **{rejected}/{total}** "
                    f"({round(rejected / total * 100)}%)"
                )
        else:
            st.info("No applications yet.")

    if stats["app_by_week"]:
        st.subheader("Applications by Week")
        week_df = pd.DataFrame(
            sorted(stats["app_by_week"].items()),
            columns=["Week", "Applications"],
        )
        fig = px.bar(
            week_df, x="Week", y="Applications",
            color_discrete_sequence=["#198754"],
        )
        fig.update_layout(
            margin=dict(l=0, r=0, t=20, b=0), height=200,
            plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        )
        st.plotly_chart(fig, use_container_width=True)


# ── Page: Review ──────────────────────────────────────────────────────────────

def page_review():
    """Review + confirm LLM-guessed form field answers."""
    st.header("✅ Form Answer Review")
    st.caption(
        "Answers filled by the LLM during automation. "
        "Review and confirm — confirmed answers are reused for future applications."
    )

    from core import form_answers as fa

    tab_apps, tab_global = st.tabs(["By Application", "All Saved Answers"])

    # ── Tab 1: Per-application review ─────────────────────────────────────────
    with tab_apps:
        with get_session() as session:
            apps_with_guesses = session.exec(
                select(Application).where(Application.form_guesses.isnot(None))
            ).all()

        if not apps_with_guesses:
            st.info("No LLM-guessed answers yet. Run the automation to populate this.")
        else:
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

            options = [
                f"{r['Company']} — {r['Title']} ({r['Applied']})  [{r['Pending']} pending]"
                for r in rows
            ]
            selected_idx = st.selectbox(
                "Select application",
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

                edited: list[dict] = []
                for i, guess in enumerate(guesses):
                    label        = guess.get("label", "")
                    current_val  = guess.get("value", "")
                    options_list = guess.get("options", [])
                    source       = guess.get("source", "llm")
                    confirmed    = guess.get("confirmed", False)

                    col_label, col_input, col_status = st.columns([3, 3, 1])
                    with col_label:
                        st.markdown(f"**{label}**")
                        st.caption(f"source: {source}")
                    with col_input:
                        if options_list:
                            all_opts = (
                                options_list
                                if current_val in options_list
                                else [current_val] + options_list
                            )
                            new_val = st.selectbox(
                                "value", all_opts,
                                index=all_opts.index(current_val) if current_val in all_opts else 0,
                                key=f"rev_sel_{selected_row['app_id']}_{i}",
                                label_visibility="collapsed",
                            )
                        else:
                            new_val = st.text_input(
                                "value", value=current_val,
                                key=f"rev_txt_{selected_row['app_id']}_{i}",
                                label_visibility="collapsed",
                            )
                    with col_status:
                        st.markdown("✅" if confirmed else "⏳")

                    edited.append({"label": label, "value": new_val})

                st.divider()
                if st.button(
                    "✅ Confirm All & Save",
                    type="primary",
                    key=f"confirm_{selected_row['app_id']}",
                ):
                    fa.confirm_many(edited)
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

    # ── Tab 2: Global answers store ────────────────────────────────────────────
    with tab_global:
        st.subheader("All Saved Answers")
        st.caption("Edit any value and click Save to update `configs/form_answers.yaml`.")

        all_answers = fa.load()
        if not all_answers:
            st.info("No answers saved yet.")
        else:
            edited_global: list[dict] = []
            for label, entry in all_answers.items():
                val       = entry.get("value", "")
                confirmed = entry.get("confirmed", False)
                source    = entry.get("source", "")

                col1, col2, col3 = st.columns([3, 3, 1])
                with col1:
                    st.markdown(f"**{label}**")
                    st.caption(f"{'✅ confirmed' if confirmed else '⏳ pending'}  ·  {source}")
                with col2:
                    new_val = st.text_input(
                        "val", value=val,
                        key=f"global_{label}",
                        label_visibility="collapsed",
                    )
                with col3:
                    st.write("")
                edited_global.append({"label": label, "value": new_val})

            if st.button("💾 Save All Changes", key="save_global", type="primary"):
                fa.confirm_many(edited_global)
                st.success("Saved.")
                st.rerun()


# ── Router ────────────────────────────────────────────────────────────────────

def main():
    page = render_sidebar()
    if page == "🚀 Discover":
        page_discover()
    elif page == "🔍 Jobs":
        page_jobs()
    elif page == "📋 Applications":
        page_applications()
    elif page == "📊 Stats":
        page_stats()
    elif page == "✅ Review":
        page_review()


main()
