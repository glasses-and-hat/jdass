"""
Resume tailoring pipeline.

Given a job's parsed JD, generates 3 senior-level resume bullet points
using a local LLM, then builds a versioned tailored resume (DOCX + PDF).

The master resume is NEVER modified. All output goes to a timestamped
directory under applications/.

Usage (standalone):
    tailor = ResumeTailor()
    result = tailor.tailor(job, parsed_jd)
    print(result.pdf_path)
    print(result.bullets)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from loguru import logger

from core.jd_parser import ParsedJD
from storage.file_store import ApplicationDir, ResumeBuilder
from storage.models import Job, ResumeVersion

_PROMPT_PATH = Path(__file__).parent.parent / "llm" / "prompts" / "resume_bullets.txt"
_MASTER_RESUME = Path("resumes") / "master_resume.docx"

_PROMPT_TEMPLATE: Optional[str] = None


def _load_prompt() -> str:
    global _PROMPT_TEMPLATE
    if _PROMPT_TEMPLATE is None:
        _PROMPT_TEMPLATE = _PROMPT_PATH.read_text()
    return _PROMPT_TEMPLATE


# ── Result dataclass ──────────────────────────────────────────────────────────


@dataclass
class TailoringResult:
    job_id: str
    version_id: str
    docx_path: Path
    pdf_path: Path
    bullets: list[str]
    llm_model: str
    matched_tech: list[str] = field(default_factory=list)

    def to_db_record(self) -> ResumeVersion:
        return ResumeVersion(
            job_id=self.job_id,
            version_id=self.version_id,
            file_path=str(self.pdf_path),
            bullets_used=json.dumps(self.bullets),
            llm_model=self.llm_model,
        )


# ── Tailor ─────────────────────────────────────────────────────────────────────


class ResumeTailor:
    """
    Generates tailored resume bullets and builds the versioned resume artifact.

    Steps:
      1. Extract existing bullets from master resume (for context)
      2. Build LLM prompt with job requirements + existing bullets
      3. Call LLM → parse 3 bullets
      4. Inject bullets into a copy of master resume
      5. Export PDF
      6. Save metadata
    """

    def __init__(
        self,
        llm_client=None,
        master_resume: Optional[Path] = None,
        use_llm: bool = True,
    ):
        self._llm = llm_client
        self.master_resume = master_resume or _MASTER_RESUME
        self.use_llm = use_llm
        self._builder = ResumeBuilder()

    @property
    def llm(self):
        if self._llm is None:
            from llm.client import get_llm_client
            self._llm = get_llm_client()
        return self._llm

    # ── Public API ─────────────────────────────────────────────────────────────

    def tailor(
        self,
        job: Job,
        parsed: ParsedJD,
        matched_tech: Optional[list[str]] = None,
    ) -> Optional[TailoringResult]:
        """
        Generate a tailored resume for this job. Returns None on failure.

        Args:
            job:          The Job DB record.
            parsed:       Parsed job description from JDParser.
            matched_tech: Pre-computed list of overlapping tech (from scorer).
        """
        if not self.master_resume.exists():
            logger.error(
                "Master resume not found at {}. "
                "Copy your resume to resumes/master_resume.docx",
                self.master_resume,
            )
            return None

        # 1. Extract existing bullets for LLM context
        existing_bullets = self._extract_existing_bullets()

        # 2. Generate tailored bullets
        bullets = self._generate_bullets(parsed, existing_bullets)
        if not bullets:
            logger.error("Failed to generate bullets for job {}", job.id)
            return None

        # 3. Create versioned application directory
        app_dir = ApplicationDir.create(job.company, job.title)
        app_dir.save_jd(job.description)
        app_dir.save_metadata({
            "job_id": job.id,
            "company": job.company,
            "title": job.title,
            "url": job.url,
            "key_technologies": parsed.key_technologies,
            "frameworks": parsed.frameworks,
            "cloud_platforms": parsed.cloud_platforms,
            "important_skills": parsed.important_skills,
            "matched_tech": matched_tech or [],
            "generated_bullets": bullets,
            "llm_model": self.llm.primary_model if self.use_llm else "regex_fallback",
        })

        # 4. Build resume (inject bullets + export PDF)
        pdf_path = self._builder.build(app_dir, bullets, self.master_resume)

        result = TailoringResult(
            job_id=job.id,
            version_id=app_dir.version_id,
            docx_path=app_dir.resume_docx,
            pdf_path=pdf_path,
            bullets=bullets,
            llm_model=self.llm.primary_model if self.use_llm else "regex_fallback",
            matched_tech=matched_tech or [],
        )

        logger.info(
            "Resume tailored | {} | {} | {} bullets | {}",
            job.company, job.title, len(bullets), pdf_path,
        )
        return result

    # ── Bullet generation ──────────────────────────────────────────────────────

    def _generate_bullets(self, parsed: ParsedJD, existing_bullets: list[str]) -> list[str]:
        """Generate 3 senior-level bullets. Falls back to template bullets on LLM failure."""
        if self.use_llm:
            bullets = self._generate_llm_bullets(parsed, existing_bullets)
            if bullets:
                return bullets
            logger.warning("LLM bullet generation failed — using template fallback")

        return self._generate_template_bullets(parsed)

    def _generate_llm_bullets(
        self, parsed: ParsedJD, existing_bullets: list[str]
    ) -> Optional[list[str]]:
        tech_str = ", ".join(parsed.all_tech()[:12]) or "various backend technologies"
        skills_str = ", ".join(parsed.important_skills[:6]) or "software engineering best practices"
        bullets_str = "\n".join(f"• {b}" for b in existing_bullets[:8]) or "(no bullets extracted)"

        prompt = (
            _load_prompt()
            .replace("{key_technologies}", tech_str)
            .replace("{important_skills}", skills_str)
            .replace("{existing_bullets}", bullets_str)
        )

        try:
            raw = self.llm.generate(prompt, temperature=0.4)
            bullets = self._parse_bullets(raw)
            if len(bullets) >= 2:  # Accept 2–3 bullets
                return bullets[:3]
            logger.warning("LLM returned {} bullets (expected 3): {!r}", len(bullets), raw[:300])
            return None
        except Exception as exc:
            logger.warning("LLM bullet generation error: {}", exc)
            return None

    def _generate_template_bullets(self, parsed: ParsedJD) -> list[str]:
        """
        Rule-based fallback bullet generator.
        Uses the job's tech stack to create plausible-sounding bullets.
        """
        tech = parsed.all_tech()
        primary = tech[0] if tech else "Python"
        secondary = tech[1] if len(tech) > 1 else "distributed systems"
        cloud = parsed.cloud_platforms[0] if parsed.cloud_platforms else "cloud infrastructure"

        return [
            f"Designed and implemented scalable {primary} services handling millions of daily requests, "
            f"reducing p99 latency by 40% through targeted optimisation and caching strategies",
            f"Led architecture of {secondary} platform serving cross-functional teams, enabling "
            f"3x throughput improvement and 99.9% uptime SLA compliance",
            f"Migrated critical workloads to {cloud}, reducing infrastructure costs by 30% while "
            f"improving deployment reliability and observability with automated CI/CD pipelines",
        ]

    # ── Resume parsing ─────────────────────────────────────────────────────────

    def _extract_existing_bullets(self) -> list[str]:
        """
        Extract bullet point text from the master resume for LLM context.
        Returns empty list if master resume is not available or not a .docx.
        """
        if not self.master_resume.exists():
            return []
        try:
            from docx import Document
            doc = Document(str(self.master_resume))

            bullet_styles = {
                "List Bullet", "List Bullet 2", "List Bullet 3",
                "List Paragraph", "List",
            }
            bullets = []
            for para in doc.paragraphs:
                text = para.text.strip()
                if not text:
                    continue
                is_bullet = (
                    para.style.name in bullet_styles
                    or text.startswith(("•", "·", "-", "*", "○"))
                )
                if is_bullet:
                    bullets.append(text.lstrip("•·-* ").strip())
                if len(bullets) >= 15:  # Enough context for the LLM
                    break
            return bullets
        except Exception as exc:
            logger.warning("Could not extract bullets from master resume: {}", exc)
            return []

    # ── Output parsing ─────────────────────────────────────────────────────────

    @staticmethod
    def _parse_bullets(raw: str) -> list[str]:
        """
        Extract bullet points from raw LLM output.
        Handles '•', '-', '*', numbered lists, and bare sentences.
        """
        lines = raw.strip().splitlines()
        bullets = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            # Strip common bullet markers
            line = re.sub(r"^[•·\-\*]\s*", "", line)
            line = re.sub(r"^\d+\.\s*", "", line)
            line = line.strip()
            if len(line) > 20:  # Skip very short/empty lines
                bullets.append(line)
        return bullets
