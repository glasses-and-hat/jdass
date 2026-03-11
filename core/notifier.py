"""
macOS native notification system.

Sends desktop notifications for:
  • High-score jobs discovered (score >= threshold from settings.yaml)
  • Application submitted successfully
  • Application failed
  • Outreach messages queued for review

Uses AppleScript (osascript) — no third-party dependencies required.
Falls back silently on non-macOS platforms.

Usage:
    notify = Notifier()
    notify.job_found("Stripe", "Staff Engineer", score=91)
    notify.application_submitted("Stripe", "Staff Engineer")
    notify.outreach_ready(count=3)
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Optional

import yaml
from loguru import logger


# ── Config ────────────────────────────────────────────────────────────────────


def _load_notification_config(settings_path: str = "configs/settings.yaml") -> dict:
    defaults = {"enabled": True, "notify_score_threshold": 80}
    try:
        with open(settings_path) as f:
            cfg = yaml.safe_load(f) or {}
        return {**defaults, **cfg.get("notifications", {})}
    except Exception:
        return defaults


# ── Notifier ──────────────────────────────────────────────────────────────────


class Notifier:
    """
    Sends macOS desktop notifications via AppleScript.

    All methods are safe to call on non-macOS platforms — they just log
    instead of showing a notification.

    Usage:
        n = Notifier()
        n.job_found("Stripe", "Staff Engineer", score=91)
    """

    APP_NAME = "JDASS"

    def __init__(self, settings_path: str = "configs/settings.yaml"):
        cfg = _load_notification_config(settings_path)
        self.enabled: bool = cfg.get("enabled", True)
        self.score_threshold: int = int(cfg.get("notify_score_threshold", 80))
        self._is_mac = sys.platform == "darwin"

    # ── Public event methods ──────────────────────────────────────────────────

    def job_found(self, company: str, title: str, score: int) -> None:
        """Notify when a high-score job is discovered."""
        if score < self.score_threshold:
            return
        self._send(
            title=f"New job: {score}/100",
            message=f"{company} — {title}",
            subtitle="Open dashboard to review",
        )
        logger.info("Notified: high-score job | company={} score={}", company, score)

    def application_submitted(self, company: str, title: str) -> None:
        """Notify when an application is successfully submitted."""
        self._send(
            title="Application submitted",
            message=f"{company} — {title}",
            subtitle="Check the Applications tab",
        )

    def application_failed(self, company: str, title: str, reason: str = "") -> None:
        """Notify when an application fails."""
        self._send(
            title="Application failed",
            message=f"{company} — {title}",
            subtitle=reason[:60] if reason else "Check logs for details",
        )

    def outreach_ready(self, count: int) -> None:
        """Notify when recruiter messages are ready for review."""
        if count == 0:
            return
        self._send(
            title=f"{count} outreach message{'s' if count != 1 else ''} ready",
            message="Review and approve in the dashboard",
            subtitle="Outreach tab → Pending Review",
        )

    def discovery_complete(self, new_jobs: int, top_score: Optional[int] = None) -> None:
        """Notify when a discovery run finishes."""
        if new_jobs == 0:
            return
        score_str = f" (top score: {top_score})" if top_score else ""
        self._send(
            title=f"Discovery: {new_jobs} new job{'s' if new_jobs != 1 else ''}",
            message=f"Ready to review{score_str}",
            subtitle="Open dashboard at localhost:8501",
        )

    def scheduler_error(self, job_name: str, error: str) -> None:
        """Notify when a scheduled job fails."""
        self._send(
            title=f"Scheduler error: {job_name}",
            message=error[:80],
            subtitle="Run: make logs-errors",
        )

    # ── Core send ─────────────────────────────────────────────────────────────

    def _send(self, title: str, message: str, subtitle: str = "") -> None:
        """Send a native macOS notification via osascript."""
        if not self.enabled:
            return
        if not self._is_mac:
            logger.debug("Notification (non-macOS): {} — {}", title, message)
            return

        # Escape single quotes so AppleScript doesn't break
        def esc(s: str) -> str:
            return s.replace("\\", "\\\\").replace('"', '\\"')

        subtitle_part = f', subtitle:"{esc(subtitle)}"' if subtitle else ""
        script = (
            f'display notification "{esc(message)}"'
            f' with title "{esc(self.APP_NAME)}: {esc(title)}"'
            f'{subtitle_part}'
        )

        try:
            subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                timeout=5,
                check=False,
            )
        except Exception as exc:
            logger.debug("Notification failed (osascript): {}", exc)


# ── Module-level singleton ────────────────────────────────────────────────────

_notifier: Optional[Notifier] = None


def get_notifier(settings_path: str = "configs/settings.yaml") -> Notifier:
    """Return a module-level Notifier singleton."""
    global _notifier
    if _notifier is None:
        _notifier = Notifier(settings_path)
    return _notifier
