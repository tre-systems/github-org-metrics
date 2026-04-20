"""Analysis: turn cached/fetched data into developer, repo, and DORA metrics."""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd

from github_metrics.models import (
    DeveloperMetrics,
    RepositoryMetrics,
    format_date_for_display,
    parse_github_date,
)

logger = logging.getLogger(__name__)

PR_MERGE_OUTLIER_HOURS = 30 * 24  # 30 days
BRANCH_MERGE_OUTLIER_HOURS = 90 * 24  # 90 days
OUTLIER_LINE_THRESHOLD = 100_000  # dev with more lines added -> outlier report

DEVELOPER_COLUMNS = [
    "Developer",
    "Commits",
    "Lines Added",
    "Lines Deleted",
    "PRs Opened",
    "PRs Reviewed",
    "PR Comments",
    "Repositories",
]

REPOSITORY_COLUMNS = [
    "Repository",
    "Commits",
    "PRs",
    "Branch->Merge (h)",
    "CI Runs",
    "CI Fail %",
    "CI Recovery (h)",
    "CI Duration (m)",
    "Created",
    "Updated",
    "Language",
    "Branches",
    "Contributors",
]


# -------------------------------------------------------------------- helpers


@dataclass
class _DoraRepoStats:
    deploys: int = 0
    failures: int = 0
    durations: list[float] = field(default_factory=list)
    recoveries: list[float] = field(default_factory=list)

    @property
    def avg_duration(self) -> float:
        return sum(self.durations) / len(self.durations) if self.durations else 0.0

    @property
    def avg_recovery(self) -> float:
        return sum(self.recoveries) / len(self.recoveries) if self.recoveries else 0.0

    @property
    def failure_rate(self) -> float:
        return (self.failures / self.deploys * 100) if self.deploys else 0.0


def detect_ci_workflow(runs: list[dict[str, Any]]) -> str | None:
    """Pick the dominant CI/CD workflow name from a list of runs.

    Preference order: names matching any of {ci, test, build, deploy}
    (most common wins), then the overall most-common name. `None` if no
    runs have names.
    """
    names = [r["name"].lower() for r in runs if r.get("name")]
    if not names:
        return None
    ci_keywords = ("ci", "test", "build", "deploy")
    ci_names = [n for n in names if any(kw in n for kw in ci_keywords)]
    counter = Counter(ci_names or names)
    return counter.most_common(1)[0][0]


def compute_dora_for_repo(
    runs: list[dict[str, Any]],
    workflow: str,
    since_date: datetime,
) -> _DoraRepoStats:
    """Compute deploy counts, failure rate, avg duration, and MTTR.

    MTTR: for the filtered CI workflow, the mean hours between each
    distinct failure and the next success. Consecutive failures count as
    one incident (only the first starts the clock).
    """
    stats = _DoraRepoStats()
    filtered = [
        r
        for r in runs
        if r.get("name", "").lower() == workflow
        and parse_github_date(r["created_at"]) >= since_date
    ]
    if not filtered:
        return stats

    filtered.sort(key=lambda r: r["created_at"])
    pending_failure_at: datetime | None = None

    for run in filtered:
        conclusion = run.get("conclusion")
        created = parse_github_date(run["created_at"])

        if conclusion == "success":
            stats.deploys += 1
            updated = run.get("updated_at")
            if updated:
                duration_min = (parse_github_date(updated) - created).total_seconds() / 60
                stats.durations.append(duration_min)
            if pending_failure_at is not None:
                stats.recoveries.append((created - pending_failure_at).total_seconds() / 3600)
                pending_failure_at = None

        elif conclusion == "failure":
            stats.deploys += 1
            stats.failures += 1
            if pending_failure_at is None:
                pending_failure_at = created

    return stats


def _count_pr_reviews_and_comments(
    data: dict[str, Any],
    since_date: datetime,
    developers: dict[str, DeveloperMetrics],
) -> None:
    """Populate PR review/comment counts on `developers`."""

    def _count(section: str, date_key: str, attr: str) -> None:
        for repo_pages in data.get(section, {}).values():
            for items in repo_pages.values():
                for item in items or []:
                    user_login = (item or {}).get("user", {}).get("login")
                    when = (item or {}).get(date_key)
                    if not user_login or not when:
                        continue
                    if parse_github_date(when) >= since_date:
                        dev = developers.setdefault(user_login, DeveloperMetrics(name=user_login))
                        setattr(dev, attr, getattr(dev, attr) + 1)

    _count("pr_reviews", "submitted_at", "prs_reviewed")
    _count("pr_comments", "created_at", "pr_comments")


def _process_repo_commits(
    repo_name: str,
    commits: list[dict[str, Any]],
    stats: dict[str, dict[str, int] | None],
    since: str,
    developers: dict[str, DeveloperMetrics],
    repo_activity: dict[str, int],
) -> None:
    """Attribute commits and line changes to developers."""
    for commit in commits:
        commit_date = commit.get("commit", {}).get("author", {}).get("date", "")
        if commit_date < since:
            continue
        author_login = (commit.get("author") or {}).get("login")
        if not author_login:
            continue

        dev = developers.setdefault(author_login, DeveloperMetrics(name=author_login))
        dev.commits += 1
        dev.repositories[repo_name] = dev.repositories.get(repo_name, 0) + 1
        repo_activity[repo_name] += 1

        commit_stats = stats.get(commit["sha"]) or {}
        dev.lines_added += commit_stats.get("additions", 0)
        dev.lines_deleted += commit_stats.get("deletions", 0)


def _process_repo_prs(
    repo_name: str,
    prs: list[dict[str, Any]],
    branch_first_commits: dict[str, dict[str, Any]],
    since_date: datetime,
    developers: dict[str, DeveloperMetrics],
    pr_merge_times: list[float],
    branch_to_merge_times: list[float],
) -> tuple[int, list[float]]:
    """Attribute PRs to developers and compute merge times.

    Returns `(pr_count_in_window, per_repo_branch_merge_times)`.
    """
    pr_count = 0
    per_repo_branch_times: list[float] = []

    for pr in prs:
        pr_created = parse_github_date(pr["created_at"])
        pr_updated = parse_github_date(pr["updated_at"])
        if pr_created < since_date and pr_updated < since_date:
            continue
        pr_count += 1

        author = (pr.get("user") or {}).get("login")
        if not author:
            continue

        dev = developers.setdefault(author, DeveloperMetrics(name=author))
        dev.repositories[repo_name] = dev.repositories.get(repo_name, 0) + 1

        if pr_created >= since_date:
            dev.prs_opened += 1

        if not pr.get("merged_at"):
            continue

        merged_at = parse_github_date(pr["merged_at"])

        if pr_created >= since_date:
            merge_hours = (merged_at - pr_created).total_seconds() / 3600
            if merge_hours <= PR_MERGE_OUTLIER_HOURS:
                pr_merge_times.append(merge_hours)

        branch = (pr.get("head") or {}).get("ref")
        if not branch:
            continue
        first_commit_date = (
            (branch_first_commits.get(branch) or {})
            .get("commit", {})
            .get("committer", {})
            .get("date")
        )
        if not first_commit_date:
            continue
        branch_hours = (merged_at - parse_github_date(first_commit_date)).total_seconds() / 3600
        if branch_hours <= BRANCH_MERGE_OUTLIER_HOURS:
            branch_to_merge_times.append(branch_hours)
            per_repo_branch_times.append(branch_hours)

    return pr_count, per_repo_branch_times


# ---------------------------------------------------------------------- main


def analyze(data: dict[str, Any], since: str) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Analyze fetched data, returning `(developers, repositories, outliers)`.

    `outliers` holds developers whose lines-added would distort the main
    table (typically a bulk import or a generated-file commit); they are
    separated rather than dropped so reviewers can still inspect them.
    """
    repo_names = [r["name"] for r in data.get("repos") or []]
    if not repo_names:
        logger.warning("No repositories to analyze.")
        return (
            pd.DataFrame(columns=DEVELOPER_COLUMNS),
            pd.DataFrame(columns=REPOSITORY_COLUMNS),
            pd.DataFrame(columns=DEVELOPER_COLUMNS),
        )

    since_date = parse_github_date(since)
    has_pr_details = bool(data.get("fetch_pr_details"))

    developers: dict[str, DeveloperMetrics] = {}
    repo_activity: dict[str, int] = defaultdict(int)
    pr_merge_times: list[float] = []
    branch_to_merge_times: list[float] = []

    _count_pr_reviews_and_comments(data, since_date, developers)

    # DORA per repo
    dora_by_repo: dict[str, _DoraRepoStats] = {}
    for name in repo_names:
        runs = data.get("workflow_runs", {}).get(name) or []
        # Tolerate old cache shape where workflow_runs was a dict payload.
        if isinstance(runs, dict):
            runs = runs.get("workflow_runs") or []
        workflow = detect_ci_workflow(runs)
        if workflow:
            dora_by_repo[name] = compute_dora_for_repo(runs, workflow, since_date)

    repo_metrics: list[RepositoryMetrics] = []

    for repo in data["repos"]:
        name = repo["name"]

        _process_repo_commits(
            name,
            data.get("commits", {}).get(name) or [],
            data.get("commit_stats", {}).get(name) or {},
            since,
            developers,
            repo_activity,
        )

        pr_count, repo_branch_times = _process_repo_prs(
            name,
            data.get("pull_requests", {}).get(name) or [],
            data.get("branch_first_commits", {}).get(name) or {},
            since_date,
            developers,
            pr_merge_times,
            branch_to_merge_times,
        )

        dora = dora_by_repo.get(name, _DoraRepoStats())
        branches = data.get("branches", {}).get(name) or []
        contributors = data.get("contributors", {}).get(name) or []

        metrics = RepositoryMetrics(
            name=name,
            created_at=format_date_for_display(repo["created_at"]),
            updated_at=format_date_for_display(repo["updated_at"]),
            language=repo.get("language") or "N/A",
            branch_count=len(branches),
            contributor_count=len(contributors),
            activity=repo_activity.get(name, 0),
            pr_count=pr_count,
            deployment_count=dora.deploys,
            deployment_failures=dora.failures,
            failure_rate=dora.failure_rate,
            avg_deployment_duration=dora.avg_duration,
            mttr_hours=dora.avg_recovery,
            recoveries_count=len(dora.recoveries),
        )
        if repo_branch_times:
            metrics.avg_branch_to_merge_time = sum(repo_branch_times) / len(repo_branch_times)
            metrics.branch_merges_count = len(repo_branch_times)

        if metrics.activity > 0 or metrics.pr_count > 0:
            repo_metrics.append(metrics)

    df_developers, df_outliers = _build_developer_dataframes(
        developers, has_pr_details=has_pr_details
    )
    df_repos = _build_repository_dataframe(repo_metrics)

    _log_summary(pr_merge_times, branch_to_merge_times, dora_by_repo, repo_names)

    return df_developers, df_repos, df_outliers


def _build_developer_dataframes(
    developers: dict[str, DeveloperMetrics], *, has_pr_details: bool
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Build the main developer DataFrame and split outliers off the top."""
    rows = [
        {
            "Developer": dev.name,
            "Commits": dev.commits,
            "Lines Added": dev.lines_added,
            "Lines Deleted": dev.lines_deleted,
            "PRs Opened": dev.prs_opened,
            "PRs Reviewed": dev.prs_reviewed if has_pr_details else "N/A",
            "PR Comments": dev.pr_comments if has_pr_details else "N/A",
            "Repositories": _format_repo_list(dev.repositories, limit=5),
        }
        for dev in developers.values()
        if not dev.name.endswith("[bot]")
    ]
    df = pd.DataFrame(rows, columns=DEVELOPER_COLUMNS)
    if df.empty:
        return df, pd.DataFrame(columns=DEVELOPER_COLUMNS)

    df = df[(df["Lines Added"] > 0) | (df["Lines Deleted"] > 0)]
    df = df.sort_values("Lines Added", ascending=False)

    outliers = df[df["Lines Added"] > OUTLIER_LINE_THRESHOLD].copy()
    df = df[df["Lines Added"] <= OUTLIER_LINE_THRESHOLD].copy()
    return df, outliers


def _build_repository_dataframe(
    repo_metrics: list[RepositoryMetrics],
) -> pd.DataFrame:
    rows = [
        {
            "Repository": m.name,
            "Commits": m.activity,
            "PRs": m.pr_count,
            "Branch->Merge (h)": round(m.avg_branch_to_merge_time, 1),
            "CI Runs": m.deployment_count,
            "CI Fail %": round(m.failure_rate, 1),
            "CI Recovery (h)": round(m.mttr_hours, 1) if m.recoveries_count else 0.0,
            "CI Duration (m)": round(m.avg_deployment_duration, 1),
            "Created": m.created_at,
            "Updated": m.updated_at,
            "Language": m.language,
            "Branches": m.branch_count,
            "Contributors": m.contributor_count,
        }
        for m in repo_metrics
    ]
    df = pd.DataFrame(rows, columns=REPOSITORY_COLUMNS)
    if not df.empty:
        df = df.sort_values("Commits", ascending=False)
    return df


def _format_repo_list(repos: dict[str, int], limit: int) -> str:
    sorted_repos = sorted(repos.items(), key=lambda x: x[1], reverse=True)
    if len(sorted_repos) <= limit:
        return ", ".join(r for r, _ in sorted_repos)
    top = ", ".join(r for r, _ in sorted_repos[:limit])
    return f"{top} +{len(sorted_repos) - limit} more"


def _log_summary(
    pr_merge_times: list[float],
    branch_to_merge_times: list[float],
    dora_by_repo: dict[str, _DoraRepoStats],
    repo_names: list[str],
) -> None:
    avg_pr_merge = sum(pr_merge_times) / len(pr_merge_times) if pr_merge_times else 0.0
    avg_branch_merge = (
        sum(branch_to_merge_times) / len(branch_to_merge_times) if branch_to_merge_times else 0.0
    )
    logger.info("")
    logger.info("=" * 80)
    logger.info("SUMMARY")
    logger.info("=" * 80)
    logger.info("Repositories analyzed: %s", ", ".join(repo_names))
    logger.info("Average PR merge time: %.2f hours", avg_pr_merge)
    logger.info("Average branch-to-merge time: %.2f hours", avg_branch_merge)

    total_deploys = sum(d.deploys for d in dora_by_repo.values())
    total_failures = sum(d.failures for d in dora_by_repo.values())
    all_durations = [d for s in dora_by_repo.values() for d in s.durations]
    all_recoveries = [r for s in dora_by_repo.values() for r in s.recoveries]

    if all_durations:
        logger.info(
            "Average deployment duration: %.2f minutes",
            sum(all_durations) / len(all_durations),
        )
    if total_deploys:
        logger.info("Change failure rate: %.2f%%", total_failures / total_deploys * 100)
    if all_recoveries:
        logger.info(
            "Mean time to recover: %.2f hours (%d recoveries)",
            sum(all_recoveries) / len(all_recoveries),
            len(all_recoveries),
        )
