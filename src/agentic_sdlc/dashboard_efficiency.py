"""Honest dashboard freshness wrapper for Forge efficiency metrics."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

__all__ = [
    "DashboardEfficiencyError",
    "build_dashboard_efficiency",
    "build_infrastructure_blocker_panel",
]


class DashboardEfficiencyError(ValueError):
    """Raised when dashboard freshness evidence is internally inconsistent."""


def _non_negative(value: int, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise DashboardEfficiencyError(f"{name} must be a non-negative integer")
    return value


def build_dashboard_efficiency(
    metrics: Mapping[str, Any] | None,
    *,
    observed_at: str | None,
    age_seconds: int | None,
    stale_after_seconds: int = 900,
) -> dict[str, Any]:
    """Expose observed, stale, or unknown factory metrics without fabricating values."""

    stale_after = _non_negative(stale_after_seconds, "stale_after_seconds")
    if stale_after == 0:
        raise DashboardEfficiencyError("stale_after_seconds must be greater than zero")

    if metrics is None:
        if observed_at is not None or age_seconds is not None:
            raise DashboardEfficiencyError(
                "unknown metrics cannot carry an observation timestamp or age"
            )
        return {
            "schemaVersion": 1,
            "status": "unknown",
            "observedAt": None,
            "ageSeconds": None,
            "staleAfterSeconds": stale_after,
            "metrics": None,
        }

    if not observed_at or age_seconds is None:
        raise DashboardEfficiencyError(
            "observed metrics require observed_at and age_seconds freshness evidence"
        )
    age = _non_negative(age_seconds, "age_seconds")
    status = "stale" if age > stale_after else "observed"
    return {
        "schemaVersion": 1,
        "status": status,
        "observedAt": observed_at,
        "ageSeconds": age,
        "staleAfterSeconds": stale_after,
        "metrics": dict(metrics),
    }


_RETRY_ACTIONS = {"retry_failed_jobs", "block", "noop", "route_to_bounded_repair"}


def _required_int(document: Mapping[str, Any], name: str) -> int:
    value = document.get(name)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise DashboardEfficiencyError(f"retry decision {name} must be a non-negative integer")
    return value


def _next_retry_at(observed_at: str, delay_seconds: int) -> str | None:
    try:
        observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    due = observed + timedelta(seconds=delay_seconds)
    return due.isoformat().replace("+00:00", "Z")


def build_infrastructure_blocker_panel(
    decision: Mapping[str, Any],
    *,
    observed_at: str | None = None,
) -> dict[str, Any]:
    """Render one transient-infrastructure retry decision for the Control Center.

    The panel reports only what the decision already established. It never
    invents a recovery estimate, and it never reports a red pull request as
    anything other than blocked or retrying.
    """

    action = decision.get("action")
    if action not in _RETRY_ACTIONS:
        raise DashboardEfficiencyError("retry decision action is not recognized")
    head_sha = decision.get("headSha")
    if not isinstance(head_sha, str) or not head_sha:
        raise DashboardEfficiencyError("retry decision headSha is required")
    attempts = _required_int(decision, "attempts")
    max_attempts = _required_int(decision, "maxAttempts")
    if max_attempts == 0:
        raise DashboardEfficiencyError("retry decision maxAttempts must be greater than zero")
    head_unchanged = decision.get("headUnchanged", True)
    if not isinstance(head_unchanged, bool):
        raise DashboardEfficiencyError("retry decision headUnchanged must be a boolean")

    blocker = decision.get("blocker")
    if action == "block":
        if not isinstance(blocker, Mapping):
            raise DashboardEfficiencyError("a blocked retry decision must carry a blocker record")
        status = "blocked"
        headline = "Blocked: GitHub infrastructure"
        user_action_required = bool(blocker.get("userActionRequired", False))
        last_error_summary = str(blocker.get("lastErrorSummary", ""))
        next_action = str(blocker.get("nextAction", "blocked_exhausted"))
        blocker_class = str(blocker.get("class", "external_infrastructure"))
        next_retry_at = None
    elif action == "retry_failed_jobs":
        status = "retrying"
        headline = "Retrying: GitHub infrastructure"
        user_action_required = False
        last_error_summary = str(decision.get("reason", ""))
        next_action = "retry_failed_jobs"
        blocker_class = "external_infrastructure"
        delay = _required_int(decision, "nextDelaySeconds")
        next_retry_at = _next_retry_at(observed_at, delay) if observed_at else None
    else:
        status = "clear"
        headline = "No GitHub infrastructure blocker"
        user_action_required = False
        last_error_summary = str(decision.get("reason", ""))
        next_action = str(action)
        blocker_class = None
        next_retry_at = None

    return {
        "schemaVersion": 1,
        "status": status,
        "headline": headline,
        "blockerClass": blocker_class,
        "userActionRequired": user_action_required,
        "headSha": head_sha,
        "headUnchanged": head_unchanged,
        "attempts": attempts,
        "maxAttempts": max_attempts,
        "autoRetry": f"{attempts}/{max_attempts}",
        "lastErrorSummary": last_error_summary,
        "nextAction": next_action,
        "nextRetryAt": next_retry_at,
        "observedAt": observed_at,
    }
