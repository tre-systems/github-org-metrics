"""A small, resilient client for the GitHub REST API.

The client handles the three things that make long metric runs fail in
practice: primary rate limits (wait for the reset), secondary rate limits
(honour ``Retry-After``), and transient server or network errors (bounded
exponential backoff). Pagination follows ``Link`` headers and supports early
termination, so a query sorted newest-first can stop as soon as it walks past
the window being analysed.

The client is safe to share across threads: each thread lazily builds its own
``requests.Session``.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

import requests

from . import __version__

logger = logging.getLogger(__name__)

__all__ = [
    "GITHUB_API_URL",
    "AuthenticationError",
    "GitHubClient",
    "GitHubError",
]

GITHUB_API_URL = "https://api.github.com"

DEFAULT_TIMEOUT = 30
DEFAULT_MAX_RETRIES = 3
DEFAULT_PER_PAGE = 100

#: Longest a single primary rate-limit wait may last before giving up (1 hour
#: is the full GitHub reset window, so anything longer indicates a bad clock).
MAX_RATE_LIMIT_SLEEP = 3600

#: Rate-limit waits get their own budget so a misbehaving endpoint cannot spin.
MAX_RATE_LIMIT_WAITS = 10

#: Fallback wait when GitHub reports a secondary rate limit without a hint.
SECONDARY_RATE_LIMIT_SLEEP = 60

HTTP_UNAUTHORIZED = 401
HTTP_FORBIDDEN = 403
HTTP_NOT_FOUND = 404
HTTP_TOO_MANY_REQUESTS = 429
HTTP_SERVER_ERROR = 500


class GitHubError(RuntimeError):
    """Raised for GitHub API failures that should abort the run."""


class AuthenticationError(GitHubError):
    """Raised when the token is missing, expired, or lacks the required scopes."""


class GitHubClient:
    """A read-only GitHub REST API client with retry and rate-limit handling."""

    def __init__(
        self,
        token: str,
        *,
        base_url: str = GITHUB_API_URL,
        timeout: int = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        session_factory: Callable[[], requests.Session] = requests.Session,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Initialise the client.

        Args:
            token: A GitHub personal access token.
            base_url: API root, overridable for GitHub Enterprise or tests.
            timeout: Per-request timeout in seconds.
            max_retries: Retries for transient (network/5xx) failures.
            session_factory: Builds the per-thread session; injectable for tests.
            sleep: Sleep function, injectable for tests.
            clock: Wall-clock source used to size rate-limit waits.
        """
        if not token:
            message = "A GitHub token is required"
            raise AuthenticationError(message)

        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self._session_factory = session_factory
        self._sleep = sleep
        self._clock = clock
        self._local = threading.local()
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": f"github-org-metrics/{__version__}",
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(
        self, path: str, params: dict[str, str] | None = None
    ) -> dict[str, Any] | list[Any] | None:
        """Fetch a single endpoint.

        Args:
            path: API path (``/repos/o/r``) or absolute URL.
            params: Optional query parameters.

        Returns:
            The decoded JSON body, or None if the resource is unavailable.
        """
        response = self._request(self._url(path), params)
        if response is None:
            return None
        return self._decode(response)

    def paginate(
        self,
        path: str,
        params: dict[str, str] | None = None,
        *,
        max_items: int | None = None,
        stop_after: Callable[[dict[str, Any]], bool] | None = None,
    ) -> list[dict[str, Any]]:
        """Collect every item from a paginated list endpoint.

        Args:
            path: API path or absolute URL.
            params: Optional query parameters (``per_page`` is set for you).
            max_items: Stop once this many items have been collected.
            stop_after: Predicate evaluated per item; when it returns True that
                item is kept and pagination stops. Use with a sorted query to
                avoid walking history you are going to discard anyway.

        Returns:
            The collected items.
        """
        return self._paginate(path, params, max_items=max_items, stop_after=stop_after)

    def paginate_envelope(
        self,
        path: str,
        key: str,
        params: dict[str, str] | None = None,
        *,
        max_items: int | None = None,
    ) -> list[dict[str, Any]]:
        """Paginate an endpoint that wraps its items in an object.

        GitHub's Actions endpoints return ``{"total_count": n, "<key>": [...]}``
        rather than a bare array.

        Args:
            path: API path or absolute URL.
            key: The envelope field holding the items.
            params: Optional query parameters.
            max_items: Stop once this many items have been collected.

        Returns:
            The collected items.
        """
        return self._paginate(path, params, max_items=max_items, envelope_key=key)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @property
    def _session(self) -> requests.Session:
        session: requests.Session | None = getattr(self._local, "session", None)
        if session is None:
            session = self._session_factory()
            session.headers.update(self._headers)
            self._local.session = session
        return session

    def _url(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        return f"{self.base_url}/{path.lstrip('/')}"

    @staticmethod
    def _decode(response: requests.Response) -> dict[str, Any] | list[Any] | None:
        try:
            decoded: dict[str, Any] | list[Any] = response.json()
        except ValueError:
            logger.warning("Malformed JSON from %s", response.url)
            return None
        return decoded

    def _paginate(
        self,
        path: str,
        params: dict[str, str] | None = None,
        *,
        max_items: int | None = None,
        stop_after: Callable[[dict[str, Any]], bool] | None = None,
        envelope_key: str | None = None,
    ) -> list[dict[str, Any]]:
        url: str | None = self._url(path)
        query: dict[str, str] | None = {
            **(params or {}),
            "per_page": str(DEFAULT_PER_PAGE),
        }
        items: list[dict[str, Any]] = []

        while url:
            response = self._request(url, query)
            # Subsequent URLs come from the Link header and already carry the
            # full query string; re-sending params would duplicate them.
            query = None
            if response is None:
                break

            payload = self._decode(response)
            page = self._extract_page(payload, envelope_key, url)
            if page is None:
                break

            for item in page:
                items.append(item)
                if max_items is not None and len(items) >= max_items:
                    return items
                if stop_after is not None and stop_after(item):
                    return items

            url = response.links.get("next", {}).get("url")

        return items

    @staticmethod
    def _extract_page(
        payload: dict[str, Any] | list[Any] | None,
        envelope_key: str | None,
        url: str,
    ) -> list[dict[str, Any]] | None:
        if payload is None:
            return None

        if envelope_key is None:
            if not isinstance(payload, list):
                logger.warning("Expected a list from %s, got %s", url, type(payload))
                return None
            return payload

        if not isinstance(payload, dict):
            logger.warning("Expected an object from %s, got %s", url, type(payload))
            return None

        page = payload.get(envelope_key)
        if not isinstance(page, list):
            return None
        return page

    def _request(
        self, url: str, params: dict[str, str] | None = None
    ) -> requests.Response | None:
        """Perform a GET, retrying transient failures and rate limits.

        Returns:
            A successful response, or None if the resource is permanently
            unavailable (missing, forbidden, or out of retries).

        Raises:
            AuthenticationError: If the token is rejected.
        """
        attempts = 0
        rate_limit_waits = 0

        while True:
            try:
                response = self._session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                attempts += 1
                if attempts > self.max_retries:
                    logger.error("Request failed for %s: %s", url, exc)
                    return None
                self._backoff(attempts, reason=str(exc), url=url)
                continue

            if response.ok:
                return response

            wait = self._rate_limit_wait(response)
            if wait is not None:
                rate_limit_waits += 1
                if rate_limit_waits > MAX_RATE_LIMIT_WAITS or wait > MAX_RATE_LIMIT_SLEEP:
                    logger.error("Giving up on %s after repeated rate limiting", url)
                    return None
                logger.warning("Rate limited; waiting %.0fs before retrying", wait)
                self._sleep(wait)
                continue

            if response.status_code >= HTTP_SERVER_ERROR:
                attempts += 1
                if attempts > self.max_retries:
                    logger.error(
                        "Server error %d for %s; giving up", response.status_code, url
                    )
                    return None
                self._backoff(attempts, reason=f"HTTP {response.status_code}", url=url)
                continue

            self._report_client_error(response, url)
            return None

    @staticmethod
    def _report_client_error(response: requests.Response, url: str) -> None:
        """Log a non-retryable 4xx, or raise when the token itself is the problem.

        Raises:
            AuthenticationError: If GitHub rejected the credentials.
        """
        status = response.status_code

        if status == HTTP_UNAUTHORIZED:
            message = (
                "GitHub rejected the token (401). Check that GITHUB_TOKEN is set to "
                "a valid, unexpired token."
            )
            raise AuthenticationError(message)

        if status == HTTP_NOT_FOUND:
            logger.debug("Not found: %s", url)
            return

        if status == HTTP_FORBIDDEN:
            logger.warning(
                "Permission denied for %s. The token may lack the required scope.", url
            )
            return

        logger.error("Unexpected status %d for %s", status, url)
        logger.debug("Response body: %s", response.text[:500])

    def _backoff(self, attempt: int, *, reason: str, url: str) -> None:
        delay = float(2 ** (attempt - 1))
        logger.warning(
            "Retrying %s in %.0fs (attempt %d/%d): %s",
            url,
            delay,
            attempt,
            self.max_retries,
            reason,
        )
        self._sleep(delay)

    def _rate_limit_wait(self, response: requests.Response) -> float | None:
        """Return how long to wait if this response is a rate limit, else None."""
        if response.status_code not in (HTTP_FORBIDDEN, HTTP_TOO_MANY_REQUESTS):
            return None

        headers = response.headers

        retry_after = _first_int(headers.get("Retry-After"))
        if retry_after is not None:
            return float(max(1, retry_after))

        remaining = _first_int(headers.get("X-RateLimit-Remaining"))
        reset = _first_int(headers.get("X-RateLimit-Reset"))
        if remaining == 0 and reset is not None:
            return float(max(1, reset - self._clock() + 1))

        if "secondary rate limit" in _body_text(response).lower():
            return float(SECONDARY_RATE_LIMIT_SLEEP)

        return None


def _first_int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _body_text(response: requests.Response) -> str:
    try:
        return response.text or ""
    except (ValueError, UnicodeDecodeError):  # pragma: no cover - defensive
        return ""
