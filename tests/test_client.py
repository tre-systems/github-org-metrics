"""Tests for the GitHub API client: pagination, rate-limit, and error paths."""

from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from github_metrics.client import PAGE_SIZE, GitHubAPIClient


def _fake_response(status: int, json_body=None, text: str = "", headers=None):
    response = Mock()
    response.status_code = status
    response.json.return_value = json_body
    response.text = text
    response.headers = headers or {}
    return response


@pytest.fixture
def client():
    return GitHubAPIClient("fake-token")


def test_get_returns_json_on_200(client):
    with patch.object(client._session, "get") as mock_get:
        mock_get.return_value = _fake_response(200, {"ok": True})
        assert client._get("https://api/foo") == {"ok": True}


def test_get_returns_none_on_404(client):
    with patch.object(client._session, "get") as mock_get:
        mock_get.return_value = _fake_response(404, text="Not Found")
        assert client._get("https://api/foo") is None


def test_get_handles_permission_denied_403(client):
    with patch.object(client._session, "get") as mock_get:
        mock_get.return_value = _fake_response(
            403,
            text="Resource not accessible by personal access token",
            headers={"X-RateLimit-Remaining": "1000"},
        )
        assert client._get("https://api/foo") is None


def test_get_sleeps_and_retries_on_rate_limit(client):
    rate_limited = _fake_response(
        403,
        text="API rate limit exceeded",
        headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1000000"},
    )
    success = _fake_response(200, {"ok": True})
    with (
        patch.object(client._session, "get", side_effect=[rate_limited, success]),
        patch("github_metrics.client.time.sleep") as mock_sleep,
    ):
        result = client._get("https://api/foo")
    assert result == {"ok": True}
    assert mock_sleep.called


def test_paginate_stops_when_page_is_short(client):
    responses = [
        _fake_response(200, [{"id": i} for i in range(PAGE_SIZE)]),  # full page
        _fake_response(200, [{"id": PAGE_SIZE}]),  # short page, stop
    ]
    with patch.object(client._session, "get", side_effect=responses):
        items = client._paginate("https://api/list")
    assert len(items) == PAGE_SIZE + 1


def test_paginate_respects_max_items(client):
    responses = [_fake_response(200, [{"id": i} for i in range(PAGE_SIZE)])]
    with patch.object(client._session, "get", side_effect=responses):
        items = client._paginate("https://api/list", max_items=5)
    assert len(items) == 5


def test_get_org_repos_exits_early_when_page_is_past_window(client):
    """Once the sort by pushed desc crosses `since`, pagination stops."""
    page1 = [
        {"name": "a", "pushed_at": "2025-04-01T00:00:00Z"},
        {"name": "b", "pushed_at": "2025-03-15T00:00:00Z"},
        {"name": "c", "pushed_at": "2024-12-01T00:00:00Z"},  # past cutoff
    ] + [{"name": f"pad{i}", "pushed_at": "2024-11-01T00:00:00Z"} for i in range(PAGE_SIZE - 3)]
    # No second page should be requested.
    with patch.object(client, "_get", side_effect=[page1]) as mock_get:
        repos = client.get_org_repos("org", "2025-03-01T00:00:00Z")
    assert [r["name"] for r in repos] == ["a", "b"]
    assert mock_get.call_count == 1


def test_get_org_repos_respects_max_repos(client):
    full_page = [{"name": f"r{i}", "pushed_at": "2025-04-01T00:00:00Z"} for i in range(PAGE_SIZE)]
    with patch.object(client, "_get", side_effect=[full_page]):
        repos = client.get_org_repos("org", "2025-01-01T00:00:00Z", max_repos=3)
    assert len(repos) == 3


def test_get_org_repos_filters_to_target_repos(client):
    page1 = [
        {"name": "keep", "pushed_at": "2025-04-01T00:00:00Z"},
        {"name": "skip", "pushed_at": "2025-04-01T00:00:00Z"},
    ]
    with patch.object(client, "_get", side_effect=[page1]):
        repos = client.get_org_repos("org", "2025-01-01T00:00:00Z", target_repos=["keep"])
    assert [r["name"] for r in repos] == ["keep"]


def test_get_workflow_runs_paginates_until_past_since(client):
    page1_runs = {
        "workflow_runs": [
            {"id": 1, "created_at": "2025-04-01T00:00:00Z"},
            {"id": 2, "created_at": "2025-01-15T00:00:00Z"},  # past since
        ]
    }
    with patch.object(client, "_get", side_effect=[page1_runs]) as mock_get:
        runs = client.get_workflow_runs("o", "r", since="2025-03-01T00:00:00Z")
    assert [r["id"] for r in runs] == [1]
    assert mock_get.call_count == 1


def test_get_commit_stats_returns_stats_dict(client):
    payload = {"stats": {"additions": 10, "deletions": 2}}
    with patch.object(client, "_get", return_value=payload):
        assert client.get_commit_stats("o", "r", "abc") == {"additions": 10, "deletions": 2}


def test_get_commit_stats_handles_missing_stats(client):
    with patch.object(client, "_get", return_value={"sha": "abc"}):
        assert client.get_commit_stats("o", "r", "abc") is None
