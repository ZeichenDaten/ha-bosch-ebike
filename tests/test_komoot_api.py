"""Offline tests for the asynchronous Komoot API client."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
from typing import Any

import aiohttp

_MODULE_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "ha_bosch_ebike"
    / "komoot_api.py"
)
_spec = importlib.util.spec_from_file_location("komoot_api", _MODULE_PATH)
komoot_api = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
sys.modules[_spec.name] = komoot_api
_spec.loader.exec_module(komoot_api)

KomootApiClient = komoot_api.KomootApiClient
KomootAuthenticationError = komoot_api.KomootAuthenticationError
KomootConnectionError = komoot_api.KomootConnectionError
KomootHttpError = komoot_api.KomootHttpError
KomootPaginationError = komoot_api.KomootPaginationError
KomootRateLimitError = komoot_api.KomootRateLimitError
KomootSchemaError = komoot_api.KomootSchemaError


class FakeResponse:
    """Minimal aiohttp response-shaped async context manager."""

    def __init__(
        self,
        status: int = 200,
        *,
        json_data: Any = None,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
        json_error: Exception | None = None,
        content_length: int | None = None,
    ) -> None:
        self.status = status
        self._json_data = json_data
        self._body = body
        self.headers = headers or {}
        self._json_error = json_error
        self.content_length = (
            len(body) if content_length is None and body else content_length
        )

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None

    async def json(self, *, content_type=None) -> Any:
        if self._json_error is not None:
            raise self._json_error
        return self._json_data

    async def read(self) -> bytes:
        return self._body


class FakeSession:
    """Queue-backed aiohttp session fake that performs no network access."""

    def __init__(self, *results: FakeResponse | Exception) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((url, kwargs))
        if not self.results:
            raise AssertionError(f"unexpected GET {url}")
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def run(awaitable):
    return asyncio.run(awaitable)


def expect_error(error_type, awaitable):
    try:
        run(awaitable)
    except error_type as err:
        return err
    raise AssertionError(f"expected {error_type.__name__}")


def login_response(
    user_id: str = "1234567890", token: str = "session-token"
) -> FakeResponse:
    return FakeResponse(
        json_data={
            "username": user_id,
            "password": token,
            "user": {"displayname": "Carbon Tester"},
        }
    )


def test_login_uses_encoded_v006_url_and_basic_auth():
    session = FakeSession(login_response())
    client = KomootApiClient(session, "rider+test@example.com", "very-secret")

    identity = run(client.async_login())

    assert identity.user_id == "1234567890"
    assert identity.display_name == "Carbon Tester"
    url, kwargs = session.calls[0]
    assert url.endswith("/v006/account/email/rider%2Btest%40example.com/")
    assert kwargs["auth"].login == "rider+test@example.com"
    assert kwargs["auth"].password == "very-secret"


def test_login_rejects_invalid_schema_without_leaking_credentials():
    session = FakeSession(FakeResponse(json_data={"username": "123"}))
    client = KomootApiClient(session, "secret@example.com", "do-not-print")

    err = expect_error(KomootSchemaError, client.async_login())

    assert "secret@example.com" not in str(err)
    assert "do-not-print" not in str(err)


def test_login_maps_401_and_403_to_authentication_error():
    for status in (401, 403):
        session = FakeSession(FakeResponse(status, json_data={}))
        client = KomootApiClient(session, "mail@example.com", "password")
        err = expect_error(KomootAuthenticationError, client.async_login())
        assert str(status) in str(err)
        assert "mail@example.com" not in str(err)
        assert "password" not in str(err)


def test_list_tours_paginates_filters_and_deduplicates():
    page_2_url = "https://api.komoot.de/v007/users/7/tours/?page=2"
    session = FakeSession(
        login_response("7"),
        FakeResponse(
            json_data={
                "_embedded": {
                    "tours": [
                        {"id": 10, "type": "tour_recorded", "name": "A"},
                        {"id": 11, "type": "tour_planned", "name": "Plan"},
                    ]
                },
                "_links": {"next": {"href": page_2_url}},
            }
        ),
        FakeResponse(
            json_data={
                "_embedded": {
                    "tours": [
                        {"id": 10, "type": "tour_recorded", "name": "A duplicate"},
                        {"id": 12, "type": "tour_recorded", "name": "B"},
                    ]
                },
                "_links": {},
            }
        ),
    )
    client = KomootApiClient(session, "mail@example.com", "password")

    tours = run(client.async_list_tours())

    assert [tour["id"] for tour in tours] == [10, 12]
    assert len(session.calls) == 3
    assert session.calls[1][1]["auth"].login == "7"
    assert session.calls[1][1]["auth"].password == "session-token"


def test_list_tours_can_return_all_types():
    session = FakeSession(
        login_response(),
        FakeResponse(
            json_data={
                "_embedded": {
                    "tours": [
                        {"id": 1, "type": "tour_recorded"},
                        {"id": 2, "type": "tour_planned"},
                    ]
                },
                "_links": {},
            }
        ),
    )
    client = KomootApiClient(session, "mail@example.com", "password")
    tours = run(client.async_list_tours(tour_type=None))
    assert [tour["id"] for tour in tours] == [1, 2]


def test_list_tours_rejects_hostile_pagination_before_sending_token():
    session = FakeSession(
        login_response(),
        FakeResponse(
            json_data={
                "_embedded": {"tours": []},
                "_links": {
                    "next": {"href": "https://attacker.invalid/steal-token"}
                },
            }
        ),
    )
    client = KomootApiClient(session, "mail@example.com", "password")

    expect_error(KomootPaginationError, client.async_list_tours())

    assert len(session.calls) == 2


def test_list_tours_detects_pagination_loop():
    first_url = "https://api.komoot.de/v007/users/1234567890/tours/"
    session = FakeSession(
        login_response(),
        FakeResponse(
            json_data={
                "_embedded": {"tours": []},
                "_links": {"next": {"href": first_url}},
            }
        ),
    )
    client = KomootApiClient(session, "mail@example.com", "password")
    expect_error(KomootPaginationError, client.async_list_tours())


def test_list_tours_enforces_page_limit():
    session = FakeSession(
        login_response(),
        FakeResponse(
            json_data={
                "_embedded": {"tours": []},
                "_links": {
                    "next": {
                        "href": "https://api.komoot.de/v007/users/1/tours/?page=2"
                    }
                },
            }
        ),
    )
    client = KomootApiClient(session, "mail@example.com", "password")
    expect_error(
        KomootPaginationError, client.async_list_tours(max_pages=1)
    )


def test_list_tours_rejects_malformed_schema():
    session = FakeSession(login_response(), FakeResponse(json_data={"_links": {}}))
    client = KomootApiClient(session, "mail@example.com", "password")
    expect_error(KomootSchemaError, client.async_list_tours())


def test_detail_uses_v007_and_requires_id():
    session = FakeSession(login_response(), FakeResponse(json_data={"id": 42}))
    client = KomootApiClient(session, "mail@example.com", "password")

    detail = run(client.async_get_tour_detail("42/unsafe", language="de"))

    assert detail["id"] == 42
    detail_url = session.calls[1][0]
    assert "/v007/tours/42%2Funsafe?" in detail_url
    assert "format=coordinate_array" in detail_url
    assert "hl=de" in detail_url

    invalid_session = FakeSession(
        login_response(), FakeResponse(json_data={"name": "missing id"})
    )
    invalid_client = KomootApiClient(
        invalid_session, "mail@example.com", "password"
    )
    expect_error(
        KomootSchemaError, invalid_client.async_get_tour_detail(42)
    )


def test_gpx_download_validates_document_and_size():
    valid = b'<?xml version="1.0"?><gpx version="1.1"></gpx>'
    session = FakeSession(login_response(), FakeResponse(body=valid))
    client = KomootApiClient(session, "mail@example.com", "password")
    assert run(client.async_get_tour_gpx(42)) == valid
    assert session.calls[1][0].endswith("/v007/tours/42.gpx")

    malformed_session = FakeSession(
        login_response(), FakeResponse(body=b"<html>not a track</html>")
    )
    malformed_client = KomootApiClient(
        malformed_session, "mail@example.com", "password"
    )
    expect_error(KomootSchemaError, malformed_client.async_get_tour_gpx(42))

    oversized_session = FakeSession(
        login_response(), FakeResponse(body=b"<gpx>too large</gpx>")
    )
    oversized_client = KomootApiClient(
        oversized_session,
        "mail@example.com",
        "password",
        max_gpx_bytes=10,
    )
    expect_error(KomootSchemaError, oversized_client.async_get_tour_gpx(42))


def test_401_reauthenticates_once_and_retries_with_new_token():
    session = FakeSession(
        login_response("7", "old-token"),
        FakeResponse(401),
        login_response("7", "new-token"),
        FakeResponse(
            json_data={"_embedded": {"tours": []}, "_links": {}}
        ),
    )
    client = KomootApiClient(session, "mail@example.com", "password")

    assert run(client.async_list_tours()) == []

    assert len(session.calls) == 4
    assert session.calls[1][1]["auth"].password == "old-token"
    assert session.calls[3][1]["auth"].password == "new-token"


def test_second_401_fails_without_an_authentication_loop():
    session = FakeSession(
        login_response("7", "old-token"),
        FakeResponse(401),
        login_response("7", "new-token"),
        FakeResponse(401),
    )
    client = KomootApiClient(session, "mail@example.com", "password")

    expect_error(KomootAuthenticationError, client.async_list_tours())

    assert len(session.calls) == 4


def test_429_exposes_retry_after_without_response_body():
    session = FakeSession(
        login_response(),
        FakeResponse(
            429,
            headers={"Retry-After": "120"},
            body=b"secret upstream body",
        ),
    )
    client = KomootApiClient(session, "mail@example.com", "password")

    err = expect_error(KomootRateLimitError, client.async_list_tours())

    assert err.status == 429
    assert err.retry_after == 120
    assert "secret upstream body" not in str(err)


def test_other_http_errors_do_not_include_body_or_credentials():
    session = FakeSession(
        login_response(),
        FakeResponse(500, body=b"mail@example.com password"),
    )
    client = KomootApiClient(session, "mail@example.com", "password")

    err = expect_error(KomootHttpError, client.async_list_tours())

    assert err.status == 500
    assert "mail@example.com" not in str(err)
    assert "password" not in str(err)


def test_transport_failure_is_wrapped_without_url_or_credentials():
    session = FakeSession(
        aiohttp.ClientConnectionError("mail@example.com password")
    )
    client = KomootApiClient(session, "mail@example.com", "password")

    err = expect_error(KomootConnectionError, client.async_login())

    assert "mail@example.com" not in str(err)
    assert "password" not in str(err)


def test_invalid_json_is_a_schema_error():
    session = FakeSession(
        FakeResponse(json_error=ValueError("not json and secret"))
    )
    client = KomootApiClient(session, "mail@example.com", "password")
    err = expect_error(KomootSchemaError, client.async_login())
    assert "secret" not in str(err)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("ALL TESTS PASSED")
