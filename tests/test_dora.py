"""Tests for the DORA performance bands."""

from __future__ import annotations

import pytest

from github_metrics import dora


class TestLeadTime:
    @pytest.mark.parametrize(
        ("hours", "expected"),
        [
            (0.5, dora.ELITE),
            (1, dora.ELITE),
            (12, dora.HIGH),
            (200, dora.MEDIUM),
            (1000, dora.LOW),
        ],
    )
    def test_bands(self, hours, expected):
        assert dora.rate_lead_time(hours, samples=5) == expected

    def test_no_samples_is_not_elite(self):
        assert dora.rate_lead_time(0.0, samples=0) == dora.UNKNOWN


class TestDeploymentFrequency:
    @pytest.mark.parametrize(
        ("per_day", "expected"),
        [
            (3, dora.ELITE),
            (1, dora.ELITE),
            (0.5, dora.HIGH),
            (0.1, dora.MEDIUM),
            (0.01, dora.LOW),
        ],
    )
    def test_bands(self, per_day, expected):
        assert dora.rate_deployment_frequency(per_day) == expected

    def test_no_deployments_is_unknown(self):
        assert dora.rate_deployment_frequency(0) == dora.UNKNOWN


class TestChangeFailureRate:
    @pytest.mark.parametrize(
        ("percent", "expected"),
        [
            (0, dora.ELITE),
            (15, dora.ELITE),
            (25, dora.HIGH),
            (40, dora.MEDIUM),
            (60, dora.LOW),
        ],
    )
    def test_bands(self, percent, expected):
        assert dora.rate_change_failure_rate(percent, deployments=20) == expected

    def test_zero_percent_of_nothing_is_unknown(self):
        assert dora.rate_change_failure_rate(0.0, deployments=0) == dora.UNKNOWN


class TestRecoveryTime:
    @pytest.mark.parametrize(
        ("hours", "expected"),
        [(0.5, dora.ELITE), (10, dora.HIGH), (100, dora.MEDIUM), (500, dora.LOW)],
    )
    def test_bands(self, hours, expected):
        assert dora.rate_recovery_time(hours, samples=3) == expected

    def test_never_having_failed_is_not_measured(self):
        assert dora.rate_recovery_time(0.0, samples=0) == dora.UNKNOWN
