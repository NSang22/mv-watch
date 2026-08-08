"""HTTP helpers with retry, backoff, and polite delays."""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

import requests
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)


class TransientHTTPError(Exception):
    """Raised for 429 and 5xx responses that deserve a retry."""

    def __init__(self, status_code: int, url: str, body: str = "") -> None:
        self.status_code = status_code
        self.url = url
        self.body = body
        super().__init__(f"HTTP {status_code} for {url}")


class HTTPStatusError(Exception):
    """Raised for non-retryable HTTP error status codes."""

    def __init__(self, status_code: int, url: str, body: str = "") -> None:
        self.status_code = status_code
        self.url = url
        self.body = body
        super().__init__(f"HTTP {status_code} for {url}")


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, TransientHTTPError):
        return True
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return True
    return False


class HttpClient:
    """Thin requests wrapper that retries transient failures."""

    def __init__(
        self,
        delay_seconds: float = 0.35,
        user_agent: str = "watchlist-watcher/1.0 (+https://github.com/local/watchlist-watcher)",
        session: Optional[requests.Session] = None,
    ) -> None:
        self.delay_seconds = delay_seconds
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", user_agent)
        self._last_request_at = 0.0

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.delay_seconds:
            time.sleep(self.delay_seconds - elapsed)

    @retry(
        retry=retry_if_exception(_is_retryable),
        wait=wait_exponential(multiplier=1, min=1, max=60),
        stop=stop_after_attempt(6),
        reraise=True,
    )
    def request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[dict[str, Any]] = None,
        headers: Optional[dict[str, str]] = None,
        timeout: float = 30.0,
    ) -> requests.Response:
        """Send an HTTP request with exponential backoff on 429/5xx."""
        self._throttle()
        response = self.session.request(
            method,
            url,
            params=params,
            headers=headers,
            timeout=timeout,
        )
        self._last_request_at = time.monotonic()

        if response.status_code == 429 or response.status_code >= 500:
            raise TransientHTTPError(response.status_code, url, response.text[:200])
        return response

    def get_json(
        self,
        url: str,
        *,
        params: Optional[dict[str, Any]] = None,
        headers: Optional[dict[str, str]] = None,
    ) -> Any:
        """GET a URL and parse JSON, raising for non-retryable errors."""
        response = self.request("GET", url, params=params, headers=headers)
        if response.status_code >= 400:
            raise HTTPStatusError(response.status_code, url, response.text[:200])
        return response.json()

    def get_text(
        self,
        url: str,
        *,
        params: Optional[dict[str, Any]] = None,
        headers: Optional[dict[str, str]] = None,
    ) -> str:
        """GET a URL and return response text."""
        response = self.request("GET", url, params=params, headers=headers)
        if response.status_code >= 400:
            raise HTTPStatusError(response.status_code, url, response.text[:200])
        return response.text
