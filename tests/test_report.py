"""Tests for console rendering and CSV export."""

from __future__ import annotations

import csv
from dataclasses import replace

from rich.console import Console

from github_metrics.analyze import analyze
from github_metrics.report import (
    DEVELOPER_HEADERS,
    anonymize_name,
    export_csv,
    render,
)
from tests.conftest import UNTIL, commit, iso, pull, raw_data, repo, run


def build_report(options, **overrides):
    payload = {
        "repos": [repo("api")],
        "commits": {
            "api": [
                commit("a1", "alice", iso(2025, 2, 1)),
                commit("b1", "bulk", iso(2025, 2, 1)),
            ]
        },
        "commit_stats": {
            "api": {
                "a1": {"additions": 120, "deletions": 30},
                "b1": {"additions": 500_000, "deletions": 0},
            }
        },
        "pull_requests": {
            "api": [pull(1, "alice", iso(2025, 2, 1), merged=iso(2025, 2, 2))]
        },
        "workflow_runs": {"api": [run("Deploy", iso(2025, 2, 5), "success")]},
    }
    payload.update(overrides)
    return analyze(raw_data(**payload), options)


def read_csv(path):
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.reader(handle))


class TestCsvExport:
    def test_writes_developer_and_repository_files(self, options, tmp_path):
        paths = export_csv(build_report(options), tmp_path)

        assert [path.name for path in paths] == [
            "acme_github_developer_metrics.csv",
            "acme_github_repository_metrics.csv",
        ]

    def test_developer_rows_carry_the_documented_columns(self, options, tmp_path):
        export_csv(build_report(options), tmp_path)

        rows = read_csv(tmp_path / "acme_github_developer_metrics.csv")

        assert rows[0] == list(DEVELOPER_HEADERS)
        assert rows[1] == ["alice", "1", "120", "30", "1", "0", "0", "api"]

    def test_never_writes_an_outliers_file(self, options, tmp_path):
        paths = export_csv(build_report(options), tmp_path)

        assert not any("outliers" in path.name for path in paths)

    def test_fast_mode_marks_review_columns_unavailable(self, options, tmp_path):
        report = build_report(options, fetch_pr_details=False)

        export_csv(report, tmp_path)
        rows = read_csv(tmp_path / "acme_github_developer_metrics.csv")

        assert rows[1][5:7] == ["N/A", "N/A"]

    def test_anonymised_export_contains_no_real_logins(self, options, tmp_path):
        export_csv(build_report(options), tmp_path, anonymize=True)

        text = (tmp_path / "acme_github_developer_metrics.csv").read_text()

        assert "alice" not in text
        assert anonymize_name("alice") in text

    def test_creates_the_output_directory(self, options, tmp_path):
        target = tmp_path / "reports"

        export_csv(build_report(options), target)

        assert target.is_dir()


class TestAnonymisation:
    def test_is_stable_for_the_same_login(self):
        assert anonymize_name("alice") == anonymize_name("alice")

    def test_differs_between_logins(self):
        assert anonymize_name("alice") != anonymize_name("bob")

    def test_reveals_nothing_of_the_original(self):
        assert "alice" not in anonymize_name("alice")


class TestConsoleRendering:
    def render_to_text(self, report, **kwargs):
        console = Console(record=True, width=120, force_terminal=False)
        render(report, console, **kwargs)
        return console.export_text()

    def test_shows_developers_repositories_and_dora(self, options):
        text = self.render_to_text(build_report(options))

        assert "Developer Activity" in text
        assert "Repository Details" in text
        assert "DORA Metrics" in text
        assert "alice" in text

    def test_says_what_it_excluded(self, options):
        text = self.render_to_text(build_report(options))

        assert "1 bulk commit" in text
        assert "500,000" in text

    def test_says_nothing_when_no_commits_were_excluded(self, options):
        report = build_report(
            options,
            commit_stats={"api": {"a1": {"additions": 120, "deletions": 30}}},
            commits={"api": [commit("a1", "alice", iso(2025, 2, 1))]},
        )

        assert "bulk" not in self.render_to_text(report)

    def test_anonymises_on_request(self, options):
        text = self.render_to_text(build_report(options), anonymize=True)

        assert "alice" not in text
        assert anonymize_name("alice") in text

    def test_reports_an_empty_window_without_failing(self, options):
        report = analyze(raw_data(), options)

        text = self.render_to_text(report)

        assert "No developer activity" in text
        assert "No repository activity" in text

    def test_shows_no_data_rather_than_a_flattering_zero(self, options):
        report = analyze(raw_data(repos=[repo("api")]), options)

        text = self.render_to_text(report)

        assert "no data" in text
        assert "Elite" not in text

    def test_window_is_shown_in_the_header(self, options):
        text = self.render_to_text(build_report(options))

        assert UNTIL.strftime("%d %b %Y") in text


class TestDeploymentWorkflowVisibility:
    def test_console_names_the_workflow_that_was_counted(self, options):
        console = Console(record=True, width=140, force_terminal=False)
        render(build_report(options), console)

        assert "deploy" in console.export_text().lower()

    def test_console_says_so_when_no_workflow_was_found(self, options):
        report = analyze(
            raw_data(
                repos=[repo("api")],
                commits={"api": [commit("a1", "alice", iso(2025, 2, 1))]},
                commit_stats={"api": {"a1": {"additions": 5}}},
            ),
            options,
        )
        console = Console(record=True, width=140, force_terminal=False)
        render(report, console)

        assert "none found" in console.export_text()

    def test_csv_carries_the_workflow(self, options, tmp_path):
        export_csv(build_report(options), tmp_path)

        rows = read_csv(tmp_path / "acme_github_repository_metrics.csv")

        assert rows[0][8] == "Deploy Workflow"
        assert rows[1][8] == "deploy"


class TestComparison:
    def render_to_text(self, report):
        console = Console(record=True, width=140, force_terminal=False)
        render(report, console)
        return console.export_text()

    def compared(self, options, previous_overrides=None):
        current = build_report(options)
        previous = build_report(options, **(previous_overrides or {}))
        return replace(current, previous=previous)

    def test_nothing_is_compared_without_a_previous_window(self, options):
        text = self.render_to_text(build_report(options))

        assert "Compared with" not in text
        assert "↑" not in text
        assert "↓" not in text

    def test_shows_how_each_metric_moved(self, options):
        report = self.compared(
            options,
            {
                "pull_requests": {
                    "api": [pull(1, "alice", iso(2025, 2, 1), merged=iso(2025, 2, 3))]
                }
            },
        )

        text = self.render_to_text(report)

        # Lead time fell from 48 h in the previous window to 24 h in this one.
        assert "↓ 24.0 h" in text

    def test_identical_windows_report_no_change(self, options):
        text = self.render_to_text(self.compared(options))

        assert "no change" in text

    def test_summarises_activity_against_the_previous_window(self, options):
        report = self.compared(options)

        text = self.render_to_text(report)

        assert "Compared with" in text
        assert "2 commits" in text

    def test_counts_are_singular_when_there_is_one(self, options):
        report = self.compared(options)

        assert "1 active developer " in self.render_to_text(report)

    def test_missing_data_on_either_side_shows_no_delta(self, options):
        current = build_report(options)
        empty = analyze(raw_data(), options)

        text = self.render_to_text(replace(current, previous=empty))

        assert "Compared with" in text
        assert "↑" not in text
        assert "↓" not in text

    def test_a_worse_metric_is_still_reported(self, options):
        """Regressions must show up as clearly as improvements."""
        worse = build_report(
            options,
            pull_requests={
                "api": [pull(1, "alice", iso(2025, 2, 1), merged=iso(2025, 2, 1, 18))]
            },
        )
        report = replace(build_report(options), previous=worse)

        # Lead time rose from 6 h to 24 h.
        assert "↑ 18.0 h" in self.render_to_text(report)
