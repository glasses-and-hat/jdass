"""
Outreach message generator.

Given a Job record + RecruiterCandidate, uses a local LLM to generate
a personalised LinkedIn connection request / cold email.

The generated message is saved to the outreach_queue table with status
PENDING_REVIEW — the user approves or discards it from the dashboard
before anything is sent.

Nothing is ever sent automatically. User approval is always required.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from loguru import logger

from core.recruiter_finder import RecruiterCandidate
from storage.models import Job, OutreachQueue


_PROMPT_PATH = Path(__file__).parent.parent / "llm" / "prompts" / "outreach_message.txt"
_PROMPT_TEMPLATE: Optional[str] = None


def _load_prompt() -> str:
    global _PROMPT_TEMPLATE
    if _PROMPT_TEMPLATE is None:
        _PROMPT_TEMPLATE = _PROMPT_PATH.read_text()
    return _PROMPT_TEMPLATE


# ── Message generator ─────────────────────────────────────────────────────────


class MessageGenerator:
    """
    Generates personalised outreach messages using the local LLM.

    Usage:
        gen = MessageGenerator(profile)
        items = gen.generate_for_job(job, candidates)
        # items is a list of OutreachQueue objects ready to insert into DB
    """

    def __init__(
        self,
        profile: dict,
        llm_client=None,
        use_llm: bool = True,
    ):
        self._personal = profile.get("personal", {})
        self._prefs = profile.get("preferences", {})
        self._work_auth = profile.get("work_authorization", {})
        self._llm = llm_client
        self.use_llm = use_llm

    @property
    def llm(self):
        if self._llm is None:
            from llm.client import get_llm_client
            self._llm = get_llm_client()
        return self._llm

    # ── Public API ─────────────────────────────────────────────────────────────

    def generate_for_job(
        self,
        job: Job,
        candidates: list[RecruiterCandidate],
        application_id: Optional[str] = None,
    ) -> list[OutreachQueue]:
        """
        Generate one outreach message per candidate for a given job.
        Returns a list of OutreachQueue items ready to be saved to the DB.
        """
        items: list[OutreachQueue] = []

        if not candidates:
            logger.debug("No recruiter candidates for job {} — skipping", job.id)
            return items

        for candidate in candidates:
            text = self._generate_message(job, candidate)
            if not text:
                continue

            item = OutreachQueue(
                job_id=job.id,
                application_id=application_id,
                recruiter_name=candidate.name,
                recruiter_title=candidate.title,
                recruiter_url=candidate.linkedin_url,
                message_text=text,
            )
            items.append(item)
            logger.info(
                "Generated outreach message | company={} recruiter={}",
                job.company, candidate.name,
            )

        return items

    # ── Message generation ─────────────────────────────────────────────────────

    def _generate_message(self, job: Job, candidate: RecruiterCandidate) -> Optional[str]:
        """Generate message text; falls back to a template if LLM unavailable."""
        if self.use_llm:
            text = self._generate_llm_message(job, candidate)
            if text:
                return text
            logger.warning("LLM message generation failed — using template fallback")

        return self._template_message(job, candidate)

    def _generate_llm_message(
        self, job: Job, candidate: RecruiterCandidate
    ) -> Optional[str]:
        techs = job.get_technologies() + job.get_frameworks()
        tech_str = ", ".join(techs[:6]) or "Python, distributed systems"

        visa = "I require H1B visa sponsorship" if self._work_auth.get("require_sponsorship") else "I am authorized to work in the US"

        prompt = (
            _load_prompt()
            .replace("{company}", job.company)
            .replace("{role_title}", job.title)
            .replace("{key_technologies}", tech_str)
            .replace("{recruiter_name}", candidate.name.split()[0])  # first name only
            .replace("{recruiter_title}", candidate.title)
            .replace("{location}", self._personal.get("location_city", "Chicago"))
            .replace("{years_experience}", str(self._prefs.get("years_of_experience", 5)))
            .replace("{core_skills}", tech_str)
            .replace("{visa_status}", visa)
        )

        try:
            raw = self.llm.generate(prompt, fast=True, temperature=0.5)
            text = raw.strip()
            if len(text) < 20:
                return None
            # Truncate to 300 chars for LinkedIn connection requests
            return text[:300] if len(text) > 300 else text
        except Exception as exc:
            logger.warning("LLM outreach generation error: {}", exc)
            return None

    def _template_message(self, job: Job, candidate: RecruiterCandidate) -> str:
        first_name = candidate.name.split()[0] if candidate.name else "there"
        my_first = self._personal.get("first_name", "")
        city = self._personal.get("location_city", "Chicago")
        years = self._prefs.get("years_of_experience", 5)

        techs = job.get_technologies()
        top_tech = techs[0] if techs else "Python"

        return (
            f"Hi {first_name}, I recently applied for the {job.title} role at {job.company} "
            f"and wanted to reach out directly. I'm a {years}-year software engineer based in {city}, "
            f"with strong {top_tech} experience. Happy to share more if it would be helpful. "
            f"— {my_first}"
        )[:300]
