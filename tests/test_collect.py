"""Tests for what gets fetched, and what deliberately does not."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta

import pytest
import responses

from github_metrics.client import GitHubClient
from github_metrics.collect import CollectionOptions, Collector, collect
from github_metrics.dates import to_github_date
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


def stub_graphql(*, stats: dict[str, dict[str, int]] | None = None) -> None:
    """Answer commit-stat queries, mapping each aliased object to its result."""

    def respond(request):
        query = json.loads(request.body)["query"]
        aliases = re.findall(r'(c\d+): object\(oid: "([^"]+)"\)', query)
        repository = {
            alias: (dict(stats[sha]) if stats and sha in stats else None)
            for alias, sha in aliases
        }
        return 200, {}, json.dumps({"data": {"repository": repository}})

    responses.add_callback(
        responses.POST,
        f"{BASE}/graphql",
        callback=respond,
        content_type="application/json",
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
    def test_collects_commit_line_counts_in_one_graphql_query(self, client):
        responses.get(f"{BASE}/orgs/acme/repos", json=[repo("api")])
        stub_repository("api", commits=[{"sha": "s1"}, {"sha": "s2"}])
        stub_graphql(
            stats={
                "s1": {"additions": 3, "deletions": 1},
                "s2": {"additions": 5, "deletions": 0},
            }
        )

        data = collect(client, options())

        assert data["commit_stats"]["api"] == {
            "s1": {"additions": 3, "deletions": 1},
            "s2": {"additions": 5, "deletions": 0},
        }
        graphql_calls = [
            url for url in requested_urls(responses.calls) if url.endswith("/graphql")
        ]
        assert len(graphql_calls) == 1

    @responses.activate
    def test_falls_back_to_rest_for_commits_graphql_could_not_resolve(self, client):
        responses.get(f"{BASE}/orgs/acme/repos", json=[repo("api")])
        stub_repository("api", commits=[{"sha": "s1"}, {"sha": "s2"}])
        stub_graphql(stats={"s1": {"additions": 3, "deletions": 1}})
        responses.get(
            f"{BASE}/repos/acme/api/commits/s2",
            json={"stats": {"additions": 9, "deletions": 2}},
        )

        data = collect(client, options())

        assert data["commit_stats"]["api"]["s2"] == {"additions": 9, "deletions": 2}

    @responses.activate
    def test_falls_back_entirely_when_graphql_is_unavailable(self, client):
        responses.get(f"{BASE}/orgs/acme/repos", json=[repo("api")])
        stub_repository("api", commits=[{"sha": "s1"}])
        responses.post(f"{BASE}/graphql", status=404, json={})
        responses.get(
            f"{BASE}/repos/acme/api/commits/s1",
            json={"stats": {"additions": 4, "deletions": 4}},
        )

        data = collect(client, options())

        assert data["commit_stats"]["api"] == {"s1": {"additions": 4, "deletions": 4}}

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
    def test_counts_branches_and_contributors_without_listing_them(self, client):
        responses.get(f"{BASE}/orgs/acme/repos", json=[repo("api")])
        responses.get(
            f"{BASE}/repos/acme/api/branches",
            json=[{"name": "main"}],
            headers={"Link": f'<{BASE}/repos/acme/api/branches?page=12>; rel="last"'},
        )
        responses.get(f"{BASE}/repos/acme/api/contributors", json=[{"login": "alice"}])
        responses.get(f"{BASE}/repos/acme/api/commits", json=[])
        responses.get(f"{BASE}/repos/acme/api/pulls", json=[])
        responses.get(f"{BASE}/repos/acme/api/actions/runs", json={"workflow_runs": []})

        data = collect(client, options())

        assert data["branch_counts"]["api"] == 12
        assert data["contributor_counts"]["api"] == 1
        branch_calls = [
            url for url in requested_urls(responses.calls) if "/branches" in url
        ]
        assert len(branch_calls) == 1

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


class TestResume:
    def unfinished(self, **overrides):
        """A checkpoint left behind by an interrupted run."""
        base = {
            "org": "acme",
            "since": to_github_date(SINCE),
            "until": to_github_date(UNTIL),
            "fetch_pr_details": True,
            "complete": False,
            "repos": [repo("done")],
            "commits": {"done": [{"sha": "cached"}]},
            "commit_stats": {"done": {"cached": {"additions": 7}}},
            "branch_counts": {"done": 4},
            "contributor_counts": {"done": 2},
            "pull_requests": {"done": []},
            "pr_reviews": {"done": {}},
            "pr_comments": {"done": {}},
            "pr_first_commit_dates": {"done": {}},
            "workflow_runs": {"done": []},
        }
        base.update(overrides)
        return base

    @responses.activate
    def test_skips_repositories_already_collected(self, client):
        responses.get(
            f"{BASE}/orgs/acme/repos",
            json=[
                repo("done", pushed_at=iso(2025, 3, 1)),
                repo("todo", pushed_at=iso(2025, 3, 1)),
            ],
        )
        stub_repository("todo")
        stub_graphql()

        data = collect(client, options(), resume_from=self.unfinished())

        assert data["commits"]["done"] == [{"sha": "cached"}]
        assert data["commit_stats"]["done"] == {"cached": {"additions": 7}}
        assert data["branch_counts"]["done"] == 4
        assert not any("/repos/acme/done/" in u for u in requested_urls(responses.calls))
        assert any("/repos/acme/todo/" in u for u in requested_urls(responses.calls))

    @responses.activate
    def test_marks_the_run_complete_when_it_finishes(self, client):
        responses.get(f"{BASE}/orgs/acme/repos", json=[repo("api")])
        stub_repository("api")

        assert collect(client, options())["complete"] is True

    @responses.activate
    def test_partial_data_is_available_after_a_failure(self, client):
        responses.get(f"{BASE}/orgs/acme/repos", json=[repo("api")])
        stub_repository("api")
        collector = Collector(client, options())

        def explode(*_args):
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            collector.run(explode)

        assert collector.data["commits"]["api"] == []
        assert collector.data["complete"] is False

    @responses.activate
    def test_refuses_to_resume_a_finished_run(self, client):
        responses.get(f"{BASE}/orgs/acme/repos", json=[repo("done")])
        stub_repository("done")
        stub_graphql()

        data = collect(client, options(), resume_from=self.unfinished(complete=True))

        assert data["commits"]["done"] == []

    @responses.activate
    def test_refuses_to_resume_a_different_window(self, client):
        responses.get(f"{BASE}/orgs/acme/repos", json=[repo("done")])
        stub_repository("done")
        stub_graphql()

        stale = self.unfinished(since=to_github_date(datetime(2024, 1, 1, tzinfo=UTC)))
        data = collect(client, options(), resume_from=stale)

        assert data["commits"]["done"] == []

    @responses.activate
    def test_refuses_to_resume_a_run_with_a_different_detail_level(self, client):
        responses.get(f"{BASE}/orgs/acme/repos", json=[repo("done")])
        stub_repository("done")
        stub_graphql()

        data = collect(
            client, options(fetch_pr_details=False), resume_from=self.unfinished()
        )

        assert data["commits"]["done"] == []

    @responses.activate
    def test_tolerates_the_clock_moving_between_attempts(self, client):
        responses.get(f"{BASE}/orgs/acme/repos", json=[repo("done")])
        stub_repository("done")
        stub_graphql()

        drifted = self.unfinished(until=to_github_date(UNTIL - timedelta(minutes=20)))
        data = collect(client, options(), resume_from=drifted)

        assert data["commits"]["done"] == [{"sha": "cached"}]
