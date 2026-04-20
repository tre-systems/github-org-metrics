"""End-to-end wiring: drive `cli.run()` with a pre-seeded cache and verify outputs."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

from github_metrics.cli import run
from github_metrics.fetch import DATA_SCHEMA_VERSION


def _seed_cache(output_dir: Path) -> Path:
    """Write a cache file with a complete mini-org payload."""
    # Anchor fixture timestamps just inside the default 3-month window.
    now = datetime.now(UTC).replace(microsecond=0)
    recent = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent_plus_hour = (now - timedelta(hours=23)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ten_days_ago = (now - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")

    payload = {
        "_schema": DATA_SCHEMA_VERSION,
        "fetch_pr_details": True,
        "repos": [
            {
                "name": "api",
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": recent,
                "language": "Python",
            }
        ],
        "commits": {
            "api": [
                {
                    "sha": "abc",
                    "commit": {"author": {"date": recent, "email": "a@x", "name": "alice"}},
                    "author": {"login": "alice"},
                }
            ]
        },
        "commit_stats": {"api": {"abc": {"additions": 42, "deletions": 7}}},
        "branches": {"api": [{"name": "main"}, {"name": "feat"}]},
        "contributors": {"api": [{"login": "alice"}]},
        "pull_requests": {
            "api": [
                {
                    "number": 1,
                    "state": "closed",
                    "user": {"login": "alice"},
                    "created_at": ten_days_ago,
                    "updated_at": recent,
                    "merged_at": recent,
                    "head": {"ref": "feat"},
                }
            ]
        },
        "pr_reviews": {
            "api": {1: [{"user": {"login": "alice"}, "submitted_at": recent_plus_hour}]}
        },
        "pr_comments": {"api": {1: [{"user": {"login": "alice"}, "created_at": recent_plus_hour}]}},
        "branch_first_commits": {
            "api": {"feat": {"commit": {"committer": {"date": ten_days_ago}}}}
        },
        "workflow_runs": {
            "api": [
                {
                    "name": "CI",
                    "conclusion": "success",
                    "created_at": recent,
                    "updated_at": recent_plus_hour,
                }
            ]
        },
    }
    path = output_dir / "my-org_github_data_cache.json"
    path.write_text(json.dumps(payload))
    return path


def test_run_end_to_end_emits_expected_csvs(tmp_path: Path, capsys):
    _seed_cache(tmp_path)

    run(
        "my-org",
        months=3,
        token="unused-when-use-cache",
        use_cache=True,
        output_dir=tmp_path,
    )

    dev_csv = tmp_path / "my-org_github_developer_metrics.csv"
    repo_csv = tmp_path / "my-org_github_repository_metrics.csv"
    assert dev_csv.exists()
    assert repo_csv.exists()
    assert not (tmp_path / "my-org_github_outliers.csv").exists()  # nobody >100k lines

    devs = pd.read_csv(dev_csv)
    assert list(devs["Developer"]) == ["alice"]
    assert int(devs.iloc[0]["Lines Added"]) == 42
    assert int(devs.iloc[0]["Lines Deleted"]) == 7
    assert int(devs.iloc[0]["PRs Opened"]) == 1
    assert int(devs.iloc[0]["PRs Reviewed"]) == 1
    assert int(devs.iloc[0]["PR Comments"]) == 1

    repos = pd.read_csv(repo_csv)
    row = repos.iloc[0]
    assert row["Repository"] == "api"
    assert int(row["Commits"]) == 1
    assert int(row["PRs"]) == 1
    assert int(row["CI Runs"]) == 1
    assert float(row["CI Fail %"]) == 0.0

    stdout = capsys.readouterr().out
    assert "Developer Activity" in stdout
    assert "Repository Details" in stdout


def test_run_end_to_end_anonymize_hides_names_in_console_only(tmp_path: Path, capsys):
    _seed_cache(tmp_path)
    run("my-org", months=3, token="unused", use_cache=True, anonymize=True, output_dir=tmp_path)

    stdout = capsys.readouterr().out
    assert "alice" not in stdout
    assert "user-" in stdout

    # CSVs should still have real names.
    devs = pd.read_csv(tmp_path / "my-org_github_developer_metrics.csv")
    assert "alice" in set(devs["Developer"])
