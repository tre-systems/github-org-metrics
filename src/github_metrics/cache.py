"""On-disk cache for raw GitHub payloads.

Fetching an organization costs thousands of API calls, so a run's raw data is
kept verbatim on disk. The cache is versioned: a file written by an
incompatible version is reported and ignored rather than half-understood.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from .models import RawData

logger = logging.getLogger(__name__)

__all__ = ["CACHE_SCHEMA", "CACHE_SUFFIX", "cache_path", "load", "save"]

CACHE_SCHEMA = 2
CACHE_SUFFIX = "_github_data_cache.json"


def cache_path(org: str, directory: Path) -> Path:
    """Return the cache file path for an organization."""
    return directory / f"{org}{CACHE_SUFFIX}"


def save(data: RawData, path: Path) -> Path:
    """Write raw data to the cache, replacing any existing file atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    envelope = {
        "schema": CACHE_SCHEMA,
        "fetched_at": datetime.now(UTC).isoformat(),
        "data": data,
    }

    temporary = path.with_suffix(f"{path.suffix}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(envelope, handle)
    temporary.replace(path)

    logger.info("Cached raw data to %s (%.1f MB)", path, path.stat().st_size / 1e6)
    return path


def load(path: Path) -> RawData | None:
    """Read raw data from the cache.

    Returns:
        The cached payloads, or None if the cache is absent, unreadable, or
        written by an incompatible version.
    """
    if not path.exists():
        return None

    try:
        with path.open(encoding="utf-8") as handle:
            envelope = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Ignoring unreadable cache %s: %s", path, exc)
        return None

    if not isinstance(envelope, dict) or envelope.get("schema") != CACHE_SCHEMA:
        logger.warning(
            "Cache %s was written by an incompatible version; re-run with "
            "--update-cache to rebuild it.",
            path,
        )
        return None

    data = envelope.get("data")
    if not isinstance(data, dict):
        logger.warning("Cache %s has no data section; ignoring.", path)
        return None

    fetched_at = envelope.get("fetched_at")
    if isinstance(fetched_at, str):
        logger.info("Using cached data fetched at %s", fetched_at)

    return cast(RawData, cast(dict[str, Any], data))
