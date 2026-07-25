"""Tests for metric aggregation."""

from __future__ import annotations

from dataclasses import replace

import pytest

from github_metrics.analyze import analyze
from tests.conftest import commit, iso, pull, raw_data, repo, run


def developer(report, name):
    """Find a developer in a report by name."""
    for entry in [*report.developers, *report.outliers]:
        if entry.name == name:
            return entry
    raise AssertionError(f"{name} not in report")


class TestCommits:
    def test_aggregates_commits_and_line_counts(self, options):
        data = raw_data(
            repos=[repo("api")],
            commits={
                "api": [
                    commit("a1", "alice", iso(2025, 2, 1)),
                    commit("a2", "alice", iso(2025, 2, 2)),
                    commit("b1", "bob", iso(2025, 2, 3)),
                ]
            },
            commit_stats={
                "api": {
                    "a1": {"additions": 100, "deletions": 5},
                    "a2": {"additions": 20, "deletions": 1},
                    "b1": {"additions": 7, "deletions": 3},
                }
            },
        )

        report = analyze(data, options)

        alice = developer(report, "alice")
        assert alice.commits == 2
        assert alice.lines_added == 120
        assert alice.lines_deleted == 6
        assert alice.repositories["api"] == 2
        assert report.repositories[0].commit_count == 3

    def test_ignores_commits_outside_the_window(self, options):
        data = raw_data(
            repos=[repo("api")],
            commits={
                "api": [
                    commit("old", "alice", iso(2024, 12, 31)),
                    commit("new", "alice", iso(2025, 1, 2)),
                    commit("future", "alice", iso(2025, 5, 1)),
                ]
            },
            commit_stats={
                "api": {sha: {"additions": 1} for sha in ("old", "new", "future")}
            },
        )

        report = analyze(data, options)

        assert developer(report, "alice").commits == 1
        assert report.repositories[0].commit_count == 1

    def test_counts_commits_without_a_linked_account_for_the_repository(self, options):
        data = raw_data(
            repos=[repo("api")],
            commits={"api": [commit("x1", None, iso(2025, 2, 1))]},
        )

        report = analyze(data, options)

        assert report.repositories[0].commit_count == 1
        assert report.developers == []

    def test_excludes_bots(self, options):
        data = raw_data(
            repos=[repo("api")],
            commits={
                "api": [
                    commit("d1", "dependabot[bot]", iso(2025, 2, 1)),
                    commit("d2", "renovate", iso(2025, 2, 1), user_type="Bot"),
                    commit("h1", "alice", iso(2025, 2, 1)),
                ]
            },
            commit_stats={"api": {sha: {"additions": 10} for sha in ("d1", "d2", "h1")}},
        )

        report = analyze(data, options)

        assert [dev.name for dev in report.developers] == ["alice"]


class TestPullRequests:
    def test_counts_opened_reviewed_and_commented(self, options):
        data = raw_data(
            repos=[repo("api")],
            commits={"api": [commit("a1", "alice", iso(2025, 2, 1))]},
            commit_stats={"api": {"a1": {"additions": 10, "deletions": 0}}},
            pull_requests={"api": [pull(1, "alice", iso(2025, 2, 1))]},
            pr_reviews={
                "api": {
                    "1": [
                        {"user": {"login": "bob"}, "submitted_at": iso(2025, 2, 2)},
                        {"user": {"login": "bob"}, "submitted_at": iso(2024, 2, 2)},
                    ]
                }
            },
            pr_comments={
                "api": {"1": [{"user": {"login": "bob"}, "created_at": iso(2025, 2, 3)}]}
            },
        )

        report = analyze(data, replace(options, include_inactive=True))

        assert developer(report, "alice").prs_opened == 1
        bob = developer(report, "bob")
        assert bob.prs_reviewed == 1  # the 2024 review is outside the window
        assert bob.pr_comments == 1

    def test_pull_request_updated_in_window_counts_but_is_not_opened(self, options):
        data = raw_data(
            repos=[repo("api")],
            pull_requests={
                "api": [pull(1, "alice", iso(2024, 11, 1), updated=iso(2025, 2, 1))]
            },
        )

        report = analyze(data, replace(options, include_inactive=True))

        assert report.repositories[0].pr_count == 1
        assert developer(report, "alice").prs_opened == 0


class TestLeadTime:
    def test_measured_from_the_branch_first_commit(self, options):
        data = raw_data(
            repos=[repo("api")],
            pull_requests={
                "api": [
                    pull(
                        1,
                        "alice",
                        iso(2025, 2, 10, 12),
                        merged=iso(2025, 2, 11, 12),
                    )
                ]
            },
            pr_first_commit_dates={"api": {"1": iso(2025, 2, 10, 0)}},
        )

        report = analyze(data, replace(options, include_inactive=True))

        assert report.repositories[0].lead_times == [36.0]
        assert report.dora.lead_time_mean == pytest.approx(36.0)

    def test_falls_back_to_creation_when_no_commit_data(self, options):
        data = raw_data(
            repos=[repo("api")],
            pull_requests={
                "api": [pull(1, "alice", iso(2025, 2, 10, 0), merged=iso(2025, 2, 10, 6))]
            },
        )

        report = analyze(data, replace(options, include_inactive=True))

        assert report.repositories[0].lead_times == [6.0]

    def test_discards_implausibly_long_branches(self, options):
        data = raw_data(
            repos=[repo("api")],
            pull_requests={
                "api": [pull(1, "alice", iso(2025, 1, 2), merged=iso(2025, 3, 30))]
            },
            pr_first_commit_dates={"api": {"1": iso(2020, 1, 1)}},
        )

        report = analyze(data, replace(options, include_inactive=True))

        assert report.repositories[0].lead_times == []
        assert report.dora.lead_time_samples == 0


class TestDeployments:
    def test_counts_runs_failures_and_durations(self, options):
        data = raw_data(
            repos=[repo("api")],
            commits={"api": [commit("a1", "alice", iso(2025, 2, 1))]},
            commit_stats={"api": {"a1": {"additions": 1}}},
            workflow_runs={
                "api": [
                    run(
                        "Deploy",
                        iso(2025, 2, 1, 10),
                        "success",
                        updated=iso(2025, 2, 1, 10, 6),
                    ),
                    run("Deploy", iso(2025, 2, 2, 10), "failure"),
                    run(
                        "Deploy",
                        iso(2025, 2, 3, 10),
                        "success",
                        updated=iso(2025, 2, 3, 10, 4),
                    ),
                ]
            },
        )

        report = analyze(data, options)
        metrics = report.repositories[0]

        assert metrics.deployment_count == 3
        assert metrics.deployment_failures == 1
        assert metrics.failure_rate == pytest.approx(33.3, abs=0.1)
        assert metrics.avg_deployment_duration == pytest.approx(5.0)

    def test_recovery_time_spans_failure_to_next_success(self, options):
        data = raw_data(
            repos=[repo("api")],
            commits={"api": [commit("a1", "alice", iso(2025, 2, 1))]},
            commit_stats={"api": {"a1": {"additions": 1}}},
            workflow_runs={
                "api": [
                    run("Deploy", iso(2025, 2, 2, 10), "failure"),
                    run("Deploy", iso(2025, 2, 2, 11), "failure"),
                    run(
                        "Deploy",
                        iso(2025, 2, 2, 12),
                        "success",
                        updated=iso(2025, 2, 2, 13),
                    ),
                ]
            },
        )

        report = analyze(data, options)

        # Measured from the first failure of the streak to the recovering run.
        assert report.repositories[0].recovery_times == [3.0]
        assert report.dora.recovery_time_mean == pytest.approx(3.0)

    def test_prefers_a_deployment_workflow_over_plain_ci(self, options):
        data = raw_data(
            repos=[repo("api")],
            commits={"api": [commit("a1", "alice", iso(2025, 2, 1))]},
            commit_stats={"api": {"a1": {"additions": 1}}},
            workflow_runs={
                "api": [
                    run("CI", iso(2025, 2, 1), "success"),
                    run("CI", iso(2025, 2, 2), "success"),
                    run("CI", iso(2025, 2, 3), "success"),
                    run("Release", iso(2025, 2, 4), "success"),
                ]
            },
        )

        report = analyze(data, options)

        assert report.repositories[0].deployment_count == 1

    def test_prefers_runs_on_the_default_branch(self, options):
        data = raw_data(
            repos=[repo("api", default_branch="main")],
            commits={"api": [commit("a1", "alice", iso(2025, 2, 1))]},
            commit_stats={"api": {"a1": {"additions": 1}}},
            workflow_runs={
                "api": [
                    run("CI", iso(2025, 2, 1), "success", branch="feature-x"),
                    run("CI", iso(2025, 2, 2), "failure", branch="feature-y"),
                    run("CI", iso(2025, 2, 3), "success", branch="main"),
                ]
            },
        )

        report = analyze(data, options)

        assert report.repositories[0].deployment_count == 1
        assert report.repositories[0].deployment_failures == 0

    def test_deployment_frequency_uses_the_window_length(self, options):
        runs = [run("Deploy", iso(2025, 2, day), "success") for day in range(1, 11)]
        data = raw_data(
            repos=[repo("api")],
            commits={"api": [commit("a1", "alice", iso(2025, 2, 1))]},
            commit_stats={"api": {"a1": {"additions": 1}}},
            workflow_runs={"api": runs},
        )

        report = analyze(data, options)

        assert report.dora.deploys_total == 10
        assert report.dora.deploys_per_day == pytest.approx(10 / 90, abs=0.01)


class TestReportShape:
    def test_splits_outliers_from_the_main_table(self, options):
        data = raw_data(
            repos=[repo("api")],
            commits={
                "api": [
                    commit("a1", "alice", iso(2025, 2, 1)),
                    commit("b1", "bulk", iso(2025, 2, 1)),
                ]
            },
            commit_stats={
                "api": {
                    "a1": {"additions": 500, "deletions": 10},
                    "b1": {"additions": 250_000, "deletions": 0},
                }
            },
        )

        report = analyze(data, options)

        assert [dev.name for dev in report.developers] == ["alice"]
        assert [dev.name for dev in report.outliers] == ["bulk"]

    def test_outlier_split_can_be_disabled(self, options):
        data = raw_data(
            repos=[repo("api")],
            commits={"api": [commit("b1", "bulk", iso(2025, 2, 1))]},
            commit_stats={"api": {"b1": {"additions": 250_000}}},
        )

        report = analyze(data, replace(options, outlier_threshold=0))

        assert [dev.name for dev in report.developers] == ["bulk"]
        assert report.outliers == []

    def test_drops_contributors_who_changed_no_code_by_default(self, options):
        data = raw_data(
            repos=[repo("api")],
            pr_reviews={
                "api": {
                    "1": [{"user": {"login": "bob"}, "submitted_at": iso(2025, 2, 2)}]
                }
            },
        )

        assert analyze(data, options).developers == []
        assert [
            dev.name
            for dev in analyze(data, replace(options, include_inactive=True)).developers
        ] == ["bob"]

    def test_drops_repositories_with_no_activity(self, options):
        data = raw_data(repos=[repo("api"), repo("dormant")])

        assert analyze(data, options).repositories == []

    def test_sorts_developers_by_lines_added(self, options):
        data = raw_data(
            repos=[repo("api")],
            commits={
                "api": [
                    commit("a", "alice", iso(2025, 2, 1)),
                    commit("b", "bob", iso(2025, 2, 1)),
                    commit("c", "carol", iso(2025, 2, 1)),
                ]
            },
            commit_stats={
                "api": {
                    "a": {"additions": 10},
                    "b": {"additions": 900},
                    "c": {"additions": 50},
                }
            },
        )

        report = analyze(data, options)

        assert [dev.name for dev in report.developers] == ["bob", "carol", "alice"]

    def test_handles_completely_empty_data(self, options):
        report = analyze(raw_data(), options)

        assert report.developers == []
        assert report.repositories == []
        assert report.dora.deploys_total == 0
        assert report.dora.lead_time_mean == 0.0

    def test_tolerates_malformed_timestamps(self, options):
        data = raw_data(
            repos=[repo("api")],
            commits={"api": [commit("a1", "alice", "not-a-date")]},
        )

        report = analyze(data, options)

        assert report.repositories == []


class TestDeploymentWorkflowSelection:
    def data_with_workflows(self, *names):
        return raw_data(
            repos=[repo("api")],
            commits={"api": [commit("a1", "alice", iso(2025, 2, 1))]},
            commit_stats={"api": {"a1": {"additions": 1}}},
            workflow_runs={
                "api": [
                    run(name, iso(2025, 2, i + 1), "success")
                    for i, name in enumerate(names)
                ]
            },
        )

    def test_records_the_workflow_it_counted(self, options):
        report = analyze(self.data_with_workflows("Deploy", "CI"), options)

        assert report.repositories[0].deployment_workflow == "deploy"

    def test_override_wins_over_the_heuristic(self, options):
        report = analyze(
            self.data_with_workflows("Deploy", "Nightly Smoke"),
            replace(options, deploy_workflow="Nightly Smoke"),
        )

        assert report.repositories[0].deployment_workflow == "nightly smoke"
        assert report.repositories[0].deployment_count == 1

    def test_override_matches_case_insensitively(self, options):
        report = analyze(
            self.data_with_workflows("Release Please"),
            replace(options, deploy_workflow="release please"),
        )

        assert report.repositories[0].deployment_workflow == "release please"

    def test_override_that_never_ran_counts_nothing(self, options):
        report = analyze(
            self.data_with_workflows("Deploy"),
            replace(options, deploy_workflow="does-not-exist"),
        )

        assert report.repositories[0].deployment_workflow is None
        assert report.repositories[0].deployment_count == 0

    def test_no_workflows_at_all_records_nothing(self, options):
        report = analyze(self.data_with_workflows(), options)

        assert report.repositories[0].deployment_workflow is None

    def test_cd_matches_as_a_word_but_not_inside_one(self, options):
        report = analyze(self.data_with_workflows("cdn-purge", "build and cd"), options)

        assert report.repositories[0].deployment_workflow == "build and cd"
