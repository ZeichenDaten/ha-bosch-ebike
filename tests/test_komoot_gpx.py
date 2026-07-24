"""Offline tests for embedded-coordinate GPX generation."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

_MODULE_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "ha_bosch_ebike"
    / "komoot_gpx.py"
)
_spec = importlib.util.spec_from_file_location("komoot_gpx", _MODULE_PATH)
module = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
sys.modules[_spec.name] = module
_spec.loader.exec_module(module)
detail_to_gpx = module.detail_to_gpx


def test_elapsed_timestamps_and_xml_escaping():
    gpx = detail_to_gpx(
        {
            "name": "Wald & Wiese",
            "date": "2026-07-19T14:12:12+00:00",
            "_embedded": {
                "coordinates": {
                    "items": [
                        {"lat": 49.7, "lng": 12.2, "alt": 450, "t": 0},
                        {"lat": 49.71, "lng": 12.21, "alt": 455, "t": 2_000},
                    ]
                }
            },
        }
    )
    assert "Wald &amp; Wiese" in gpx
    assert "2026-07-19T14:12:12Z" in gpx
    assert "2026-07-19T14:12:14Z" in gpx
    assert gpx.count("<trkpt ") == 2


def test_epoch_timestamp_is_preserved():
    gpx = detail_to_gpx(
        {
            "name": "Tour",
            "_embedded": {
                "coordinates": {
                    "items": [
                        {
                            "lat": 49.7,
                            "lng": 12.2,
                            "t": 1_753_020_000_000,
                        },
                        {
                            "lat": 49.71,
                            "lng": 12.21,
                            "t": 1_753_020_002_000,
                        },
                    ]
                }
            },
        }
    )
    assert "2025-" in gpx


def test_rejects_missing_or_too_short_coordinate_array():
    for detail in (
        {},
        {"_embedded": {"coordinates": {"items": [{"lat": 1, "lng": 2}]}}},
    ):
        try:
            detail_to_gpx(detail)
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("ALL TESTS PASSED")
