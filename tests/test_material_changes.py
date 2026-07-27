"""Tests for rider-visible Komoot change detection."""

from __future__ import annotations

import importlib.util
from pathlib import Path


SOURCE_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "ha_bosch_ebike"
    / "material_changes.py"
)
spec = importlib.util.spec_from_file_location("material_changes", SOURCE_PATH)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

material_consumption_change = module.material_consumption_change
material_tour_changes = module.material_tour_changes
route_geometry_changed = module.route_geometry_changed
build_notification_summary = module.build_notification_summary


def _record(**overrides):
    record = {
        "title": "Morning ride",
        "start_time": "2026-01-15T10:00:00+00:00",
        "distance": 40_000.0,
        "duration_without_stops": 7_200.0,
        "elevation": {"gain": 500.0, "loss": 480.0},
        "speed": {"average": 20.0, "maximum": 52.0},
        "points": [
            {"lat": 49.7000, "lon": 12.1000},
            {"lat": 49.7100, "lon": 12.1100},
            {"lat": 49.7200, "lon": 12.1200},
        ],
    }
    record.update(overrides)
    return record


def test_identical_tour_and_whitespace_only_title_are_silent():
    old = _record()
    new = _record(title="  Morning   ride ")

    assert material_tour_changes(old, new) == []


def test_numeric_thresholds_ignore_noise_and_report_real_changes():
    old = _record()
    below = _record(
        distance=40_049,
        duration_without_stops=7_229,
        elevation={"gain": 509, "loss": 489},
        speed={"average": 20.19, "maximum": 52.19},
    )
    above = _record(
        distance=40_051,
        duration_without_stops=7_237,
        elevation={"gain": 511, "loss": 491},
        speed={"average": 20.21, "maximum": 52.27},
    )

    assert material_tour_changes(old, below) == []
    assert {item["field"] for item in material_tour_changes(old, above)} == {
        "distance",
        "duration",
        "elevation_gain",
        "elevation_loss",
        "speed_average",
        "speed_maximum",
    }


def test_start_time_normalises_timezones_and_handles_naive_values():
    old = _record(start_time="2026-01-15T10:00:00")
    equivalent = _record(start_time="2026-01-15T11:00:00+01:00")
    changed = _record(start_time="2026-01-15T11:01:00+01:00")

    assert material_tour_changes(old, equivalent) == []
    assert [item["field"] for item in material_tour_changes(old, changed)] == [
        "start_time"
    ]


def test_route_resampling_ignores_point_density_but_detects_detour():
    old = _record()
    denser = _record(
        points=[
            {"lat": 49.7000, "lon": 12.1000},
            {"lat": 49.7050, "lon": 12.1050},
            {"lat": 49.7100, "lon": 12.1100},
            {"lat": 49.7150, "lon": 12.1150},
            {"lat": 49.7200, "lon": 12.1200},
        ]
    )
    detour = _record(
        points=[
            {"lat": 49.7000, "lon": 12.1000},
            {"lat": 49.7300, "lon": 12.0900},
            {"lat": 49.7200, "lon": 12.1200},
        ]
    )

    assert route_geometry_changed(old, denser) is False
    assert route_geometry_changed(old, detour) is True


def test_consumption_only_reports_new_or_relevant_values():
    new = {"percentage": 30.0, "consumed_wh": 120.0}
    assert material_consumption_change(None, new)["field"] == "consumption"
    assert material_consumption_change(new, dict(new)) is None
    assert (
        material_consumption_change(
            new, {"percentage": 30.9, "consumed_wh": 124.9}
        )
        is None
    )
    assert (
        material_consumption_change(
            new, {"percentage": 31.0, "consumed_wh": 125.0}
        )["field"]
        == "consumption"
    )
    assert (
        material_consumption_change(
            new, {"percentage": float("nan"), "consumed_wh": float("inf")}
        )
        is None
    )


def test_notification_summary_is_compact_and_coordinate_free():
    summary = build_notification_summary(
        [
            {
                "kind": "updated",
                "title": "Morning ride",
                "distance": 40_000,
                "changes": [
                    {
                        "field": "distance",
                        "label": "Distanz",
                        "old": 39_900,
                        "new": 40_100,
                    },
                    {
                        "field": "route",
                        "label": "Streckenverlauf",
                        "old": None,
                        "new": "geändert",
                    },
                ],
            }
        ]
    )

    assert summary == (
        "„Morning ride“ geändert: Distanz 39,9 km → 40,1 km · "
        "Streckenverlauf geändert"
    )
    assert "49." not in summary


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("ALL TESTS PASSED")
