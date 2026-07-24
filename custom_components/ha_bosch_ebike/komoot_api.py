"""Small asynchronous client for Komoot's undocumented web API.

Komoot does not publish a supported public API.  Keep this module deliberately
small so endpoint changes remain isolated from the rest of the integration.
The caller owns the injected Home Assistant aiohttp session.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote, urlencode, urljoin, urlparse

import aiohttp

API_ORIGIN = "https://api.komoot.de"
LOGIN_PATH = "/v006/account/email/{email}/"
TOURS_PATH = "/v007/users/{user_id}/tours/"
TOUR_PATH = "/v007/tours/{tour_id}"

DEFAULT_MAX_PAGES = 100
DEFAULT_MAX_GPX_BYTES = 5 * 1024 * 1024
DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=30)


class KomootApiError(Exception):
    """Base class for expected Komoot client failures."""


class KomootAuthenticationError(KomootApiError):
    """Raised when Komoot rejects the configured credentials."""


class KomootConnectionError(KomootApiError):
    """Raised for timeouts and transport failures."""


class KomootSchemaError(KomootApiError):
    """Raised when the undocumented API returns an unexpected response."""


class KomootPaginationError(KomootSchemaError):
    """Raised for unsafe or non-terminating pagination responses."""


class KomootHttpError(KomootApiError):
    """Raised for an HTTP failure that is not an authentication failure."""

    def __init__(self, status: int, endpoint: str) -> None:
        self.status = status
        self.endpoint = endpoint
        super().__init__(f"Komoot {endpoint} request failed with HTTP {status}")


class KomootRateLimitError(KomootHttpError):
    """Raised for HTTP 429, optionally with a parsed Retry-After value."""

    def __init__(self, endpoint: str, retry_after: float | None) -> None:
        self.retry_after = retry_after
        super().__init__(429, endpoint)


@dataclass(frozen=True, slots=True)
class KomootIdentity:
    """Non-secret account information returned after login."""

    user_id: str
    display_name: str | None


def _parse_retry_after(value: str | None) -> float | None:
    """Parse Retry-After seconds or HTTP date without raising."""
    if not value:
        return None
    try:
        return max(0.0, float(value.strip()))
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())


def _safe_page_url(href: str) -> str:
    """Resolve and validate a pagination URL before sending credentials."""
    url = urljoin(f"{API_ORIGIN}/", href)
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "api.komoot.de"
        or parsed.port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise KomootPaginationError("Komoot returned an unsafe pagination URL")
    return url


class KomootApiClient:
    """Async Komoot API client using an injected aiohttp web session."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        email: str,
        password: str,
        *,
        timeout: aiohttp.ClientTimeout = DEFAULT_TIMEOUT,
        max_gpx_bytes: int = DEFAULT_MAX_GPX_BYTES,
    ) -> None:
        self._session = session
        self._email = email
        self._password = password
        self._timeout = timeout
        self._max_gpx_bytes = max_gpx_bytes
        self._identity: KomootIdentity | None = None
        self._token: str | None = None

    @property
    def identity(self) -> KomootIdentity | None:
        """Return non-secret identity data after a successful login."""
        return self._identity

    @property
    def user_id(self) -> str | None:
        """Return the authenticated Komoot user id, if available."""
        return self._identity.user_id if self._identity else None

    async def async_login(self) -> KomootIdentity:
        """Authenticate using v006 and retain the short-lived token in memory."""
        login_url = f"{API_ORIGIN}{LOGIN_PATH.format(email=quote(self._email, safe=''))}"
        data = await self._async_request_json(
            login_url,
            endpoint="login",
            auth=aiohttp.BasicAuth(
                self._email, self._password, encoding="utf-8"
            ),
            auth_statuses=(401, 403),
        )

        user_id = data.get("username")
        token = data.get("password")
        user = data.get("user")
        if (
            not isinstance(user_id, (str, int))
            or not str(user_id)
            or not isinstance(token, str)
            or not token
            or (user is not None and not isinstance(user, dict))
        ):
            raise KomootSchemaError("Komoot login response has an unexpected schema")

        display_name = user.get("displayname") if isinstance(user, dict) else None
        if display_name is not None and not isinstance(display_name, str):
            display_name = None

        self._identity = KomootIdentity(str(user_id), display_name)
        self._token = token
        return self._identity

    async def async_list_tours(
        self,
        *,
        tour_type: str | None = "tour_recorded",
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> list[dict[str, Any]]:
        """Return the authenticated user's tours, following HAL pagination."""
        if max_pages < 1:
            raise ValueError("max_pages must be at least 1")
        await self._async_ensure_login()
        assert self._identity is not None

        next_url: str | None = (
            f"{API_ORIGIN}"
            f"{TOURS_PATH.format(user_id=quote(self._identity.user_id, safe=''))}"
        )
        seen_urls: set[str] = set()
        seen_ids: set[str] = set()
        tours: list[dict[str, Any]] = []
        page_count = 0

        while next_url is not None:
            next_url = _safe_page_url(next_url)
            if next_url in seen_urls:
                raise KomootPaginationError("Komoot pagination contains a loop")
            if page_count >= max_pages:
                raise KomootPaginationError("Komoot pagination exceeded the page limit")
            seen_urls.add(next_url)
            page_count += 1

            page = await self._async_authenticated_json(
                next_url, endpoint="tour list"
            )
            embedded = page.get("_embedded")
            if not isinstance(embedded, dict) or not isinstance(
                embedded.get("tours"), list
            ):
                raise KomootSchemaError(
                    "Komoot tour-list response has an unexpected schema"
                )

            for tour in embedded["tours"]:
                if not isinstance(tour, dict) or "id" not in tour:
                    raise KomootSchemaError(
                        "Komoot tour-list entry has an unexpected schema"
                    )
                if tour_type is not None and tour.get("type") != tour_type:
                    continue
                tour_id = str(tour["id"])
                if tour_id in seen_ids:
                    continue
                seen_ids.add(tour_id)
                tours.append(tour)

            links = page.get("_links", {})
            if not isinstance(links, dict):
                raise KomootSchemaError(
                    "Komoot tour-list links have an unexpected schema"
                )
            next_link = links.get("next")
            if next_link is None:
                next_url = None
            elif isinstance(next_link, dict) and isinstance(
                next_link.get("href"), str
            ):
                next_url = next_link["href"]
            else:
                raise KomootSchemaError(
                    "Komoot tour-list pagination has an unexpected schema"
                )

        return tours

    async def async_get_tour_detail(
        self, tour_id: str | int, *, language: str = "de"
    ) -> dict[str, Any]:
        """Return full v007 tour metadata with embedded coordinate data."""
        quoted_id = quote(str(tour_id), safe="")
        query = urlencode(
            {
                "_embedded": (
                    "coordinates,way_types,surfaces,directions,"
                    "participants,timeline"
                ),
                "hl": language,
                "directions": "v2",
                "fields": "timeline",
                "format": "coordinate_array",
                "timeline_highlights_fields": "tips,recommenders",
            }
        )
        url = f"{API_ORIGIN}{TOUR_PATH.format(tour_id=quoted_id)}?{query}"
        data = await self._async_authenticated_json(url, endpoint="tour detail")
        if "id" not in data:
            raise KomootSchemaError(
                "Komoot tour-detail response has an unexpected schema"
            )
        return data

    async def async_get_tour_gpx(self, tour_id: str | int) -> bytes:
        """Download a GPX track, rejecting empty, malformed, or oversized data."""
        quoted_id = quote(str(tour_id), safe="")
        url = f"{API_ORIGIN}{TOUR_PATH.format(tour_id=quoted_id)}.gpx"
        body = await self._async_authenticated_bytes(url, endpoint="tour GPX")
        probe = body.lstrip(b"\xef\xbb\xbf \t\r\n")[:1024].lower()
        if not body or b"<gpx" not in probe:
            raise KomootSchemaError("Komoot GPX response is not a GPX document")
        return body

    async def _async_ensure_login(self) -> None:
        if self._identity is None or not self._token:
            await self.async_login()

    def _token_auth(self) -> aiohttp.BasicAuth:
        if self._identity is None or not self._token:
            raise KomootAuthenticationError("Komoot authentication is required")
        return aiohttp.BasicAuth(
            self._identity.user_id, self._token, encoding="utf-8"
        )

    async def _async_authenticated_json(
        self, url: str, *, endpoint: str
    ) -> dict[str, Any]:
        await self._async_ensure_login()
        for attempt in range(2):
            try:
                return await self._async_request_json(
                    url,
                    endpoint=endpoint,
                    auth=self._token_auth(),
                    auth_statuses=(401,),
                )
            except KomootAuthenticationError:
                if attempt:
                    raise
                self._identity = None
                self._token = None
                await self.async_login()
        raise AssertionError("unreachable")

    async def _async_authenticated_bytes(
        self, url: str, *, endpoint: str
    ) -> bytes:
        await self._async_ensure_login()
        for attempt in range(2):
            try:
                return await self._async_request_bytes(
                    url,
                    endpoint=endpoint,
                    auth=self._token_auth(),
                    auth_statuses=(401,),
                )
            except KomootAuthenticationError:
                if attempt:
                    raise
                self._identity = None
                self._token = None
                await self.async_login()
        raise AssertionError("unreachable")

    async def _async_request_json(
        self,
        url: str,
        *,
        endpoint: str,
        auth: aiohttp.BasicAuth,
        auth_statuses: tuple[int, ...],
    ) -> dict[str, Any]:
        async def parse(response: aiohttp.ClientResponse) -> dict[str, Any]:
            try:
                data = await response.json(content_type=None)
            except (ValueError, aiohttp.ContentTypeError) as err:
                raise KomootSchemaError(
                    f"Komoot {endpoint} response is not valid JSON"
                ) from err
            if not isinstance(data, dict):
                raise KomootSchemaError(
                    f"Komoot {endpoint} response has an unexpected schema"
                )
            return data

        return await self._async_request(
            url,
            endpoint=endpoint,
            auth=auth,
            auth_statuses=auth_statuses,
            parser=parse,
        )

    async def _async_request_bytes(
        self,
        url: str,
        *,
        endpoint: str,
        auth: aiohttp.BasicAuth,
        auth_statuses: tuple[int, ...],
    ) -> bytes:
        async def parse(response: aiohttp.ClientResponse) -> bytes:
            content_length = response.content_length
            if (
                content_length is not None
                and content_length > self._max_gpx_bytes
            ):
                raise KomootSchemaError("Komoot GPX response exceeds the size limit")
            body = await response.read()
            if len(body) > self._max_gpx_bytes:
                raise KomootSchemaError("Komoot GPX response exceeds the size limit")
            return body

        return await self._async_request(
            url,
            endpoint=endpoint,
            auth=auth,
            auth_statuses=auth_statuses,
            parser=parse,
        )

    async def _async_request(
        self,
        url: str,
        *,
        endpoint: str,
        auth: aiohttp.BasicAuth,
        auth_statuses: tuple[int, ...],
        parser: Any,
    ) -> Any:
        """Perform one GET without leaking URLs, credentials, or response bodies."""
        try:
            async with self._session.get(
                url, auth=auth, timeout=self._timeout
            ) as response:
                if response.status in auth_statuses:
                    raise KomootAuthenticationError(
                        f"Komoot authentication failed with HTTP {response.status}"
                    )
                if response.status == 429:
                    raise KomootRateLimitError(
                        endpoint, _parse_retry_after(response.headers.get("Retry-After"))
                    )
                if response.status >= 400:
                    raise KomootHttpError(response.status, endpoint)
                return await parser(response)
        except (
            KomootApiError,
            asyncio.CancelledError,
        ):
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise KomootConnectionError(
                f"Komoot {endpoint} request failed"
            ) from err
