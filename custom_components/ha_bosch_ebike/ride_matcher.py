"""Pure matching logic for Komoot tours and garage BLE contact windows.

This module intentionally has no Home Assistant imports.  The state listener
stores small, serialisable contact windows; these helpers decide whether a
recorded tour can be attributed to the configured bike without guessing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any


DEPARTURE_BEFORE = timedelta(minutes=45)
DEPARTURE_AFTER = timedelta(minutes=10)
ARRIVAL_BEFORE = timedelta(minutes=10)
ARRIVAL_AFTER = timedelta(minutes=45)
AMBIGUITY_SECONDS = 5 * 60

MIN_SESSION_KM = 0.3
MIN_DISTANCE_RATIO = 0.55
MAX_DISTANCE_RATIO = 1.8


@dataclass(frozen=True, slots=True)
class RideContactMatch:
    """One unambiguous departure/arrival contact pair."""

    departure_id: str
    arrival_id: str
    start_sample: dict[str, Any]
    end_sample: dict[str, Any]
    score_seconds: float


@dataclass(frozen=True, slots=True)
class RideMatchDecision:
    """Result that preserves why a tour was deliberately not enriched."""

    status: str
    match: RideContactMatch | None = None
    reason: str | None = None


def parse_datetime(value: Any) -> datetime | None:
    """Parse an ISO timestamp and require an explicit timezone."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed:  # NaN
        return None
    return parsed


def _sample(window: dict[str, Any], key: str) -> dict[str, Any] | None:
    value = window.get(key)
    if not isinstance(value, dict):
        return None
    soc = _number(value.get("soc"))
    odometer_km = _number(value.get("odometer_km"))
    sampled_at = parse_datetime(value.get("at"))
    if (
        soc is None
        or not 0 <= soc <= 100
        or odometer_km is None
        or odometer_km < 0
        or sampled_at is None
    ):
        return None
    return {
        "at": sampled_at.isoformat(),
        "soc": soc,
        "odometer_km": odometer_km,
    }


def _window_id(window: dict[str, Any]) -> str | None:
    value = window.get("id")
    return value if isinstance(value, str) and value else None


def match_contact_windows(
    *,
    tour_start: datetime,
    tour_end: datetime,
    tour_distance_m: float,
    windows: list[dict[str, Any]],
) -> RideMatchDecision:
    """Find a unique, physically plausible contact pair for one tour.

    Departure is the contact ending when the bike leaves bridge range; its
    last valid sample is the pre-ride snapshot. Arrival is a later contact
    starting when the bike returns; its first valid sample is the post-ride
    snapshot.  Close competing pairs are rejected as ambiguous.
    """
    if (
        tour_start.tzinfo is None
        or tour_end.tzinfo is None
        or tour_end <= tour_start
        or tour_distance_m <= 0
    ):
        return RideMatchDecision("unmatched", reason="invalid_tour")

    departures: list[tuple[dict[str, Any], datetime, dict[str, Any]]] = []
    arrivals: list[tuple[dict[str, Any], datetime, dict[str, Any]]] = []

    for window in windows:
        if not isinstance(window, dict):
            continue
        started = parse_datetime(window.get("started_at"))
        ended = parse_datetime(window.get("ended_at"))
        if (
            window.get("reliable_end") is True
            and
            ended is not None
            and tour_start - DEPARTURE_BEFORE
            <= ended
            <= tour_start + DEPARTURE_AFTER
        ):
            start_sample = _sample(window, "last_sample")
            if start_sample is not None:
                departures.append((window, ended, start_sample))
        if (
            window.get("reliable_start") is True
            and
            started is not None
            and tour_end - ARRIVAL_BEFORE
            <= started
            <= tour_end + ARRIVAL_AFTER
        ):
            end_sample = _sample(window, "first_sample")
            if end_sample is not None:
                arrivals.append((window, started, end_sample))

    candidates: list[RideContactMatch] = []
    for departure, departure_at, start_sample in departures:
        departure_id = _window_id(departure)
        if departure_id is None:
            continue
        for arrival, arrival_at, end_sample in arrivals:
            arrival_id = _window_id(arrival)
            if arrival_id is None or arrival_id == departure_id:
                continue
            if arrival_at <= departure_at:
                continue

            start_odo = _number(start_sample.get("odometer_km"))
            end_odo = _number(end_sample.get("odometer_km"))
            if start_odo is None or end_odo is None:
                continue
            session_km = end_odo - start_odo
            gpx_km = tour_distance_m / 1000.0
            if session_km < MIN_SESSION_KM:
                continue
            ratio = session_km / gpx_km
            if not MIN_DISTANCE_RATIO <= ratio <= MAX_DISTANCE_RATIO:
                continue

            score = abs((departure_at - tour_start).total_seconds())
            score += abs((arrival_at - tour_end).total_seconds())
            candidates.append(
                RideContactMatch(
                    departure_id=departure_id,
                    arrival_id=arrival_id,
                    start_sample=start_sample,
                    end_sample=end_sample,
                    score_seconds=score,
                )
            )

    if not candidates:
        return RideMatchDecision("unmatched", reason="no_plausible_contact_pair")

    candidates.sort(key=lambda candidate: candidate.score_seconds)
    if (
        len(candidates) > 1
        and candidates[1].score_seconds - candidates[0].score_seconds
        < AMBIGUITY_SECONDS
    ):
        return RideMatchDecision("ambiguous", reason="multiple_contact_pairs")

    return RideMatchDecision("matched", match=candidates[0])


def consumption_from_match(
    match: RideContactMatch,
    *,
    capacity_wh: float,
    activity_distance_m: float,
) -> dict[str, Any] | None:
    """Derive whole-tour consumption for the personal range estimate.

    The activity distance is the complete Komoot route, including sections
    ridden without motor support. A one-percent minimum avoids pretending
    that the integer SoC sensor is more precise than it is.
    """
    if capacity_wh <= 0 or activity_distance_m <= 0:
        return None
    start_soc = _number(match.start_sample.get("soc"))
    end_soc = _number(match.end_sample.get("soc"))
    start_odo = _number(match.start_sample.get("odometer_km"))
    end_odo = _number(match.end_sample.get("odometer_km"))
    if None in (start_soc, end_soc, start_odo, end_odo):
        return None

    percentage = start_soc - end_soc
    session_km = end_odo - start_odo
    activity_km = activity_distance_m / 1000.0
    if percentage < 1.0 or percentage > 90.0 or session_km < MIN_SESSION_KM:
        return None
    ratio = session_km / activity_km
    if not MIN_DISTANCE_RATIO <= ratio <= MAX_DISTANCE_RATIO:
        return None

    return {
        "consumed_wh": round(capacity_wh * percentage / 100.0, 1),
        "percentage": round(percentage, 1),
        "capacity_wh": round(capacity_wh, 1),
        "start_soc": round(start_soc, 1),
        "end_soc": round(end_soc, 1),
        "session_distance_m": round(session_km * 1000.0, 1),
        "source": "komoot_ble_journal",
        "departure_contact_id": match.departure_id,
        "arrival_contact_id": match.arrival_id,
    }
