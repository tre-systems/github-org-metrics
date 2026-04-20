"""Command-line entry point for `github-metrics`."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import pandas as pd

from github_metrics import __version__
from github_metrics.analyze import analyze
from github_metrics.client import GitHubAPIClient
from github_metrics.fetch import fetch_data
from github_metrics.models import iso_since

CACHE_FILE_SUFFIX = "_github_data_cache.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------- cache


def _cache_path(org: str, output_dir: Path) -> Path:
    return output_dir / f"{org}{CACHE_FILE_SUFFIX}"


def save_cache(data: dict[str, Any], org: str, output_dir: Path) -> None:
    path = _cache_path(org, output_dir)
    with path.open("w") as fh:
        json.dump(data, fh)
    logger.info("Cache saved to %s", path)


def load_cache(org: str, output_dir: Path) -> dict[str, Any] | None:
    path = _cache_path(org, output_dir)
    if not path.exists():
        return None
    with path.open() as fh:
        return json.load(fh)


# -------------------------------------------------------------------- display


def _anonymize(name: str) -> str:
    return f"user-{hashlib.md5(name.encode()).hexdigest()[:6]}"


def print_dataframe(df: pd.DataFrame, *, anonymize: bool = False) -> None:
    """Print a DataFrame left-aligned and free of index noise."""
    if df.empty:
        print("(no rows)")
        return
    shown = df.copy()
    if anonymize and "Developer" in shown.columns:
        shown["Developer"] = shown["Developer"].apply(_anonymize)

    formatters: dict[str, Any] = {}
    for col in shown.columns:
        if pd.api.types.is_string_dtype(shown[col]) or shown[col].dtype == "object":
            width = max(int(shown[col].astype(str).str.len().max() or 0), len(col))
            formatters[col] = f"{{:<{width}}}".format

    pd.set_option("display.max_colwidth", None)
    print(shown.to_string(index=False, formatters=formatters))


# --------------------------------------------------------------------- runner


def run(
    org: str,
    months: int,
    token: str,
    *,
    max_repos: int | None = None,
    target_repos: list[str] | None = None,
    use_cache: bool = False,
    update_cache: bool = False,
    fetch_pr_details: bool = True,
    anonymize: bool = False,
    max_prs_per_repo: int = 50,
    output_dir: Path | None = None,
) -> None:
    """Fetch/analyze metrics for `org` and emit CSVs + a console report."""
    output_dir = output_dir or Path.cwd()
    output_dir.mkdir(parents=True, exist_ok=True)

    data: dict[str, Any] | None = None
    if use_cache and not update_cache:
        data = load_cache(org, output_dir)
        if data is None:
            logger.warning("Cache not found; fetching fresh data.")
        else:
            logger.info("Using cached data from %s", _cache_path(org, output_dir))
            if target_repos:
                data["repos"] = [r for r in data["repos"] if r["name"] in target_repos]

    if data is None or update_cache:
        since = iso_since(months)
        logger.info("Fetching data for %s (last %d months)", org, months)
        client = GitHubAPIClient(token)
        data = fetch_data(
            client,
            org,
            since,
            target_repos,
            fetch_pr_details=fetch_pr_details,
            max_prs_per_repo=max_prs_per_repo,
            max_repos=max_repos,
        )
        save_cache(data, org, output_dir)

    since = iso_since(months)
    df_devs, df_repos, df_outliers = analyze(data, since)

    print("\nDeveloper Activity:")
    print_dataframe(df_devs, anonymize=anonymize)

    if not df_outliers.empty:
        print("\nOutliers (>100k lines — likely bulk imports or generated files):")
        print_dataframe(df_outliers, anonymize=anonymize)

    print("\nRepository Details:")
    print_dataframe(df_repos)

    dev_path = output_dir / f"{org}_github_developer_metrics.csv"
    repo_path = output_dir / f"{org}_github_repository_metrics.csv"
    df_devs.to_csv(dev_path, index=False)
    df_repos.to_csv(repo_path, index=False)
    logger.info("Wrote %s", dev_path)
    logger.info("Wrote %s", repo_path)
    if not df_outliers.empty:
        outlier_path = output_dir / f"{org}_github_outliers.csv"
        df_outliers.to_csv(outlier_path, index=False)
        logger.info("Wrote %s", outlier_path)


# ----------------------------------------------------------------------- argv


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="github-metrics",
        description=(
            "Fetch and analyze GitHub organization metrics: developer activity, "
            "repository health, and DORA-adjacent signals."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  github-metrics my-org\n"
            "  github-metrics my-org --months 6 --fast\n"
            "  github-metrics my-org --target-repos api web\n"
            "  github-metrics my-org --use-cache\n"
        ),
    )
    parser.add_argument("org", help="GitHub organization name")
    parser.add_argument(
        "--months", type=int, default=3, help="Months of history to analyze (default: 3)"
    )
    parser.add_argument(
        "--repos",
        type=int,
        default=None,
        metavar="N",
        help="Limit to the top N most recently pushed repos (default: all)",
    )
    parser.add_argument(
        "--target-repos",
        nargs="+",
        metavar="REPO",
        help="Analyze only the listed repositories",
    )
    parser.add_argument("--use-cache", action="store_true", help="Reuse cached data if available")
    parser.add_argument("--update-cache", action="store_true", help="Refresh the on-disk cache")
    parser.add_argument(
        "--fast",
        action="store_true",
        help="Skip per-PR reviews/comments for a faster run",
    )
    parser.add_argument(
        "--anonymize",
        action="store_true",
        help="Anonymize developer names in console output (CSVs are unaffected)",
    )
    parser.add_argument(
        "--max-prs",
        type=int,
        default=50,
        metavar="N",
        help="Cap on recent PRs to hydrate with review/comment details (default: 50)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory to write cache + CSVs into (default: cwd)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    parser.add_argument("--version", action="version", version=f"github-metrics {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        logger.error(
            "GITHUB_TOKEN environment variable not set. "
            "Create a fine-grained token with read access and export it: "
            "export GITHUB_TOKEN=..."
        )
        return 1

    logger.info("Token: %s…%s", token[:4], token[-4:])
    logger.info("Organization: %s", args.org)
    logger.info("Window: last %d month(s)", args.months)
    if args.target_repos:
        logger.info("Target repos: %s", ", ".join(args.target_repos))
    elif args.repos:
        logger.info("Limiting to top %d repos by recent push", args.repos)

    run(
        args.org,
        args.months,
        token,
        max_repos=args.repos,
        target_repos=args.target_repos,
        use_cache=args.use_cache,
        update_cache=args.update_cache,
        fetch_pr_details=not args.fast,
        anonymize=args.anonymize,
        max_prs_per_repo=args.max_prs,
        output_dir=args.output_dir,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
