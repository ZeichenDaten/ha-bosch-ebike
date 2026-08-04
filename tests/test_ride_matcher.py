"""Offline tests for conservative Komoot/BLE ride matching."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import sys

_MODULE_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "ha_bosch_ebike"
    / "ride_matcher.py"
)
_spec = importlib.util.spec_from_file_location("ride_matcher", _MODULE_PATH)
ride_matcher = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
sys.modules[_spec.name] = ride_matcher
_spec.loader.exec_module(ride_matcher)

match_contact_windows = ride_matcher.match_contact_windows
consumption_from_match = ride_matcher.consumption_from_match

UTC = timezone.utc
START = datetime(2026, 7, 24, 16, 0, tzinfo=UTC)
END = datetime(2026, 7, 24, 17, 30, tzinfo=UTC)


def iso(value: datetime) -> str:
    return value.isoformat()


def window(
    identifier: str,
    started: datetime,
    ended: datetime,
    first_soc: float,
    first_odo: float,
    last_soc: float,
    last_odo: float,
) -> dict:
    return {
        "id": identifier,
        "started_at": iso(started),
        "ended_at": iso(ended),
        "reliable_start": True,
        "reliable_end": True,
        "first_sample": {
            "at": iso(started + timedelta(seconds=5)),
            "soc": first_soc,
            "odometer_km": first_odo,
        },
        "last_sample": {
            "at": iso(ended - timedelta(seconds=5)),
            "soc": last_soc,
            "odometer_km": last_odo,
        },
    }


def test_matches_departure_and_arrival_and_uses_whole_tour_distance():
    windows = [
        window(
            "departure",
            START - timedelta(hours=2),
            START - timedelta(minutes=2),
            80,
            100.0,
            80,
            100.0,
        ),
        window(
            "arrival",
            END + timedelta(minutes=3),
            END + timedelta(hours=1),
            60,
            130.0,
            70,
            130.0,
        ),
    ]

    decision = match_contact_windows(
        tour_start=START,
        tour_end=END,
        tour_distance_m=30_000,
        windows=windows,
    )

    assert decision.status == "matched"
    assert decision.match is not None
    consumption = consumption_from_match(
        decision.match, capacity_wh=400, activity_distance_m=30_000
    )
    assert consumption is not None
    assert consumption["consumed_wh"] == 80.0
    assert consumption["percentage"] == 20.0
    assert consumption["session_distance_m"] == 30_000.0
    assert consumption["source"] == "komoot_ble_journal"


def test_matches_arrival_before_late_komoot_tour_end():
    """A forgotten Komoot stop must not hide an otherwise exact BLE match."""
    windows = [
        window(
            "departure",
            START - timedelta(hours=1),
            START + timedelta(minutes=2),
            100,
            303.26,
            100,
            303.26,
        ),
        window(
            "arrival",
            END - timedelta(minutes=26),
            END + timedelta(minutes=20),
            69,
            323.36,
            69,
            323.36,
        ),
    ]

    decision = match_contact_windows(
        tour_start=START,
        tour_end=END,
        tour_distance_m=20_496,
        windows=windows,
    )

    assert decision.status == "matched"
    assert decision.match is not None
    consumption = consumption_from_match(
        decision.match, capacity_wh=400, activity_distance_m=20_496
    )
    assert consumption is not None
    assert consumption["percentage"] == 31.0
    assert consumption["consumed_wh"] == 124.0


def test_friend_bike_without_contact_pair_is_not_attributed():
    decision = match_contact_windows(
        tour_start=START,
        tour_end=END,
        tour_distance_m=20_000,
        windows=[],
    )
    assert decision.status == "unmatched"
    assert decision.match is None


def test_implausible_odometer_delta_is_rejected():
    windows = [
        window(
            "departure",
            START - timedelta(hours=1),
            START,
            90,
            100.0,
            90,
            100.0,
        ),
        window(
            "arrival",
            END,
            END + timedelta(minutes=10),
            80,
            150.0,
            80,
            150.0,
        ),
    ]
    decision = match_contact_windows(
        tour_start=START,
        tour_end=END,
        tour_distance_m=10_000,
        windows=windows,
    )
    assert decision.status == "unmatched"


def test_close_competing_pairs_are_ambiguous():
    windows = [
        window(
            "dep-a",
            START - timedelta(hours=1),
            START - timedelta(minutes=2),
            90,
            100.0,
            90,
            100.0,
        ),
        window(
            "dep-b",
            START - timedelta(minutes=20),
            START - timedelta(minutes=1),
            89,
            100.1,
            89,
            100.1,
        ),
        window(
            "arrival",
            END + timedelta(minutes=2),
            END + timedelta(minutes=20),
            80,
            120.0,
            80,
            120.0,
        ),
    ]
    decision = match_contact_windows(
        tour_start=START,
        tour_end=END,
        tour_distance_m=20_000,
        windows=windows,
    )
    assert decision.status == "ambiguous"
    assert decision.match is None


def test_equivalent_departure_reconnects_are_deduplicated():
    windows = [
        window(
            "dep-a",
            START - timedelta(minutes=10),
            START + timedelta(seconds=37),
            94,
            391.468,
            94,
            391.468,
        ),
        window(
            "dep-b",
            START + timedelta(seconds=50),
            START + timedelta(seconds=86),
            94,
            391.468,
            94,
            391.468,
        ),
        window(
            "arrival",
            END + timedelta(seconds=47),
            END + timedelta(hours=1),
            56,
            412.293,
            56,
            412.293,
        ),
    ]

    decision = match_contact_windows(
        tour_start=START,
        tour_end=END,
        tour_distance_m=19_781.5,
        windows=windows,
    )

    assert decision.status == "matched"
    assert decision.match is not None
    consumption = consumption_from_match(
        decision.match, capacity_wh=400, activity_distance_m=19_781.5
    )
    assert consumption is not None
    assert consumption["percentage"] == 38.0
    assert consumption["consumed_wh"] == 152.0


def test_nearby_reconnect_with_different_measurement_stays_ambiguous():
    windows = [
        window(
            "dep-a",
            START - timedelta(minutes=10),
            START - timedelta(seconds=20),
            94,
            391.468,
            94,
            391.468,
        ),
        window(
            "dep-b",
            START - timedelta(minutes=1),
            START - timedelta(seconds=10),
            93,
            391.5,
            93,
            391.5,
        ),
        window(
            "arrival",
            END,
            END + timedelta(minutes=10),
            56,
            412.293,
            56,
            412.293,
        ),
    ]
    decision = match_contact_windows(
        tour_start=START,
        tour_end=END,
        tour_distance_m=20_000,
        windows=windows,
    )
    assert decision.status == "ambiguous"


def test_equivalent_candidate_does_not_hide_third_real_ambiguity():
    windows = [
        window(
            "dep-best",
            START - timedelta(minutes=10),
            START - timedelta(seconds=30),
            94,
            100.0,
            94,
            100.0,
        ),
        window(
            "dep-equivalent",
            START - timedelta(minutes=5),
            START - timedelta(seconds=20),
            94,
            100.0,
            94,
            100.0,
        ),
        window(
            "dep-different",
            START - timedelta(minutes=4),
            START - timedelta(seconds=10),
            92,
            100.1,
            92,
            100.1,
        ),
        window(
            "arrival",
            END,
            END + timedelta(minutes=10),
            70,
            120.0,
            70,
            120.0,
        ),
    ]
    decision = match_contact_windows(
        tour_start=START,
        tour_end=END,
        tour_distance_m=20_000,
        windows=windows,
    )
    assert decision.status == "ambiguous"


def test_equivalent_arrival_reconnects_are_deduplicated():
    windows = [
        window(
            "departure",
            START - timedelta(minutes=10),
            START,
            90,
            100.0,
            90,
            100.0,
        ),
        window(
            "arr-a",
            END,
            END + timedelta(minutes=5),
            70,
            120.0,
            70,
            120.0,
        ),
        window(
            "arr-b",
            END + timedelta(seconds=20),
            END + timedelta(minutes=6),
            70,
            120.0,
            70,
            120.0,
        ),
    ]
    decision = match_contact_windows(
        tour_start=START,
        tour_end=END,
        tour_distance_m=20_000,
        windows=windows,
    )
    assert decision.status == "matched"


def test_zero_or_sub_percent_soc_delta_is_excluded_from_range():
    windows = [
        window(
            "departure",
            START - timedelta(hours=1),
            START,
            80.0,
            100.0,
            80.0,
            100.0,
        ),
        window(
            "arrival",
            END,
            END + timedelta(minutes=10),
            79.5,
            120.0,
            79.5,
            120.0,
        ),
    ]
    decision = match_contact_windows(
        tour_start=START,
        tour_end=END,
        tour_distance_m=20_000,
        windows=windows,
    )
    assert decision.match is not None
    assert (
        consumption_from_match(
            decision.match, capacity_wh=400, activity_distance_m=20_000
        )
        is None
    )


def test_naive_timestamps_are_rejected():
    bad_start = datetime(2026, 7, 24, 16, 0)
    decision = match_contact_windows(
        tour_start=bad_start,
        tour_end=END,
        tour_distance_m=20_000,
        windows=[],
    )
    assert decision.status == "unmatched"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("ALL TESTS PASSED")
