"""Command-line interface."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)

from . import __version__, cache
from .analyze import DEFAULT_OUTLIER_THRESHOLD, AnalysisOptions, analyze
from .client import AuthenticationError, GitHubClient, GitHubError
from .collect import CollectionOptions, Collector
from .dates import months_before, parse_github_date
from .models import RawData
from .report import export_csv, render

logger = logging.getLogger("github_metrics")

__all__ = ["build_parser", "main"]

TOKEN_ENV_VARS = ("GITHUB_TOKEN", "GH_TOKEN")
DEFAULT_MONTHS = 3
DEFAULT_WORKERS = 8

#: Repositories between cache checkpoints during a long fetch.
CHECKPOINT_EVERY = 10


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="github-metrics",
        description=(
            "Fetch and analyse GitHub organization metrics, including DORA metrics."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  %(prog)s my-org                          Analyse the last 3 months
  %(prog)s my-org --months 6               Analyse the last 6 months
  %(prog)s my-org --repos 10               Analyse the 10 most recently active repos
  %(prog)s my-org --target-repos api web   Analyse specific repositories
  %(prog)s my-org --fast                   Skip per-pull-request detail calls
  %(prog)s my-org --use-cache              Re-analyse without calling the API

Set GITHUB_TOKEN (or GH_TOKEN) to a personal access token with read access to
the organization. A token is not needed when reading from the cache.
""",
    )
    parser.add_argument("org", help="GitHub organization name")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    window = parser.add_argument_group("analysis window")
    exclusive = window.add_mutually_exclusive_group()
    exclusive.add_argument(
        "--months",
        type=_positive_int,
        help=f"Calendar months to analyse (default: {DEFAULT_MONTHS})",
    )
    exclusive.add_argument(
        "--days", type=_positive_int, help="Days to analyse, instead of --months"
    )

    scope = parser.add_argument_group("scope")
    scope.add_argument(
        "--repos",
        type=_positive_int,
        default=None,
        metavar="N",
        help="Analyse only the N most recently active repositories (default: all)",
    )
    scope.add_argument(
        "--target-repos",
        nargs="+",
        metavar="REPO",
        help="Analyse specific repositories by name",
    )
    scope.add_argument(
        "--fast",
        action="store_true",
        help="Skip per-pull-request reviews, comments, and branch history",
    )
    scope.add_argument(
        "--deploy-workflow",
        metavar="NAME",
        help=(
            "Treat this workflow as the deployment, instead of inferring one "
            "from workflow names"
        ),
    )
    scope.add_argument(
        "--workers",
        type=_positive_int,
        default=DEFAULT_WORKERS,
        metavar="N",
        help=f"Concurrent API requests (default: {DEFAULT_WORKERS})",
    )

    output = parser.add_argument_group("output")
    output.add_argument(
        "--output-dir",
        type=Path,
        default=Path.cwd(),
        metavar="DIR",
        help="Where to write CSV files and the cache (default: current directory)",
    )
    output.add_argument("--no-csv", action="store_true", help="Skip writing CSV files")
    output.add_argument(
        "--export-svg",
        type=Path,
        metavar="FILE",
        help="Also save the console report as an SVG image",
    )
    output.add_argument(
        "--anonymize",
        action="store_true",
        help="Replace developer logins with stable pseudonyms everywhere",
    )
    output.add_argument(
        "--outlier-threshold",
        type=int,
        default=DEFAULT_OUTLIER_THRESHOLD,
        metavar="LINES",
        help=(
            "Report developers above this many added lines separately "
            f"(default: {DEFAULT_OUTLIER_THRESHOLD:,}; 0 disables)"
        ),
    )
    output.add_argument(
        "--include-inactive",
        action="store_true",
        help="Include contributors who changed no lines (reviewers, for example)",
    )

    caching = parser.add_argument_group("caching")
    caching.add_argument(
        "--use-cache", action="store_true", help="Analyse cached data if present"
    )
    caching.add_argument(
        "--update-cache",
        action="store_true",
        help="Fetch fresh data and rewrite the cache",
    )
    caching.add_argument(
        "--no-cache", action="store_true", help="Do not write a cache file"
    )

    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug logging"
    )
    parser.add_argument(
        "-q", "--quiet", action="store_true", help="Only log warnings and errors"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI.

    Args:
        argv: Argument list, defaulting to ``sys.argv[1:]``.

    Returns:
        A process exit code.
    """
    args = build_parser().parse_args(argv)
    console = Console(record=args.export_svg is not None)
    _configure_logging(console, verbose=args.verbose, quiet=args.quiet)

    until = datetime.now(UTC)
    since = (
        until - timedelta(days=args.days)
        if args.days
        else months_before(until, args.months or DEFAULT_MONTHS)
    )

    try:
        data = _load_or_collect(args, since, until, console)
    except AuthenticationError as exc:
        logger.error("%s", exc)
        return 2
    except GitHubError as exc:
        logger.error("GitHub API error: %s", exc)
        return 1
    except KeyboardInterrupt:
        logger.warning("Interrupted")
        return 130

    if data is None:
        return 1

    _warn_on_window_mismatch(data, since)

    report = analyze(
        data,
        AnalysisOptions(
            since=since,
            until=until,
            outlier_threshold=args.outlier_threshold,
            include_inactive=args.include_inactive,
            deploy_workflow=args.deploy_workflow,
        ),
    )

    render(report, console, anonymize=args.anonymize)

    if args.export_svg is not None:
        args.export_svg.parent.mkdir(parents=True, exist_ok=True)
        args.export_svg.write_text(
            console.export_svg(title=f"github-metrics {args.org}"), encoding="utf-8"
        )
        logger.info("Wrote %s", args.export_svg)

    if not args.no_csv:
        paths = export_csv(report, args.output_dir, anonymize=args.anonymize)
        console.print("Saved " + ", ".join(f"[cyan]{path.name}[/cyan]" for path in paths))

    return 0


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _load_or_collect(
    args: argparse.Namespace,
    since: datetime,
    until: datetime,
    console: Console,
) -> RawData | None:
    """Return raw data from the cache when asked for, otherwise from the API."""
    path = cache.cache_path(args.org, args.output_dir)

    if args.use_cache and not args.update_cache:
        cached = cache.load(path)
        if cached is not None:
            if args.target_repos:
                targets = set(args.target_repos)
                cached["repos"] = [
                    repo for repo in cached.get("repos", []) if repo["name"] in targets
                ]
            return cached
        logger.warning("No usable cache at %s; fetching fresh data", path)

    token = _resolve_token()
    if token is None:
        logger.error(
            "No GitHub token found. Set %s, or pass --use-cache to analyse "
            "previously fetched data.",
            " or ".join(TOKEN_ENV_VARS),
        )
        return None

    options = CollectionOptions(
        org=args.org,
        since=since,
        until=until,
        target_repos=tuple(args.target_repos) if args.target_repos else None,
        max_repos=args.repos,
        fetch_pr_details=not args.fast,
        max_workers=args.workers,
    )

    logger.info(
        "Fetching %s from %s to %s%s",
        args.org,
        since.date(),
        until.date(),
        " (fast mode)" if args.fast else "",
    )

    client = GitHubClient(token)
    checkpoint = None if args.no_cache else path
    # --update-cache asks for a fresh answer, so it never resumes.
    resume_from = None if args.update_cache or args.no_cache else cache.load(path)
    collector = Collector(client, options, resume_from=resume_from)

    try:
        _collect_with_progress(collector, console, checkpoint)
    except KeyboardInterrupt:
        _checkpoint(collector.data, checkpoint)
        logger.warning(
            "Interrupted. Progress so far is cached; re-run to continue from it."
        )
        raise

    _checkpoint(collector.data, checkpoint)
    return collector.data


def _checkpoint(data: RawData, path: Path | None) -> None:
    """Write partial or complete data to the cache, if caching is enabled."""
    if path is not None:
        cache.save(data, path)


def _collect_with_progress(
    collector: Collector, console: Console, checkpoint: Path | None
) -> RawData:
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Listing repositories", total=None)

        def advance(name: str, completed: int, total: int) -> None:
            progress.update(
                task, completed=completed, total=total, description=f"Fetched {name}"
            )
            # Checkpoint periodically so a long run survives being interrupted.
            if completed % CHECKPOINT_EVERY == 0 and completed != total:
                _checkpoint(collector.data, checkpoint)

        return collector.run(advance)


def _warn_on_window_mismatch(data: RawData, since: datetime) -> None:
    """Warn when the requested window starts before the cached data does."""
    cached_since = parse_github_date(data.get("since"))
    if cached_since is not None and cached_since > since:
        logger.warning(
            "Cached data only covers %s onwards, but %s was requested. "
            "Re-run with --update-cache for the full window.",
            cached_since.date(),
            since.date(),
        )


def _resolve_token() -> str | None:
    for name in TOKEN_ENV_VARS:
        token = os.environ.get(name)
        if token:
            return token.strip()
    return None


def _configure_logging(console: Console, *, verbose: bool, quiet: bool) -> None:
    level = logging.DEBUG if verbose else logging.WARNING if quiet else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            RichHandler(
                console=console,
                show_path=False,
                rich_tracebacks=True,
                omit_repeated_times=False,
            )
        ],
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        message = f"{value!r} is not an integer"
        raise argparse.ArgumentTypeError(message) from exc
    if parsed <= 0:
        message = "value must be greater than zero"
        raise argparse.ArgumentTypeError(message)
    return parsed


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
