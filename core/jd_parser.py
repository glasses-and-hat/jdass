"""
Job description parser — uses a local LLM to extract structured data
from free-text job descriptions.

Output is a ParsedJD dataclass which is then stored as JSON columns on the Job model.
The parser is resilient: if the LLM returns malformed JSON or times out, it falls back
to a fast regex-based extractor so the pipeline never blocks.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from loguru import logger

_PROMPT_PATH = Path(__file__).parent.parent / "llm" / "prompts" / "jd_parse.txt"
_PROMPT_TEMPLATE: Optional[str] = None


def _load_prompt() -> str:
    global _PROMPT_TEMPLATE
    if _PROMPT_TEMPLATE is None:
        _PROMPT_TEMPLATE = _PROMPT_PATH.read_text()
    return _PROMPT_TEMPLATE


# ── Output dataclass ──────────────────────────────────────────────────────────


@dataclass
class ParsedJD:
    key_technologies: list[str] = field(default_factory=list)
    frameworks: list[str] = field(default_factory=list)
    cloud_platforms: list[str] = field(default_factory=list)
    databases: list[str] = field(default_factory=list)
    important_skills: list[str] = field(default_factory=list)
    seniority: str = "unknown"
    years_experience_min: Optional[int] = None
    h1b_mentioned: bool = False
    remote_eligible: bool = False
    keywords: list[str] = field(default_factory=list)

    def all_tech(self) -> list[str]:
        """Flat list of all tech extracted — used for scoring."""
        return list(dict.fromkeys(
            self.key_technologies + self.frameworks + self.cloud_platforms + self.databases
        ))

    def to_db_fields(self) -> dict:
        """Return a dict ready to merge into a Job model."""
        return {
            "key_technologies": json.dumps(self.key_technologies),
            "frameworks": json.dumps(self.frameworks),
            "cloud_platforms": json.dumps(self.cloud_platforms),
            "databases": json.dumps(self.databases),
            "seniority": self.seniority,
            "h1b_mentioned": self.h1b_mentioned,
            "remote_eligible": self.remote_eligible,
        }


# ── LLM-based parser ──────────────────────────────────────────────────────────


class JDParser:
    """
    Parses a job description using a local LLM (via Ollama).

    Falls back to regex extraction if the LLM is unavailable or returns
    malformed output.

    Usage:
        parser = JDParser()
        result = parser.parse("We're looking for a senior Python engineer...")
    """

    def __init__(self, llm_client=None, use_llm: bool = True):
        self._llm = llm_client
        self.use_llm = use_llm

    @property
    def llm(self):
        if self._llm is None:
            from llm.client import get_llm_client
            self._llm = get_llm_client()
        return self._llm

    def parse(self, description: str, title: str = "") -> ParsedJD:
        """
        Parse a job description. Returns a ParsedJD.
        Always succeeds — falls back to regex on LLM failure.
        """
        # Truncate to ~3000 chars to keep prompt within context window
        truncated = description[:3000]

        if self.use_llm:
            result = self._parse_llm(truncated)
            if result:
                return result
            logger.warning("LLM parse failed — falling back to regex extractor")

        return self._parse_regex(truncated, title)

    # ── LLM path ──────────────────────────────────────────────────────────────

    def _parse_llm(self, description: str) -> Optional[ParsedJD]:
        try:
            prompt = _load_prompt().replace("{description}", description)
            raw = self.llm.generate(prompt, temperature=0.1)
            return self._parse_json_response(raw)
        except Exception as exc:
            logger.warning("LLM JD parse error: {}", exc)
            return None

    def _parse_json_response(self, raw: str) -> Optional[ParsedJD]:
        """Extract JSON from LLM output, handling common formatting issues."""
        # Strip markdown code fences if present
        raw = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.MULTILINE)
        raw = re.sub(r"\s*```$", "", raw.strip(), flags=re.MULTILINE)

        # Find the first { ... } block
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            logger.debug("No JSON object found in LLM response: {!r}", raw[:200])
            return None

        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            logger.debug("JSON decode error: {} | raw={!r}", exc, raw[:200])
            return None

        return ParsedJD(
            key_technologies=_clean_list(data.get("key_technologies")),
            frameworks=_clean_list(data.get("frameworks")),
            cloud_platforms=_clean_list(data.get("cloud_platforms")),
            databases=_clean_list(data.get("databases")),
            important_skills=_clean_list(data.get("important_skills")),
            seniority=str(data.get("seniority") or "unknown").lower(),
            years_experience_min=_safe_int(data.get("years_experience_min")),
            h1b_mentioned=bool(data.get("h1b_mentioned", False)),
            remote_eligible=bool(data.get("remote_eligible", False)),
            keywords=_clean_list(data.get("keywords")),
        )

    # ── Regex fallback ────────────────────────────────────────────────────────

    def _parse_regex(self, description: str, title: str = "") -> ParsedJD:
        """
        Fast, dependency-free tech stack extraction using keyword matching.
        Less accurate than LLM but never fails.
        """
        text = f"{title} {description}".lower()

        return ParsedJD(
            key_technologies=_regex_match(text, _TECH_KEYWORDS),
            frameworks=_regex_match(text, _FRAMEWORK_KEYWORDS),
            cloud_platforms=_regex_match(text, _CLOUD_KEYWORDS),
            databases=_regex_match(text, _DATABASE_KEYWORDS),
            important_skills=_regex_match(text, _SKILL_KEYWORDS),
            seniority=_regex_seniority(title or description),
            h1b_mentioned=bool(re.search(r"\bh.?1.?b\b|\bvisa sponsor", text)),
            remote_eligible=bool(re.search(r"\bremote\b|\bhybrid\b", text)),
        )


# ── Regex keyword dictionaries ────────────────────────────────────────────────
# Each dict maps canonical name → list of patterns to match

_TECH_KEYWORDS: dict[str, list[str]] = {
    "Python": [r"\bpython\b"],
    "Go": [r"\bgolang\b", r"\b(?<![a-z])go\b(?!\s*(?:to|and|the|for|with|in|on))"],
    "Java": [r"\bjava\b(?!script)"],
    "JavaScript": [r"\bjavascript\b", r"\bjs\b"],
    "TypeScript": [r"\btypescript\b", r"\bts\b"],
    "Rust": [r"\brust\b"],
    "C++": [r"\bc\+\+\b"],
    "Scala": [r"\bscala\b"],
    "Ruby": [r"\bruby\b"],
    "Elixir": [r"\belixir\b"],
    "Kafka": [r"\bkafka\b"],
    "Kubernetes": [r"\bkubernetes\b", r"\bk8s\b"],
    "Docker": [r"\bdocker\b"],
    "gRPC": [r"\bgrpc\b"],
    "GraphQL": [r"\bgraphql\b"],
    "REST": [r"\brest\s*api\b", r"\brestful\b"],
    "Terraform": [r"\bterraform\b"],
    "Linux": [r"\blinux\b"],
    "Spark": [r"\bapache\s*spark\b", r"\bspark\b"],
    "Flink": [r"\bflink\b"],
    "Airflow": [r"\bairflow\b"],
    "Hadoop": [r"\bhadoop\b"],
}

_FRAMEWORK_KEYWORDS: dict[str, list[str]] = {
    "FastAPI": [r"\bfastapi\b"],
    "Django": [r"\bdjango\b"],
    "Flask": [r"\bflask\b"],
    "React": [r"\breact\b(?!\.?js\s*native)"],
    "React Native": [r"\breact\s*native\b"],
    "Next.js": [r"\bnext\.?js\b"],
    "Vue": [r"\bvue\.?js\b", r"\bvuejs\b"],
    "Angular": [r"\bangular\b"],
    "Spring Boot": [r"\bspring\s*boot\b"],
    "Rails": [r"\brails\b", r"\bruby\s*on\s*rails\b"],
    "Express": [r"\bexpress\.?js\b"],
    "Node.js": [r"\bnode\.?js\b"],
    "Celery": [r"\bcelery\b"],
    "SQLAlchemy": [r"\bsqlalchemy\b"],
    "Pandas": [r"\bpandas\b"],
    "PyTorch": [r"\bpytorch\b"],
    "TensorFlow": [r"\btensorflow\b"],
    "LangChain": [r"\blangchain\b"],
}

_CLOUD_KEYWORDS: dict[str, list[str]] = {
    "AWS": [r"\baws\b", r"\bamazon\s*web\s*services\b"],
    "GCP": [r"\bgcp\b", r"\bgoogle\s*cloud\b"],
    "Azure": [r"\bazure\b", r"\bmicrosoft\s*azure\b"],
    "AWS Lambda": [r"\blambda\b"],
    "AWS S3": [r"\bs3\b", r"\baws\s*s3\b"],
    "AWS EKS": [r"\beks\b"],
    "AWS RDS": [r"\brds\b"],
    "BigQuery": [r"\bbigquery\b"],
    "Snowflake": [r"\bsnowflake\b"],
    "Databricks": [r"\bdatabricks\b"],
    "Cloudflare": [r"\bcloudflare\b"],
    "Vercel": [r"\bvercel\b"],
}

_DATABASE_KEYWORDS: dict[str, list[str]] = {
    "PostgreSQL": [r"\bpostgres(?:ql)?\b"],
    "MySQL": [r"\bmysql\b"],
    "SQLite": [r"\bsqlite\b"],
    "MongoDB": [r"\bmongodb\b", r"\bmongo\b"],
    "Redis": [r"\bredis\b"],
    "Cassandra": [r"\bcassandra\b"],
    "DynamoDB": [r"\bdynamodb\b"],
    "Elasticsearch": [r"\belasticsearch\b"],
    "ClickHouse": [r"\bclickhouse\b"],
    "TimescaleDB": [r"\btimescaledb\b"],
    "CockroachDB": [r"\bcockroachdb\b"],
    "Pinecone": [r"\bpinecone\b"],
    "Weaviate": [r"\bweaviate\b"],
}

_SKILL_KEYWORDS: dict[str, list[str]] = {
    "distributed systems": [r"\bdistributed\s*systems?\b"],
    "microservices": [r"\bmicroservices?\b"],
    "system design": [r"\bsystem\s*design\b"],
    "scalability": [r"\bscalab\w+\b"],
    "high availability": [r"\bhigh[\s-]availability\b"],
    "observability": [r"\bobservability\b"],
    "CI/CD": [r"\bci/?cd\b", r"\bcontinuous\s*(?:integration|deployment)\b"],
    "machine learning": [r"\bmachine\s*learning\b", r"\bml\b"],
    "data pipelines": [r"\bdata\s*pipeline\b"],
    "event-driven": [r"\bevent[\s-]driven\b"],
    "API design": [r"\bapi\s*design\b"],
    "cross-functional": [r"\bcross[\s-]functional\b"],
    "on-call": [r"\bon[\s-]call\b"],
    "mentorship": [r"\bmentor\w*\b"],
    "ownership": [r"\bownership\b"],
}


def _regex_match(text: str, keywords: dict[str, list[str]]) -> list[str]:
    found = []
    for canonical, patterns in keywords.items():
        if any(re.search(p, text, re.IGNORECASE) for p in patterns):
            found.append(canonical)
    return found


def _regex_seniority(text: str) -> str:
    text = text.lower()
    if re.search(r"\b(staff|principal)\b", text):
        return "staff"
    if re.search(r"\b(senior|sr\.?)\b", text):
        return "senior"
    if re.search(r"\b(mid.level|midlevel|software\s+engineer\s+ii)\b", text):
        return "mid"
    if re.search(r"\b(junior|jr\.?|entry.level|new.?grad)\b", text):
        return "junior"
    return "unknown"


# ── Utility helpers ───────────────────────────────────────────────────────────


def _clean_list(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if v and str(v).strip()]
    return []


def _safe_int(value) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
