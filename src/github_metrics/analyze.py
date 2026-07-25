"""Turn raw GitHub payloads into developer, repository, and DORA metrics.

Everything here is a pure function of the collected data, so the whole
analysis is exercised by tests without touching the network.
"""

from __future__ import annotations

import logging
import re
import statistics
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .dates import format_date_for_display, parse_github_date
from .models import (
    DeveloperMetrics,
    DoraSummary,
    RawData,
    Report,
    RepositoryMetrics,
)

logger = logging.getLogger(__name__)

__all__ = ["AnalysisOptions", "analyze"]

#: Developers above this many added lines are reported separately; such totals
#: almost always mean vendored dependencies or generated files, and they distort
#: every other row in the table.
DEFAULT_OUTLIER_THRESHOLD = 100_000

#: Pull requests open longer than this are excluded from lead time. Long-lived
#: branches are real, but they are not what lead time is trying to measure.
MAX_LEAD_TIME_HOURS = 90 * 24

#: Workflow names suggesting an actual deployment, preferred over plain CI.
DEPLOY_KEYWORDS = ("deploy", "release", "publish")

#: Fallback workflow names, matched as whole words so "cd" does not match "cdn".
CI_KEYWORDS = ("ci", "cd", "build", "test")


@dataclass(frozen=True)
class AnalysisOptions:
    """Knobs controlling how raw data is summarised."""

    since: datetime
    until: datetime
    outlier_threshold: int = DEFAULT_OUTLIER_THRESHOLD
    include_inactive: bool = False
    max_lead_time_hours: float = MAX_LEAD_TIME_HOURS


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

    for repo in data.get("repos", []):
        name = repo.get("name")
        if not name:
            continue
        repositories.append(_analyze_repository(data, repo, name, developers, options))

    active = [repo for repo in repositories if repo.is_active]
    active.sort(key=lambda repo: (-repo.commit_count, -repo.pr_count, repo.name))

    ranked, outliers = _rank_developers(developers.values(), options)

    return Report(
        org=data.get("org", ""),
        since=options.since,
        until=options.until,
        developers=ranked,
        outliers=outliers,
        repositories=active,
        dora=_summarise_dora(active, options),
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

    _apply_commits(data, name, metrics, developers, options)
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
) -> None:
    stats_by_sha = data.get("commit_stats", {}).get(repo, {})

    for commit in data.get("commits", {}).get(repo, []):
        detail = commit.get("commit") or {}
        authored = parse_github_date((detail.get("author") or {}).get("date"))
        if not _in_window(authored, options):
            continue

        metrics.commit_count += 1

        login = _login(commit.get("author"))
        if login is None:
            continue

        developer = _developer(developers, login)
        developer.commits += 1
        developer.repositories[repo] += 1

        stats = stats_by_sha.get(commit.get("sha", ""))
        if stats:
            developer.lines_added += int(stats.get("additions", 0) or 0)
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

        login = _login(pull.get("user"))
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
            login = _login(review.get("user"))
            if login is not None:
                _developer(developers, login).prs_reviewed += 1

    for comments in data.get("pr_comments", {}).get(repo, {}).values():
        for comment in comments or []:
            if not _in_window(parse_github_date(comment.get("created_at")), options):
                continue
            login = _login(comment.get("user"))
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
    runs = _deployment_runs(data.get("workflow_runs", {}).get(name) or [], repo, options)

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
            _append_if_positive(
                metrics.recovery_times,
                (finished - failure_started_at).total_seconds() / 3600,
            )
            failure_started_at = None


def _deployment_runs(
    runs: list[dict[str, Any]], repo: dict[str, Any], options: AnalysisOptions
) -> list[tuple[datetime, dict[str, Any]]]:
    """Select the in-window runs of a repository's deployment workflow.

    Returns:
        (start time, run) pairs in chronological order.
    """
    workflow = _select_deployment_workflow(runs)
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


def _select_deployment_workflow(runs: list[dict[str, Any]]) -> str | None:
    """Pick the workflow that best represents deployments for a repository.

    Deployment-shaped names win outright; otherwise the busiest CI workflow
    stands in, since for many repositories a green pipeline on the default
    branch *is* the deployment.
    """
    names = [run["name"].lower() for run in runs if run.get("name")]
    if not names:
        return None

    for match in (_matches_deploy, _matches_ci):
        candidates = [name for name in names if match(name)]
        if candidates:
            return _most_common(candidates)

    return _most_common(names)


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
        recovery_time_mean=_mean(recovery_times),
        recovery_time_samples=len(recovery_times),
        deployment_duration_mean=_mean(durations),
    )


# ----------------------------------------------------------------------
# Developers
# ----------------------------------------------------------------------


def _rank_developers(
    developers: Iterable[DeveloperMetrics],
    options: AnalysisOptions,
) -> tuple[list[DeveloperMetrics], list[DeveloperMetrics]]:
    """Sort developers and split out implausibly large contributions."""
    selected = [
        developer
        for developer in developers
        if options.include_inactive or developer.touched_code
    ]
    selected.sort(key=lambda dev: (-dev.lines_added, -dev.commits, dev.name))

    if options.outlier_threshold <= 0:
        return selected, []

    ranked = [d for d in selected if d.lines_added <= options.outlier_threshold]
    outliers = [d for d in selected if d.lines_added > options.outlier_threshold]
    return ranked, outliers


def _developer(developers: dict[str, DeveloperMetrics], login: str) -> DeveloperMetrics:
    developer = developers.get(login)
    if developer is None:
        developer = DeveloperMetrics(name=login)
        developers[login] = developer
    return developer


def _login(user: dict[str, Any] | None) -> str | None:
    """Extract a human account's login, filtering out bots."""
    if not isinstance(user, dict):
        return None

    login = user.get("login")
    if not login or not isinstance(login, str):
        return None
    if user.get("type") == "Bot" or login.endswith("[bot]"):
        return None
    return login


def _in_window(value: datetime | None, options: AnalysisOptions) -> bool:
    return value is not None and options.since <= value <= options.until


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0
