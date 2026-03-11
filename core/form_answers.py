"""
Persistent store for learned form field answers.

Answers are keyed by normalised label text so they transfer across companies
that ask the same question with slightly different wording.

File: configs/form_answers.yaml
Schema:
  answers:
    "do you require visa sponsorship?":
      value: "Yes"
      confirmed: true
      source: "rule"          # "rule" | "llm" | "user"
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import yaml
from loguru import logger

_PATH = Path("configs/form_answers.yaml")


def _normalise(label: str) -> str:
    """Lowercase, collapse whitespace, strip trailing punctuation."""
    label = label.lower().strip()
    label = re.sub(r"\s+", " ", label)
    label = label.rstrip("*:?")
    return label


def load() -> dict:
    """Return the full answers dict (keyed by normalised label)."""
    if not _PATH.exists():
        return {}
    try:
        data = yaml.safe_load(_PATH.read_text()) or {}
        return data.get("answers", {}) or {}
    except Exception as exc:
        logger.warning("Could not load form_answers.yaml: {}", exc)
        return {}


def get(label: str, confirmed_only: bool = False) -> Optional[str]:
    """
    Look up a stored answer by label.

    Args:
        label:          Raw label text from the form.
        confirmed_only: If True, only return answers the user has confirmed.

    Returns:
        The stored value string, or None if not found / not yet confirmed.
    """
    answers = load()
    key = _normalise(label)
    entry = answers.get(key)
    if entry is None:
        return None
    if confirmed_only and not entry.get("confirmed", False):
        return None
    return entry.get("value")


def save(label: str, value: str, confirmed: bool = False, source: str = "llm") -> None:
    """
    Upsert an answer for `label`.

    Existing confirmed answers are never downgraded to unconfirmed.
    """
    answers = load()
    key = _normalise(label)

    existing = answers.get(key, {})
    # Don't overwrite a user-confirmed answer with an unconfirmed one
    if existing.get("confirmed") and not confirmed:
        return

    answers[key] = {
        "value": value,
        "confirmed": confirmed,
        "source": source,
    }
    _write(answers)


def confirm(label: str, value: str) -> None:
    """Mark an answer as confirmed (user-reviewed) and update its value."""
    answers = load()
    key = _normalise(label)
    answers[key] = {
        "value": value,
        "confirmed": True,
        "source": "user",
    }
    _write(answers)


def confirm_many(updates: list[dict]) -> None:
    """
    Bulk-confirm a list of answers.
    Each dict must have keys: label, value.
    """
    answers = load()
    for item in updates:
        key = _normalise(item["label"])
        answers[key] = {
            "value": item["value"],
            "confirmed": True,
            "source": "user",
        }
    _write(answers)


def pending() -> list[dict]:
    """Return all unconfirmed answers as a list of dicts with 'label' and 'value'."""
    answers = load()
    return [
        {"label": k, "value": v.get("value", ""), "source": v.get("source", "")}
        for k, v in answers.items()
        if not v.get("confirmed", False)
    ]


def _write(answers: dict) -> None:
    try:
        _PATH.write_text(
            yaml.dump({"answers": answers}, default_flow_style=False, allow_unicode=True),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.error("Could not write form_answers.yaml: {}", exc)
