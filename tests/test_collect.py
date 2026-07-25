"""Tests for what gets fetched, and what deliberately does not."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import responses

from github_metrics.client import GitHubClient
from github_metrics.collect import CollectionOptions, collect
from tests.conftest import iso, pull, repo, requested_urls

BASE = "https://api.github.com"
SINCE = datetime(2025, 1, 1, tzinfo=UTC)
UNTIL = datetime(2025, 4, 1, tzinfo=UTC)


@pytest.fixture
def client():
    return GitHubClient("test-token", sleep=lambda _: None)


def options(**overrides):
    return CollectionOptions(
        org="acme", since=SINCE, until=UNTIL, max_workers=2, **overrides
    )


def stub_repository(name: str, **payloads) -> None:
    """Register empty responses for every per-repository endpoint."""
    prefix = f"{BASE}/repos/acme/{name}"
    responses.get(f"{prefix}/commits", json=payloads.get("commits", []))
    responses.get(f"{prefix}/branches", json=payloads.get("branches", []))
    responses.get(f"{prefix}/contributors", json=payloads.get("contributors", []))
    responses.get(f"{prefix}/pulls", json=payloads.get("pulls", []))
    responses.get(
        f"{prefix}/actions/runs",
        json={"workflow_runs": payloads.get("runs", [])},
    )


class TestRepositoryDiscovery:
    @responses.activate
    def test_skips_repositories_untouched_during_the_window(self, client):
        responses.get(
            f"{BASE}/orgs/acme/repos",
            json=[
                repo("fresh", pushed_at=iso(2025, 3, 1)),
                repo("stale", pushed_at=iso(2024, 6, 1)),
            ],
        )
        stub_repository("fresh")

        data = collect(client, options())

        assert [r["name"] for r in data["repos"]] == ["fresh"]

    @responses.activate
    def test_repos_limit_is_applied(self, client):
        responses.get(
            f"{BASE}/orgs/acme/repos",
            json=[repo(name, pushed_at=iso(2025, 3, 1)) for name in ("a", "b", "c")],
        )
        stub_repository("a")

        data = collect(client, options(max_repos=1))

        assert [r["name"] for r in data["repos"]] == ["a"]

    @responses.activate
    def test_named_repositories_are_fetched_directly(self, client):
        responses.get(f"{BASE}/repos/acme/api", json=repo("api"))
        stub_repository("api")

        data = collect(client, options(target_repos=("api",)))

        assert [r["name"] for r in data["repos"]] == ["api"]
        assert not any("/orgs/" in url for url in requested_urls(responses.calls))

    @responses.activate
    def test_missing_named_repositories_are_reported(self, client, caplog):
        responses.get(f"{BASE}/repos/acme/ghost", status=404, json={})

        data = collect(client, options(target_repos=("ghost",)))

        assert data["repos"] == []
        assert "ghost" in caplog.text


class TestRepositoryData:
    @responses.activate
    def test_collects_commits_with_line_counts(self, client):
        responses.get(f"{BASE}/orgs/acme/repos", json=[repo("api")])
        stub_repository("api", commits=[{"sha": "s1"}, {"sha": "s2"}])
        responses.get(
            f"{BASE}/repos/acme/api/commits/s1",
            json={"stats": {"additions": 3, "deletions": 1}},
        )
        responses.get(f"{BASE}/repos/acme/api/commits/s2", json={"no": "stats"})

        data = collect(client, options())

        assert data["commit_stats"]["api"] == {"s1": {"additions": 3, "deletions": 1}}

    @responses.activate
    def test_bounds_the_commit_query_to_the_window(self, client):
        responses.get(f"{BASE}/orgs/acme/repos", json=[repo("api")])
        stub_repository("api")

        collect(client, options())

        commits_url = next(
            url for url in requested_urls(responses.calls) if "/commits" in url
        )
        assert "since=2025-01-01T00%3A00%3A00Z" in commits_url
        assert "until=2025-04-01T00%3A00%3A00Z" in commits_url

    @responses.activate
    def test_counts_branches_and_contributors_without_storing_them(self, client):
        responses.get(f"{BASE}/orgs/acme/repos", json=[repo("api")])
        stub_repository(
            "api",
            branches=[{"name": "main"}, {"name": "dev"}],
            contributors=[{"login": "alice"}],
        )

        data = collect(client, options())

        assert data["branch_counts"]["api"] == 2
        assert data["contributor_counts"]["api"] == 1

    @responses.activate
    def test_requests_only_completed_workflow_runs_in_the_window(self, client):
        responses.get(f"{BASE}/orgs/acme/repos", json=[repo("api")])
        stub_repository("api", runs=[{"id": 1, "name": "CI"}])

        data = collect(client, options())

        runs_url = next(
            url for url in requested_urls(responses.calls) if "/actions/runs" in url
        )
        assert "status=completed" in runs_url
        assert "created=%3E%3D2025-01-01" in runs_url
        assert data["workflow_runs"]["api"] == [{"id": 1, "name": "CI"}]

    @responses.activate
    def test_does_not_fetch_endpoints_it_never_reports_on(self, client):
        responses.get(f"{BASE}/orgs/acme/repos", json=[repo("api")])
        stub_repository("api")

        collect(client, options())

        fetched = " ".join(requested_urls(responses.calls))
        for unused in ("/deployments", "/releases", "/tags", "/issues"):
            assert unused not in fetched


class TestPullRequestDetails:
    @responses.activate
    def test_lead_time_uses_the_earliest_commit_on_the_branch(self, client):
        responses.get(f"{BASE}/orgs/acme/repos", json=[repo("api")])
        stub_repository(
            "api",
            pulls=[pull(7, "alice", iso(2025, 2, 5), merged=iso(2025, 2, 6))],
        )
        responses.get(f"{BASE}/repos/acme/api/pulls/7/reviews", json=[])
        responses.get(f"{BASE}/repos/acme/api/pulls/7/comments", json=[])
        responses.get(
            f"{BASE}/repos/acme/api/pulls/7/commits",
            json=[
                {"commit": {"author": {"date": iso(2025, 2, 4, 9)}}},
                {"commit": {"author": {"date": iso(2025, 2, 3, 9)}}},
            ],
        )

        data = collect(client, options())

        assert data["pr_first_commit_dates"]["api"] == {"7": "2025-02-03T09:00:00Z"}

    @responses.activate
    def test_fast_mode_skips_per_pull_request_calls(self, client):
        responses.get(f"{BASE}/orgs/acme/repos", json=[repo("api")])
        stub_repository(
            "api",
            pulls=[pull(7, "alice", iso(2025, 2, 5), merged=iso(2025, 2, 6))],
        )

        data = collect(client, options(fetch_pr_details=False))

        assert data["pr_reviews"] == {"api": {}}
        assert data["pr_first_commit_dates"]["api"] == {"7": iso(2025, 2, 5)}
        assert not any("/pulls/7/" in url for url in requested_urls(responses.calls))

    @responses.activate
    def test_branch_history_is_only_fetched_for_merged_pull_requests(self, client):
        responses.get(f"{BASE}/orgs/acme/repos", json=[repo("api")])
        stub_repository("api", pulls=[pull(7, "alice", iso(2025, 2, 5))])
        responses.get(f"{BASE}/repos/acme/api/pulls/7/reviews", json=[])
        responses.get(f"{BASE}/repos/acme/api/pulls/7/comments", json=[])

        collect(client, options())

        assert not any(
            "/pulls/7/commits" in url for url in requested_urls(responses.calls)
        )

    @responses.activate
    def test_reports_progress_per_repository(self, client):
        responses.get(
            f"{BASE}/orgs/acme/repos",
            json=[
                repo("a", pushed_at=iso(2025, 3, 1)),
                repo("b", pushed_at=iso(2025, 3, 1)),
            ],
        )
        stub_repository("a")
        stub_repository("b")
        seen = []

        collect(client, options(), on_repo_complete=lambda *args: seen.append(args))

        assert seen == [("a", 1, 2), ("b", 2, 2)]
