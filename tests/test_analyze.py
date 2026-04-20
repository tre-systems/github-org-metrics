"""Tests for the analysis pipeline."""

from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd

from github_metrics.analyze import (
    DEVELOPER_COLUMNS,
    REPOSITORY_COLUMNS,
    analyze,
    compute_dora_for_repo,
    detect_ci_workflow,
)

SINCE = "2025-01-01T00:00:00Z"
SINCE_DT = datetime(2025, 1, 1, tzinfo=UTC)


def _repo(name: str = "r") -> dict:
    return {
        "name": name,
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2025-06-01T00:00:00Z",
        "language": "Python",
    }


def _commit(sha: str, login: str, date: str) -> dict:
    return {
        "sha": sha,
        "commit": {"author": {"date": date, "email": "a@x", "name": login}},
        "author": {"login": login},
    }


def _pr(
    number: int,
    login: str,
    created: str,
    updated: str | None = None,
    merged: str | None = None,
    branch: str = "feat",
    state: str = "closed",
) -> dict:
    return {
        "number": number,
        "state": state,
        "user": {"login": login},
        "created_at": created,
        "updated_at": updated or created,
        "merged_at": merged,
        "head": {"ref": branch},
    }


def _run(conclusion: str, at: str, end: str | None = None, name: str = "CI") -> dict:
    return {
        "name": name,
        "conclusion": conclusion,
        "created_at": at,
        "updated_at": end or at,
    }


def _empty_repo_data(name: str) -> dict:
    return {
        "repos": [_repo(name)],
        "fetch_pr_details": True,
        "commits": {name: []},
        "commit_stats": {name: {}},
        "branches": {name: []},
        "contributors": {name: []},
        "pull_requests": {name: []},
        "pr_reviews": {name: {}},
        "pr_comments": {name: {}},
        "branch_first_commits": {name: {}},
        "workflow_runs": {name: []},
    }


# ---------------------------------------------------------------------- DORA


def test_detect_ci_workflow_prefers_ci_keyword():
    runs = [
        {"name": "Lint"},
        {"name": "CI"},
        {"name": "CI"},
        {"name": "Release Notes"},
    ]
    assert detect_ci_workflow(runs) == "ci"


def test_detect_ci_workflow_falls_back_to_most_common():
    runs = [{"name": "Lint"}, {"name": "Lint"}, {"name": "Publish"}]
    assert detect_ci_workflow(runs) == "lint"


def test_detect_ci_workflow_returns_none_for_empty():
    assert detect_ci_workflow([]) is None


def test_compute_dora_counts_failures_and_successes():
    runs = [
        _run("success", "2025-02-01T00:00:00Z", "2025-02-01T00:05:00Z"),
        _run("failure", "2025-02-02T00:00:00Z", "2025-02-02T00:10:00Z"),
        _run("success", "2025-02-02T02:00:00Z", "2025-02-02T02:03:00Z"),
    ]
    stats = compute_dora_for_repo(runs, "ci", SINCE_DT)
    assert stats.deploys == 3
    assert stats.failures == 1
    assert round(stats.failure_rate, 2) == 33.33
    # Recovery: 02:00 - 00:00 on 02/02 = 2 hours.
    assert stats.recoveries == [2.0]


def test_compute_dora_collapses_consecutive_failures():
    runs = [
        _run("failure", "2025-02-01T00:00:00Z"),
        _run("failure", "2025-02-01T01:00:00Z"),
        _run("success", "2025-02-01T03:00:00Z"),
    ]
    stats = compute_dora_for_repo(runs, "ci", SINCE_DT)
    # First failure at 00:00, recovery at 03:00 -> 3.0h.
    # The second failure does not restart the clock.
    assert stats.recoveries == [3.0]
    assert stats.failures == 2


def test_compute_dora_ignores_runs_before_since():
    runs = [_run("success", "2024-12-31T00:00:00Z")]
    stats = compute_dora_for_repo(runs, "ci", SINCE_DT)
    assert stats.deploys == 0


# ---------------------------------------------------------------- top-level


def test_analyze_empty_repos_returns_empty_dataframes_with_columns():
    devs, repos, outliers = analyze({"repos": []}, SINCE)
    assert list(devs.columns) == DEVELOPER_COLUMNS
    assert list(repos.columns) == REPOSITORY_COLUMNS
    assert devs.empty and repos.empty and outliers.empty


def test_analyze_attributes_commits_and_lines_to_developer():
    data = _empty_repo_data("r")
    data["commits"]["r"] = [_commit("sha1", "alice", "2025-02-01T00:00:00Z")]
    data["commit_stats"]["r"] = {"sha1": {"additions": 10, "deletions": 3}}

    devs, repos, _ = analyze(data, SINCE)

    alice = devs[devs["Developer"] == "alice"].iloc[0]
    assert alice["Commits"] == 1
    assert alice["Lines Added"] == 10
    assert alice["Lines Deleted"] == 3
    assert repos.iloc[0]["Commits"] == 1


def test_analyze_excludes_bots():
    data = _empty_repo_data("r")
    data["commits"]["r"] = [
        _commit("s1", "dependabot[bot]", "2025-02-01T00:00:00Z"),
        _commit("s2", "alice", "2025-02-01T00:00:00Z"),
    ]
    data["commit_stats"]["r"] = {
        "s1": {"additions": 99, "deletions": 0},
        "s2": {"additions": 1, "deletions": 0},
    }
    devs, _, _ = analyze(data, SINCE)
    assert set(devs["Developer"]) == {"alice"}


def test_analyze_splits_outliers_above_threshold():
    data = _empty_repo_data("r")
    data["commits"]["r"] = [
        _commit("s1", "alice", "2025-02-01T00:00:00Z"),
        _commit("s2", "bob", "2025-02-01T00:00:00Z"),
    ]
    data["commit_stats"]["r"] = {
        "s1": {"additions": 500_000, "deletions": 0},  # outlier
        "s2": {"additions": 50, "deletions": 0},
    }
    devs, _, outliers = analyze(data, SINCE)
    assert list(devs["Developer"]) == ["bob"]
    assert list(outliers["Developer"]) == ["alice"]


def test_analyze_filters_devs_with_no_line_changes():
    """Zero-line contributors (e.g. GH-native merge commits only) fall out."""
    data = _empty_repo_data("r")
    data["commits"]["r"] = [_commit("s1", "alice", "2025-02-01T00:00:00Z")]
    # No commit_stats -> lines stay at 0.
    devs, _, _ = analyze(data, SINCE)
    assert devs.empty


def test_analyze_pr_merge_counts_and_branch_time():
    data = _empty_repo_data("r")
    data["pull_requests"]["r"] = [
        _pr(
            1,
            "alice",
            created="2025-02-01T00:00:00Z",
            merged="2025-02-02T00:00:00Z",
            branch="feat",
        )
    ]
    data["branch_first_commits"]["r"] = {
        "feat": {"commit": {"committer": {"date": "2025-01-31T00:00:00Z"}}}
    }
    # Add a commit so alice shows up after the lines-added filter.
    data["commits"]["r"] = [_commit("sha1", "alice", "2025-02-01T00:00:00Z")]
    data["commit_stats"]["r"] = {"sha1": {"additions": 1, "deletions": 0}}

    devs, repos, _ = analyze(data, SINCE)
    alice = devs[devs["Developer"] == "alice"].iloc[0]
    assert alice["PRs Opened"] == 1
    # Branch started 2025-01-31, merged 2025-02-02 -> 48h
    assert repos.iloc[0]["Branch->Merge (h)"] == 48.0
    assert repos.iloc[0]["PRs"] == 1


def test_analyze_fast_mode_reports_na_for_reviews_and_comments():
    data = _empty_repo_data("r")
    data["fetch_pr_details"] = False
    data["commits"]["r"] = [_commit("sha1", "alice", "2025-02-01T00:00:00Z")]
    data["commit_stats"]["r"] = {"sha1": {"additions": 1, "deletions": 0}}

    devs, _, _ = analyze(data, SINCE)
    row = devs[devs["Developer"] == "alice"].iloc[0]
    assert row["PRs Reviewed"] == "N/A"
    assert row["PR Comments"] == "N/A"


def test_analyze_ignores_commits_older_than_since():
    data = _empty_repo_data("r")
    data["commits"]["r"] = [_commit("sha1", "alice", "2024-12-01T00:00:00Z")]
    data["commit_stats"]["r"] = {"sha1": {"additions": 10, "deletions": 0}}

    devs, repos, _ = analyze(data, SINCE)
    assert devs.empty
    assert repos.empty  # repo has no activity in window -> dropped


def test_analyze_counts_pr_reviews_and_comments_from_payload():
    data = _empty_repo_data("r")
    data["commits"]["r"] = [_commit("sha1", "alice", "2025-02-01T00:00:00Z")]
    data["commit_stats"]["r"] = {"sha1": {"additions": 1, "deletions": 0}}
    data["pr_reviews"]["r"] = {
        1: [
            {"user": {"login": "alice"}, "submitted_at": "2025-02-05T00:00:00Z"},
            {"user": {"login": "alice"}, "submitted_at": "2024-11-01T00:00:00Z"},  # old
        ]
    }
    data["pr_comments"]["r"] = {
        1: [{"user": {"login": "alice"}, "created_at": "2025-02-06T00:00:00Z"}]
    }
    devs, _, _ = analyze(data, SINCE)
    alice = devs[devs["Developer"] == "alice"].iloc[0]
    assert alice["PRs Reviewed"] == 1
    assert alice["PR Comments"] == 1


def test_analyze_repository_df_has_expected_columns():
    data = _empty_repo_data("r")
    data["commits"]["r"] = [_commit("sha1", "alice", "2025-02-01T00:00:00Z")]
    data["commit_stats"]["r"] = {"sha1": {"additions": 1, "deletions": 0}}
    _, repos, _ = analyze(data, SINCE)
    assert list(repos.columns) == REPOSITORY_COLUMNS


def test_analyze_tolerates_legacy_workflow_runs_dict_shape():
    """v1 cache shape stored {workflow_runs: [...]} instead of a flat list."""
    data = _empty_repo_data("r")
    data["workflow_runs"]["r"] = {
        "workflow_runs": [_run("success", "2025-02-01T00:00:00Z", "2025-02-01T00:05:00Z")]
    }
    data["commits"]["r"] = [_commit("sha1", "alice", "2025-02-01T00:00:00Z")]
    data["commit_stats"]["r"] = {"sha1": {"additions": 1, "deletions": 0}}
    _, repos, _ = analyze(data, SINCE)
    assert isinstance(repos, pd.DataFrame)
    assert repos.iloc[0]["CI Runs"] == 1
