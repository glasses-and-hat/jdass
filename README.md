# JDASS — Job Discovery & Application System

A fully local, LLM-powered system that discovers software engineering jobs, scores them against your profile, tailors your resume, auto-fills application forms, and drafts recruiter outreach messages — all without sending your data to any external AI service.

**Runs entirely on your MacBook. No cloud AI. No monthly fees.**

---

## Table of Contents

1. [What It Does](#what-it-does)
2. [System Architecture](#system-architecture)
3. [Technologies Used](#technologies-used)
4. [Prerequisites](#prerequisites)
5. [Installation](#installation)
6. [Configuration](#configuration)
7. [Usage — Step by Step](#usage--step-by-step)
   - [Phase 1 — Discover Jobs](#phase-1--discover-jobs)
   - [Phase 2 — Parse & Score Jobs](#phase-2--parse--score-jobs)
   - [Phase 3 — Dashboard](#phase-3--dashboard)
   - [Phase 4 — Apply to Jobs](#phase-4--apply-to-jobs)
   - [Phase 5 — Outreach & Scheduling](#phase-5--outreach--scheduling)
8. [All Make Commands](#all-make-commands)
9. [Debugging Guide](#debugging-guide)
10. [Project Structure](#project-structure)
11. [FAQ](#faq)

---

## What It Does

| Step | What Happens | You Do |
|------|-------------|--------|
| **Discover** | Scrapes Greenhouse, Lever, HackerNews, Wellfound for SE jobs | Run `make discover` |
| **Filter** | Keeps only remote/Chicago, mid-senior level, H1B-compatible jobs | Automatic |
| **Score** | Rates each job 0–100 against your tech stack | Automatic |
| **Dashboard** | Browse jobs, see score breakdowns, manage applications | Open browser |
| **Apply** | Playwright fills Greenhouse/Lever forms and submits | Review + approve |
| **Outreach** | LLM writes personalised recruiter messages | Review + approve |
| **Schedule** | Runs all of the above daily at configured times | Run `make scheduler` |

**Nothing is submitted without you seeing it first.** The dashboard is your control centre.

---

## System Architecture

```
┌─────────────────────────────────────────────────────────┐
│                     Your MacBook                        │
│                                                         │
│  ┌──────────┐    ┌──────────┐    ┌──────────────────┐  │
│  │ Scrapers │───▶│ Filters  │───▶│  SQLite DB       │  │
│  │ GH/Lever │    │ H1B/     │    │  (jdass.db)      │  │
│  │ HN/WF    │    │ Remote/  │    │                  │  │
│  └──────────┘    │ Seniority│    │  jobs            │  │
│                  └──────────┘    │  applications    │  │
│  ┌──────────┐                    │  outreach_queue  │  │
│  │  Ollama  │◀──────────────────▶│  task_queue      │  │
│  │ (local   │    JD parsing      │  resume_versions │  │
│  │  LLM)    │    Resume bullets  └──────────────────┘  │
│  └──────────┘    Outreach msgs           │              │
│                                          │              │
│  ┌──────────────────────────────────┐    │              │
│  │  Dashboard                       │◀───┘              │
│  │  FastAPI (port 8000)             │                   │
│  │  Streamlit (port 8501)           │                   │
│  └──────────────────────────────────┘                   │
│                                                         │
│  ┌──────────┐    ┌──────────┐    ┌──────────────────┐  │
│  │Playwright│───▶│ ATS Form │    │  applications/   │  │
│  │(Chromium)│    │ Handler  │    │  stripe/backend/ │  │
│  └──────────┘    │ GH/Lever │    │  resume.docx     │  │
│                  └──────────┘    │  resume.pdf      │  │
│                                  └──────────────────┘  │
└─────────────────────────────────────────────────────────┘
```

**Data flow for one job:**
```
Scraper → RawJob → Filter → Dedup → DB (Job) → JDParser → JobScorer
→ ResumeTailor → ApplicationRunner → DB (Application) → MessageGenerator
→ DB (OutreachQueue) → Dashboard review → [User approves] → Done
```

---

## Technologies Used

### Core Language
- **Python 3.14** — main language (managed with `pyenv`)

### Database
- **SQLite** — single local file `jdass.db`, no server needed
- **SQLModel** — ORM layer (combines SQLAlchemy + Pydantic). Tables: `jobs`, `applications`, `outreach_queue`, `task_queue`, `resume_versions`

### Local AI (no cloud)
- **Ollama** — runs LLMs locally on your Mac
  - `llama3.1:8b` — primary model for JD parsing + resume bullets
  - `mistral:7b` — fast model for outreach messages
  - `nomic-embed-text` — embeddings for semantic deduplication

### Web Scraping
- **httpx** — async-capable HTTP client with rate limiting
- **BeautifulSoup4** — HTML parsing for job descriptions
- **Playwright** — browser automation for form-filling (Chromium)

### Job Sources
| Source | Method | Auth Required |
|--------|--------|---------------|
| Greenhouse | Public JSON API | No |
| Lever | Public JSON API | No |
| HackerNews | Algolia search API | No |
| Wellfound | Algolia index + HTML fallback | No |

### API & Dashboard
- **FastAPI** — REST API backend (port 8000)
- **Uvicorn** — ASGI server for FastAPI
- **Streamlit** — interactive dashboard UI (port 8501)
- **Pandas** — data manipulation for dashboard stats
- **Plotly** — charts (histogram, pie, funnel, timeline)

### Resume Generation
- **python-docx** — read/write `.docx` files (never modifies master)
- **LibreOffice** (optional) — DOCX → PDF conversion
  - Install: `brew install --cask libreoffice`

### Scheduling
- **APScheduler** — cron-style job scheduler with SQLite persistence

### Utilities
- **loguru** — structured logging with rotation
- **tenacity** — retry logic for LLM calls and HTTP requests
- **pyyaml** — config file parsing
- **python-dateutil** — date parsing for HN posts

### Dev Tools
- **pytest** — test runner (83 tests, all pass without Ollama or internet)
- **ruff** — linter + formatter

---

## Prerequisites

Before installing, make sure you have these on your Mac:

### 1. Python 3.14 via pyenv

```bash
# Install pyenv if you don't have it
brew install pyenv

# Install Python 3.14
pyenv install 3.14.0
pyenv global 3.14.0

# Verify
python3 --version   # should say Python 3.14.x
```

### 2. Ollama (local LLM runtime)

```bash
# Install via homebrew (starts automatically at login)
brew install ollama
brew services start ollama

# Pull the three models used by the system:
ollama pull llama3.1:8b       # primary model — JD parsing + resume bullets (~5GB)
ollama pull mistral:7b        # fast model — outreach messages (~4GB)
ollama pull nomic-embed-text  # embeddings — semantic dedup (~270MB)

# Verify everything is running
curl http://localhost:11434/api/tags
```

> **Note:** Ollama is optional for basic operation. Without it, the system uses regex-based JD parsing and template resume bullets. All scrapers and the dashboard work without Ollama.

### 3. LibreOffice (for PDF export)

```bash
brew install --cask libreoffice
```

> Without LibreOffice, the system uploads `.docx` files instead of `.pdf` to application forms. This works fine on most ATS platforms.

### 4. SQLite CLI (for debugging)

```bash
# Usually pre-installed on macOS. Check with:
sqlite3 --version

# If missing:
brew install sqlite
```

---

## Installation

```bash
# 1. Clone or navigate to the project directory
cd "Job Discovery And Application System/JDASS"

# 2. Create virtual environment and install all dependencies
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

# 3. Install Playwright's Chromium browser (for form filling)
.venv/bin/playwright install chromium

# 4. Create the SQLite database
make db-init

# 5. Copy the example env file and edit if needed
cp .env.example .env

# 6. Check everything is working
make doctor
```

**Expected output from `make doctor`:**
```
=== JDASS Health Check ===

[1/3] Ollama...
  ✓ Ollama running

[2/3] Database...
  ✓ jdass.db exists

[3/3] Configs...
  ✓ settings.yaml
  ✓ sources.yaml
```

---

## Configuration

### Your Personal Profile — `configs/applicant.yaml`

**Fill this in before running anything.** This is what gets put into application forms.

```yaml
personal:
  first_name: "Rahul"
  last_name: "YourLastName"          # ← fill in
  email: "you@example.com"           # ← fill in
  phone: "+1-555-555-5555"           # ← fill in
  linkedin_url: "https://linkedin.com/in/yourprofile"
  github_url: "https://github.com/yourhandle"
  portfolio_url: ""                  # optional
  location_city: "Chicago"
  location_state: "IL"

work_authorization:
  authorized_to_work_in_us: true
  require_sponsorship: true          # true = you need H1B
  visa_type: "H1B"

preferences:
  desired_salary_min: 150000
  desired_salary_max: 220000
  years_of_experience: 5
```

### Your Resume — `resumes/master_resume.docx`

Place your master resume here. **This file is never modified.** The system always makes a copy before tailoring.

```bash
cp /path/to/your/resume.docx resumes/master_resume.docx
```

### Job Filters — `configs/settings.yaml`

Controls what jobs get saved:

```yaml
filters:
  remote_ok: true
  allowed_locations:
    - chicago
    - remote
    - anywhere
  target_seniority:
    - mid
    - senior
    - staff
  require_h1b: false          # if true: only save jobs mentioning H1B
  reject_no_sponsorship: true # if true: reject jobs saying "no sponsorship"

  # Recency filter — only keep jobs posted within N days
  # Options: 3 (last 3 days), 7 (last week), null (no limit)
  max_age_days: 3
  keep_undated: true          # if true: keep jobs with no posted date

application:
  max_per_hour: 10            # rate limit for auto-apply
  min_score_to_apply: 70     # only auto-apply to jobs scoring >= 70

scheduler:
  discover_time: "08:00"      # run discovery at 8am daily
  apply_time:    "09:00"      # apply to queued jobs at 9am
  outreach_time: "10:00"      # generate messages at 10am
```

### Job Sources — `configs/sources.yaml`

Add company slugs to scrape from Greenhouse and Lever:

```yaml
greenhouse:
  - stripe
  - figma
  - vercel
  - datadog
  - snowflake

lever:
  - netflix
  - modal
  - railway
```

### Candidate Scoring Profile — `core/scorer.py`

Edit the `CANDIDATE_PROFILE` dict to match your actual skills. Jobs are scored 0–100 based on tech overlap.

```python
CANDIDATE_PROFILE = {
    "core_languages": ["Python", "Go", "TypeScript"],
    "frameworks": ["FastAPI", "React", "Django"],
    "cloud": ["AWS", "GCP", "Docker", "Kubernetes"],
    "databases": ["PostgreSQL", "Redis", "MongoDB"],
    # ...
}
```

---

## Usage — Step by Step

### Phase 1 — Discover Jobs

Scrapes all configured job sources, filters, deduplicates, and saves to the database.

```bash
# Fast mode — regex parsing only, no Ollama needed (~30 seconds)
make discover

# With LLM — better JD parsing, requires Ollama (~2-5 minutes)
make discover-llm

# With semantic deduplication — also requires Ollama
make discover-semantic

# See what was found
make show-top-jobs
```

**What you'll see:**
```
 92  Stripe                     Staff Backend Engineer               Remote
 88  Figma                      Senior Software Engineer, Platform   Remote / San Francisco
 85  Datadog                    Software Engineer, Infrastructure    Remote
...
```

**How often to run:** Daily, or use the scheduler (Phase 5).

---

### Phase 2 — Parse & Score Jobs

If you ran `make discover` (without `--llm`), jobs may have lower-quality parsed data. Run this to improve them:

```bash
# Re-parse all unscored jobs using regex (fast)
make parse-jobs

# Re-parse using LLM for better tech extraction (requires Ollama)
make parse-jobs-llm

# View top jobs again
make show-top-jobs
```

---

### Phase 3 — Dashboard

The dashboard lets you browse jobs, manage applications, and review outreach messages.

```bash
# Start the FastAPI backend (terminal 1)
make api
# → Running at http://localhost:8000

# Start the Streamlit dashboard (terminal 2)
make dashboard
# → Open http://localhost:8501 in your browser
```

**Dashboard pages:**

| Page | What You Can Do |
|------|----------------|
| **Jobs** | Filter by score/source/remote/H1B, click a job to see full JD + score breakdown |
| **Applications** | See applied jobs, update status (Interview/Offer/Rejected), add notes, download resume |
| **Stats** | Score histogram, source breakdown, tech frequency, application funnel |

**Outreach tab** (in Jobs page): Review and approve/discard recruiter messages before anything is sent.

---

### Phase 4 — Apply to Jobs

The system auto-fills Greenhouse and Lever application forms using Playwright. Every job goes through an **interactive approval prompt** before anything is submitted — you decide job by job.

#### Step 1: Check what's queued

Jobs are enqueued automatically during discovery. Check the queue:

```bash
# Count pending tasks
sqlite3 jdass.db "SELECT count(*) FROM task_queue WHERE task_type='application' AND status='PENDING';"

# View top scored jobs
make show-top-jobs
```

#### Step 2: Dry run first (always recommended)

```bash
# Open the form in a visible browser — does NOT submit
make apply-dry-run JOB_ID=<paste-uuid-here>
```

This opens a Chrome window so you can inspect the form and verify fields are being filled correctly. The browser stays open for 5 minutes. Close it with Ctrl+C.

#### Step 3: Drain the queue with per-job approval

```bash
make apply-queue
```

For each job you'll see details and a prompt:

```
──────────────────────────────────────────────────────────────
  Staff Fullstack Engineer, Privy
  Stripe  |  Remote - US
  Score : 90   Seniority: staff
  Tech  : typescript, react, go, postgresql
  URL   : https://boards.greenhouse.io/stripe/jobs/...
──────────────────────────────────────────────────────────────
  Action: [a]pply  [d]ry-run  [s]kip  [q]uit →
```

- **`a`** — tailor resume + fill form + submit
- **`d`** — open browser, fill form, do NOT submit (inspect first)
- **`s`** — skip this job, move to next
- **`q`** — quit, leave remaining tasks in queue for later

#### Step 4: Apply to one specific job

```bash
make apply-job JOB_ID=<uuid>
# Shows job details and prompts before applying
```

#### Step 5: Auto-apply without prompts (advanced)

```bash
# Skip approval prompts — applies to every eligible job automatically
.venv/bin/python -m pipelines.application --auto
```

**Rate limiting:** The system won't apply to more than `max_per_hour` jobs per hour (default: 10). Counted from the `applications` table, persists across restarts.

#### Step 6: Check results

```bash
make show-applications
```

```
Stripe                     Staff Backend Engineer             APPLIED       2026-03-09 09:12:44
Figma                      Senior Software Engineer           APPLIED       2026-03-09 09:08:11
```

---

### Phase 5 — Outreach & Scheduling

#### Recruiter Outreach

After applying, generate personalised LinkedIn messages to recruiters and hiring managers.

```bash
# Generate messages for all jobs applied in the last 7 days
make outreach

# Generate for a shorter window
make outreach DAYS=3

# Generate for one specific job
make outreach-job JOB_ID=<uuid>
```

Messages are saved as `PENDING_REVIEW` — **nothing is sent automatically**.

**To review messages:** Open the dashboard → Outreach tab → Approve or Discard each message.

#### Daily Scheduler

Run all three phases automatically every day:

```bash
# Start the scheduler (leave this terminal open)
make scheduler

# Check when the next runs are
make scheduler-status

# Trigger a specific job right now (without waiting for the scheduled time)
make run-discover
make run-apply
make run-outreach
```

**Default schedule (America/Chicago timezone):**
- `08:00` — discovery pipeline
- `09:00` — application queue
- `10:00` — outreach message generation

Change times in `configs/settings.yaml` under the `scheduler:` key.

---

## All Make Commands

```bash
# ── Setup ────────────────────────────────────────────────────────────────────
make install          # Install all dependencies
make install-dev      # Install + Playwright chromium
make db-init          # Create SQLite database tables
make doctor           # Health check (Ollama, DB, configs)

# ── Phase 1 — Discovery ──────────────────────────────────────────────────────
make discover         # Scrape all sources (regex, fast — no Ollama)
make discover-llm     # Scrape + LLM JD parsing (requires Ollama)
make discover-semantic# Scrape + LLM + semantic dedup (requires Ollama)

# ── Phase 2 — Scoring ────────────────────────────────────────────────────────
make parse-jobs       # Re-parse/score existing jobs (regex, fast)
make parse-jobs-llm   # Re-parse/score with LLM
make show-top-jobs    # Print top 20 scored jobs from DB

# ── Phase 3 — Dashboard ──────────────────────────────────────────────────────
make api              # Start FastAPI backend   → http://localhost:8000
make dashboard        # Start Streamlit UI      → http://localhost:8501

# ── Phase 4 — Application Automation ─────────────────────────────────────────
make apply-queue               # Apply to all queued jobs
make apply-job JOB_ID=<uuid>   # Apply to one specific job
make apply-dry-run             # Open browser, fill form, do NOT submit
make apply-dry-run JOB_ID=<uuid>  # Dry run for a specific job
make show-applications         # List recent applications with status

# ── Phase 5 — Outreach & Scheduler ───────────────────────────────────────────
make outreach                  # Generate messages (last 7 days)
make outreach DAYS=3           # Generate messages (last 3 days)
make outreach-job JOB_ID=<uuid># Generate for one job
make scheduler                 # Start daily scheduler (blocks terminal)
make scheduler-status          # Show next run times
make run-discover              # Trigger discovery job now
make run-apply                 # Trigger apply job now
make run-outreach              # Trigger outreach job now

# ── Debugging ─────────────────────────────────────────────────────────────────
make logs-tail        # Live-tail today's log file
make logs-errors      # Show all ERROR lines from all logs
make db-shell         # Open interactive SQLite shell
make retry-failed     # Re-queue all failed tasks

# ── Dev ───────────────────────────────────────────────────────────────────────
make test             # Run all 83 tests
make test-fast        # Run tests, stop on first failure
make lint             # Check code style with ruff
make format           # Auto-fix code style
make clean            # Remove __pycache__ files
```

---

## Debugging Guide

### Problem: `make discover` finds no jobs

**Check 1 — Are the sources configured?**
```bash
cat configs/sources.yaml
# Should list company slugs under greenhouse: and lever:
```

**Check 2 — Is the Greenhouse slug correct?**
```bash
# Test manually — replace 'stripe' with your company slug
curl "https://boards-api.greenhouse.io/v1/boards/stripe/jobs?content=true" | python3 -m json.tool | head -50
# If you get {"error":"..."} the slug is wrong
```

**Check 3 — Are your filters too strict?**
```bash
# Temporarily disable H1B filter in configs/settings.yaml:
# require_h1b: false
# reject_no_sponsorship: false
make discover
```

**Check 4 — See what the scraper fetched (before filters)**
```bash
# Run with debug logging
.venv/bin/python -c "
from scrapers.greenhouse import GreenhouseScraper
s = GreenhouseScraper()
jobs = s.fetch_jobs('stripe')
print(f'Fetched {len(jobs)} jobs from Stripe')
for j in jobs[:3]:
    print(f'  {j.title} | {j.location}')
"
```

---

### Problem: `make discover-llm` is slow or hangs

**Check Ollama is running:**
```bash
curl http://localhost:11434/api/tags
# Should return a JSON list of models
# If connection refused: run `ollama serve` in another terminal
```

**Check the model is downloaded:**
```bash
ollama list
# Should show llama3.1:8b, mistral:7b, nomic-embed-text
# If missing: ollama pull llama3.1:8b
```

**Run without LLM while Ollama downloads:**
```bash
make discover   # uses fast regex parsing, no Ollama needed
```

---

### Problem: Application form not filled correctly

**Step 1 — Always dry run first:**
```bash
make apply-dry-run JOB_ID=<uuid>
# Watch what the browser does. Fields highlighted yellow = being filled.
```

**Step 2 — Check the screenshot:**
```bash
ls -lt logs/screenshots/
# Open the most recent screenshot to see what the form looked like
open logs/screenshots/<most-recent>.png
```

**Step 3 — Check the submission log:**
```bash
sqlite3 jdass.db "SELECT submission_log FROM applications WHERE job_id='<uuid>';"
# Shows exactly which fields were filled and which selectors worked
```

**Step 4 — Check if the ATS is supported:**
```bash
.venv/bin/python -c "
from automation.greenhouse_handler import GreenhouseHandler
from automation.lever_handler import LeverHandler
url = 'https://boards.greenhouse.io/stripe/jobs/12345'
print('Greenhouse:', GreenhouseHandler.detect(url))
print('Lever:', LeverHandler.detect(url))
"
```

---

### Problem: `make apply-dry-run` fails immediately

**Most common causes:**

| Error | Fix |
|-------|-----|
| `SyntaxError: f-string` | Already fixed — update `base_handler.py` line 115 |
| `Error: Message not understood` | Word not licensed — install LibreOffice: `brew install --cask libreoffice` |
| `Resume not found` | Check `resumes/master_resume.docx` exists |
| `No handler for URL` | That ATS isn't supported yet (only Greenhouse + Lever) |
| `Job not found` | Wrong UUID — check `make show-top-jobs` for valid IDs |

---

### Problem: Score is 0 or very low

**Check if the job was parsed:**
```bash
sqlite3 jdass.db "
SELECT title, company, match_score, key_technologies
FROM jobs
WHERE id = '<uuid>';
"
```

If `key_technologies` is empty, the JD wasn't parsed yet:
```bash
make parse-jobs-llm   # or make parse-jobs for regex
```

**Check your candidate profile matches the job's tech:**
```bash
# Open core/scorer.py and look at CANDIDATE_PROFILE
# Add any tech you know that isn't listed there
```

---

### Problem: Database looks wrong or jobs missing

**Open the database directly:**
```bash
make db-shell
# Then run SQL queries:
.tables                              -- list all tables
SELECT count(*) FROM jobs;          -- how many jobs total
SELECT count(*) FROM applications;  -- how many applied
SELECT status, count(*) FROM jobs GROUP BY status;  -- status breakdown
SELECT company, title, match_score FROM jobs ORDER BY match_score DESC LIMIT 10;
.quit
```

**Reset everything and start fresh:**
```bash
# WARNING: this deletes all data
rm jdass.db
make db-init
make discover
```

---

### Problem: Log file shows errors

```bash
# See all errors across all log files
make logs-errors

# Follow today's log in real time
make logs-tail

# Or look at a specific pipeline log
cat logs/application_2026-03-09.log
cat logs/discovery_2026-03-09.log
cat logs/outreach_2026-03-09.log
```

**Log file locations:**
```
logs/
  discovery_YYYY-MM-DD.log    # scraping + filtering
  application_YYYY-MM-DD.log  # form filling + submission
  outreach_YYYY-MM-DD.log     # recruiter message generation
  scheduler_YYYY-MM-DD.log    # scheduler job runs
  screenshots/                # Playwright screenshots on errors
```

---

### Problem: Rate limit hit — too many applications

```bash
# See how many applications were submitted in the last hour
.venv/bin/python -c "
from automation.rate_limiter import RateLimiter
rl = RateLimiter()
print(f'Applied this hour: {rl.applied_this_hour()} / {rl.max_per_hour}')
print(f'Next slot in: {rl.seconds_until_slot()}s')
"

# Change the limit in configs/settings.yaml:
# application:
#   max_per_hour: 20   ← increase this
```

---

### Problem: Failed tasks stuck in queue

```bash
# See task status breakdown
.venv/bin/python -c "
from storage.database import get_session
from storage.models import TaskQueue
from sqlmodel import select
with get_session() as s:
    tasks = s.exec(select(TaskQueue)).all()
    from collections import Counter
    print(Counter(t.status for t in tasks))
"

# Re-queue all failed tasks
make retry-failed

# See why a specific task failed
sqlite3 jdass.db "SELECT task_type, status, last_error, attempts FROM task_queue WHERE status='FAILED' LIMIT 10;"
```

---

### Running Tests

```bash
# Run all tests (no Ollama or internet required)
make test

# Run with verbose output
.venv/bin/pytest tests/ -v

# Run only a specific test file
.venv/bin/pytest tests/test_filters.py -v

# Run tests and stop at first failure
make test-fast
```

---

## Project Structure

```
JDASS/
├── configs/
│   ├── applicant.yaml       ← YOUR PERSONAL INFO — fill this in
│   ├── settings.yaml        ← job filters, rate limits, scheduler times
│   └── sources.yaml         ← company slugs to scrape
│
├── resumes/
│   └── master_resume.docx   ← YOUR RESUME — place it here, never modified
│
├── scrapers/
│   ├── base.py              ← BaseScraper ABC + RawJob dataclass
│   ├── greenhouse.py        ← Greenhouse public JSON API
│   ├── lever.py             ← Lever public JSON API
│   ├── hn_hiring.py         ← HackerNews monthly hiring thread
│   └── wellfound.py         ← Wellfound via Algolia + HTML fallback
│
├── core/
│   ├── filters.py           ← H1B / remote / seniority filters
│   ├── deduplicator.py      ← fingerprint + cosine similarity dedup
│   ├── jd_parser.py         ← LLM or regex job description parser
│   ├── scorer.py            ← 0-100 job scoring (edit CANDIDATE_PROFILE here)
│   ├── resume_tailor.py     ← LLM bullet generation + DOCX/PDF builder
│   ├── recruiter_finder.py  ← DuckDuckGo → LinkedIn recruiter search
│   └── message_generator.py ← LLM outreach message writer
│
├── storage/
│   ├── models.py            ← SQLModel DB schema (5 tables)
│   ├── database.py          ← DB session + all repo functions
│   └── file_store.py        ← Versioned application artifact storage
│
├── pipelines/
│   ├── discovery.py         ← Phase 1+2: scrape → filter → score
│   ├── parse_jobs.py        ← Re-parse/score existing DB jobs
│   ├── application.py       ← Phase 4: drain application task queue
│   └── outreach.py          ← Phase 5: generate recruiter messages
│
├── automation/
│   ├── base_handler.py      ← BaseATSHandler ABC + ApplyResult
│   ├── greenhouse_handler.py← Playwright Greenhouse form handler
│   ├── lever_handler.py     ← Playwright Lever form handler
│   ├── application_runner.py← Orchestrates one application end-to-end
│   └── rate_limiter.py      ← SQLite-backed per-hour rate limiter
│
├── llm/
│   ├── client.py            ← Ollama HTTP client with retry
│   └── prompts/
│       ├── jd_parse.txt         ← Job description extraction prompt
│       ├── resume_bullets.txt   ← Resume bullet generation prompt
│       └── outreach_message.txt ← Recruiter message prompt
│
├── dashboard/
│   ├── api.py               ← FastAPI REST API (port 8000)
│   └── ui.py                ← Streamlit dashboard (port 8501)
│
├── scheduler/
│   └── scheduler.py         ← APScheduler daily cron jobs
│
├── applications/            ← Generated per application (gitignored)
│   └── {company}/{role}/{date}/
│       ├── resume.docx      ← Tailored resume copy
│       ├── resume.pdf       ← PDF version (if LibreOffice installed)
│       ├── job_description.txt
│       └── tailoring_metadata.json
│
├── logs/                    ← Log files (gitignored)
│   └── screenshots/         ← Playwright screenshots on errors
│
├── tests/                   ← 83 unit tests
├── Makefile                 ← All commands live here
├── pyproject.toml           ← Dependencies + project metadata
├── .env.example             ← Copy to .env and edit
└── jdass.db                 ← SQLite database (gitignored)
```

---

## FAQ

**Q: Can this submit applications without me watching?**
A: By default, `make apply-queue` shows you each job and prompts `[a]pply / [d]ry-run / [s]kip / [q]uit` before touching anything. To skip prompts and apply automatically, pass `--auto`: `.venv/bin/python -m pipelines.application --auto`. The rate limit of 10/hour always applies.

**Q: How do I control how recent the discovered jobs must be?**
A: Set `max_age_days` in `configs/settings.yaml` under `filters:`. Use `3` for the last 3 days, `7` for the last week, or `null` to disable the filter entirely and keep all jobs regardless of age. Jobs with no posted date are always kept unless you set `keep_undated: false`.

**Q: Will companies know I'm using a bot?**
A: The browser runs with a real Chrome user agent, slow_mo=200ms between actions, and a realistic viewport. It's not foolproof — some companies use Cloudflare or reCAPTCHA. When that happens the result is `BLOCKED` in the database, and a screenshot is saved so you can apply manually.

**Q: What happens if Ollama is offline?**
A: Every LLM call has a regex/template fallback. Discovery still runs (regex JD parsing). Resume tailoring uses template bullets. Outreach uses a simple template message. Nothing crashes — it just logs a warning.

**Q: Can I add more job sources?**
A: Yes. Create a new file in `scrapers/` that subclasses `BaseScraper` and implements `fetch_jobs()`. Then add it to the sources list in `configs/sources.yaml` and import it in `pipelines/discovery.py`.

**Q: Can I add more ATS platforms (not just Greenhouse and Lever)?**
A: Yes. Subclass `BaseATSHandler` in `automation/`, implement `detect()` and `apply()`, then add it to the `_HANDLER_CLASSES` list in `automation/application_runner.py`.

**Q: My master resume has an unusual format — will bullet injection work?**
A: The system tries two strategies: (1) finds paragraphs with List Bullet styles, (2) finds the "Experience" section heading and collects the following long paragraphs. If neither works, it logs a warning and you'll see the original bullets in the output. The PDF is still generated and uploaded.

**Q: Where are my tailored resumes stored?**
A: In `applications/{company_slug}/{role_slug}/{timestamp}/resume.docx` and `resume.pdf`. Each application gets its own directory with the JD, metadata, and resume.

**Q: How do I stop the scheduler?**
A: Press `Ctrl+C` in the terminal where `make scheduler` is running. It shuts down cleanly.

**Q: The outreach messages look generic. How do I improve them?**
A: Edit `llm/prompts/outreach_message.txt`. The prompt already instructs the LLM to avoid buzzwords and include one specific company detail — but the LLM only knows what's in the JD. For better messages, add a `company_notes` field to `configs/applicant.yaml` and extend the prompt to include it.
