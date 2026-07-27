"""Detect and describe user-visible changes to imported Komoot tours."""

from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Any


ROUTE_SAMPLE_COUNT = 64


def _finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _normalised_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _nested_number(record: dict[str, Any], *path: str) -> float | None:
    value: Any = record
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return _finite_number(value)


def _number_changed(
    old: float | None,
    new: float | None,
    *,
    absolute: float,
    relative: float,
) -> bool:
    if new is None:
        return False
    if old is None:
        return True
    threshold = max(absolute, max(abs(old), abs(new)) * relative)
    return abs(new - old) >= threshold


def _change(
    field: str,
    label: str,
    old: Any,
    new: Any,
    unit: str | None = None,
) -> dict[str, Any]:
    result = {"field": field, "label": label, "old": old, "new": new}
    if unit:
        result["unit"] = unit
    return result


def _haversine_m(
    a_lat: float, a_lon: float, b_lat: float, b_lon: float
) -> float:
    radius = 6_371_000.0
    p1 = math.radians(a_lat)
    p2 = math.radians(b_lat)
    dp = math.radians(b_lat - a_lat)
    dl = math.radians(b_lon - a_lon)
    h = (
        math.sin(dp / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    )
    return 2 * radius * math.asin(min(1.0, math.sqrt(h)))


def _route_points(record: dict[str, Any]) -> list[tuple[float, float]]:
    result: list[tuple[float, float]] = []
    for point in record.get("points") or []:
        if not isinstance(point, dict):
            continue
        lat = _finite_number(point.get("lat"))
        lon = _finite_number(point.get("lon"))
        if lat is None or lon is None:
            continue
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            result.append((lat, lon))
    return result


def _resample_route(
    points: list[tuple[float, float]], count: int = ROUTE_SAMPLE_COUNT
) -> list[tuple[float, float]]:
    """Resample by travelled distance so point-density changes stay silent."""
    if len(points) < 2:
        return []

    cumulative = [0.0]
    for previous, current in zip(points, points[1:]):
        cumulative.append(
            cumulative[-1]
            + _haversine_m(previous[0], previous[1], current[0], current[1])
        )
    total = cumulative[-1]
    if total <= 0:
        return [points[0], points[-1]]

    samples: list[tuple[float, float]] = []
    segment = 1
    for index in range(count):
        target = total * index / (count - 1)
        while segment < len(cumulative) - 1 and cumulative[segment] < target:
            segment += 1
        before_distance = cumulative[segment - 1]
        after_distance = cumulative[segment]
        before = points[segment - 1]
        after = points[segment]
        span = after_distance - before_distance
        ratio = 0.0 if span <= 0 else (target - before_distance) / span
        samples.append(
            (
                before[0] + (after[0] - before[0]) * ratio,
                before[1] + (after[1] - before[1]) * ratio,
            )
        )
    return samples


def route_geometry_changed(
    old_record: dict[str, Any], new_record: dict[str, Any]
) -> bool:
    """Ignore GPS jitter and point density, but detect a real route change."""
    old_samples = _resample_route(_route_points(old_record))
    new_samples = _resample_route(_route_points(new_record))
    if not old_samples or not new_samples:
        return False

    deviations = [
        _haversine_m(old[0], old[1], new[0], new[1])
        for old, new in zip(old_samples, new_samples)
    ]
    if deviations[0] > 50 or deviations[-1] > 50:
        return True

    over_30 = sum(value > 30 for value in deviations)
    over_100 = sum(value > 100 for value in deviations)
    return over_30 >= max(3, math.ceil(len(deviations) * 0.10)) or over_100 >= max(
        2, math.ceil(len(deviations) * 0.03)
    )


def material_tour_changes(
    old_record: dict[str, Any], new_record: dict[str, Any]
) -> list[dict[str, Any]]:
    """Return only changes a rider can reasonably see or care about."""
    changes: list[dict[str, Any]] = []

    old_title = _normalised_text(old_record.get("title"))
    new_title = _normalised_text(new_record.get("title"))
    if new_title and old_title != new_title:
        changes.append(_change("title", "Titel", old_title or None, new_title))

    old_start = _parse_time(old_record.get("start_time"))
    new_start = _parse_time(new_record.get("start_time"))
    if new_start is not None and (
        old_start is None or abs((new_start - old_start).total_seconds()) >= 60
    ):
        changes.append(
            _change(
                "start_time",
                "Startzeit",
                old_record.get("start_time"),
                new_record.get("start_time"),
            )
        )

    numeric_fields = (
        ("distance", "Distanz", ("distance",), 50.0, 0.001, "m"),
        (
            "duration",
            "Fahrzeit",
            ("duration_without_stops",),
            30.0,
            0.005,
            "s",
        ),
        (
            "elevation_gain",
            "Anstieg",
            ("elevation", "gain"),
            10.0,
            0.01,
            "m",
        ),
        (
            "elevation_loss",
            "Abstieg",
            ("elevation", "loss"),
            10.0,
            0.01,
            "m",
        ),
        (
            "speed_average",
            "Ø Tempo",
            ("speed", "average"),
            0.2,
            0.005,
            "km/h",
        ),
        (
            "speed_maximum",
            "Max. Tempo",
            ("speed", "maximum"),
            0.2,
            0.005,
            "km/h",
        ),
    )
    for field, label, path, absolute, relative, unit in numeric_fields:
        old = _nested_number(old_record, *path)
        new = _nested_number(new_record, *path)
        if _number_changed(old, new, absolute=absolute, relative=relative):
            changes.append(_change(field, label, old, new, unit))

    if route_geometry_changed(old_record, new_record):
        changes.append(
            _change("route", "Streckenverlauf", None, "geändert")
        )
    return changes


def material_consumption_change(
    old_value: Any, new_value: Any
) -> dict[str, Any] | None:
    """Return a relevant battery-consumption change, if one exists."""
    if not isinstance(new_value, dict):
        return None
    old = old_value if isinstance(old_value, dict) else {}
    old_percentage = _finite_number(old.get("percentage"))
    new_percentage = _finite_number(new_value.get("percentage"))
    old_wh = _finite_number(old.get("consumed_wh"))
    new_wh = _finite_number(new_value.get("consumed_wh"))
    if not old:
        relevant = new_percentage is not None or new_wh is not None
    else:
        relevant = _number_changed(
            old_percentage, new_percentage, absolute=1.0, relative=0.0
        ) or _number_changed(old_wh, new_wh, absolute=5.0, relative=0.0)
    if not relevant:
        return None
    return _change(
        "consumption",
        "Akkuverbrauch",
        {"percentage": old_percentage, "consumed_wh": old_wh} if old else None,
        {"percentage": new_percentage, "consumed_wh": new_wh},
    )


def _german_number(value: float, decimals: int = 1) -> str:
    return f"{value:.{decimals}f}".replace(".", ",")


def _format_value(field: str, value: Any) -> str:
    number = _finite_number(value)
    if number is None:
        return "unbekannt"
    if field == "distance":
        return f"{_german_number(number / 1000)} km"
    if field == "duration":
        return f"{round(number / 60)} min"
    if field in {"elevation_gain", "elevation_loss"}:
        return f"{round(number)} m"
    if field in {"speed_average", "speed_maximum"}:
        return f"{_german_number(number)} km/h"
    return _german_number(number)


def format_change_summary(changes: list[dict[str, Any]]) -> str:
    """Format structured, coordinate-free changes for a phone notification."""
    parts: list[str] = []
    for change in changes:
        field = str(change.get("field") or "")
        label = str(change.get("label") or field)
        old = change.get("old")
        new = change.get("new")
        if field == "route":
            parts.append("Streckenverlauf geändert")
        elif field == "title":
            if old:
                parts.append(f'Titel „{old}“ → „{new}“')
            else:
                parts.append(f'Titel ergänzt: „{new}“')
        elif field == "start_time":
            parts.append("Startzeit geändert")
        elif field == "consumption":
            old_consumption = old if isinstance(old, dict) else {}
            new_consumption = new if isinstance(new, dict) else {}
            new_percentage = _finite_number(new_consumption.get("percentage"))
            new_wh = _finite_number(new_consumption.get("consumed_wh"))
            if not old_consumption:
                values = []
                if new_percentage is not None:
                    values.append(f"{_german_number(new_percentage, 0)} %")
                if new_wh is not None:
                    values.append(f"{_german_number(new_wh, 0)} Wh")
                parts.append(f"Akkuverbrauch ergänzt: {' · '.join(values)}")
            else:
                old_percentage = _finite_number(old_consumption.get("percentage"))
                old_wh = _finite_number(old_consumption.get("consumed_wh"))
                values = []
                if old_percentage is not None and new_percentage is not None:
                    values.append(
                        f"{_german_number(old_percentage, 0)} → "
                        f"{_german_number(new_percentage, 0)} %"
                    )
                if old_wh is not None and new_wh is not None:
                    values.append(
                        f"{_german_number(old_wh, 0)} → "
                        f"{_german_number(new_wh, 0)} Wh"
                    )
                parts.append(f"Akkuverbrauch: {' · '.join(values)}")
        else:
            parts.append(
                f"{label} {_format_value(field, old)} → "
                f"{_format_value(field, new)}"
            )
    return " · ".join(parts)


def build_notification_summary(tours: list[dict[str, Any]]) -> str:
    """Build one compact German summary without exposing coordinates."""

    def describe(tour: dict[str, Any]) -> str:
        title = _normalised_text(tour.get("title")) or "Komoot-Tour"
        kind = tour.get("kind")
        if kind == "created":
            distance = _finite_number(tour.get("distance"))
            suffix = (
                f" · {_german_number(distance / 1000)} km"
                if distance is not None
                else ""
            )
            return f'Neue Tour „{title}“{suffix}'
        summary = format_change_summary(list(tour.get("changes") or []))
        if summary.startswith("Akkuverbrauch"):
            return f'„{title}“: {summary}'
        return f'„{title}“ geändert: {summary}'

    descriptions = [describe(tour) for tour in tours[:3]]
    if len(tours) <= 1:
        return descriptions[0] if descriptions else ""
    result = f"{len(tours)} Komoot-Touren aktualisiert:\n" + "\n".join(
        f"• {description}" for description in descriptions
    )
    if len(tours) > 3:
        result += f"\n• +{len(tours) - 3} weitere"
    return result
