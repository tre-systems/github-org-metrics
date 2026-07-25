"""Turn raw GitHub payloads into developer, repository, and DORA metrics.

Everything here is a pure function of the collected data, so the whole
analysis is exercised by tests without touching the network.
"""

from __future__ import annotations

import logging
import re
import statistics
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .dates import format_date_for_display, parse_github_date
from .models import (
    DEPLOY_SHAPED,
    EXPLICIT,
    INFERRED_FROM_CI,
    BulkCommits,
    DeveloperMetrics,
    DoraSummary,
    RawData,
    Report,
    RepositoryMetrics,
)

logger = logging.getLogger(__name__)

__all__ = ["AnalysisOptions", "analyze"]

#: Commits adding more than this many lines are left out of developer line
#: counts. A typical commit is under a hundred lines; ones this size are
#: lockfiles, vendored dependencies, generated output, or bulk imports, and a
#: handful of them can outweigh every other commit in an organization.
DEFAULT_BULK_COMMIT_LINES = 10_000

#: Pull requests open longer than this are excluded from lead time. Long-lived
#: branches are real, but they are not what lead time is trying to measure.
MAX_LEAD_TIME_HOURS = 90 * 24

#: Recovery gaps longer than this are excluded from time-to-restore. A pipeline
#: left red for a fortnight is an abandoned workflow, not a fortnight-long
#: outage, and one such gap otherwise dominates the mean.
MAX_RECOVERY_HOURS = 7 * 24

#: Workflow names suggesting an actual deployment, preferred over plain CI.
DEPLOY_KEYWORDS = ("deploy", "release", "publish")

#: Fallback workflow names, matched as whole words so "cd" does not match "cdn".
CI_KEYWORDS = ("ci", "cd", "build", "test")


@dataclass(frozen=True)
class AnalysisOptions:
    """Knobs controlling how raw data is summarised."""

    since: datetime
    until: datetime
    bulk_commit_lines: int = DEFAULT_BULK_COMMIT_LINES
    """Skip commits adding more than this many lines; 0 counts everything."""

    include_inactive: bool = False
    max_lead_time_hours: float = MAX_LEAD_TIME_HOURS
    max_recovery_hours: float = MAX_RECOVERY_HOURS
    deploy_workflow: str | None = None
    """Count this workflow as the deployment in every repository."""

    deploy_workflow_by_repo: Mapping[str, str] = field(default_factory=dict)
    """Per-repository workflow names, taking precedence over the default."""

    strict_deployments: bool = False
    """Count nothing rather than fall back to a plain CI workflow."""

    exclude_users: frozenset[str] = frozenset()
    """Logins to leave out entirely, matched case-insensitively."""


def analyze(data: RawData, options: AnalysisOptions) -> Report:
    """Summarise collected data into a report.

    Args:
        data: Raw payloads from :func:`~github_metrics.collect.collect`.
        options: The analysis window and filtering rules.

    Returns:
        The finished report.
    """
    developers: dict[str, DeveloperMetrics] = {}
    repositories: list[RepositoryMetrics] = []
    bulk = _BulkTally(options.bulk_commit_lines)

    for repo in data.get("repos", []):
        name = repo.get("name")
        if not name:
            continue
        repositories.append(
            _analyze_repository(data, repo, name, developers, options, bulk)
        )

    active = [repo for repo in repositories if repo.is_active]
    active.sort(key=lambda repo: (-repo.commit_count, -repo.pr_count, repo.name))

    ranked = _rank_developers(developers.values(), options)

    return Report(
        org=data.get("org", ""),
        since=options.since,
        until=options.until,
        developers=ranked,
        repositories=active,
        dora=_summarise_dora(active, options),
        bulk_commits=bulk.result(),
        has_pr_details=bool(data.get("fetch_pr_details", False)),
    )


# ----------------------------------------------------------------------
# Repositories
# ----------------------------------------------------------------------


def _analyze_repository(
    data: RawData,
    repo: dict[str, Any],
    name: str,
    developers: dict[str, DeveloperMetrics],
    options: AnalysisOptions,
    bulk: _BulkTally,
) -> RepositoryMetrics:
    metrics = RepositoryMetrics(
        name=name,
        created_at=format_date_for_display(repo.get("created_at")),
        updated_at=format_date_for_display(
            repo.get("pushed_at") or repo.get("updated_at")
        ),
        language=repo.get("language") or "N/A",
        branch_count=data.get("branch_counts", {}).get(name, 0),
        contributor_count=data.get("contributor_counts", {}).get(name, 0),
    )

    _apply_commits(data, name, metrics, developers, options, bulk)
    _apply_pull_requests(data, name, metrics, developers, options)
    _apply_reviews_and_comments(data, name, developers, options)
    _apply_workflow_runs(data, repo, name, metrics, options)
    return metrics


def _apply_commits(
    data: RawData,
    repo: str,
    metrics: RepositoryMetrics,
    developers: dict[str, DeveloperMetrics],
    options: AnalysisOptions,
    bulk: _BulkTally,
) -> None:
    stats_by_sha = data.get("commit_stats", {}).get(repo, {})

    for commit in data.get("commits", {}).get(repo, []):
        detail = commit.get("commit") or {}
        authored = parse_github_date((detail.get("author") or {}).get("date"))
        if not _in_window(authored, options):
            continue

        metrics.commit_count += 1

        login = _login(commit.get("author"), options)
        if login is None:
            continue

        developer = _developer(developers, login)
        developer.commits += 1
        developer.repositories[repo] += 1

        stats = stats_by_sha.get(commit.get("sha", ""))
        if not stats:
            continue

        additions = int(stats.get("additions", 0) or 0)
        if bulk.is_bulk(additions):
            continue

        developer.lines_added += additions
        developer.lines_deleted += int(stats.get("deletions", 0) or 0)


def _apply_pull_requests(
    data: RawData,
    repo: str,
    metrics: RepositoryMetrics,
    developers: dict[str, DeveloperMetrics],
    options: AnalysisOptions,
) -> None:
    first_commit_dates = data.get("pr_first_commit_dates", {}).get(repo, {})

    for pull in data.get("pull_requests", {}).get(repo, []):
        created = parse_github_date(pull.get("created_at"))
        updated = parse_github_date(pull.get("updated_at"))
        if not (_in_window(created, options) or _in_window(updated, options)):
            continue

        metrics.pr_count += 1

        login = _login(pull.get("user"), options)
        if login is not None:
            developer = _developer(developers, login)
            developer.repositories[repo] += 1
            if _in_window(created, options):
                developer.prs_opened += 1

        lead_time = _lead_time_hours(pull, first_commit_dates, options)
        if lead_time is not None:
            metrics.lead_times.append(lead_time)


def _lead_time_hours(
    pull: dict[str, Any],
    first_commit_dates: dict[str, str],
    options: AnalysisOptions,
) -> float | None:
    """Hours from a branch's first commit to its merge, if that is measurable."""
    merged = parse_github_date(pull.get("merged_at"))
    if merged is None or not _in_window(merged, options):
        return None

    number = str(pull.get("number", ""))
    started = parse_github_date(first_commit_dates.get(number)) or parse_github_date(
        pull.get("created_at")
    )
    if started is None:
        return None

    hours = (merged - started).total_seconds() / 3600
    if hours < 0 or hours > options.max_lead_time_hours:
        return None
    return hours


def _apply_reviews_and_comments(
    data: RawData,
    repo: str,
    developers: dict[str, DeveloperMetrics],
    options: AnalysisOptions,
) -> None:
    for reviews in data.get("pr_reviews", {}).get(repo, {}).values():
        for review in reviews or []:
            if not _in_window(parse_github_date(review.get("submitted_at")), options):
                continue
            login = _login(review.get("user"), options)
            if login is not None:
                _developer(developers, login).prs_reviewed += 1

    for comments in data.get("pr_comments", {}).get(repo, {}).values():
        for comment in comments or []:
            if not _in_window(parse_github_date(comment.get("created_at")), options):
                continue
            login = _login(comment.get("user"), options)
            if login is not None:
                _developer(developers, login).pr_comments += 1


# ----------------------------------------------------------------------
# Deployments (DORA)
# ----------------------------------------------------------------------


def _apply_workflow_runs(
    data: RawData,
    repo: dict[str, Any],
    name: str,
    metrics: RepositoryMetrics,
    options: AnalysisOptions,
) -> None:
    all_runs = data.get("workflow_runs", {}).get(name) or []
    workflow, source = _select_deployment_workflow(all_runs, name, options)
    metrics.deployment_workflow = workflow
    metrics.deployment_workflow_source = source
    runs = _deployment_runs(all_runs, workflow, repo, options)

    failure_started_at: datetime | None = None

    for started, run in runs:
        conclusion = run.get("conclusion")
        if conclusion not in ("success", "failure"):
            continue

        metrics.deployment_count += 1

        if conclusion == "failure":
            metrics.deployment_failures += 1
            failure_started_at = failure_started_at or started
            continue

        finished = parse_github_date(run.get("updated_at"))
        if finished is None:
            continue

        run_started = parse_github_date(run.get("run_started_at")) or started
        _append_if_positive(
            metrics.deployment_durations, (finished - run_started).total_seconds() / 60
        )

        if failure_started_at is not None:
            recovery = (finished - failure_started_at).total_seconds() / 3600
            if 0 <= recovery <= options.max_recovery_hours:
                metrics.recovery_times.append(recovery)
            else:
                metrics.abandoned_failures += 1
            failure_started_at = None


def _deployment_runs(
    runs: list[dict[str, Any]],
    workflow: str | None,
    repo: dict[str, Any],
    options: AnalysisOptions,
) -> list[tuple[datetime, dict[str, Any]]]:
    """Select the in-window runs of a repository's deployment workflow.

    Returns:
        (start time, run) pairs in chronological order.
    """
    if workflow is None:
        return []

    selected = [run for run in runs if (run.get("name") or "").lower() == workflow]

    # Prefer runs on the default branch: those are deployments, whereas runs on
    # feature branches are pull request validation.
    default_branch = repo.get("default_branch")
    if default_branch:
        on_default = [r for r in selected if r.get("head_branch") == default_branch]
        selected = on_default or selected

    dated: list[tuple[datetime, dict[str, Any]]] = []
    for run in selected:
        started = parse_github_date(run.get("created_at"))
        if started is not None and options.since <= started <= options.until:
            dated.append((started, run))

    dated.sort(key=lambda item: item[0])
    logger.debug("Deployment workflow '%s': %d runs in window", workflow, len(dated))
    return dated


def _append_if_positive(target: list[float], value: float) -> None:
    """Record a duration, discarding negatives from clock skew or bad payloads."""
    if value >= 0:
        target.append(value)


def _select_deployment_workflow(
    runs: list[dict[str, Any]], repo: str, options: AnalysisOptions
) -> tuple[str | None, str | None]:
    """Pick the workflow that best represents deployments for a repository.

    A name given for this repository wins, then a name given for the whole run,
    then a deployment-shaped name. Failing all of those the busiest CI workflow
    stands in, since for many repositories a green pipeline on the default
    branch *is* the deployment — unless ``strict_deployments`` says not to
    guess.

    Returns:
        The workflow name and how it was chosen, or (None, None) if nothing
        qualifies.
    """
    names = [run["name"].lower() for run in runs if run.get("name")]
    if not names:
        return None, None

    requested = options.deploy_workflow_by_repo.get(repo, options.deploy_workflow)
    if requested is not None:
        wanted = requested.lower()
        return (wanted, EXPLICIT) if wanted in names else (None, None)

    deploy_shaped = [name for name in names if _matches_deploy(name)]
    if deploy_shaped:
        return _most_common(deploy_shaped), DEPLOY_SHAPED

    if options.strict_deployments:
        return None, None

    ci_shaped = [name for name in names if _matches_ci(name)]
    if ci_shaped:
        return _most_common(ci_shaped), INFERRED_FROM_CI

    return _most_common(names), INFERRED_FROM_CI


def _most_common(names: list[str]) -> str:
    """Return the most frequent name, breaking ties alphabetically."""
    ranked = Counter(names).most_common()
    best = max(count for _, count in ranked)
    return min(name for name, count in ranked if count == best)


def _matches_deploy(name: str) -> bool:
    return any(keyword in name for keyword in DEPLOY_KEYWORDS)


def _matches_ci(name: str) -> bool:
    tokens = set(re.split(r"[^a-z0-9]+", name))
    return any(keyword in tokens for keyword in CI_KEYWORDS)


def _summarise_dora(
    repositories: list[RepositoryMetrics], options: AnalysisOptions
) -> DoraSummary:
    lead_times = [hours for repo in repositories for hours in repo.lead_times]
    recovery_times = [hours for repo in repositories for hours in repo.recovery_times]
    durations = [mins for repo in repositories for mins in repo.deployment_durations]

    deploys = sum(repo.deployment_count for repo in repositories)
    failures = sum(repo.deployment_failures for repo in repositories)
    window_days = max((options.until - options.since).total_seconds() / 86400, 1.0)

    return DoraSummary(
        lead_time_mean=_mean(lead_times),
        lead_time_median=statistics.median(lead_times) if lead_times else 0.0,
        lead_time_samples=len(lead_times),
        deploys_total=deploys,
        deploys_per_day=deploys / window_days,
        change_failure_rate=(failures / deploys * 100) if deploys else 0.0,
        inferred_deployment_repos=sum(
            1
            for repo in repositories
            if repo.deployment_workflow_source == INFERRED_FROM_CI
            and repo.deployment_count > 0
        ),
        recovery_time_mean=_mean(recovery_times),
        recovery_time_samples=len(recovery_times),
        abandoned_failures=sum(repo.abandoned_failures for repo in repositories),
        deployment_duration_mean=_mean(durations),
    )


# ----------------------------------------------------------------------
# Developers
# ----------------------------------------------------------------------


def _rank_developers(
    developers: Iterable[DeveloperMetrics],
    options: AnalysisOptions,
) -> list[DeveloperMetrics]:
    """Sort developers, dropping those who changed no code."""
    selected = [
        developer
        for developer in developers
        if options.include_inactive or developer.touched_code
    ]
    selected.sort(key=lambda dev: (-dev.lines_added, -dev.commits, dev.name))
    return selected


class _BulkTally:
    """Counts the commits excluded from line totals for being oversized."""

    def __init__(self, threshold: int) -> None:
        self.threshold = threshold
        self.count = 0
        self.lines_added = 0

    def is_bulk(self, additions: int) -> bool:
        """Whether a commit is too large to count, recording it if so."""
        if self.threshold <= 0 or additions <= self.threshold:
            return False
        self.count += 1
        self.lines_added += additions
        return True

    def result(self) -> BulkCommits:
        """The tally so far."""
        return BulkCommits(
            count=self.count,
            lines_added=self.lines_added,
            threshold=max(self.threshold, 0),
        )


def _developer(developers: dict[str, DeveloperMetrics], login: str) -> DeveloperMetrics:
    developer = developers.get(login)
    if developer is None:
        developer = DeveloperMetrics(name=login)
        developers[login] = developer
    return developer


def _login(user: dict[str, Any] | None, options: AnalysisOptions) -> str | None:
    """Extract a human account's login, filtering out bots and exclusions."""
    if not isinstance(user, dict):
        return None

    login = user.get("login")
    if not login or not isinstance(login, str):
        return None
    if user.get("type") == "Bot" or login.endswith("[bot]"):
        return None
    if login.lower() in options.exclude_users:
        return None
    return login


def _in_window(value: datetime | None, options: AnalysisOptions) -> bool:
    return value is not None and options.since <= value <= options.until


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0
