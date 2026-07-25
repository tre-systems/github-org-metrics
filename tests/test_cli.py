"""Tests for argument handling and the end-to-end cached run."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import responses

from github_metrics import cache
from github_metrics.cli import build_parser, main
from github_metrics.dates import to_github_date
from tests.conftest import commit, pull, raw_data, repo, run


def days_ago(days: int, hours: int = 0) -> str:
    """A timestamp inside the default three-month window."""
    return to_github_date(datetime.now(UTC) - timedelta(days=days, hours=hours))


@pytest.fixture(autouse=True)
def no_ambient_token(monkeypatch):
    """Keep a developer's real token out of the tests."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)


@pytest.fixture
def cached_org(tmp_path):
    """A cache file with one repository's worth of recent activity."""
    data = raw_data(
        repos=[repo("api")],
        commits={"api": [commit("a1", "alice", days_ago(10))]},
        commit_stats={"api": {"a1": {"additions": 42, "deletions": 7}}},
        pull_requests={"api": [pull(1, "alice", days_ago(9), merged=days_ago(8))]},
        pr_reviews={
            "api": {"1": [{"user": {"login": "bob"}, "submitted_at": days_ago(8)}]}
        },
        workflow_runs={"api": [run("Deploy", days_ago(7), "success")]},
        since=days_ago(90),
    )
    cache.save(data, cache.cache_path("acme", tmp_path))
    return tmp_path


class TestArgumentParsing:
    def test_months_and_days_are_mutually_exclusive(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["acme", "--months", "3", "--days", "30"])

    @pytest.mark.parametrize("value", ["0", "-1", "many"])
    def test_rejects_a_nonsensical_window(self, value):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["acme", "--months", value])

    def test_defaults(self):
        args = build_parser().parse_args(["acme"])

        assert args.org == "acme"
        assert args.months is None
        assert args.repos is None
        assert not args.fast


class TestCachedRun:
    def test_writes_reports_and_exits_cleanly(self, cached_org, capsys):
        code = main(["acme", "--use-cache", "--output-dir", str(cached_org)])

        assert code == 0
        assert (cached_org / "acme_github_developer_metrics.csv").exists()
        assert (cached_org / "acme_github_repository_metrics.csv").exists()
        assert "alice" in capsys.readouterr().out

    def test_needs_no_token(self, cached_org):
        assert main(["acme", "--use-cache", "--output-dir", str(cached_org)]) == 0

    def test_no_csv_leaves_the_directory_alone(self, cached_org):
        main(["acme", "--use-cache", "--no-csv", "--output-dir", str(cached_org)])

        assert not list(cached_org.glob("*.csv"))

    def test_anonymize_hides_logins_everywhere(self, cached_org, capsys):
        main(["acme", "--use-cache", "--anonymize", "--output-dir", str(cached_org)])

        assert "alice" not in capsys.readouterr().out
        csv_text = (cached_org / "acme_github_developer_metrics.csv").read_text()
        assert "alice" not in csv_text

    def test_target_repos_filters_the_cache(self, cached_org, capsys):
        main(
            [
                "acme",
                "--use-cache",
                "--target-repos",
                "other",
                "--output-dir",
                str(cached_org),
            ]
        )

        assert "alice" not in capsys.readouterr().out

    def test_warns_when_the_cache_does_not_cover_the_window(self, cached_org, caplog):
        main(
            [
                "acme",
                "--use-cache",
                "--months",
                "12",
                "--output-dir",
                str(cached_org),
            ]
        )

        assert "--update-cache" in caplog.text


class TestFailureModes:
    def test_missing_token_is_a_clear_error(self, tmp_path, caplog):
        code = main(["acme", "--use-cache", "--output-dir", str(tmp_path)])

        assert code == 1
        assert "No GitHub token found" in caplog.text

    def test_rejected_token_exits_distinctly(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setenv("GITHUB_TOKEN", "bad-token")

        with responses.RequestsMock() as mocked:
            mocked.get("https://api.github.com/orgs/acme/repos", status=401, json={})
            code = main(["acme", "--output-dir", str(tmp_path), "--no-cache"])

        assert code == 2
        assert "rejected the token" in caplog.text
