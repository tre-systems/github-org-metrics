#!/usr/bin/env python3
"""Render the README's example report from synthetic data.

Run this after changing anything about the console output:

    uv run scripts/generate_report_image.py

The data is invented; no real organization's names or numbers appear here.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from rich.console import Console

from github_metrics.analyze import AnalysisOptions, analyze
from github_metrics.dates import to_github_date
from github_metrics.models import RawData
from github_metrics.report import render

OUTPUT = Path(__file__).resolve().parent.parent / "docs" / "report.svg"
CONSOLE_WIDTH = 118

#: Which run in each cycle of seven fails, so failures are spread out.
FAILING_RUN_IN_CYCLE = 3

UNTIL = datetime(2025, 4, 1, tzinfo=UTC)
SINCE = datetime(2025, 1, 1, tzinfo=UTC)

DEVELOPERS = [
    ("rowan-hale", 88, 16_993, 2_307, 21, 34, 41),
    ("imani-okafor", 76, 12_488, 7_137, 45, 84, 132),
    ("dev-null-jones", 23, 7_956, 293, 24, 18, 5),
    ("sasha-petrov", 105, 6_459, 4_074, 28, 66, 57),
    ("min-jae-park", 46, 4_514, 1_762, 24, 8, 7),
    ("teodora-vidal", 20, 3_067, 1_070, 25, 33, 22),
    ("bea-lindqvist", 58, 1_152, 314, 20, 98, 115),
    ("quinn-abara", 14, 1_250, 134, 16, 46, 7),
    ("noor-haddad", 9, 780, 612, 6, 4, 3),
    # A handful of enormous commits: lockfiles and generated output, which the
    # bulk-commit filter sets aside.
    ("vendored-deps", 6, 1_500_000, 1_400_000, 2, 1, 0),
]

REPOSITORIES = [
    ("checkout-service", "Go", 142, 61, 9.4, 84, 4.8, 2.1),
    ("web-storefront", "TypeScript", 118, 47, 21.6, 62, 11.3, 6.5),
    ("pricing-engine", "Python", 74, 30, 3.2, 45, 0.0, 0.0),
    ("identity-gateway", "Rust", 51, 22, 46.1, 18, 16.7, 19.4),
    ("data-pipelines", "Python", 38, 14, 88.5, 12, 8.3, 3.7),
    ("infra-terraform", "HCL", 27, 19, 12.8, 31, 6.5, 1.2),
    ("mobile-client", "Kotlin", 22, 11, 64.0, 7, 0.0, 0.0),
    ("design-system", "TypeScript", 16, 9, 5.5, 0, 0.0, 0.0),
]


def build_data(seed: int = 20_250_401) -> RawData:
    """Compose synthetic payloads that analyse into the example report."""
    rng = random.Random(seed)  # noqa: S311 - cosmetic sampling, not security

    def moment(day: int, hour: int = 12) -> str:
        return to_github_date(SINCE + timedelta(days=day, hours=hour))

    repos: list[dict[str, Any]] = []
    commits: dict[str, list[dict[str, Any]]] = {}
    commit_stats: dict[str, dict[str, dict[str, int]]] = {}
    pulls: dict[str, list[dict[str, Any]]] = {}
    reviews: dict[str, dict[str, list[dict[str, Any]]]] = {}
    comments: dict[str, dict[str, list[dict[str, Any]]]] = {}
    first_commits: dict[str, dict[str, str]] = {}
    runs: dict[str, list[dict[str, Any]]] = {}
    branch_counts: dict[str, int] = {}
    contributor_counts: dict[str, int] = {}

    # Spread each developer's totals across the busiest repositories.
    weights = [row[2] for row in REPOSITORIES]
    total_weight = sum(weights)

    for index, (
        name,
        language,
        _commit_count,
        pr_count,
        lead,
        deploys,
        fail,
        mttr,
    ) in enumerate(REPOSITORIES):
        repos.append(
            {
                "name": name,
                "language": language,
                "default_branch": "main",
                "created_at": to_github_date(SINCE - timedelta(days=400 + index * 60)),
                "pushed_at": moment(85 - index * 3),
            }
        )
        branch_counts[name] = 3 + index
        contributor_counts[name] = 4 + index % 5

        share = weights[index] / total_weight
        repo_commits: list[dict[str, Any]] = []
        stats: dict[str, dict[str, int]] = {}

        for login, dev_commits, added, deleted, *_ in DEVELOPERS:
            for n in range(round(dev_commits * share)):
                sha = f"{name}-{login}-{n}"
                repo_commits.append(
                    {
                        "sha": sha,
                        "author": {"login": login, "type": "User"},
                        "commit": {"author": {"date": moment(rng.randrange(1, 89))}},
                    }
                )
                stats[sha] = {
                    "additions": round(
                        added * share / max(round(dev_commits * share), 1)
                    ),
                    "deletions": round(
                        deleted * share / max(round(dev_commits * share), 1)
                    ),
                }

        commits[name] = repo_commits
        commit_stats[name] = stats

        repo_pulls: list[dict[str, Any]] = []
        repo_reviews: dict[str, list[dict[str, Any]]] = {}
        repo_comments: dict[str, list[dict[str, Any]]] = {}
        repo_first: dict[str, str] = {}

        for number in range(1, pr_count + 1):
            author = DEVELOPERS[number % len(DEVELOPERS)][0]
            opened_on = rng.randrange(1, 88)
            merged_at = SINCE + timedelta(days=opened_on, hours=12 + lead)
            repo_pulls.append(
                {
                    "number": number,
                    "user": {"login": author, "type": "User"},
                    "created_at": moment(opened_on),
                    "updated_at": moment(opened_on),
                    "merged_at": to_github_date(merged_at),
                }
            )
            repo_first[str(number)] = moment(opened_on)
            reviewer = DEVELOPERS[(number + 3) % len(DEVELOPERS)][0]
            repo_reviews[str(number)] = [
                {"user": {"login": reviewer}, "submitted_at": moment(opened_on, 14)}
            ]
            repo_comments[str(number)] = [
                {"user": {"login": reviewer}, "created_at": moment(opened_on, 15)}
                for _ in range(number % 3)
            ]

        pulls[name] = repo_pulls
        reviews[name] = repo_reviews
        comments[name] = repo_comments
        first_commits[name] = repo_first

        repo_runs: list[dict[str, Any]] = []
        failures_left = round(deploys * fail / 100)
        for number in range(deploys):
            day = 1 + number * (88 // max(deploys, 1))
            failed = failures_left > 0 and number % 7 == FAILING_RUN_IN_CYCLE
            failures_left -= 1 if failed else 0
            repo_runs.append(
                {
                    "name": "Deploy",
                    "head_branch": "main",
                    "status": "completed",
                    "conclusion": "failure" if failed else "success",
                    "created_at": moment(day, 9),
                    "run_started_at": moment(day, 9),
                    "updated_at": to_github_date(
                        SINCE + timedelta(days=day, hours=9 + (mttr if failed else 0.1))
                    ),
                }
            )
        runs[name] = repo_runs

    return {
        "org": "example-org",
        "since": to_github_date(SINCE),
        "until": to_github_date(UNTIL),
        "fetch_pr_details": True,
        "complete": True,
        "repos": repos,
        "commits": commits,
        "commit_stats": commit_stats,
        "branch_counts": branch_counts,
        "contributor_counts": contributor_counts,
        "pull_requests": pulls,
        "pr_reviews": reviews,
        "pr_comments": comments,
        "pr_first_commit_dates": first_commits,
        "workflow_runs": runs,
    }


def main() -> None:
    """Render the example report and save it as an SVG."""
    report = analyze(build_data(), AnalysisOptions(since=SINCE, until=UNTIL))
    console = Console(record=True, width=CONSOLE_WIDTH, force_terminal=True)
    render(report, console)

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        console.export_svg(title="github-metrics example-org"), encoding="utf-8"
    )
    print(f"Wrote {OUTPUT}")  # noqa: T201 - this script's entire job


if __name__ == "__main__":
    main()
