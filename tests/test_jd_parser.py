"""Unit tests for JDParser — regex path only (no LLM, no network)."""

import pytest

from core.jd_parser import JDParser, ParsedJD, _clean_list, _safe_int


@pytest.fixture
def parser():
    return JDParser(use_llm=False)


# ── Regex extraction ──────────────────────────────────────────────────────────


def test_extracts_python(parser):
    result = parser.parse("We need a Python developer with FastAPI experience.")
    assert "Python" in result.key_technologies


def test_extracts_go(parser):
    result = parser.parse("Experience with Golang and gRPC required.")
    assert "Go" in result.key_technologies


def test_extracts_typescript(parser):
    result = parser.parse("Strong TypeScript and React skills.")
    assert "TypeScript" in result.key_technologies
    assert "React" in result.frameworks


def test_extracts_aws(parser):
    result = parser.parse("Deploy to AWS Lambda and S3.")
    assert "AWS" in result.cloud_platforms


def test_extracts_postgres(parser):
    result = parser.parse("PostgreSQL and Redis experience required.")
    assert "PostgreSQL" in result.databases
    assert "Redis" in result.databases


def test_extracts_kafka(parser):
    result = parser.parse("Build event streaming pipelines using Kafka.")
    assert "Kafka" in result.key_technologies


def test_extracts_kubernetes(parser):
    result = parser.parse("Deploy microservices to Kubernetes (k8s).")
    assert "Kubernetes" in result.key_technologies


def test_extracts_distributed_systems(parser):
    result = parser.parse("Strong understanding of distributed systems required.")
    assert "distributed systems" in result.important_skills


def test_remote_eligible_true(parser):
    result = parser.parse("This is a fully remote position.")
    assert result.remote_eligible is True


def test_remote_eligible_false(parser):
    result = parser.parse("Onsite in our Chicago office required.")
    assert result.remote_eligible is False


def test_h1b_mentioned_true(parser):
    result = parser.parse("We offer H1B sponsorship for qualified candidates.")
    assert result.h1b_mentioned is True


def test_h1b_mentioned_false(parser):
    result = parser.parse("No mention of immigration whatsoever.")
    assert result.h1b_mentioned is False


def test_seniority_senior(parser):
    result = parser.parse("", title="Senior Software Engineer")
    assert result.seniority == "senior"


def test_seniority_staff(parser):
    result = parser.parse("", title="Staff Engineer, Platform")
    assert result.seniority == "staff"


def test_seniority_junior(parser):
    result = parser.parse("", title="Junior Software Engineer")
    assert result.seniority == "junior"


def test_seniority_unknown(parser):
    result = parser.parse("Looking for a software engineer.", title="Software Engineer")
    assert result.seniority == "unknown"


def test_no_hallucination(parser):
    """Parser should not invent tech that isn't in the description."""
    result = parser.parse("Looking for someone with strong communication skills.")
    assert result.key_technologies == []
    assert result.frameworks == []


def test_all_tech_deduplicates(parser):
    result = parser.parse("Python, FastAPI, PostgreSQL, AWS")
    all_tech = result.all_tech()
    assert len(all_tech) == len(set(all_tech))


def test_to_db_fields_returns_json_strings(parser):
    result = parser.parse("Python engineer with AWS experience.")
    fields = result.to_db_fields()
    import json
    # Should be JSON strings, not lists
    assert isinstance(fields["key_technologies"], str)
    parsed_back = json.loads(fields["key_technologies"])
    assert isinstance(parsed_back, list)


# ── JSON response parsing ─────────────────────────────────────────────────────


def test_parse_json_response_clean():
    p = JDParser(use_llm=False)
    raw = '{"key_technologies": ["Python"], "frameworks": [], "cloud_platforms": [], "databases": [], "important_skills": [], "seniority": "senior", "years_experience_min": 5, "h1b_mentioned": false, "remote_eligible": true, "keywords": []}'
    result = p._parse_json_response(raw)
    assert result is not None
    assert "Python" in result.key_technologies
    assert result.seniority == "senior"
    assert result.years_experience_min == 5
    assert result.remote_eligible is True


def test_parse_json_response_with_code_fence():
    p = JDParser(use_llm=False)
    raw = '```json\n{"key_technologies": ["Go"], "frameworks": [], "cloud_platforms": [], "databases": [], "important_skills": [], "seniority": "mid", "years_experience_min": null, "h1b_mentioned": false, "remote_eligible": false, "keywords": []}\n```'
    result = p._parse_json_response(raw)
    assert result is not None
    assert "Go" in result.key_technologies


def test_parse_json_response_invalid_returns_none():
    p = JDParser(use_llm=False)
    result = p._parse_json_response("This is not JSON at all.")
    assert result is None


# ── Helpers ───────────────────────────────────────────────────────────────────


def test_clean_list_filters_empty():
    assert _clean_list(["Python", "", "  ", "Go"]) == ["Python", "Go"]


def test_clean_list_none():
    assert _clean_list(None) == []


def test_safe_int_valid():
    assert _safe_int(5) == 5
    assert _safe_int("3") == 3


def test_safe_int_none():
    assert _safe_int(None) is None


def test_safe_int_invalid():
    assert _safe_int("not a number") is None
