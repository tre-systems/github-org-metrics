"""Orchestration: fetch all data needed for analysis from GitHub."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from github_metrics.client import GitHubAPIClient
from github_metrics.models import parse_github_date

logger = logging.getLogger(__name__)

DEFAULT_COMMIT_STATS_WORKERS = 10
DATA_SCHEMA_VERSION = 2  # Bumped when `fetch_data` output shape changes.


def _fetch_commit_stats(
    client: GitHubAPIClient,
    org: str,
    repo: str,
    commits: list[dict[str, Any]],
    *,
    workers: int,
) -> dict[str, dict[str, int] | None]:
    """Fetch per-commit stats concurrently.

    GitHub's commits list endpoint doesn't include additions/deletions, so
    we have to follow up with `/commits/{sha}` per commit. A thread pool
    sits inside the client's rate-limit retries without fuss because the
    client is session-safe.
    """
    if not commits:
        return {}
    stats: dict[str, dict[str, int] | None] = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(client.get_commit_stats, org, repo, c["sha"]): c["sha"] for c in commits
        }
        for future, sha in futures.items():
            stats[sha] = future.result()
    return stats


def fetch_data(
    client: GitHubAPIClient,
    org: str,
    since: str,
    target_repos: list[str] | None = None,
    *,
    fetch_pr_details: bool = True,
    max_prs_per_repo: int = 50,
    max_repos: int | None = None,
    commit_stats_workers: int = DEFAULT_COMMIT_STATS_WORKERS,
) -> dict[str, Any]:
    """Fetch every piece of data the analyzer needs for `org`.

    Returns a JSON-serialisable dict suitable for caching.

    Args:
        client: GitHub API client.
        org: Organization name.
        since: ISO-8601 cutoff; older data is ignored by the analyzer.
        target_repos: If set, only these repos are fetched.
        fetch_pr_details: If True, fetch per-PR reviews/comments (slow).
        max_prs_per_repo: Cap on recent PRs to hydrate with review/comment
            detail when `fetch_pr_details` is True.
        max_repos: When no `target_repos` are given, limit to the top N
            most-recently-pushed repos (None = all).
        commit_stats_workers: Thread-pool size for concurrent per-commit
            stats fetches.
    """
    data: dict[str, Any] = {
        "_schema": DATA_SCHEMA_VERSION,
        "fetch_pr_details": fetch_pr_details,
        "repos": client.get_org_repos(org, since, target_repos, max_repos),
        "commits": {},
        "commit_stats": {},
        "branches": {},
        "contributors": {},
        "pull_requests": {},
        "pr_reviews": {},
        "pr_comments": {},
        "branch_first_commits": {},
        "workflow_runs": {},
    }

    total_repos = len(data["repos"])
    for idx, repo in enumerate(data["repos"], start=1):
        name = repo["name"]
        logger.info("[%d/%d] Fetching data for %s", idx, total_repos, name)

        commits = client.get_commits(org, name, since)
        data["commits"][name] = commits
        data["commit_stats"][name] = _fetch_commit_stats(
            client, org, name, commits, workers=commit_stats_workers
        )

        data["branches"][name] = client.get_branches(org, name)
        data["contributors"][name] = client.get_contributors(org, name)
        data["pull_requests"][name] = client.get_pull_requests(org, name)
        data["workflow_runs"][name] = client.get_workflow_runs(org, name, since=since)

        data["pr_reviews"][name] = {}
        data["pr_comments"][name] = {}
        data["branch_first_commits"][name] = {}

        _hydrate_pr_details(
            client,
            org,
            name,
            data,
            since=since,
            fetch_pr_details=fetch_pr_details,
            max_prs_per_repo=max_prs_per_repo,
        )

    return data


def _hydrate_pr_details(
    client: GitHubAPIClient,
    org: str,
    repo: str,
    data: dict[str, Any],
    *,
    since: str,
    fetch_pr_details: bool,
    max_prs_per_repo: int,
) -> None:
    """Populate pr_reviews, pr_comments, and branch_first_commits for `repo`."""
    all_prs = data["pull_requests"][repo]

    if not fetch_pr_details:
        # Fast mode: skip per-PR calls, approximate branch start from PR open.
        for pr in all_prs:
            if pr.get("merged_at") and pr.get("head", {}).get("ref"):
                data["branch_first_commits"][repo][pr["head"]["ref"]] = {
                    "commit": {"committer": {"date": pr["created_at"]}}
                }
        return

    since_date = parse_github_date(since)
    recent_prs = [
        pr
        for pr in all_prs
        if parse_github_date(pr["created_at"]) >= since_date
        or parse_github_date(pr["updated_at"]) >= since_date
    ][:max_prs_per_repo]

    logger.info(
        "Fetching details for %d recent PRs in %s (of %d total)",
        len(recent_prs),
        repo,
        len(all_prs),
    )

    for pr in recent_prs:
        number = pr["number"]
        data["pr_reviews"][repo][number] = client.get_pull_request_reviews(org, repo, number)
        data["pr_comments"][repo][number] = client.get_pull_request_comments(org, repo, number)

        branch = pr.get("head", {}).get("ref")
        if not branch:
            continue
        if pr.get("state") == "open":
            data["branch_first_commits"][repo][branch] = client.get_branch_commits(
                org, repo, branch
            )
        elif pr.get("merged_at"):
            # Merged PRs: approximate branch start from PR open time.
            data["branch_first_commits"][repo][branch] = {
                "commit": {"committer": {"date": pr["created_at"]}}
            }
