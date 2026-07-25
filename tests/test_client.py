"""Tests for pagination, retries, and rate-limit handling."""

from __future__ import annotations

import pytest
import requests
import responses

from github_metrics.client import AuthenticationError, GitHubClient
from tests.conftest import request_json, requested_urls

BASE = "https://api.github.com"


@pytest.fixture
def sleeps() -> list[float]:
    """Collects the durations the client would have slept for."""
    return []


@pytest.fixture
def client(sleeps):
    return GitHubClient(
        "test-token",
        max_retries=2,
        sleep=sleeps.append,
        clock=lambda: 1_000.0,
    )


class TestRequests:
    @responses.activate
    def test_sends_authorization_and_api_version_headers(self, client):
        responses.get(f"{BASE}/repos/acme/api", json={"name": "api"})

        assert client.get("/repos/acme/api") == {"name": "api"}

        request = responses.calls[0].request
        assert request.headers["Authorization"] == "Bearer test-token"
        assert request.headers["X-GitHub-Api-Version"] == "2022-11-28"

    @responses.activate
    def test_missing_resource_returns_none(self, client):
        responses.get(f"{BASE}/repos/acme/gone", status=404, json={})

        assert client.get("/repos/acme/gone") is None

    @responses.activate
    def test_rejected_token_raises(self, client):
        responses.get(f"{BASE}/repos/acme/api", status=401, json={})

        with pytest.raises(AuthenticationError):
            client.get("/repos/acme/api")

    @responses.activate
    def test_forbidden_returns_none_without_retrying(self, client, sleeps):
        responses.get(f"{BASE}/repos/acme/api", status=403, json={"message": "denied"})

        assert client.get("/repos/acme/api") is None
        assert sleeps == []

    @responses.activate
    def test_malformed_json_returns_none(self, client):
        responses.get(f"{BASE}/repos/acme/api", body="{not json", status=200)

        assert client.get("/repos/acme/api") is None


class TestRetries:
    @responses.activate
    def test_retries_server_errors_then_succeeds(self, client, sleeps):
        responses.get(f"{BASE}/x", status=502, json={})
        responses.get(f"{BASE}/x", status=200, json={"ok": True})

        assert client.get("/x") == {"ok": True}
        assert sleeps == [1.0]

    @responses.activate
    def test_gives_up_after_the_retry_budget(self, client, sleeps):
        for _ in range(4):
            responses.get(f"{BASE}/x", status=500, json={})

        assert client.get("/x") is None
        assert sleeps == [1.0, 2.0]

    @responses.activate
    def test_retries_connection_errors(self, client, sleeps):
        responses.get(f"{BASE}/x", body=requests.ConnectionError("boom"))
        responses.get(f"{BASE}/x", json={"ok": True})

        assert client.get("/x") == {"ok": True}
        assert sleeps == [1.0]


class TestRateLimits:
    @responses.activate
    def test_waits_for_the_primary_rate_limit_reset(self, client, sleeps):
        responses.get(
            f"{BASE}/x",
            status=403,
            json={"message": "rate limited"},
            headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1060"},
        )
        responses.get(f"{BASE}/x", json={"ok": True})

        assert client.get("/x") == {"ok": True}
        assert sleeps == [61.0]  # reset - clock + 1

    @responses.activate
    def test_honours_retry_after(self, client, sleeps):
        responses.get(f"{BASE}/x", status=429, json={}, headers={"Retry-After": "30"})
        responses.get(f"{BASE}/x", json={"ok": True})

        assert client.get("/x") == {"ok": True}
        assert sleeps == [30.0]

    @responses.activate
    def test_handles_secondary_rate_limits(self, client, sleeps):
        responses.get(
            f"{BASE}/x",
            status=403,
            json={"message": "You have exceeded a secondary rate limit"},
        )
        responses.get(f"{BASE}/x", json={"ok": True})

        assert client.get("/x") == {"ok": True}
        assert sleeps == [60.0]

    @responses.activate
    def test_refuses_an_absurd_wait(self, client, sleeps):
        responses.get(f"{BASE}/x", status=429, json={}, headers={"Retry-After": "999999"})

        assert client.get("/x") is None
        assert sleeps == []


class TestPagination:
    @responses.activate
    def test_follows_link_headers(self, client):
        responses.get(
            f"{BASE}/items",
            json=[{"id": 1}],
            headers={"Link": f'<{BASE}/items?page=2>; rel="next"'},
        )
        responses.get(f"{BASE}/items?page=2", json=[{"id": 2}])

        assert client.paginate("/items") == [{"id": 1}, {"id": 2}]

    @responses.activate
    def test_requests_the_maximum_page_size(self, client):
        responses.get(f"{BASE}/items", json=[])

        client.paginate("/items", {"state": "all"})

        url = requested_urls(responses.calls)[0]
        assert "per_page=100" in url
        assert "state=all" in url

    @responses.activate
    def test_stops_at_max_items(self, client):
        responses.get(
            f"{BASE}/items",
            json=[{"id": 1}, {"id": 2}, {"id": 3}],
            headers={"Link": f'<{BASE}/items?page=2>; rel="next"'},
        )

        assert client.paginate("/items", max_items=2) == [{"id": 1}, {"id": 2}]
        assert len(responses.calls) == 1

    @responses.activate
    def test_stop_after_keeps_the_triggering_item(self, client):
        responses.get(
            f"{BASE}/items",
            json=[{"id": 1}, {"id": 2}, {"id": 3}],
            headers={"Link": f'<{BASE}/items?page=2>; rel="next"'},
        )

        items = client.paginate("/items", stop_after=lambda item: item["id"] == 2)

        assert items == [{"id": 1}, {"id": 2}]
        assert len(responses.calls) == 1

    @responses.activate
    def test_unwraps_envelope_endpoints(self, client):
        responses.get(
            f"{BASE}/runs",
            json={"total_count": 2, "workflow_runs": [{"id": 1}]},
            headers={"Link": f'<{BASE}/runs?page=2>; rel="next"'},
        )
        responses.get(f"{BASE}/runs?page=2", json={"workflow_runs": [{"id": 2}]})

        assert client.paginate_envelope("/runs", "workflow_runs") == [
            {"id": 1},
            {"id": 2},
        ]

    @responses.activate
    def test_stops_when_a_page_fails(self, client):
        responses.get(
            f"{BASE}/items",
            json=[{"id": 1}],
            headers={"Link": f'<{BASE}/items?page=2>; rel="next"'},
        )
        responses.get(f"{BASE}/items?page=2", status=404, json={})

        assert client.paginate("/items") == [{"id": 1}]


def test_requires_a_token():
    with pytest.raises(AuthenticationError):
        GitHubClient("")


class TestCount:
    @responses.activate
    def test_reads_the_total_from_the_last_link(self, client):
        responses.get(
            f"{BASE}/repos/acme/api/branches",
            json=[{"name": "main"}],
            headers={
                "Link": f'<{BASE}/repos/acme/api/branches?page=2>; rel="next", '
                f'<{BASE}/repos/acme/api/branches?page=37>; rel="last"'
            },
        )

        assert client.count("/repos/acme/api/branches") == 37
        assert len(responses.calls) == 1
        assert "per_page=1" in requested_urls(responses.calls)[0]

    @responses.activate
    def test_falls_back_to_the_body_on_a_single_page(self, client):
        responses.get(f"{BASE}/items", json=[{"id": 1}])

        assert client.count("/items") == 1

    @responses.activate
    def test_empty_endpoint_counts_zero(self, client):
        responses.get(f"{BASE}/items", json=[])

        assert client.count("/items") == 0

    @responses.activate
    def test_unavailable_endpoint_counts_zero(self, client):
        responses.get(f"{BASE}/items", status=404, json={})

        assert client.count("/items") == 0

    @responses.activate
    def test_ignores_an_unparsable_last_link(self, client):
        responses.get(
            f"{BASE}/items",
            json=[{"id": 1}],
            headers={"Link": f'<{BASE}/items?page=abc>; rel="last"'},
        )

        assert client.count("/items") == 1


class TestGraphQL:
    @responses.activate
    def test_posts_the_query_and_returns_data(self, client):
        responses.post(f"{BASE}/graphql", json={"data": {"repository": {"c0": {}}}})

        result = client.graphql("query {}", {"owner": "acme"})

        assert result == {"repository": {"c0": {}}}
        assert responses.calls[0].request.method == "POST"
        assert request_json(responses.calls[0])["variables"] == {"owner": "acme"}

    @responses.activate
    def test_returns_partial_data_alongside_errors(self, client):
        responses.post(
            f"{BASE}/graphql",
            json={"data": {"repository": {"c0": None}}, "errors": [{"message": "nope"}]},
        )

        assert client.graphql("query {}") == {"repository": {"c0": None}}

    @responses.activate
    def test_returns_none_when_the_query_fails(self, client):
        responses.post(f"{BASE}/graphql", status=502, json={})
        responses.post(f"{BASE}/graphql", status=502, json={})
        responses.post(f"{BASE}/graphql", status=502, json={})

        assert client.graphql("query {}") is None

    @responses.activate
    def test_rate_limits_apply_to_graphql_too(self, client, sleeps):
        responses.post(
            f"{BASE}/graphql", status=429, json={}, headers={"Retry-After": "5"}
        )
        responses.post(f"{BASE}/graphql", json={"data": {"ok": True}})

        assert client.graphql("query {}") == {"ok": True}
        assert sleeps == [5.0]
