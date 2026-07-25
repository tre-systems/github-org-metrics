"""Tests for the on-disk cache."""

from __future__ import annotations

import json

from github_metrics import cache
from tests.conftest import raw_data, repo


def test_round_trips_raw_data(tmp_path):
    path = cache.cache_path("acme", tmp_path)
    data = raw_data(repos=[repo("api")])

    cache.save(data, path)

    assert path.name == "acme_github_data_cache.json"
    assert cache.load(path) == data


def test_missing_cache_returns_none(tmp_path):
    assert cache.load(tmp_path / "absent.json") is None


def test_rejects_an_incompatible_schema(tmp_path, caplog):
    path = tmp_path / "cache.json"
    path.write_text(json.dumps({"schema": 1, "data": {"repos": []}}))

    assert cache.load(path) is None
    assert "incompatible version" in caplog.text


def test_rejects_a_legacy_bare_payload(tmp_path):
    path = tmp_path / "cache.json"
    path.write_text(json.dumps({"repos": [], "commits": {}}))

    assert cache.load(path) is None


def test_ignores_a_corrupt_file(tmp_path, caplog):
    path = tmp_path / "cache.json"
    path.write_text("{ truncated")

    assert cache.load(path) is None
    assert "unreadable" in caplog.text


def test_leaves_no_temporary_file_behind(tmp_path):
    path = cache.cache_path("acme", tmp_path)

    cache.save(raw_data(), path)

    assert [p.name for p in tmp_path.iterdir()] == [path.name]


def test_creates_the_output_directory(tmp_path):
    path = cache.cache_path("acme", tmp_path / "nested" / "dir")

    cache.save(raw_data(), path)

    assert path.exists()
