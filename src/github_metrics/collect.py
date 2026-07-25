"""Collection of the raw GitHub payloads an analysis run needs.

Only data that feeds a reported metric is fetched. The two expensive parts are
handled deliberately: commit line counts are batched through GraphQL (one
request per few dozen commits instead of one each), and per-pull-request calls
are issued concurrently.

Collection is resumable. :class:`Collector` exposes the partial data as it
fills, so a caller can checkpoint it, and a later run can be given that
checkpoint to skip repositories it already has.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

from .client import GitHubClient
from .dates import parse_github_date, to_github_date
from .models import RawData

logger = logging.getLogger(__name__)

__all__ = ["CollectionOptions", "Collector", "collect"]

#: Per-repository ceilings, bounding the cost of pathological repositories
#: (a decade of CI history) without affecting anything realistic.
MAX_WORKFLOW_RUNS = 1000

#: How far a checkpoint's window may drift from the requested one and still be
#: resumable. An interrupted run is normally retried within minutes; this is
#: slack for the clock moving, not for analysing a different period.
RESUME_WINDOW_TOLERANCE = timedelta(days=2)

#: Commits per GraphQL query. GitHub costs a query by the nodes it returns, so
#: this trades one large request against several small ones; 50 keeps the
#: document well inside the complexity limit.
COMMIT_STATS_BATCH = 50

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


class Collector:
    """Fetches everything an analysis run needs, one repository at a time.

    The partial result is available as :attr:`data` throughout, so an
    interrupted run can still be cached and resumed.
    """

    def __init__(
        self,
        client: GitHubClient,
        options: CollectionOptions,
        *,
        resume_from: RawData | None = None,
    ) -> None:
        """Prepare a collection run.

        Args:
            client: The API client.
            options: What to fetch.
            resume_from: Data from an earlier run. Repositories already present
                are reused, provided it covers the same window and detail level.
        """
        self.client = client
        self.options = options
        self.data = _empty(options)
        self._reusable = _reusable_repositories(resume_from, options)
        if self._reusable:
            self._resume_from: RawData | None = resume_from
            logger.info(
                "Resuming: %d repositories already collected", len(self._reusable)
            )
        else:
            self._resume_from = None

    def run(self, on_repo_complete: ProgressCallback | None = None) -> RawData:
        """Fetch every selected repository.

        Args:
            on_repo_complete: Called with (repo name, completed, total) after
                each repository, for progress display and checkpointing.

        Returns:
            The collected payloads. Also available as :attr:`data`, including
            after an interruption.
        """
        repos = _list_repositories(self.client, self.options)
        self.data["repos"] = repos

        with ThreadPoolExecutor(max_workers=self.options.max_workers) as pool:
            for index, repo in enumerate(repos, start=1):
                name = repo["name"]
                if name in self._reusable:
                    self._reuse(name)
                    logger.debug("Reusing cached data for %s", name)
                else:
                    logger.debug("Fetching %s (%d/%d)", name, index, len(repos))
                    self._collect_repository(name, pool)

                if on_repo_complete is not None:
                    on_repo_complete(name, index, len(repos))

        self.data["complete"] = True
        return self.data

    # ------------------------------------------------------------------
    # Per-repository collection
    # ------------------------------------------------------------------

    def _reuse(self, repo: str) -> None:
        if self._resume_from is None:  # pragma: no cover - guarded by _reusable
            return
        # Indexed by a runtime key, so step outside the TypedDict here.
        source = cast(dict[str, Any], self._resume_from)
        target = cast(dict[str, Any], self.data)
        for key in _PER_REPO_KEYS:
            existing = source.get(key, {}).get(repo)
            if existing is not None:
                target[key][repo] = existing

    def _collect_repository(self, repo: str, pool: ThreadPoolExecutor) -> None:
        client = self.client
        org = self.options.org
        data = self.data

        commits = client.paginate(
            f"/repos/{org}/{repo}/commits",
            {
                "since": to_github_date(self.options.since),
                "until": to_github_date(self.options.until),
            },
        )
        data["commits"][repo] = commits
        data["commit_stats"][repo] = self._commit_stats(repo, commits, pool)

        data["branch_counts"][repo] = client.count(f"/repos/{org}/{repo}/branches")
        data["contributor_counts"][repo] = client.count(
            f"/repos/{org}/{repo}/contributors"
        )

        pulls = self._pull_requests(repo)
        data["pull_requests"][repo] = pulls

        data["workflow_runs"][repo] = client.paginate_envelope(
            f"/repos/{org}/{repo}/actions/runs",
            "workflow_runs",
            {
                "created": f">={self.options.since.date().isoformat()}",
                "status": "completed",
            },
            max_items=MAX_WORKFLOW_RUNS,
        )

        reviews, comments, first_commits = self._pull_request_details(repo, pulls, pool)
        data["pr_reviews"][repo] = reviews
        data["pr_comments"][repo] = comments
        data["pr_first_commit_dates"][repo] = first_commits

        logger.info(
            "%s: %d commits, %d pull requests, %d workflow runs",
            repo,
            len(commits),
            len(pulls),
            len(data["workflow_runs"][repo]),
        )

    def _commit_stats(
        self, repo: str, commits: list[dict[str, Any]], pool: ThreadPoolExecutor
    ) -> dict[str, dict[str, int]]:
        """Fetch line counts for each commit, preferring GraphQL.

        REST returns line counts only from the single-commit endpoint, one
        request per commit. GraphQL answers for a whole batch at once, so it is
        tried first and REST fills in whatever it could not resolve.
        """
        shas = [commit["sha"] for commit in commits if commit.get("sha")]
        if not shas:
            return {}

        stats = self._commit_stats_graphql(repo, shas)

        missing = [sha for sha in shas if sha not in stats]
        if missing:
            logger.debug(
                "Falling back to REST for %d of %d commits in %s",
                len(missing),
                len(shas),
                repo,
            )
            stats.update(self._commit_stats_rest(repo, missing, pool))

        return stats

    def _commit_stats_graphql(
        self, repo: str, shas: list[str]
    ) -> dict[str, dict[str, int]]:
        stats: dict[str, dict[str, int]] = {}

        for batch in _chunked(shas, COMMIT_STATS_BATCH):
            payload = self.client.graphql(
                _commit_stats_query(batch),
                {"owner": self.options.org, "name": repo},
            )
            repository = (payload or {}).get("repository")
            if not isinstance(repository, dict):
                # GraphQL unavailable or rejected: let REST handle the rest.
                return stats

            for index, sha in enumerate(batch):
                node = repository.get(f"c{index}")
                if isinstance(node, dict) and "additions" in node:
                    stats[sha] = {
                        "additions": int(node.get("additions") or 0),
                        "deletions": int(node.get("deletions") or 0),
                    }

        return stats

    def _commit_stats_rest(
        self, repo: str, shas: list[str], pool: ThreadPoolExecutor
    ) -> dict[str, dict[str, int]]:
        org = self.options.org

        def fetch(sha: str) -> tuple[str, dict[str, int] | None]:
            payload = self.client.get(f"/repos/{org}/{repo}/commits/{sha}")
            if isinstance(payload, dict) and isinstance(payload.get("stats"), dict):
                return sha, payload["stats"]
            return sha, None

        return {sha: s for sha, s in _map(pool, fetch, shas) if s is not None}

    def _pull_requests(self, repo: str) -> list[dict[str, Any]]:
        """Fetch pull requests touched during the window.

        Sorting by ``updated`` descending lets pagination stop at the first
        pull request older than the window instead of walking all of history.
        """
        since = self.options.since

        def past_window(pull: dict[str, Any]) -> bool:
            updated = parse_github_date(pull.get("updated_at"))
            return updated is not None and updated < since

        pulls = self.client.paginate(
            f"/repos/{self.options.org}/{repo}/pulls",
            {"state": "all", "sort": "updated", "direction": "desc"},
            stop_after=past_window,
        )
        return [pull for pull in pulls if not past_window(pull)]

    def _pull_request_details(
        self, repo: str, pulls: list[dict[str, Any]], pool: ThreadPoolExecutor
    ) -> tuple[
        dict[str, list[dict[str, Any]]],
        dict[str, list[dict[str, Any]]],
        dict[str, str],
    ]:
        """Fetch reviews, comments, and branch start times for pull requests.

        In fast mode the per-pull-request calls are skipped and lead time falls
        back to the pull request's creation date, which understates it by
        however long the branch existed before the pull request was opened.
        """
        org = self.options.org
        client = self.client
        merged = [pull for pull in pulls if pull.get("merged_at")]

        if not self.options.fetch_pr_details:
            return {}, {}, {str(p["number"]): p["created_at"] for p in merged}

        def reviews_for(pull: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
            number = pull["number"]
            return str(number), client.paginate(
                f"/repos/{org}/{repo}/pulls/{number}/reviews"
            )

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


def collect(
    client: GitHubClient,
    options: CollectionOptions,
    *,
    resume_from: RawData | None = None,
    on_repo_complete: ProgressCallback | None = None,
) -> RawData:
    """Fetch everything needed to analyse an organization.

    Args:
        client: The API client.
        options: What to fetch.
        resume_from: Data from an earlier run to reuse where possible.
        on_repo_complete: Called after each repository, for progress display.

    Returns:
        The raw payloads, ready to analyse or cache.
    """
    return Collector(client, options, resume_from=resume_from).run(on_repo_complete)


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
# Helpers
# ----------------------------------------------------------------------

#: The RawData fields keyed by repository name.
_PER_REPO_KEYS = (
    "commits",
    "commit_stats",
    "branch_counts",
    "contributor_counts",
    "pull_requests",
    "pr_reviews",
    "pr_comments",
    "pr_first_commit_dates",
    "workflow_runs",
)


def _empty(options: CollectionOptions) -> RawData:
    return {
        "org": options.org,
        "since": to_github_date(options.since),
        "until": to_github_date(options.until),
        "fetch_pr_details": options.fetch_pr_details,
        "complete": False,
        "repos": [],
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


def _reusable_repositories(
    resume_from: RawData | None, options: CollectionOptions
) -> frozenset[str]:
    """Repositories in a checkpoint that this run may reuse verbatim.

    Only an *unfinished* run is resumable. A completed cache is a finished
    answer to an earlier question, and silently folding it into a new run would
    mix two different windows; ``--use-cache`` is how you ask for that data.
    """
    if not resume_from or resume_from.get("complete", True):
        return frozenset()

    same_run = (
        resume_from.get("org") == options.org
        and bool(resume_from.get("fetch_pr_details")) == options.fetch_pr_details
        and _close_enough(resume_from.get("since"), options.since)
        and _close_enough(resume_from.get("until"), options.until)
    )
    if not same_run:
        return frozenset()

    return frozenset(resume_from.get("commits", {}))


def _close_enough(recorded: str | None, requested: datetime) -> bool:
    """Whether a checkpoint boundary is near enough to the requested one."""
    parsed = parse_github_date(recorded)
    return parsed is not None and abs(parsed - requested) <= RESUME_WINDOW_TOLERANCE


def _commit_stats_query(shas: list[str]) -> str:
    """Build a GraphQL document asking for several commits' line counts."""
    fields = "\n".join(
        f'    c{index}: object(oid: "{sha}") {{ ... on Commit '
        "{ additions deletions } }"
        for index, sha in enumerate(shas)
    )
    return (
        "query($owner: String!, $name: String!) {\n"
        "  repository(owner: $owner, name: $name) {\n"
        f"{fields}\n"
        "  }\n"
        "}"
    )


def _commit_date(commit: dict[str, Any]) -> datetime | None:
    detail = commit.get("commit") or {}
    author = detail.get("author") or {}
    committer = detail.get("committer") or {}
    return parse_github_date(author.get("date") or committer.get("date"))


def _chunked[Item](items: list[Item], size: int) -> Iterable[list[Item]]:
    """Yield ``items`` in lists of at most ``size``."""
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _map[In, Out](
    pool: ThreadPoolExecutor, func: Callable[[In], Out], items: Iterable[In]
) -> list[Out]:
    """Run ``func`` over ``items`` concurrently, preserving input order."""
    return list(pool.map(func, items))
