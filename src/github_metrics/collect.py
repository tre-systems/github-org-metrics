"""Collection of the raw GitHub payloads an analysis run needs.

Only data that feeds a reported metric is fetched. The expensive calls — per
commit stats and per pull request details — are issued concurrently, which is
what makes a full organization run finish in minutes rather than hours.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from typing import Any, TypeVar

from .client import GitHubClient
from .dates import parse_github_date, to_github_date
from .models import RawData

logger = logging.getLogger(__name__)

__all__ = ["CollectionOptions", "collect"]

#: Per-repository ceilings. These bound the cost of pathological repositories
#: (thousands of stale branches, a decade of CI history) without affecting
#: anything realistic.
MAX_BRANCHES = 1000
MAX_CONTRIBUTORS = 1000
MAX_WORKFLOW_RUNS = 1000

T = TypeVar("T")
R = TypeVar("R")

ProgressCallback = Callable[[str, int, int], None]


@dataclass(frozen=True)
class CollectionOptions:
    """Inputs that determine what gets fetched."""

    org: str
    since: datetime
    until: datetime
    target_repos: tuple[str, ...] | None = None
    max_repos: int | None = None
    fetch_pr_details: bool = True
    max_workers: int = 8


def collect(
    client: GitHubClient,
    options: CollectionOptions,
    *,
    on_repo_complete: ProgressCallback | None = None,
) -> RawData:
    """Fetch everything needed to analyse an organization.

    Args:
        client: The API client.
        options: What to fetch.
        on_repo_complete: Called with (repo name, completed, total) after each
            repository, for progress display.

    Returns:
        The raw payloads, ready to analyse or cache.
    """
    repos = _list_repositories(client, options)

    data: RawData = {
        "org": options.org,
        "since": to_github_date(options.since),
        "until": to_github_date(options.until),
        "fetch_pr_details": options.fetch_pr_details,
        "repos": repos,
        "commits": {},
        "commit_stats": {},
        "branch_counts": {},
        "contributor_counts": {},
        "pull_requests": {},
        "pr_reviews": {},
        "pr_comments": {},
        "pr_first_commit_dates": {},
        "workflow_runs": {},
    }

    with ThreadPoolExecutor(max_workers=options.max_workers) as pool:
        for index, repo in enumerate(repos, start=1):
            name = repo["name"]
            logger.info("Fetching %s (%d/%d)", name, index, len(repos))
            _collect_repository(client, options, data, name, pool)
            if on_repo_complete is not None:
                on_repo_complete(name, index, len(repos))

    return data


# ----------------------------------------------------------------------
# Repository discovery
# ----------------------------------------------------------------------


def _list_repositories(
    client: GitHubClient, options: CollectionOptions
) -> list[dict[str, Any]]:
    """Resolve the repositories to analyse."""
    if options.target_repos:
        return _fetch_named_repositories(client, options)

    since = options.since

    def past_window(repo: dict[str, Any]) -> bool:
        pushed = parse_github_date(repo.get("pushed_at"))
        return pushed is not None and pushed < since

    repos = client.paginate(
        f"/orgs/{options.org}/repos",
        {"type": "all", "sort": "pushed", "direction": "desc"},
        stop_after=past_window,
    )
    active = [repo for repo in repos if not past_window(repo)]

    if options.max_repos is not None:
        active = active[: options.max_repos]

    logger.info(
        "Analysing %d of %d repositories active since %s",
        len(active),
        len(repos),
        options.since.date(),
    )
    return active


def _fetch_named_repositories(
    client: GitHubClient, options: CollectionOptions
) -> list[dict[str, Any]]:
    """Fetch specific repositories by name, reporting any that are missing."""
    names = options.target_repos or ()
    repos: list[dict[str, Any]] = []
    missing: list[str] = []

    for name in names:
        repo = client.get(f"/repos/{options.org}/{name}")
        if isinstance(repo, dict):
            repos.append(repo)
        else:
            missing.append(name)

    if missing:
        logger.warning("Repositories not found or inaccessible: %s", ", ".join(missing))

    logger.info("Analysing %d target repositories", len(repos))
    return repos


# ----------------------------------------------------------------------
# Per-repository collection
# ----------------------------------------------------------------------


def _collect_repository(
    client: GitHubClient,
    options: CollectionOptions,
    data: RawData,
    repo: str,
    pool: ThreadPoolExecutor,
) -> None:
    org = options.org
    since_param = to_github_date(options.since)
    until_param = to_github_date(options.until)

    commits = client.paginate(
        f"/repos/{org}/{repo}/commits",
        {"since": since_param, "until": until_param},
    )
    data["commits"][repo] = commits
    data["commit_stats"][repo] = _fetch_commit_stats(client, org, repo, commits, pool)

    data["branch_counts"][repo] = len(
        client.paginate(f"/repos/{org}/{repo}/branches", max_items=MAX_BRANCHES)
    )
    data["contributor_counts"][repo] = len(
        client.paginate(f"/repos/{org}/{repo}/contributors", max_items=MAX_CONTRIBUTORS)
    )

    pull_requests = _fetch_pull_requests(client, options, repo)
    data["pull_requests"][repo] = pull_requests

    data["workflow_runs"][repo] = client.paginate_envelope(
        f"/repos/{org}/{repo}/actions/runs",
        "workflow_runs",
        {"created": f">={options.since.date().isoformat()}", "status": "completed"},
        max_items=MAX_WORKFLOW_RUNS,
    )

    reviews, comments, first_commits = _fetch_pull_request_details(
        client, options, repo, pull_requests, pool
    )
    data["pr_reviews"][repo] = reviews
    data["pr_comments"][repo] = comments
    data["pr_first_commit_dates"][repo] = first_commits

    logger.info(
        "%s: %d commits, %d pull requests, %d workflow runs",
        repo,
        len(commits),
        len(pull_requests),
        len(data["workflow_runs"][repo]),
    )


def _fetch_commit_stats(
    client: GitHubClient,
    org: str,
    repo: str,
    commits: list[dict[str, Any]],
    pool: ThreadPoolExecutor,
) -> dict[str, dict[str, int]]:
    """Fetch line counts for each commit.

    GitHub only returns additions and deletions from the single-commit
    endpoint, so this is one request per commit — the most expensive part of a
    run, and the reason the pool exists.
    """
    shas = [commit["sha"] for commit in commits if commit.get("sha")]
    if not shas:
        return {}

    def fetch(sha: str) -> tuple[str, dict[str, int] | None]:
        payload = client.get(f"/repos/{org}/{repo}/commits/{sha}")
        if isinstance(payload, dict) and isinstance(payload.get("stats"), dict):
            return sha, payload["stats"]
        return sha, None

    return {sha: stats for sha, stats in _map(pool, fetch, shas) if stats is not None}


def _fetch_pull_requests(
    client: GitHubClient, options: CollectionOptions, repo: str
) -> list[dict[str, Any]]:
    """Fetch pull requests touched during the window.

    Sorting by ``updated`` descending lets pagination stop at the first pull
    request older than the window instead of walking a repository's entire
    history.
    """
    since = options.since

    def past_window(pull: dict[str, Any]) -> bool:
        updated = parse_github_date(pull.get("updated_at"))
        return updated is not None and updated < since

    pulls = client.paginate(
        f"/repos/{options.org}/{repo}/pulls",
        {"state": "all", "sort": "updated", "direction": "desc"},
        stop_after=past_window,
    )
    return [pull for pull in pulls if not past_window(pull)]


def _fetch_pull_request_details(
    client: GitHubClient,
    options: CollectionOptions,
    repo: str,
    pulls: list[dict[str, Any]],
    pool: ThreadPoolExecutor,
) -> tuple[
    dict[str, list[dict[str, Any]]],
    dict[str, list[dict[str, Any]]],
    dict[str, str],
]:
    """Fetch reviews, comments, and branch start times for pull requests.

    In fast mode the per-pull-request calls are skipped entirely and lead time
    falls back to the pull request's creation date, which understates it by
    however long the branch existed before the pull request was opened.
    """
    org = options.org
    merged = [pull for pull in pulls if pull.get("merged_at")]

    if not options.fetch_pr_details:
        return {}, {}, {str(p["number"]): p["created_at"] for p in merged}

    def reviews_for(pull: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        number = pull["number"]
        return str(number), client.paginate(f"/repos/{org}/{repo}/pulls/{number}/reviews")

    def comments_for(pull: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        number = pull["number"]
        return str(number), client.paginate(
            f"/repos/{org}/{repo}/pulls/{number}/comments"
        )

    def first_commit_for(pull: dict[str, Any]) -> tuple[str, str | None]:
        """Find the earliest commit on a merged pull request's branch."""
        number = pull["number"]
        commits = client.paginate(f"/repos/{org}/{repo}/pulls/{number}/commits")
        dates = [
            date
            for date in (_commit_date(commit) for commit in commits)
            if date is not None
        ]
        if not dates:
            return str(number), pull.get("created_at")
        return str(number), to_github_date(min(dates))

    reviews = dict(_map(pool, reviews_for, pulls))
    comments = dict(_map(pool, comments_for, pulls))
    first_commits = {
        number: date
        for number, date in _map(pool, first_commit_for, merged)
        if date is not None
    }
    return reviews, comments, first_commits


def _commit_date(commit: dict[str, Any]) -> datetime | None:
    detail = commit.get("commit") or {}
    author = detail.get("author") or {}
    committer = detail.get("committer") or {}
    return parse_github_date(author.get("date") or committer.get("date"))


def _map(pool: ThreadPoolExecutor, func: Callable[[T], R], items: Iterable[T]) -> list[R]:
    """Run ``func`` over ``items`` concurrently, preserving input order."""
    return list(pool.map(func, items))
