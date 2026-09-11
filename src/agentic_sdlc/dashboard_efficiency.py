"""Honest dashboard freshness wrapper for Forge efficiency metrics."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = ["DashboardEfficiencyError", "build_dashboard_efficiency"]


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
