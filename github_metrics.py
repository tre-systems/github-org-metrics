#!/usr/bin/env python3
"""GitHub Organization Metrics.

A script to fetch and analyze metrics for a GitHub organization,
providing insights into repository activity, developer contributions,
and DORA metrics.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# Constants
GITHUB_API_URL = "https://api.github.com"
CACHE_FILE_SUFFIX = "_github_data_cache.json"
REQUEST_TIMEOUT = 30  # seconds
GITHUB_DATE_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


@dataclass
class DeveloperMetrics:
    """Metrics for a single developer."""

    name: str
    commits: int = 0
    lines_added: int = 0
    lines_deleted: int = 0
    prs_opened: int = 0
    prs_reviewed: int = 0
    pr_comments: int = 0
    repositories: dict[str, int] = field(default_factory=dict)


@dataclass
class RepositoryMetrics:
    """Metrics for a single repository."""

    name: str
    created_at: str = ""
    updated_at: str = ""
    language: str = "N/A"
    branch_count: int = 0
    contributor_count: int = 0
    activity: int = 0
    # DORA metrics
    deployment_count: int = 0
    deployment_failures: int = 0
    failure_rate: float = 0.0
    avg_recovery_time: float = 0.0
    avg_deployment_duration: float = 0.0
    deployment_durations_count: int = 0
    avg_branch_to_merge_time: float = 0.0
    branch_merges_count: int = 0


def parse_github_date(date_str: str) -> datetime:
    """Parse a GitHub API date string to a timezone-aware datetime.

    Args:
        date_str: ISO format date string from GitHub API.

    Returns:
        A timezone-aware datetime object (UTC).
    """
    return datetime.strptime(date_str, GITHUB_DATE_FORMAT).replace(tzinfo=timezone.utc)


def format_date_for_display(date_str: str) -> str:
    """Format a GitHub date string for human-readable display.

    Args:
        date_str: ISO format date string from GitHub API.

    Returns:
        A formatted date string like 'January 15, 2024'.
    """
    return parse_github_date(date_str).strftime("%B %d, %Y")


class GitHubAPIClient:
    """A client for interacting with the GitHub API."""

    def __init__(self, token: str) -> None:
        """Initialize the GitHub API client.

        Args:
            token: GitHub Personal Access Token.
        """
        self.token = token
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.v3+json",
        }

    def _make_request(self, url: str) -> dict[str, Any] | list[Any] | None:
        """Make a request to the GitHub API with rate limit handling.

        Args:
            url: The API endpoint URL.

        Returns:
            The JSON response as a dict/list, or None if the request failed.
        """
        while True:
            try:
                response = requests.get(
                    url, headers=self.headers, timeout=REQUEST_TIMEOUT
                )

                if response.status_code == 200:
                    return response.json()

                if (
                    response.status_code == 403
                    and "X-RateLimit-Remaining" in response.headers
                    and int(response.headers["X-RateLimit-Remaining"]) == 0
                ):
                    reset_time = int(response.headers["X-RateLimit-Reset"])
                    sleep_time = max(1, reset_time - time.time() + 1)
                    logger.warning(
                        "Rate limit exceeded. Sleeping for %.0f seconds.", sleep_time
                    )
                    time.sleep(sleep_time)
                    continue

                if (
                    response.status_code == 403
                    and "Resource not accessible" in response.text
                ):
                    logger.warning("Permission error for %s: Token lacks access.", url)
                    return None

                if response.status_code == 404:
                    logger.debug("Resource not found: %s", url)
                    return None

                logger.error("Error fetching %s: %d", url, response.status_code)
                logger.debug("Response: %s", response.text)
                return None

            except requests.exceptions.RequestException as e:
                logger.error("Request exception for %s: %s", url, e)
                return None

    def _paginate(
        self,
        url: str,
        params: dict[str, str] | None = None,
        max_items: int | None = None,
    ) -> list[dict[str, Any]]:
        """Paginate through GitHub API results.

        Args:
            url: The API endpoint URL (without query params).
            params: Optional query parameters.
            max_items: Maximum number of items to fetch (None for all).

        Returns:
            A list of all items from all pages.
        """
        items: list[dict[str, Any]] = []
        page = 1
        params = params or {}

        while True:
            query_params = {**params, "page": str(page), "per_page": "100"}
            query_string = "&".join(f"{k}={v}" for k, v in query_params.items())
            full_url = f"{url}?{query_string}"

            page_items = self._make_request(full_url)
            if not page_items or not isinstance(page_items, list):
                break

            items.extend(page_items)

            if max_items and len(items) >= max_items:
                return items[:max_items]

            if len(page_items) < 100:
                break

            page += 1

        return items

    def get_org_repos(
        self,
        org: str,
        since: str,
        target_repos: list[str] | None = None,
        max_repos: int = 20,
    ) -> list[dict[str, Any]]:
        """Fetch repositories for an organization.

        Args:
            org: The GitHub organization name.
            since: ISO format date string to filter repos pushed after this date.
            target_repos: Optional list of specific repo names to fetch.
            max_repos: Maximum number of repositories to return if no target specified.

        Returns:
            A list of repository data dictionaries.
        """
        repos: list[dict[str, Any]] = []
        page = 1

        if target_repos:
            found_repos: set[str] = set()
            missing_repos = set(target_repos)

        while True:
            url = f"{GITHUB_API_URL}/orgs/{org}/repos"
            params = {
                "type": "all",
                "sort": "pushed",
                "direction": "desc",
                "page": str(page),
                "per_page": "100",
            }
            query_string = "&".join(f"{k}={v}" for k, v in params.items())
            full_url = f"{url}?{query_string}"

            logger.info("Fetching repositories: page %d", page)
            page_repos = self._make_request(full_url)

            if not page_repos:
                break

            if target_repos:
                filtered = [
                    r
                    for r in page_repos
                    if r["name"] in target_repos and r["pushed_at"] >= since
                ]
                repos.extend(filtered)

                newly_found = {r["name"] for r in filtered}
                found_repos.update(newly_found)
                missing_repos -= newly_found

                logger.info("Found %d target repositories", len(filtered))

                if not missing_repos or len(page_repos) < 100:
                    break
            else:
                filtered = [r for r in page_repos if r["pushed_at"] >= since]
                repos.extend(filtered)
                logger.info("Retrieved %d repositories", len(repos))

                if len(page_repos) < 100 or len(repos) >= max_repos:
                    break

            page += 1

        if target_repos and missing_repos:
            logger.warning("Repositories not found: %s", ", ".join(missing_repos))

        if not target_repos:
            repos = repos[:max_repos]

        logger.info("Total repositories to analyze: %d", len(repos))
        return repos

    def get_commits(self, org: str, repo: str, since: str) -> list[dict[str, Any]]:
        """Fetch commits for a repository.

        Args:
            org: The GitHub organization name.
            repo: The repository name.
            since: ISO format date string.

        Returns:
            A list of commit data dictionaries.
        """
        commits = self._paginate(
            f"{GITHUB_API_URL}/repos/{org}/{repo}/commits", {"since": since}
        )
        logger.info("Commits for %s: %d", repo, len(commits))
        return commits

    def get_commit_stats(self, org: str, repo: str, sha: str) -> dict[str, int] | None:
        """Fetch stats for a specific commit.

        Args:
            org: The GitHub organization name.
            repo: The repository name.
            sha: The commit SHA.

        Returns:
            A dict with 'additions' and 'deletions', or None.
        """
        url = f"{GITHUB_API_URL}/repos/{org}/{repo}/commits/{sha}"
        data = self._make_request(url)
        if data and isinstance(data, dict) and "stats" in data:
            return data["stats"]
        return None

    def get_branches(self, org: str, repo: str) -> list[dict[str, Any]]:
        """Fetch branches for a repository."""
        result = self._make_request(f"{GITHUB_API_URL}/repos/{org}/{repo}/branches")
        return result if isinstance(result, list) else []

    def get_contributors(self, org: str, repo: str) -> list[dict[str, Any]]:
        """Fetch contributors for a repository."""
        result = self._make_request(f"{GITHUB_API_URL}/repos/{org}/{repo}/contributors")
        return result if isinstance(result, list) else []

    def get_pull_requests(
        self, org: str, repo: str, state: str = "all"
    ) -> list[dict[str, Any]]:
        """Fetch pull requests for a repository."""
        prs = self._paginate(
            f"{GITHUB_API_URL}/repos/{org}/{repo}/pulls", {"state": state}
        )
        logger.info("Pull requests for %s: %d", repo, len(prs))
        return prs

    def get_pull_request_reviews(
        self, org: str, repo: str, pr_number: int
    ) -> list[dict[str, Any]]:
        """Fetch reviews for a pull request."""
        result = self._make_request(
            f"{GITHUB_API_URL}/repos/{org}/{repo}/pulls/{pr_number}/reviews"
        )
        return result if isinstance(result, list) else []

    def get_pull_request_comments(
        self, org: str, repo: str, pr_number: int
    ) -> list[dict[str, Any]]:
        """Fetch comments for a pull request."""
        result = self._make_request(
            f"{GITHUB_API_URL}/repos/{org}/{repo}/pulls/{pr_number}/comments"
        )
        return result if isinstance(result, list) else []

    def get_branch_commits(
        self, org: str, repo: str, branch: str
    ) -> dict[str, Any] | None:
        """Get the oldest commit for a branch (to estimate branch creation time)."""
        url = f"{GITHUB_API_URL}/repos/{org}/{repo}/commits?sha={branch}&per_page=100"
        commits = self._make_request(url)
        if commits and isinstance(commits, list) and len(commits) > 0:
            return commits[-1]  # Oldest commit (API returns newest first)
        return None

    def get_workflow_runs(self, org: str, repo: str) -> dict[str, Any] | None:
        """Fetch workflow runs for a repository."""
        url = f"{GITHUB_API_URL}/repos/{org}/{repo}/actions/runs?per_page=100"
        return self._make_request(url)

    def get_workflow_run_details(
        self, org: str, repo: str, run_id: int
    ) -> dict[str, Any] | None:
        """Fetch details for a specific workflow run."""
        url = f"{GITHUB_API_URL}/repos/{org}/{repo}/actions/runs/{run_id}"
        return self._make_request(url)

    def get_deployments(self, org: str, repo: str) -> list[dict[str, Any]]:
        """Fetch deployments for a repository."""
        result = self._make_request(
            f"{GITHUB_API_URL}/repos/{org}/{repo}/deployments?per_page=100"
        )
        return result if isinstance(result, list) else []

    def get_releases(self, org: str, repo: str) -> list[dict[str, Any]]:
        """Fetch releases for a repository."""
        result = self._make_request(
            f"{GITHUB_API_URL}/repos/{org}/{repo}/releases?per_page=100"
        )
        return result if isinstance(result, list) else []

    def get_tags(self, org: str, repo: str) -> list[dict[str, Any]]:
        """Fetch tags for a repository."""
        result = self._make_request(
            f"{GITHUB_API_URL}/repos/{org}/{repo}/tags?per_page=100"
        )
        return result if isinstance(result, list) else []

    def get_issues(
        self, org: str, repo: str, state: str = "all"
    ) -> list[dict[str, Any]]:
        """Fetch issues for a repository (excluding PRs)."""
        all_issues = self._paginate(
            f"{GITHUB_API_URL}/repos/{org}/{repo}/issues", {"state": state}
        )
        # Filter out pull requests
        issues = [i for i in all_issues if "pull_request" not in i]
        logger.info("Issues for %s: %d", repo, len(issues))
        return issues


def fetch_data(
    client: GitHubAPIClient,
    org: str,
    since: str,
    target_repos: list[str] | None = None,
    *,
    fetch_pr_details: bool = False,
) -> dict[str, Any]:
    """Fetch all metrics data for an organization.

    Args:
        client: The GitHub API client.
        org: The GitHub organization name.
        since: ISO format date string.
        target_repos: Optional list of specific repos to analyze.
        fetch_pr_details: If True, fetch per-PR reviews/comments (slow).

    Returns:
        A dictionary containing all fetched data.
    """
    data: dict[str, Any] = {
        "repos": client.get_org_repos(org, since, target_repos),
        "commits": {},
        "commit_stats": {},
        "branches": {},
        "contributors": {},
        "pull_requests": {},
        "pr_reviews": {},
        "pr_comments": {},
        "branch_first_commits": {},
        "workflow_runs": {},
        "workflow_run_details": {},
        "deployments": {},
        "releases": {},
        "tags": {},
        "issues": {},
    }

    for repo in data["repos"]:
        repo_name = repo["name"]
        logger.info("Fetching data for %s", repo_name)

        # Core data
        data["commits"][repo_name] = client.get_commits(org, repo_name, since)
        data["commit_stats"][repo_name] = {}
        for commit in data["commits"][repo_name]:
            data["commit_stats"][repo_name][commit["sha"]] = client.get_commit_stats(
                org, repo_name, commit["sha"]
            )

        data["branches"][repo_name] = client.get_branches(org, repo_name)
        data["contributors"][repo_name] = client.get_contributors(org, repo_name)
        data["pull_requests"][repo_name] = client.get_pull_requests(org, repo_name)

        # DORA-related data
        logger.info("Fetching DORA data for %s", repo_name)

        workflow_runs = client.get_workflow_runs(org, repo_name)
        data["workflow_runs"][repo_name] = workflow_runs

        if workflow_runs and "workflow_runs" in workflow_runs:
            data["workflow_run_details"][repo_name] = {}
            for run in workflow_runs["workflow_runs"]:
                if run.get("status") == "completed":
                    run_id = run["id"]
                    data["workflow_run_details"][repo_name][run_id] = (
                        client.get_workflow_run_details(org, repo_name, run_id)
                    )
            logger.debug(
                "Workflow runs for %s: %d",
                repo_name,
                len(workflow_runs.get("workflow_runs", [])),
            )
        else:
            data["workflow_run_details"][repo_name] = {}

        data["deployments"][repo_name] = client.get_deployments(org, repo_name)
        data["releases"][repo_name] = client.get_releases(org, repo_name)
        data["tags"][repo_name] = client.get_tags(org, repo_name)
        data["issues"][repo_name] = client.get_issues(org, repo_name)

        # PR reviews, comments, and branch data (only if --full)
        data["pr_reviews"][repo_name] = {}
        data["pr_comments"][repo_name] = {}
        data["branch_first_commits"][repo_name] = {}

        if fetch_pr_details:
            total_prs = len(data["pull_requests"][repo_name])
            logger.info("Fetching details for %d PRs in %s", total_prs, repo_name)

            for idx, pr in enumerate(data["pull_requests"][repo_name]):
                pr_number = pr["number"]

                if (idx + 1) % 20 == 0:
                    logger.debug("Processed %d/%d PRs", idx + 1, total_prs)

                data["pr_reviews"][repo_name][pr_number] = (
                    client.get_pull_request_reviews(org, repo_name, pr_number)
                )
                data["pr_comments"][repo_name][pr_number] = (
                    client.get_pull_request_comments(org, repo_name, pr_number)
                )

                # Get branch first commit for open PRs
                if pr.get("state") == "open" and pr.get("head", {}).get("ref"):
                    branch_name = pr["head"]["ref"]
                    data["branch_first_commits"][repo_name][branch_name] = (
                        client.get_branch_commits(org, repo_name, branch_name)
                    )
                # For merged PRs, use PR creation date as fallback
                elif pr.get("merged_at") and pr.get("head", {}).get("ref"):
                    branch_name = pr["head"]["ref"]
                    data["branch_first_commits"][repo_name][branch_name] = {
                        "commit": {"committer": {"date": pr["created_at"]}}
                    }
        else:
            # Fast mode: use PR creation dates as branch start (no per-PR API calls)
            for pr in data["pull_requests"][repo_name]:
                if pr.get("merged_at") and pr.get("head", {}).get("ref"):
                    branch_name = pr["head"]["ref"]
                    data["branch_first_commits"][repo_name][branch_name] = {
                        "commit": {"committer": {"date": pr["created_at"]}}
                    }

    return data


def _detect_ci_workflow(workflow_runs: dict[str, Any]) -> str | None:
    """Detect the primary CI/CD workflow for a repository.

    Args:
        workflow_runs: The workflow runs data from GitHub API.

    Returns:
        The detected workflow name (lowercase), or None.
    """
    if not workflow_runs or "workflow_runs" not in workflow_runs:
        return None

    runs = workflow_runs["workflow_runs"]
    if not runs:
        return None

    workflow_names = [run["name"].lower() for run in runs if run.get("name")]

    # Prefer CI/CD-related workflows
    ci_keywords = ("ci", "test", "build", "deploy")
    ci_workflows = [
        name for name in workflow_names if any(kw in name for kw in ci_keywords)
    ]

    if ci_workflows:
        counter = Counter(ci_workflows)
        return counter.most_common(1)[0][0]

    if workflow_names:
        counter = Counter(workflow_names)
        return counter.most_common(1)[0][0]

    return None


def analyze_data(data: dict[str, Any], since: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Analyze the fetched GitHub data.

    Args:
        data: The data dictionary from fetch_data().
        since: ISO format date string for filtering.

    Returns:
        A tuple of (developer_metrics_df, repository_metrics_df).
    """
    # Developer metrics
    developers: dict[str, DeveloperMetrics] = {}

    # Repository metrics
    repo_metrics: list[RepositoryMetrics] = []
    repo_activity: dict[str, int] = defaultdict(int)

    # PR merge time tracking
    pr_merge_times: list[float] = []
    branch_to_merge_times: list[float] = []
    repo_branch_to_merge_times: dict[str, list[float]] = defaultdict(list)

    # DORA metrics
    repo_deployment_counts: dict[str, int] = defaultdict(int)
    repo_deployment_failures: dict[str, int] = defaultdict(int)
    repo_deployment_durations: dict[str, list[float]] = defaultdict(list)
    repo_deployment_recovery_times: dict[str, list[float]] = defaultdict(list)

    repo_names = [repo["name"] for repo in data["repos"]]

    # Detect CI workflows for each repo
    specific_workflows: dict[str, str] = {}
    for repo_name in repo_names:
        if repo_name in data.get("workflow_runs", {}):
            workflow_name = _detect_ci_workflow(data["workflow_runs"][repo_name])
            if workflow_name:
                specific_workflows[repo_name] = workflow_name
                logger.debug("Detected workflow '%s' for %s", workflow_name, repo_name)

    # Pre-count PR reviews and comments
    def get_developer(name: str) -> DeveloperMetrics:
        if name not in developers:
            developers[name] = DeveloperMetrics(name=name)
        return developers[name]

    # Count PR reviews
    for repo_name in data.get("pr_reviews", {}):
        if repo_name not in repo_names:
            continue
        for pr_number, reviews in data["pr_reviews"][repo_name].items():
            for review in reviews or []:
                if (
                    review
                    and review.get("user", {}).get("login")
                    and review.get("submitted_at")
                ):
                    review_date = parse_github_date(review["submitted_at"])
                    since_date = datetime.fromisoformat(since.replace("Z", "+00:00"))
                    if review_date >= since_date:
                        dev = get_developer(review["user"]["login"])
                        dev.prs_reviewed += 1

    # Count PR comments
    for repo_name in data.get("pr_comments", {}):
        if repo_name not in repo_names:
            continue
        for pr_number, comments in data["pr_comments"][repo_name].items():
            for comment in comments or []:
                if (
                    comment
                    and comment.get("user", {}).get("login")
                    and comment.get("created_at")
                ):
                    comment_date = parse_github_date(comment["created_at"])
                    since_date = datetime.fromisoformat(since.replace("Z", "+00:00"))
                    if comment_date >= since_date:
                        dev = get_developer(comment["user"]["login"])
                        dev.pr_comments += 1

    # Process workflow data for DORA metrics
    for repo_name in repo_names:
        if repo_name not in specific_workflows or repo_name not in data.get(
            "workflow_runs", {}
        ):
            continue

        target_workflow = specific_workflows[repo_name]
        workflow_data = data["workflow_runs"][repo_name]

        if "workflow_runs" not in workflow_data:
            continue

        since_date = datetime.fromisoformat(since.replace("Z", "+00:00"))

        for run in workflow_data["workflow_runs"]:
            if run.get("name", "").lower() != target_workflow:
                continue

            run_date = parse_github_date(run.get("created_at", ""))
            if run_date < since_date:
                continue

            repo_deployment_counts[repo_name] += 1

            if run.get("conclusion") == "success":
                if run.get("created_at") and run.get("updated_at"):
                    created = parse_github_date(run["created_at"])
                    updated = parse_github_date(run["updated_at"])
                    duration_minutes = (updated - created).total_seconds() / 60
                    repo_deployment_durations[repo_name].append(duration_minutes)
            elif run.get("conclusion") == "failure":
                repo_deployment_failures[repo_name] += 1

    # Process each repository
    for repo in data["repos"]:
        repo_name = repo["name"]

        # Build repository metrics
        branches = data.get("branches", {}).get(repo_name, []) or []
        contributors = data.get("contributors", {}).get(repo_name, []) or []
        durations = repo_deployment_durations.get(repo_name, [])
        recovery_times = repo_deployment_recovery_times.get(repo_name, [])

        metrics = RepositoryMetrics(
            name=repo_name,
            created_at=format_date_for_display(repo["created_at"]),
            updated_at=format_date_for_display(repo["updated_at"]),
            language=repo.get("language") or "N/A",
            branch_count=len(branches) if isinstance(branches, list) else 0,
            contributor_count=len(contributors)
            if isinstance(contributors, list)
            else 0,
            deployment_count=repo_deployment_counts.get(repo_name, 0),
            deployment_failures=repo_deployment_failures.get(repo_name, 0),
            avg_deployment_duration=sum(durations) / len(durations) if durations else 0,
            deployment_durations_count=len(durations),
            avg_recovery_time=sum(recovery_times) / len(recovery_times)
            if recovery_times
            else 0,
        )

        if metrics.deployment_count > 0:
            metrics.failure_rate = (
                metrics.deployment_failures / metrics.deployment_count
            ) * 100

        # Process commits
        for commit in data.get("commits", {}).get(repo_name, []):
            commit_date = commit.get("commit", {}).get("author", {}).get("date", "")
            if commit_date >= since:
                author_data = commit.get("author") or {}
                if author_data.get("login"):
                    author = author_data["login"]
                    dev = get_developer(author)
                    dev.commits += 1
                    dev.repositories[repo_name] = dev.repositories.get(repo_name, 0) + 1
                    repo_activity[repo_name] += 1

                    stats = (
                        data.get("commit_stats", {})
                        .get(repo_name, {})
                        .get(commit["sha"])
                    )
                    if stats:
                        dev.lines_added += stats.get("additions", 0)
                        dev.lines_deleted += stats.get("deletions", 0)

        # Process PRs for merge time tracking
        branch_merge_times_for_repo: list[float] = []
        since_date = datetime.fromisoformat(since.replace("Z", "+00:00"))

        for pr in data.get("pull_requests", {}).get(repo_name, []):
            user = pr.get("user") or {}
            if not user.get("login"):
                continue

            pr_created = parse_github_date(pr["created_at"])
            pr_updated = parse_github_date(pr["updated_at"])

            if pr_created >= since_date or pr_updated >= since_date:
                dev = get_developer(user["login"])
                dev.prs_opened += 1

                if pr.get("merged_at"):
                    merged_at = parse_github_date(pr["merged_at"])

                    # PR merge time
                    if pr_created >= since_date:
                        merge_hours = (merged_at - pr_created).total_seconds() / 3600
                        if merge_hours <= 30 * 24:  # Filter outliers > 30 days
                            pr_merge_times.append(merge_hours)

                    # Branch to merge time
                    branch_name = pr.get("head", {}).get("ref")
                    if branch_name:
                        first_commit = (
                            data.get("branch_first_commits", {})
                            .get(repo_name, {})
                            .get(branch_name)
                        )
                        if first_commit:
                            commit_date_str = (
                                first_commit.get("commit", {})
                                .get("committer", {})
                                .get("date")
                            )
                            if commit_date_str:
                                branch_start = parse_github_date(commit_date_str)
                                branch_merge_hours = (
                                    merged_at - branch_start
                                ).total_seconds() / 3600
                                if (
                                    branch_merge_hours <= 90 * 24
                                ):  # Filter outliers > 90 days
                                    branch_to_merge_times.append(branch_merge_hours)
                                    branch_merge_times_for_repo.append(
                                        branch_merge_hours
                                    )

        repo_branch_to_merge_times[repo_name] = branch_merge_times_for_repo

        if branch_merge_times_for_repo:
            metrics.avg_branch_to_merge_time = sum(branch_merge_times_for_repo) / len(
                branch_merge_times_for_repo
            )
            metrics.branch_merges_count = len(branch_merge_times_for_repo)

        metrics.activity = repo_activity.get(repo_name, 0)
        repo_metrics.append(metrics)

    # Build DataFrames
    def format_repos_list(repos_dict: dict[str, int]) -> str:
        sorted_repos = sorted(repos_dict.items(), key=lambda x: x[1], reverse=True)
        if len(sorted_repos) <= 5:
            return ", ".join(r for r, _ in sorted_repos)
        top_5 = ", ".join(r for r, _ in sorted_repos[:5])
        return f"{top_5} +{len(sorted_repos) - 5} more"

    df_developers = pd.DataFrame(
        [
            {
                "Developer": dev.name,
                "Commits": dev.commits,
                "Lines Added": dev.lines_added,
                "Lines Deleted": dev.lines_deleted,
                "PRs Opened": dev.prs_opened,
                "PRs Reviewed": dev.prs_reviewed,
                "PR Comments": dev.pr_comments,
                "Repositories": format_repos_list(dev.repositories),
            }
            for dev in developers.values()
        ]
    )
    df_developers = df_developers.sort_values("Commits", ascending=False)

    df_repos = pd.DataFrame(
        [
            {
                "name": m.name,
                "Activity": m.activity,
                "avg_branch_to_merge_time": m.avg_branch_to_merge_time,
                "branch_merges_count": m.branch_merges_count,
                "deployment_count": m.deployment_count,
                "failure_rate": m.failure_rate,
                "avg_recovery_time": m.avg_recovery_time,
                "avg_deployment_duration": m.avg_deployment_duration,
                "deployment_durations_count": m.deployment_durations_count,
                "created_at": m.created_at,
                "updated_at": m.updated_at,
                "language": m.language,
                "branch_count": m.branch_count,
                "contributor_count": m.contributor_count,
            }
            for m in repo_metrics
        ]
    )
    df_repos = df_repos.sort_values("Activity", ascending=False)

    # Print results
    avg_pr_merge = sum(pr_merge_times) / len(pr_merge_times) if pr_merge_times else 0
    avg_branch_merge = (
        sum(branch_to_merge_times) / len(branch_to_merge_times)
        if branch_to_merge_times
        else 0
    )

    logger.info("")
    logger.info("=" * 80)
    logger.info("FINAL RESULTS")
    logger.info("=" * 80)
    logger.info("Analyzed Repositories: %s", ", ".join(repo_names))
    logger.info("Average PR Merge Time: %.2f hours", avg_pr_merge)
    logger.info("Average Branch-to-Merge Time: %.2f hours", avg_branch_merge)

    pd.set_option("display.max_colwidth", None)
    print("\nDeveloper Activity:")
    print(df_developers.to_string(index=False))
    print("\nRepository Details:")
    print(df_repos.to_string(index=False))

    # DORA summary
    print("\nDORA Metrics Summary:")
    print(f"Lead Time (Branch to Merge): {avg_branch_merge:.2f} hours")

    all_durations = [
        d for durations in repo_deployment_durations.values() for d in durations
    ]
    if all_durations:
        print(
            f"Average Deployment Duration: {sum(all_durations) / len(all_durations):.2f} minutes"
        )
    else:
        print("Average Deployment Duration: No data")

    total_deployments = sum(repo_deployment_counts.values())
    total_failures = sum(repo_deployment_failures.values())
    if total_deployments > 0:
        print(f"Change Failure Rate: {(total_failures / total_deployments) * 100:.2f}%")
    else:
        print("Change Failure Rate: No data")

    return df_developers, df_repos


def save_cache(data: dict[str, Any], org: str) -> None:
    """Save fetched data to a cache file."""
    cache_file = Path(f"{org}{CACHE_FILE_SUFFIX}")
    with cache_file.open("w") as f:
        json.dump(data, f)
    logger.info("Data cached to %s", cache_file)


def load_cache(org: str) -> dict[str, Any] | None:
    """Load cached data if available."""
    cache_file = Path(f"{org}{CACHE_FILE_SUFFIX}")
    if cache_file.exists():
        with cache_file.open() as f:
            return json.load(f)
    return None


def main(
    org: str,
    months: int,
    token: str,
    *,
    repos_count: int = 20,
    target_repos: list[str] | None = None,
    use_cache: bool = False,
    update_cache: bool = False,
    fetch_pr_details: bool = False,
) -> None:
    """Main entry point for the GitHub metrics script.

    Args:
        org: The GitHub organization name.
        months: Number of months to analyze.
        token: GitHub Personal Access Token.
        repos_count: Max repos to analyze (if no target specified).
        target_repos: Optional list of specific repos.
        use_cache: Whether to use cached data.
        update_cache: Whether to refresh the cache.
        fetch_pr_details: If True, fetch per-PR reviews/comments (slow).
    """
    data: dict[str, Any] | None = None

    if use_cache and not update_cache:
        data = load_cache(org)
        if data:
            logger.info("Using cached data")
            if target_repos:
                data["repos"] = [r for r in data["repos"] if r["name"] in target_repos]
                logger.info("Filtered cache to %d target repos", len(data["repos"]))
        else:
            logger.warning("Cache not found, fetching new data")

    if data is None or update_cache:
        end_date = datetime.now(timezone.utc)
        start_date = end_date - timedelta(days=30 * months)
        since = start_date.isoformat().replace("+00:00", "Z")

        logger.info("Fetching data for organization: %s", org)
        client = GitHubAPIClient(token)
        data = fetch_data(
            client, org, since, target_repos, fetch_pr_details=fetch_pr_details
        )
        save_cache(data, org)

    # Analyze with current time range
    end_date = datetime.now(timezone.utc)
    start_date = end_date - timedelta(days=30 * months)
    since = start_date.isoformat().replace("+00:00", "Z")

    df_developers, df_repos = analyze_data(data, since)

    # Save CSVs
    df_developers.to_csv(f"{org}_github_developer_metrics.csv", index=False)
    df_repos.to_csv(f"{org}_github_repository_metrics.csv", index=False)

    logger.info("Results saved to %s_github_*.csv", org)


def cli() -> None:
    """Command-line interface entry point."""
    parser = argparse.ArgumentParser(
        description="Fetch and analyze GitHub organization metrics with DORA insights.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s my-org                           Analyze top 20 repos from last 3 months
  %(prog)s my-org --months 6                Analyze last 6 months
  %(prog)s my-org --target-repos a b c      Analyze specific repos
  %(prog)s my-org --use-cache               Use cached data
  %(prog)s my-org --update-cache            Refresh the cache
        """,
    )
    parser.add_argument("org", help="GitHub organization name")
    parser.add_argument(
        "--months", type=int, default=3, help="Months to analyze (default: 3)"
    )
    parser.add_argument(
        "--repos", type=int, default=20, help="Max repos to analyze (default: 20)"
    )
    parser.add_argument("--target-repos", nargs="+", help="Specific repos to analyze")
    parser.add_argument("--use-cache", action="store_true", help="Use cached data")
    parser.add_argument("--update-cache", action="store_true", help="Refresh cache")
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug logging"
    )
    parser.add_argument(
        "--full", action="store_true", help="Fetch detailed PR reviews/comments (slow)"
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        logger.error("GITHUB_TOKEN environment variable not set.")
        sys.exit(1)

    logger.info("Token: %s...%s", token[:4], token[-4:])
    logger.info("Organization: %s", args.org)
    logger.info("Analyzing last %d months", args.months)

    if args.target_repos:
        logger.info("Target repos: %s", ", ".join(args.target_repos))
    else:
        logger.info("Analyzing top %d repos", args.repos)

    main(
        args.org,
        args.months,
        token,
        repos_count=args.repos,
        target_repos=args.target_repos,
        use_cache=args.use_cache,
        update_cache=args.update_cache,
        fetch_pr_details=args.full,
    )


if __name__ == "__main__":
    cli()
