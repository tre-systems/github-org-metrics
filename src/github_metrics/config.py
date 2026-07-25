"""Optional project configuration file.

Some settings are properties of an organization rather than of a single run:
which workflow counts as a deployment in each repository, which accounts are
automation, how large a commit has to be before it is generated output. Those
belong in a file next to the project, not retyped on every invocation.

Command-line flags always win over the file.
"""

from __future__ import annotations

import logging
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["DEFAULT_CONFIG_FILENAME", "Config", "ConfigError", "load"]

DEFAULT_CONFIG_FILENAME = ".github-metrics.toml"

#: Keys understood at the top level of the file.
_KNOWN_KEYS = frozenset(
    {"bulk_commit_lines", "exclude_users", "strict_deployments", "deploy_workflows"}
)

#: Inside [deploy_workflows], this key sets the fallback for every repository.
_DEFAULT_WORKFLOW_KEY = "default"


class ConfigError(RuntimeError):
    """Raised when a configuration file exists but cannot be used."""


@dataclass(frozen=True)
class Config:
    """Settings read from a configuration file.

    Every field is None or empty when unset, so the caller can tell "not
    configured" from "configured to the same value as the default".
    """

    bulk_commit_lines: int | None = None
    exclude_users: tuple[str, ...] = ()
    strict_deployments: bool | None = None
    deploy_workflow: str | None = None
    deploy_workflow_by_repo: Mapping[str, str] = field(default_factory=dict)


def load(path: Path | None, *, search_from: Path) -> Config:
    """Read a configuration file, if there is one.

    Args:
        path: An explicitly requested file. Missing files are an error, since
            the user asked for that file by name.
        search_from: Directory to look in when no path was given. A missing
            file there is not an error.

    Returns:
        The parsed settings, empty when no file was found.

    Raises:
        ConfigError: If the file is unreadable, malformed, or has a value of
            the wrong type.
    """
    target = path or (search_from / DEFAULT_CONFIG_FILENAME)

    if not target.exists():
        if path is not None:
            message = f"No configuration file at {target}"
            raise ConfigError(message)
        return Config()

    try:
        with target.open("rb") as handle:
            document = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        message = f"Could not read {target}: {exc}"
        raise ConfigError(message) from exc

    logger.info("Using configuration from %s", target)
    _warn_about_unknown_keys(document, target)

    workflows = _table(document.get("deploy_workflows"), "deploy_workflows", target)
    default_workflow = workflows.pop(_DEFAULT_WORKFLOW_KEY, None)

    return Config(
        bulk_commit_lines=_integer(
            document.get("bulk_commit_lines"), "bulk_commit_lines", target
        ),
        exclude_users=_strings(document.get("exclude_users"), "exclude_users", target),
        strict_deployments=_boolean(
            document.get("strict_deployments"), "strict_deployments", target
        ),
        deploy_workflow=default_workflow,
        deploy_workflow_by_repo=workflows,
    )


def _warn_about_unknown_keys(document: dict[str, Any], path: Path) -> None:
    unknown = sorted(set(document) - _KNOWN_KEYS)
    if unknown:
        logger.warning("Ignoring unknown settings in %s: %s", path, ", ".join(unknown))


def _wrong_type(key: str, path: Path, expected: str) -> ConfigError:
    return ConfigError(f"{key} in {path} must be {expected}")


def _integer(value: Any, key: str, path: Path) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise _wrong_type(key, path, "a number")
    return value


def _boolean(value: Any, key: str, path: Path) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise _wrong_type(key, path, "true or false")
    return value


def _strings(value: Any, key: str, path: Path) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise _wrong_type(key, path, "a list of strings")
    return tuple(value)


def _table(value: Any, key: str, path: Path) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(
        isinstance(item, str) for item in value.values()
    ):
        raise _wrong_type(key, path, "a table of strings")
    return dict(value)
