"""Tests for the optional configuration file."""

from __future__ import annotations

import pytest

from github_metrics import config


def write(tmp_path, body: str):
    path = tmp_path / config.DEFAULT_CONFIG_FILENAME
    path.write_text(body, encoding="utf-8")
    return path


class TestDiscovery:
    def test_no_file_is_not_an_error(self, tmp_path):
        assert config.load(None, search_from=tmp_path) == config.Config()

    def test_finds_the_default_filename(self, tmp_path):
        write(tmp_path, "bulk_commit_lines = 500")

        assert config.load(None, search_from=tmp_path).bulk_commit_lines == 500

    def test_an_explicit_path_is_read(self, tmp_path):
        path = tmp_path / "custom.toml"
        path.write_text("bulk_commit_lines = 42")

        assert config.load(path, search_from=tmp_path).bulk_commit_lines == 42

    def test_a_missing_explicit_path_is_an_error(self, tmp_path):
        with pytest.raises(config.ConfigError, match="No configuration file"):
            config.load(tmp_path / "absent.toml", search_from=tmp_path)


class TestParsing:
    def test_reads_every_setting(self, tmp_path):
        write(
            tmp_path,
            """
            bulk_commit_lines = 5000
            exclude_users = ["cursoragent", "renovate"]
            strict_deployments = true

            [deploy_workflows]
            default = "Deploy"
            api = "Release"
            web = "Publish"
            """,
        )

        settings = config.load(None, search_from=tmp_path)

        assert settings.bulk_commit_lines == 5000
        assert settings.exclude_users == ("cursoragent", "renovate")
        assert settings.strict_deployments is True
        assert settings.deploy_workflow == "Deploy"
        assert settings.deploy_workflow_by_repo == {"api": "Release", "web": "Publish"}

    def test_per_repo_workflows_without_a_default(self, tmp_path):
        write(tmp_path, '[deploy_workflows]\napi = "Release"')

        settings = config.load(None, search_from=tmp_path)

        assert settings.deploy_workflow is None
        assert settings.deploy_workflow_by_repo == {"api": "Release"}

    def test_unknown_settings_are_reported_but_ignored(self, tmp_path, caplog):
        write(tmp_path, "nonsense = 1\nbulk_commit_lines = 7")

        settings = config.load(None, search_from=tmp_path)

        assert settings.bulk_commit_lines == 7
        assert "nonsense" in caplog.text


class TestValidation:
    def test_malformed_toml_is_reported(self, tmp_path):
        write(tmp_path, "this is not = = toml")

        with pytest.raises(config.ConfigError, match="Could not read"):
            config.load(None, search_from=tmp_path)

    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            ('bulk_commit_lines = "lots"', "a number"),
            ("bulk_commit_lines = true", "a number"),
            ('exclude_users = "solo"', "a list of strings"),
            ("exclude_users = [1, 2]", "a list of strings"),
            ('strict_deployments = "yes"', "true or false"),
            ("[deploy_workflows]\napi = 3", "a table of strings"),
        ],
    )
    def test_wrong_types_are_rejected(self, tmp_path, body, expected):
        write(tmp_path, body)

        with pytest.raises(config.ConfigError, match=expected):
            config.load(None, search_from=tmp_path)
