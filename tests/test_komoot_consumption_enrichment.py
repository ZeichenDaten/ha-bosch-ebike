"""Regression tests for no-op semantics when a ride cannot be matched."""

from __future__ import annotations

import ast
import asyncio
from datetime import datetime, timezone
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any


SOURCE_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "ha_bosch_ebike"
    / "komoot_sync.py"
)
tree = ast.parse(SOURCE_PATH.read_text(encoding="utf-8"))
target = next(
    node
    for node in ast.walk(tree)
    if isinstance(node, ast.AsyncFunctionDef)
    and node.name == "_async_enrich_consumption"
)

setter_calls: list[dict[str, Any]] = []
decision = SimpleNamespace(match=None)
derived_consumption: dict[str, Any] | None = None


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def match_contact_windows(**_kwargs):
    return decision


def consumption_from_match(_match, **_kwargs):
    return derived_consumption


async def async_set_provider_consumption(_hass, **kwargs):
    setter_calls.append(kwargs)
    return True


namespace = {
    "Any": Any,
    "PROVIDER": "komoot",
    "parse_datetime": parse_datetime,
    "match_contact_windows": match_contact_windows,
    "consumption_from_match": consumption_from_match,
    "async_set_provider_consumption": async_set_provider_consumption,
}
exec(
    compile(ast.Module(body=[target], type_ignores=[]), str(SOURCE_PATH), "exec"),
    namespace,
)
enrich_consumption = namespace["_async_enrich_consumption"]
sync_node = next(
    node
    for node in ast.walk(tree)
    if isinstance(node, ast.AsyncFunctionDef)
    and node.name == "async_sync"
)


class FakeKomootApiError(Exception):
    pass


class FakeKomootAuthenticationError(FakeKomootApiError):
    pass


class FakeKomootRateLimitError(FakeKomootApiError):
    retry_after = None


sync_namespace = {
    "Any": Any,
    "EVENT_KOMOOT_SYNC_COMPLETED": "komoot_sync_completed",
    "KomootApiError": FakeKomootApiError,
    "KomootAuthenticationError": FakeKomootAuthenticationError,
    "KomootRateLimitError": FakeKomootRateLimitError,
    "MAX_SYNC_TOURS": 30,
    "PROVIDER": "komoot",
    "_LOGGER": logging.getLogger(__name__),
    "_text": lambda item, *keys: next(
        (str(item[key]) for key in keys if item.get(key)), None
    ),
    "dt_util": SimpleNamespace(
        now=lambda: datetime(2026, 1, 15, tzinfo=timezone.utc)
    ),
    "provider_import_is_ignored": lambda *_args: False,
    "provider_record": lambda hass, _provider, _provider_id: hass.record,
}
exec(
    compile(
        ast.Module(body=[sync_node], type_ignores=[]),
        str(SOURCE_PATH),
        "exec",
    ),
    sync_namespace,
)
sync_tours = sync_namespace["async_sync"]


class FakeManager:
    hass = object()
    bike_id = "bike-1"
    journal = SimpleNamespace(reliable_windows=lambda: [])
    coordinator = SimpleNamespace(battery_capacity_wh=lambda _bike_id: 400)


RECORD = {
    "start_time": "2026-01-15T10:00:00+00:00",
    "end_time": "2026-01-15T12:00:00+00:00",
    "distance": 40_000,
}


class FakeCoordinator:
    def __init__(self):
        self.refreshes = 0

    async def async_request_refresh(self):
        self.refreshes += 1


class FakeSyncManager:
    def __init__(self):
        self._lock = asyncio.Lock()
        self.hass = SimpleNamespace(
            record={
                "provider_changed_at": "same-revision",
                "consumption": {
                    "consumed_wh": 240.0,
                    "percentage": 60.0,
                },
            },
            bus=SimpleNamespace(async_fire=lambda *_args: None),
        )
        self.client = SimpleNamespace(
            async_list_tours=self._async_list_tours
        )
        self.coordinator = FakeCoordinator()
        self.entry = SimpleNamespace(entry_id="entry-1")
        self.bike_id = "bike-1"
        self.last_sync = None
        self.last_error = None
        self.last_result = {}
        self.enrich_calls = 0

    async def _async_list_tours(self):
        return [
            {
                "id": "tour-1",
                "date": "2026-01-15T10:00:00+00:00",
                "changed_at": "same-revision",
            }
        ]

    async def _async_enrich_consumption(self, _provider_id, _record):
        self.enrich_calls += 1
        return False


def setup_function():
    global decision, derived_consumption
    setter_calls.clear()
    decision = SimpleNamespace(match=None)
    derived_consumption = None


def test_no_match_does_not_clear_previously_confirmed_consumption():
    changed = asyncio.run(enrich_consumption(FakeManager(), "tour-1", RECORD))

    assert changed is False
    assert setter_calls == []


def test_invalid_consumption_does_not_clear_previously_confirmed_consumption():
    global decision
    decision = SimpleNamespace(match=object())

    changed = asyncio.run(enrich_consumption(FakeManager(), "tour-1", RECORD))

    assert changed is False
    assert setter_calls == []


def test_new_valid_match_updates_consumption():
    global decision, derived_consumption
    decision = SimpleNamespace(match=object())
    derived_consumption = {"consumed_wh": 240.0, "percentage": 60.0}

    changed = asyncio.run(enrich_consumption(FakeManager(), "tour-1", RECORD))

    assert changed is True
    assert setter_calls == [
        {
            "provider": "komoot",
            "provider_id": "tour-1",
            "consumption": derived_consumption,
        }
    ]


def test_full_sync_with_purged_journal_is_a_true_noop():
    manager = FakeSyncManager()

    result = asyncio.run(sync_tours(manager, reason="scheduled"))

    assert result["status"] == "ok"
    assert result["consumption_added"] == 0
    assert manager.enrich_calls == 1
    assert manager.coordinator.refreshes == 0
    assert manager.hass.record["consumption"] == {
        "consumed_wh": 240.0,
        "percentage": 60.0,
    }


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            setup_function()
            fn()
            print(f"PASS {name}")
    print("ALL TESTS PASSED")
