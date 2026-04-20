"""Authenticated GitHub REST API client with rate-limit handling."""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

GITHUB_API_URL = "https://api.github.com"
REQUEST_TIMEOUT = 30
PAGE_SIZE = 100

logger = logging.getLogger(__name__)


def _querystring(params: dict[str, str]) -> str:
    return "&".join(f"{k}={v}" for k, v in params.items())


class GitHubAPIClient:
    """Thread-safe GitHub API client.

    Rate-limit responses (HTTP 403 with `X-RateLimit-Remaining: 0`) are
    handled transparently by sleeping until the reset epoch. Permission
    errors (403 "Resource not accessible") and 404s are logged and return
    `None` so callers can degrade gracefully.
    """

    def __init__(self, token: str, *, timeout: int = REQUEST_TIMEOUT) -> None:
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github.v3+json",
            }
        )

    # ------------------------------------------------------------------ core

    def _get(self, url: str) -> Any | None:
        """GET `url`; return decoded JSON, or `None` for permanent failures."""
        while True:
            try:
                response = self._session.get(url, timeout=self._timeout)
            except requests.RequestException as exc:
                logger.error("Request failed for %s: %s", url, exc)
                return None

            if response.status_code == 200:
                return response.json()

            if (
                response.status_code == 403
                and int(response.headers.get("X-RateLimit-Remaining", "1")) == 0
            ):
                reset = int(response.headers.get("X-RateLimit-Reset", time.time() + 60))
                sleep_for = max(1, reset - time.time() + 1)
                logger.warning("Rate limit exceeded; sleeping %.0fs.", sleep_for)
                time.sleep(sleep_for)
                continue

            if response.status_code == 403 and "Resource not accessible" in response.text:
                logger.warning("Permission denied for %s (check token scope).", url)
                return None

            if response.status_code == 404:
                logger.debug("Not found: %s", url)
                return None

            logger.error("GET %s -> %d: %s", url, response.status_code, response.text[:200])
            return None

    def _paginate(
        self,
        url: str,
        params: dict[str, str] | None = None,
        *,
        max_items: int | None = None,
    ) -> list[dict[str, Any]]:
        """Collect items from every page of a paginated endpoint."""
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            query = {
                **(params or {}),
                "page": str(page),
                "per_page": str(PAGE_SIZE),
            }
            page_items = self._get(f"{url}?{_querystring(query)}")
            if not isinstance(page_items, list) or not page_items:
                break
            items.extend(page_items)
            if max_items and len(items) >= max_items:
                return items[:max_items]
            if len(page_items) < PAGE_SIZE:
                break
            page += 1
        return items

    # -------------------------------------------------------------- endpoints

    def get_org_repos(
        self,
        org: str,
        since: str,
        target_repos: list[str] | None = None,
        max_repos: int | None = None,
    ) -> list[dict[str, Any]]:
        """List `org` repositories pushed since `since`, newest-push first.

        Results are sorted by push time descending, so we stop paginating as
        soon as a page contains no repos inside the window.
        """
        repos: list[dict[str, Any]] = []
        missing = set(target_repos or [])
        page = 1

        while True:
            url = (
                f"{GITHUB_API_URL}/orgs/{org}/repos?"
                f"type=all&sort=pushed&direction=desc"
                f"&page={page}&per_page={PAGE_SIZE}"
            )
            logger.info("Fetching repositories page %d", page)
            page_repos = self._get(url)
            if not isinstance(page_repos, list) or not page_repos:
                break

            in_window = [r for r in page_repos if r["pushed_at"] >= since]

            if target_repos:
                found = [r for r in in_window if r["name"] in target_repos]
                repos.extend(found)
                missing.difference_update(r["name"] for r in found)
                if not missing:
                    break
            else:
                repos.extend(in_window)
                if max_repos and len(repos) >= max_repos:
                    repos = repos[:max_repos]
                    break

            # Page sorted newest-push-first: fewer-in-window means we're done.
            if len(in_window) < len(page_repos) or len(page_repos) < PAGE_SIZE:
                break
            page += 1

        if target_repos and missing:
            logger.warning("Target repositories not found: %s", ", ".join(sorted(missing)))
        logger.info("Repositories to analyze: %d", len(repos))
        return repos

    # Commits ----------------------------------------------------------------

    def get_commits(self, org: str, repo: str, since: str) -> list[dict[str, Any]]:
        commits = self._paginate(f"{GITHUB_API_URL}/repos/{org}/{repo}/commits", {"since": since})
        logger.info("Commits for %s: %d", repo, len(commits))
        return commits

    def get_commit_stats(self, org: str, repo: str, sha: str) -> dict[str, int] | None:
        data = self._get(f"{GITHUB_API_URL}/repos/{org}/{repo}/commits/{sha}")
        if isinstance(data, dict):
            stats = data.get("stats")
            if isinstance(stats, dict):
                return stats
        return None

    def get_branch_commits(self, org: str, repo: str, branch: str) -> dict[str, Any] | None:
        """Return the oldest commit from the first page reachable from `branch`.

        This is an approximation of "first commit on the branch" and is only
        accurate when the branch has ≤100 commits; long-lived branches will
        see an oldest-commit estimate that's still useful for DORA lead-time
        bucketing.
        """
        commits = self._get(
            f"{GITHUB_API_URL}/repos/{org}/{repo}/commits?sha={branch}&per_page={PAGE_SIZE}"
        )
        if isinstance(commits, list) and commits:
            return commits[-1]
        return None

    # Pull requests ----------------------------------------------------------

    def get_pull_requests(self, org: str, repo: str, state: str = "all") -> list[dict[str, Any]]:
        prs = self._paginate(f"{GITHUB_API_URL}/repos/{org}/{repo}/pulls", {"state": state})
        logger.info("Pull requests for %s: %d", repo, len(prs))
        return prs

    def get_pull_request_reviews(self, org: str, repo: str, pr_number: int) -> list[dict[str, Any]]:
        result = self._get(f"{GITHUB_API_URL}/repos/{org}/{repo}/pulls/{pr_number}/reviews")
        return result if isinstance(result, list) else []

    def get_pull_request_comments(
        self, org: str, repo: str, pr_number: int
    ) -> list[dict[str, Any]]:
        result = self._get(f"{GITHUB_API_URL}/repos/{org}/{repo}/pulls/{pr_number}/comments")
        return result if isinstance(result, list) else []

    # Repo meta --------------------------------------------------------------

    def get_branches(self, org: str, repo: str) -> list[dict[str, Any]]:
        result = self._get(f"{GITHUB_API_URL}/repos/{org}/{repo}/branches")
        return result if isinstance(result, list) else []

    def get_contributors(self, org: str, repo: str) -> list[dict[str, Any]]:
        result = self._get(f"{GITHUB_API_URL}/repos/{org}/{repo}/contributors")
        return result if isinstance(result, list) else []

    # Workflow runs ----------------------------------------------------------

    def get_workflow_runs(
        self, org: str, repo: str, since: str | None = None
    ) -> list[dict[str, Any]]:
        """Return workflow runs, stopping pagination once past `since`."""
        runs: list[dict[str, Any]] = []
        page = 1
        while True:
            data = self._get(
                f"{GITHUB_API_URL}/repos/{org}/{repo}/actions/runs?page={page}&per_page={PAGE_SIZE}"
            )
            if not isinstance(data, dict):
                break
            page_runs = data.get("workflow_runs") or []
            if not page_runs:
                break
            if since:
                in_window = [r for r in page_runs if r.get("created_at", "") >= since]
                runs.extend(in_window)
                if len(in_window) < len(page_runs):
                    break
            else:
                runs.extend(page_runs)
            if len(page_runs) < PAGE_SIZE:
                break
            page += 1
        return runs
