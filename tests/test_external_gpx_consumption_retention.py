"""Regression tests for preserving confirmed provider consumption."""

from __future__ import annotations

import ast
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any


SOURCE_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "ha_bosch_ebike"
    / "external_gpx.py"
)
tree = ast.parse(SOURCE_PATH.read_text(encoding="utf-8"))
targets = [
    node
    for node in tree.body
    if isinstance(node, ast.FunctionDef)
    and node.name
    in {"_replace_provider_record", "_verified_manual_consumption"}
]
namespace = {"Any": Any}
exec(
    compile(ast.Module(body=targets, type_ignores=[]), str(SOURCE_PATH), "exec"),
    namespace,
)
replace_provider_record = namespace["_replace_provider_record"]
verified_manual_consumption = namespace["_verified_manual_consumption"]

upsert_node = next(
    node
    for node in tree.body
    if isinstance(node, ast.AsyncFunctionDef)
    and node.name == "async_upsert_provider_gpx"
)
saved_tracks: list[dict[str, Any]] = []


async def save_tracks(hass):
    saved_tracks[:] = [dict(item) for item in hass.tracks]


upsert_namespace = {
    "Any": Any,
    "MAX_TRACKS": 500,
    "_apply_authoritative_metadata": lambda parsed, _metadata: parsed,
    "_clean_text": lambda value, default="", limit=None: str(
        value or default
    )[:limit],
    "_parse_gpx": lambda _content, _filename, _activity_id: {
        "title": "Updated tour",
        "content_hash": "new-hash",
        "start_time": "2026-01-15T10:00:00+00:00",
        "distance": 40_000,
    },
    "_parse_time": lambda value: (
        datetime.fromisoformat(value) if value else None
    ),
    "material_tour_changes": lambda old, new: (
        [
            {
                "field": "title",
                "label": "Titel",
                "old": old.get("title"),
                "new": new.get("title"),
            }
        ]
        if old.get("title") != new.get("title")
        else []
    ),
    "_replace_provider_record": replace_provider_record,
    "_save": save_tracks,
    "_tracks": lambda hass: hass.tracks,
    "dt_util": SimpleNamespace(
        utcnow=lambda: datetime(2026, 1, 15, tzinfo=timezone.utc)
    ),
    "provider_import_is_ignored": lambda *_args: False,
    "uuid": SimpleNamespace(uuid4=lambda: SimpleNamespace(hex="newtrack0000")),
    "vol": SimpleNamespace(Invalid=ValueError),
}
exec(
    compile(
        ast.Module(body=[upsert_node], type_ignores=[]),
        str(SOURCE_PATH),
        "exec",
    ),
    upsert_namespace,
)
upsert_provider_gpx = upsert_namespace["async_upsert_provider_gpx"]


def test_provider_update_retains_existing_consumption():
    existing = {
        "id": "track-1",
        "title": "Old title",
        "consumption": {"consumed_wh": 224.0, "percentage": 56.0},
    }

    replace_provider_record(
        existing,
        {"id": "track-1", "title": "Updated title"},
        None,
    )

    assert existing == {
        "id": "track-1",
        "title": "Updated title",
        "consumption": {"consumed_wh": 224.0, "percentage": 56.0},
    }


def test_new_confirmed_consumption_replaces_the_old_value():
    existing = {
        "id": "track-1",
        "consumption": {"consumed_wh": 100.0},
    }
    replacement = {"consumed_wh": 220.0, "percentage": 55.0}

    replace_provider_record(existing, {"id": "track-1"}, replacement)

    assert existing["consumption"] == replacement
    assert existing["consumption"] is not replacement


def test_new_record_without_consumption_stays_without_consumption():
    existing = {"id": "track-1"}

    replace_provider_record(existing, {"id": "track-1", "title": "Tour"}, None)

    assert "consumption" not in existing


def test_real_provider_upsert_persists_existing_consumption():
    previous = {"consumed_wh": 240.0, "percentage": 60.0}
    hass = SimpleNamespace(
        tracks=[
            {
                "id": "track-1",
                "provider": "komoot",
                "provider_id": "tour-1",
                "provider_changed_at": "old-revision",
                "imported_at": "2026-01-15T09:00:00+00:00",
                "title": "Old tour",
                "consumption": dict(previous),
            }
        ]
    )
    saved_tracks.clear()

    result = asyncio.run(
        upsert_provider_gpx(
            hass,
            provider="komoot",
            provider_id="tour-1",
            provider_changed_at="new-revision",
            gpx_content="<gpx />",
            filename="tour.gpx",
            bike_id="bike-1",
        )
    )

    assert result["status"] == "updated"
    assert result["record"]["title"] == "Updated tour"
    assert result["record"]["provider_changed_at"] == "new-revision"
    assert result["record"]["consumption"] == previous
    assert saved_tracks[0]["consumption"] == previous


def test_provider_only_revision_is_persisted_without_material_update():
    hass = SimpleNamespace(
        tracks=[
            {
                "id": "track-1",
                "provider": "komoot",
                "provider_id": "tour-1",
                "provider_changed_at": "old-revision",
                "imported_at": "2026-01-15T09:00:00+00:00",
                "title": "Updated tour",
            }
        ]
    )
    original_detector = upsert_provider_gpx.__globals__["material_tour_changes"]
    upsert_provider_gpx.__globals__["material_tour_changes"] = (
        lambda _old, _new: []
    )
    try:
        result = asyncio.run(
            upsert_provider_gpx(
                hass,
                provider="komoot",
                provider_id="tour-1",
                provider_changed_at="social-only-revision",
                gpx_content="<gpx />",
                filename="tour.gpx",
                bike_id="bike-1",
            )
        )
    finally:
        upsert_provider_gpx.__globals__["material_tour_changes"] = (
            original_detector
        )

    assert result["status"] == "refreshed"
    assert result["changes"] == []
    assert result["record"]["provider_changed_at"] == "social-only-revision"


def test_verified_manual_repair_uses_the_same_physical_bounds():
    result = verified_manual_consumption(
        {"distance": 40_000},
        start_soc=100,
        end_soc=40,
        capacity_wh=400,
        session_distance_m=42_000,
    )

    assert result == {
        "consumed_wh": 240.0,
        "percentage": 60,
        "capacity_wh": 400,
        "start_soc": 100,
        "end_soc": 40,
        "session_distance_m": 42_000,
        "source": "komoot_ble_journal",
        "verified_manual": True,
    }


def test_verified_manual_repair_rejects_implausible_values():
    assert (
        verified_manual_consumption(
            {"distance": 40_000},
            start_soc=40,
            end_soc=100,
            capacity_wh=400,
            session_distance_m=42_000,
        )
        is None
    )
    assert (
        verified_manual_consumption(
            {"distance": 10_000},
            start_soc=100,
            end_soc=38,
            capacity_wh=400,
            session_distance_m=50_000,
        )
        is None
    )


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("ALL TESTS PASSED")
