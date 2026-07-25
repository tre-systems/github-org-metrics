"""Rendering and export of a finished report."""

from __future__ import annotations

import csv
import hashlib
import logging
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from . import dora
from .models import DeveloperMetrics, DoraSummary, Report, RepositoryMetrics

logger = logging.getLogger(__name__)

__all__ = ["anonymize_name", "export_csv", "render"]

DEVELOPER_HEADERS = (
    "Developer",
    "Commits",
    "Lines Added",
    "Lines Deleted",
    "PRs Opened",
    "PRs Reviewed",
    "PR Comments",
    "Repositories",
)

REPOSITORY_HEADERS = (
    "Repository",
    "Commits",
    "PRs",
    "Lead Time (h)",
    "Deploys",
    "Fail %",
    "MTTR (h)",
    "Deploy (m)",
    "Deploy Workflow",
    "Created",
    "Updated",
    "Language",
    "Branches",
    "Contributors",
)

RATING_STYLES = {
    dora.ELITE: "bold green",
    dora.HIGH: "green",
    dora.MEDIUM: "yellow",
    dora.LOW: "red",
    dora.UNKNOWN: "dim",
}


def anonymize_name(name: str) -> str:
    """Map a login to a stable pseudonym, for screenshots and demos."""
    digest = hashlib.blake2s(name.encode("utf-8"), digest_size=4).hexdigest()
    return f"user-{digest[:6]}"


# ----------------------------------------------------------------------
# Console rendering
# ----------------------------------------------------------------------


def render(report: Report, console: Console, *, anonymize: bool = False) -> None:
    """Print the full report to a console."""
    console.print()
    console.rule(
        f"[bold]{report.org or 'GitHub'}[/bold] · "
        f"{report.since:%d %b %Y} to {report.until:%d %b %Y}"
    )

    if report.developers:
        console.print()
        console.print(
            _developer_table("Developer Activity", report, report.developers, anonymize)
        )
    else:
        console.print("\n[yellow]No developer activity found in this window.[/yellow]")

    bulk = report.bulk_commits
    if bulk.any_excluded:
        console.print(
            f"\n[dim]Line counts exclude {bulk.count:,} bulk "
            f"{'commit' if bulk.count == 1 else 'commits'} "
            f"({bulk.lines_added:,} added lines over {bulk.threshold:,} per commit) "
            f"— generated or vendored files.[/dim]"
        )

    console.print()
    if report.repositories:
        console.print(_repository_table(report.repositories))
    else:
        console.print("[yellow]No repository activity found in this window.[/yellow]")

    console.print()
    console.print(_dora_table(report))
    console.print()


def _developer_table(
    title: str,
    report: Report,
    developers: Sequence[DeveloperMetrics],
    anonymize: bool,
) -> Table:
    table = Table(title=title, title_justify="left", header_style="bold")
    table.add_column("Developer", style="cyan", no_wrap=True)
    for header in ("Commits", "Lines +", "Lines -", "PRs", "Reviews", "Comments"):
        table.add_column(header, justify="right")

    unavailable = "[dim]n/a[/dim]"
    for developer in developers:
        table.add_row(
            anonymize_name(developer.name) if anonymize else developer.name,
            f"{developer.commits:,}",
            f"{developer.lines_added:,}",
            f"{developer.lines_deleted:,}",
            f"{developer.prs_opened:,}",
            f"{developer.prs_reviewed:,}" if report.has_pr_details else unavailable,
            f"{developer.pr_comments:,}" if report.has_pr_details else unavailable,
        )
    return table


def _repository_table(repositories: Sequence[RepositoryMetrics]) -> Table:
    table = Table(title="Repository Details", title_justify="left", header_style="bold")
    table.add_column("Repository", style="cyan", no_wrap=True)
    for header in ("Commits", "PRs", "Lead (h)", "Deploys", "Fail %", "MTTR (h)"):
        table.add_column(header, justify="right")
    table.add_column("Workflow", no_wrap=True, overflow="ellipsis", max_width=14)
    table.add_column("Language", no_wrap=True, overflow="ellipsis", max_width=12)
    table.add_column("Updated", justify="right", no_wrap=True)

    for repo in repositories:
        table.add_row(
            repo.name,
            f"{repo.commit_count:,}",
            f"{repo.pr_count:,}",
            _optional(repo.avg_lead_time, bool(repo.lead_times)),
            f"{repo.deployment_count:,}",
            _optional(repo.failure_rate, repo.deployment_count > 0),
            _optional(repo.avg_recovery_time, bool(repo.recovery_times)),
            repo.deployment_workflow or "[dim]none found[/dim]",
            repo.language,
            repo.updated_at,
        )
    return table


def _dora_table(report: Report) -> Table:
    summary = report.dora
    table = Table(title="DORA Metrics", title_justify="left", header_style="bold")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    table.add_column("Rating")
    table.add_column("Basis", style="dim")

    rows: list[tuple[str, str, str, str]] = [
        (
            "Lead time for changes",
            f"{summary.lead_time_mean:.1f} h" if summary.lead_time_samples else "no data",
            dora.rate_lead_time(summary.lead_time_mean, summary.lead_time_samples),
            f"median {summary.lead_time_median:.1f} h over "
            f"{_count(summary.lead_time_samples, 'merged pull request')}"
            if summary.lead_time_samples
            else "no merged pull requests",
        ),
        (
            "Deployment frequency",
            f"{summary.deploys_per_day:.2f} / day"
            if summary.deploys_total
            else "no data",
            dora.rate_deployment_frequency(summary.deploys_per_day),
            f"{_count(summary.deploys_total, 'run')} over "
            f"{_count(round(report.window_days), 'day')}",
        ),
        (
            "Change failure rate",
            f"{summary.change_failure_rate:.1f} %"
            if summary.deploys_total
            else "no data",
            dora.rate_change_failure_rate(
                summary.change_failure_rate, summary.deploys_total
            ),
            _count(summary.deploys_total, "deployment run"),
        ),
        (
            "Time to restore service",
            f"{summary.recovery_time_mean:.1f} h"
            if summary.recovery_time_samples
            else "no data",
            dora.rate_recovery_time(
                summary.recovery_time_mean, summary.recovery_time_samples
            ),
            _recovery_basis(summary),
        ),
    ]

    for metric, value, rating, basis in rows:
        style = RATING_STYLES.get(rating, "")
        table.add_row(metric, value, f"[{style}]{rating}[/{style}]", basis)

    return table


def _recovery_basis(summary: DoraSummary) -> str:
    """Explain what the time-to-restore figure was computed from."""
    observed = _count(summary.recovery_time_samples, "recovery", "recoveries")
    if not summary.abandoned_failures:
        return f"{observed} observed"
    return (
        f"{observed} observed; "
        f"{summary.abandoned_failures:,} left unresolved or too long to count"
    )


def _count(quantity: int, singular: str, plural: str | None = None) -> str:
    """Render a count with its noun, pluralised."""
    noun = singular if quantity == 1 else (plural or f"{singular}s")
    return f"{quantity:,} {noun}"


def _optional(value: float, present: bool) -> str:
    return f"{value:,.1f}" if present else "[dim]-[/dim]"


# ----------------------------------------------------------------------
# CSV export
# ----------------------------------------------------------------------


def export_csv(
    report: Report, output_dir: Path, *, anonymize: bool = False
) -> list[Path]:
    """Write the report to CSV files.

    Args:
        report: The report to export.
        output_dir: Directory to write into; created if missing.
        anonymize: Replace developer logins with stable pseudonyms.

    Returns:
        The paths written, in the order they were written.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    prefix = report.org or "github"
    written: list[Path] = []

    written.append(
        _write_csv(
            output_dir / f"{prefix}_github_developer_metrics.csv",
            DEVELOPER_HEADERS,
            _developer_rows(report.developers, report, anonymize),
        )
    )
    written.append(
        _write_csv(
            output_dir / f"{prefix}_github_repository_metrics.csv",
            REPOSITORY_HEADERS,
            _repository_rows(report.repositories),
        )
    )
    return written


def _developer_rows(
    developers: Sequence[DeveloperMetrics], report: Report, anonymize: bool
) -> list[list[Any]]:
    unavailable = "N/A"
    return [
        [
            anonymize_name(developer.name) if anonymize else developer.name,
            developer.commits,
            developer.lines_added,
            developer.lines_deleted,
            developer.prs_opened,
            developer.prs_reviewed if report.has_pr_details else unavailable,
            developer.pr_comments if report.has_pr_details else unavailable,
            developer.top_repositories(),
        ]
        for developer in developers
    ]


def _repository_rows(repositories: Sequence[RepositoryMetrics]) -> list[list[Any]]:
    return [
        [
            repo.name,
            repo.commit_count,
            repo.pr_count,
            round(repo.avg_lead_time, 1),
            repo.deployment_count,
            round(repo.failure_rate, 1),
            round(repo.avg_recovery_time, 1),
            round(repo.avg_deployment_duration, 1),
            repo.deployment_workflow or "",
            repo.created_at,
            repo.updated_at,
            repo.language,
            repo.branch_count,
            repo.contributor_count,
        ]
        for repo in repositories
    ]


def _write_csv(path: Path, headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> Path:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        writer.writerows(rows)
    logger.info("Wrote %s", path)
    return path
