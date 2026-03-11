.PHONY: help install install-dev db-init discover discover-llm discover-semantic \
        parse-jobs parse-jobs-llm show-top-jobs \
        apply-queue apply-job apply-dry-run show-applications \
        outreach outreach-job scheduler scheduler-status run-discover run-apply run-outreach \
        linkedin-login notify-test \
        dashboard api logs-tail logs-errors db-shell retry-failed \
        test lint format doctor clean

PYTHON := .venv/bin/python
UV     := uv

# Auto-detect: use uv if available, otherwise fall back to venv python
RUN := $(shell command -v uv 2>/dev/null && echo "uv run" || echo ".venv/bin/python -m")
PYTEST := $(shell command -v uv 2>/dev/null && echo "uv run pytest" || echo ".venv/bin/pytest")

# ── Help ──────────────────────────────────────────────────────────────────────

help:
	@echo ""
	@echo "  JDASS — Job Discovery & Application System"
	@echo ""
	@echo "  Setup"
	@echo "    make install          Install dependencies (uv)"
	@echo "    make install-dev      Install + dev dependencies"
	@echo "    make db-init          Initialise SQLite database"
	@echo "    make doctor           Check system health (Ollama, DB, configs)"
	@echo ""
	@echo "  Phase 1 — Discovery"
	@echo "    make discover          Run discovery (regex parsing — fast, no Ollama)"
	@echo "    make discover-llm      Run discovery with LLM JD parsing (requires Ollama)"
	@echo "    make discover-semantic Run with semantic dedup (requires Ollama)"
	@echo ""
	@echo "  Phase 2 — LLM Parsing & Scoring"
	@echo "    make parse-jobs        Parse already-saved jobs with regex (fast)"
	@echo "    make parse-jobs-llm    Parse already-saved jobs with LLM (requires Ollama)"
	@echo "    make show-top-jobs     Show top 20 scored jobs from DB"
	@echo ""
	@echo "  Phase 4 — Application Automation"
	@echo "    make apply-queue           Process all queued application tasks"
	@echo "    make apply-job JOB_ID=<id> Apply to one specific job by ID"
	@echo "    make apply-dry-run         Open browser, fill form, do NOT submit"
	@echo "    make show-applications     Show recent applications from DB"
	@echo ""
	@echo "  Phase 6 — LinkedIn & Notifications"
	@echo "    make linkedin-login    One-time LinkedIn session setup (opens browser)"
	@echo "    make notify-test       Send a test macOS desktop notification"
	@echo ""
	@echo "  Phase 5 — Outreach & Scheduler"
	@echo "    make outreach              Generate recruiter messages (last 7 days)"
	@echo "    make outreach DAYS=3       Generate messages for jobs in last N days"
	@echo "    make outreach-job JOB_ID=<id> Generate messages for one job"
	@echo "    make scheduler             Start daily scheduler (blocks)"
	@echo "    make scheduler-status      Show next scheduled run times"
	@echo "    make run-discover          Trigger discovery job immediately"
	@echo "    make run-apply             Trigger apply job immediately"
	@echo "    make run-outreach          Trigger outreach job immediately"
	@echo ""
	@echo "  Dashboard"
	@echo "    make dashboard        Start Streamlit dashboard"
	@echo "    make api              Start FastAPI backend"
	@echo ""
	@echo "  Debugging"
	@echo "    make logs-tail        Tail today's log file"
	@echo "    make logs-errors      Show all ERROR lines across all logs"
	@echo "    make db-shell         Open SQLite shell"
	@echo "    make retry-failed     Re-queue all failed tasks"
	@echo ""
	@echo "  Dev"
	@echo "    make test             Run test suite"
	@echo "    make lint             Run ruff linter"
	@echo "    make format           Auto-format with ruff"
	@echo "    make clean            Remove __pycache__ and .pyc files"
	@echo ""

# ── Setup ─────────────────────────────────────────────────────────────────────

install:
	python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"

install-pip:
	python3 -m venv .venv
	.venv/bin/pip install sqlmodel aiosqlite httpx beautifulsoup4 pydantic \
	  pydantic-settings pyyaml python-dotenv loguru tenacity numpy \
	  apscheduler fastapi uvicorn streamlit pandas plotly python-docx \
	  playwright python-dateutil pytest pytest-asyncio

install-dev: install
	.venv/bin/playwright install chromium

db-init:
	$(PYTHON) -c "from storage.database import init_db; init_db(); print('DB ready.')"

doctor:
	@echo "=== JDASS Health Check ==="
	@echo ""
	@echo "[1/3] Ollama..."
	@curl -sf http://localhost:11434/api/tags > /dev/null && echo "  ✓ Ollama running" || echo "  ✗ Ollama NOT running — run: ollama serve"
	@echo ""
	@echo "[2/3] Database..."
	@test -f jdass.db && echo "  ✓ jdass.db exists" || echo "  ✗ jdass.db missing — run: make db-init"
	@echo ""
	@echo "[3/3] Configs..."
	@test -f configs/settings.yaml && echo "  ✓ settings.yaml" || echo "  ✗ configs/settings.yaml missing"
	@test -f configs/sources.yaml  && echo "  ✓ sources.yaml"  || echo "  ✗ configs/sources.yaml missing"
	@test -f .env || echo "  ⚠ .env missing — copy from .env.example"
	@echo ""

# ── Discovery ─────────────────────────────────────────────────────────────────

discover:
	$(PYTHON) -m pipelines.discovery

discover-llm:
	$(PYTHON) -m pipelines.discovery --llm

discover-semantic:
	$(PYTHON) -m pipelines.discovery --semantic

# ── Phase 2 — LLM Parsing & Scoring ──────────────────────────────────────────

parse-jobs:
	$(PYTHON) -m pipelines.parse_jobs

parse-jobs-llm:
	$(PYTHON) -m pipelines.parse_jobs --llm

show-top-jobs:
	@sqlite3 jdass.db \
	  "SELECT printf('%3d', match_score) || '  ' || printf('%-25s', company) || '  ' || printf('%-40s', title) || '  ' || location \
	   FROM jobs WHERE match_score IS NOT NULL \
	   ORDER BY match_score DESC LIMIT 20;"

# ── Phase 4 — Application Automation ─────────────────────────────────────────

apply-queue:
	$(PYTHON) -m pipelines.application

apply-job:
ifndef JOB_ID
	$(error JOB_ID is not set. Usage: make apply-job JOB_ID=<uuid>)
endif
	$(PYTHON) -m pipelines.application --job-id $(JOB_ID)

apply-dry-run:
ifdef JOB_ID
	$(PYTHON) -m pipelines.application --job-id $(JOB_ID) --dry-run
else
	$(PYTHON) -m pipelines.application --dry-run --limit 1
endif

show-applications:
	@sqlite3 jdass.db \
	  "SELECT printf('%-25s', jobs.company) || '  ' || printf('%-40s', jobs.title) || '  ' || \
	          printf('%-12s', applications.status) || '  ' || COALESCE(applications.applied_at,'—') \
	   FROM applications \
	   JOIN jobs ON applications.job_id = jobs.id \
	   ORDER BY applications.applied_at DESC LIMIT 30;"

# ── Phase 6 — LinkedIn & Notifications ───────────────────────────────────────

linkedin-login:
	$(PYTHON) -c "from automation.linkedin_handler import linkedin_login_sync; linkedin_login_sync()"

notify-test:
	$(PYTHON) -c "from core.notifier import Notifier; n = Notifier(); n.job_found('Stripe', 'Staff Engineer (test)', score=95); print('Notification sent — check your Mac notification centre.')"

# ── Phase 5 — Outreach & Scheduler ───────────────────────────────────────────

outreach:
	$(PYTHON) -m pipelines.outreach --days $(or $(DAYS),7)

outreach-job:
ifndef JOB_ID
	$(error JOB_ID is not set. Usage: make outreach-job JOB_ID=<uuid>)
endif
	$(PYTHON) -m pipelines.outreach --job-id $(JOB_ID)

scheduler:
	$(PYTHON) -m scheduler.scheduler

scheduler-status:
	$(PYTHON) -m scheduler.scheduler --status

run-discover:
	$(PYTHON) -m scheduler.scheduler --run-now discover

run-apply:
	$(PYTHON) -m scheduler.scheduler --run-now apply

run-outreach:
	$(PYTHON) -m scheduler.scheduler --run-now outreach

# ── Dashboard ─────────────────────────────────────────────────────────────────

dashboard:
	.venv/bin/streamlit run dashboard/ui.py --server.port 8501

api:
	.venv/bin/uvicorn dashboard.api:app --reload --port 8000

# ── Debugging ─────────────────────────────────────────────────────────────────

logs-tail:
	tail -f logs/$$(date +%Y-%m-%d).log 2>/dev/null || \
	tail -f logs/discovery_$$(date +%Y-%m-%d).log 2>/dev/null || \
	echo "No log file found for today."

logs-errors:
	grep -h "ERROR" logs/*.log 2>/dev/null | sort || echo "No error logs found."

db-shell:
	sqlite3 jdass.db ".mode table" ".headers on"

retry-failed:
	$(PYTHON) -c \
	  "from storage.database import retry_failed_tasks; n=retry_failed_tasks(); print(f'Re-queued {n} tasks')"

# ── Tests ─────────────────────────────────────────────────────────────────────

test:
	.venv/bin/pytest tests/ -v

test-fast:
	.venv/bin/pytest tests/ -v -x --tb=short

# ── Code quality ──────────────────────────────────────────────────────────────

lint:
	.venv/bin/ruff check . 2>/dev/null || echo "ruff not installed — run: make install-dev"

format:
	.venv/bin/ruff format . 2>/dev/null || echo "ruff not installed — run: make install-dev"

# ── Cleanup ───────────────────────────────────────────────────────────────────

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
	find . -name "*.pyo" -delete 2>/dev/null || true
	@echo "Cleaned."
