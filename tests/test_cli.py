"""Tests for CLI argument parsing and cache helpers."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pandas as pd
import pytest

from github_metrics.cli import (
    build_parser,
    load_cache,
    print_dataframe,
    save_cache,
)


def test_parser_defaults_match_expectations():
    args = build_parser().parse_args(["my-org"])
    assert args.org == "my-org"
    assert args.months == 3
    assert args.repos is None
    assert args.target_repos is None
    assert args.fast is False
    assert args.anonymize is False
    assert args.max_prs == 50
    assert args.workers == 10


def test_parser_accepts_target_repos_as_list():
    args = build_parser().parse_args(["org", "--target-repos", "a", "b", "c"])
    assert args.target_repos == ["a", "b", "c"]


def test_parser_accepts_output_dir(tmp_path: Path):
    args = build_parser().parse_args(["org", "--output-dir", str(tmp_path)])
    assert args.output_dir == tmp_path


def test_parser_requires_org():
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


def test_cache_roundtrip(tmp_path: Path):
    payload = {"repos": [{"name": "r"}], "commits": {"r": []}}
    save_cache(payload, "my-org", tmp_path)
    loaded = load_cache("my-org", tmp_path)
    assert loaded == payload


def test_load_cache_returns_none_when_missing(tmp_path: Path):
    assert load_cache("ghost", tmp_path) is None


def test_cache_file_uses_expected_name(tmp_path: Path):
    save_cache({"repos": []}, "my-org", tmp_path)
    expected = tmp_path / "my-org_github_data_cache.json"
    assert expected.exists()
    assert json.loads(expected.read_text())["repos"] == []


def test_print_dataframe_handles_empty(capsys):
    print_dataframe(pd.DataFrame())
    assert "no rows" in capsys.readouterr().out


def test_print_dataframe_anonymizes_developer_column(capsys):
    df = pd.DataFrame({"Developer": ["alice"], "Commits": [5]})
    print_dataframe(df, anonymize=True)
    output = capsys.readouterr().out
    assert "alice" not in output
    assert "user-" in output


def test_load_cache_warns_on_schema_mismatch(tmp_path: Path, caplog):
    (tmp_path / "my-org_github_data_cache.json").write_text('{"_schema": 1, "repos": []}')

    with caplog.at_level(logging.WARNING, logger="github_metrics.cli"):
        data = load_cache("my-org", tmp_path)
    assert data is not None
    assert any("schema" in record.message for record in caplog.records)


def test_parser_accepts_workers():
    args = build_parser().parse_args(["org", "--workers", "4"])
    assert args.workers == 4
