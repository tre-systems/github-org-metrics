"""DORA performance bands.

Thresholds follow the published DORA/Accelerate State of DevOps bands. They are
applied to metrics approximated from GitHub data, so treat a band as a
direction of travel rather than a verdict — see the caveats in the README.
"""

from __future__ import annotations

from collections.abc import Sequence

__all__ = [
    "ELITE",
    "HIGH",
    "LOW",
    "MEDIUM",
    "UNKNOWN",
    "rate_change_failure_rate",
    "rate_deployment_frequency",
    "rate_lead_time",
    "rate_recovery_time",
]

ELITE = "Elite"
HIGH = "High"
MEDIUM = "Medium"
LOW = "Low"
UNKNOWN = "No data"

_HOUR = 1.0
_DAY = 24.0
_WEEK = 7 * _DAY
_MONTH = 30 * _DAY


def _band(value: float, thresholds: Sequence[tuple[float, str]]) -> str:
    """Return the first band whose upper bound the value falls within."""
    for limit, label in thresholds:
        if value <= limit:
            return label
    return LOW


def rate_lead_time(hours: float, samples: int) -> str:
    """Band the lead time for changes (first commit to merge)."""
    if samples == 0:
        return UNKNOWN
    return _band(hours, ((_HOUR, ELITE), (_DAY, HIGH), (_MONTH, MEDIUM)))


def rate_deployment_frequency(per_day: float) -> str:
    """Band deployment frequency, expressed as deployments per day."""
    if per_day <= 0:
        return UNKNOWN
    if per_day >= 1:
        return ELITE
    if per_day >= 1 / 7:
        return HIGH
    if per_day >= 1 / 30:
        return MEDIUM
    return LOW


def rate_change_failure_rate(percent: float, deployments: int) -> str:
    """Band the change failure rate as a percentage of deployments."""
    if deployments == 0:
        return UNKNOWN
    return _band(percent, ((15, ELITE), (30, HIGH), (45, MEDIUM)))


def rate_recovery_time(hours: float, samples: int) -> str:
    """Band the mean time to restore service after a failed deployment."""
    if samples == 0:
        return UNKNOWN
    return _band(hours, ((_HOUR, ELITE), (_DAY, HIGH), (_WEEK, MEDIUM)))
