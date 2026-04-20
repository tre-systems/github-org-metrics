"""Tests for fetch orchestration and PR branch timing."""

from __future__ import annotations

from typing import Any, cast

from github_metrics.fetch import fetch_data


class StubClient:
    def __init__(self) -> None:
        self.review_calls = 0
        self.comment_calls = 0
        self.pr_commit_calls = 0

    def get_org_repos(
        self,
        org: str,
        since: str,
        target_repos: list[str] | None = None,
        max_repos: int | None = None,
    ) -> list[dict[str, Any]]:
        return [
            {
                "name": "api",
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2025-02-10T00:00:00Z",
                "language": "Python",
            }
        ]

    def get_commits(self, org: str, repo: str, since: str) -> list[dict[str, Any]]:
        return []

    def get_commit_stats(self, org: str, repo: str, sha: str) -> dict[str, int] | None:
        return None

    def get_branches(self, org: str, repo: str) -> list[dict[str, Any]]:
        return []

    def get_contributors(self, org: str, repo: str) -> list[dict[str, Any]]:
        return []

    def get_pull_requests(self, org: str, repo: str, state: str = "all") -> list[dict[str, Any]]:
        return [
            {
                "number": 1,
                "state": "closed",
                "user": {"login": "alice"},
                "created_at": "2025-02-05T00:00:00Z",
                "updated_at": "2025-02-06T00:00:00Z",
                "merged_at": "2025-02-07T00:00:00Z",
                "head": {"ref": "feat"},
            }
        ]

    def get_workflow_runs(
        self, org: str, repo: str, since: str | None = None
    ) -> list[dict[str, Any]]:
        return []

    def get_pull_request_reviews(self, org: str, repo: str, pr_number: int) -> list[dict[str, Any]]:
        self.review_calls += 1
        return [{"user": {"login": "reviewer"}, "submitted_at": "2025-02-06T12:00:00Z"}]

    def get_pull_request_comments(
        self, org: str, repo: str, pr_number: int
    ) -> list[dict[str, Any]]:
        self.comment_calls += 1
        return [{"user": {"login": "reviewer"}, "created_at": "2025-02-06T13:00:00Z"}]

    def get_pull_request_commits(self, org: str, repo: str, pr_number: int) -> list[dict[str, Any]]:
        self.pr_commit_calls += 1
        return [
            {"commit": {"committer": {"date": "2025-02-03T00:00:00Z"}}},
            {"commit": {"committer": {"date": "2025-02-01T00:00:00Z"}}},
            {"commit": {"author": {"date": "2025-02-02T00:00:00Z"}}},
        ]


def test_fetch_data_uses_pr_commit_dates_for_merged_prs():
    client = StubClient()

    data = fetch_data(cast(Any, client), "org", "2025-01-01T00:00:00Z")

    assert data["branch_first_commits"]["api"]["feat"]["commit"]["committer"]["date"] == (
        "2025-02-01T00:00:00Z"
    )
    assert client.review_calls == 1
    assert client.comment_calls == 1
    assert client.pr_commit_calls == 1


def test_fetch_data_fast_mode_skips_reviews_but_keeps_pr_commit_timing():
    client = StubClient()

    data = fetch_data(cast(Any, client), "org", "2025-01-01T00:00:00Z", fetch_pr_details=False)

    assert data["pr_reviews"]["api"] == {}
    assert data["pr_comments"]["api"] == {}
    assert data["branch_first_commits"]["api"]["feat"]["commit"]["committer"]["date"] == (
        "2025-02-01T00:00:00Z"
    )
    assert client.review_calls == 0
    assert client.comment_calls == 0
    assert client.pr_commit_calls == 1
