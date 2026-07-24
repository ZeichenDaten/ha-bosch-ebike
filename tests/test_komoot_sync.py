"""Pure tests for Komoot metadata normalisation and Bosch linkage."""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Load only the two pure helper functions without importing Home Assistant.
source_path = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "ha_bosch_ebike"
    / "komoot_sync.py"
)
tree = ast.parse(source_path.read_text(encoding="utf-8"))
wanted = {"_number", "_text", "normalise_komoot_metadata", "find_matching_bosch_activity"}
body = [
    node
    for node in tree.body
    if isinstance(node, (ast.Import, ast.ImportFrom))
    and any(alias.name == "typing" for alias in node.names)
    or isinstance(node, ast.FunctionDef)
    and node.name in wanted
]
module = ast.Module(body=body, type_ignores=[])


def parse_datetime(value):
    if not isinstance(value, str):
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else None


namespace = {
    "Any": __import__("typing").Any,
    "parse_datetime": parse_datetime,
    "timedelta": timedelta,
}
exec(compile(module, str(source_path), "exec"), namespace)

normalise = namespace["normalise_komoot_metadata"]
find_match = namespace["find_matching_bosch_activity"]


def test_metadata_prefers_detail_and_computes_average_speed():
    metadata = normalise(
        {"name": "Old", "distance": 10_000, "duration": 2_000},
        {
            "name": "Mountainbike-Tour",
            "distance": 14_300,
            "duration": 2_520,
            "elevation_up": 270,
            "elevation_down": 260,
            "date": "2026-07-19T14:12:12.839Z",
        },
    )
    assert metadata["title"] == "Mountainbike-Tour"
    assert metadata["distance"] == 14_300
    assert metadata["duration_without_stops"] == 2_520
    assert metadata["elevation_gain"] == 270
    assert metadata["elevation_loss"] == 260
    assert round(metadata["speed_average"], 1) == 20.4
    assert metadata["end_time"] == "2026-07-19T14:54:12.839000+00:00"


def test_unique_nearby_bosch_activity_is_linked():
    activities = [
        {
            "id": "bosch-1",
            "startTime": "2026-07-19T14:13:00+00:00",
            "distance": 14_450,
        },
        {
            "id": "bosch-old",
            "startTime": "2026-07-18T14:13:00+00:00",
            "distance": 14_450,
        },
    ]
    assert (
        find_match(
            activities,
            start_time="2026-07-19T14:12:12+00:00",
            distance_m=14_300,
        )
        == "bosch-1"
    )


def test_ambiguous_bosch_activities_are_not_guessed():
    activities = [
        {
            "id": "bosch-1",
            "startTime": "2026-07-19T14:12:00+00:00",
            "distance": 14_300,
        },
        {
            "id": "bosch-2",
            "startTime": "2026-07-19T14:13:00+00:00",
            "distance": 14_300,
        },
    ]
    assert (
        find_match(
            activities,
            start_time="2026-07-19T14:12:30+00:00",
            distance_m=14_300,
        )
        is None
    )


def test_external_activity_is_never_treated_as_bosch_match():
    activities = [
        {
            "id": "komoot:abc",
            "source": "komoot_gpx",
            "startTime": "2026-07-19T14:12:00+00:00",
            "distance": 14_300,
        }
    ]
    assert (
        find_match(
            activities,
            start_time="2026-07-19T14:12:00+00:00",
            distance_m=14_300,
        )
        is None
    )


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("ALL TESTS PASSED")
