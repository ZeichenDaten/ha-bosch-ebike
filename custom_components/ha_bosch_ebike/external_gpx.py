"""Persistent Komoot GPX imports for the Bosch eBike dashboard cards."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import PurePath
from typing import Any
import uuid
import xml.etree.ElementTree as ET

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN

DATA_TRACKS = "external_gpx_tracks"
DATA_STORE = "external_gpx_store"
DATA_TITLE_OVERRIDES = "activity_title_overrides"
DATA_TITLE_STORE = "activity_title_store"
DATA_IGNORED_PROVIDER_IDS = "external_gpx_ignored_provider_ids"
DATA_IGNORED_PROVIDER_STORE = "external_gpx_ignored_provider_store"
STORE_VERSION = 1
STORE_KEY = f"{DOMAIN}_external_gpx_tracks"
TITLE_STORE_KEY = f"{DOMAIN}_activity_title_overrides"
IGNORED_PROVIDER_STORE_KEY = f"{DOMAIN}_external_gpx_ignored_provider_ids"
MAX_FILE_BYTES = 5_000_000
MAX_RAW_POINTS = 100_000
MAX_STORED_POINTS = 20_000
MAX_TRACKS = 250
SPEED_SMOOTHING_SECONDS = 5.0


async def async_setup_external_gpx(hass: HomeAssistant) -> None:
    """Load GPX tracks and register the websocket API once."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    if DATA_STORE in domain_data:
        return

    store = Store(hass, STORE_VERSION, STORE_KEY)
    loaded = await store.async_load()
    tracks = loaded if isinstance(loaded, list) else []
    domain_data[DATA_STORE] = store
    domain_data[DATA_TRACKS] = [item for item in tracks if isinstance(item, dict)]

    title_store = Store(hass, STORE_VERSION, TITLE_STORE_KEY)
    loaded_titles = await title_store.async_load()
    domain_data[DATA_TITLE_STORE] = title_store
    domain_data[DATA_TITLE_OVERRIDES] = (
        {
            str(activity_id): _clean_text(title)
            for activity_id, title in loaded_titles.items()
            if str(activity_id).strip() and _clean_text(title)
        }
        if isinstance(loaded_titles, dict)
        else {}
    )

    ignored_store = Store(hass, STORE_VERSION, IGNORED_PROVIDER_STORE_KEY)
    loaded_ignored = await ignored_store.async_load()
    domain_data[DATA_IGNORED_PROVIDER_STORE] = ignored_store
    domain_data[DATA_IGNORED_PROVIDER_IDS] = (
        {
            str(value)
            for value in loaded_ignored
            if isinstance(value, (str, int)) and str(value)
        }
        if isinstance(loaded_ignored, list)
        else set()
    )

    websocket_api.async_register_command(hass, ws_import_gpx)
    websocket_api.async_register_command(hass, ws_list_imported_gpx)
    websocket_api.async_register_command(hass, ws_delete_imported_gpx)
    websocket_api.async_register_command(hass, ws_set_activity_title)
    websocket_api.async_register_command(
        hass, ws_set_imported_gpx_consumption
    )


def _tracks(hass: HomeAssistant) -> list[dict[str, Any]]:
    data = hass.data.setdefault(DOMAIN, {})
    tracks = data.get(DATA_TRACKS)
    if not isinstance(tracks, list):
        tracks = []
        data[DATA_TRACKS] = tracks
    return tracks


async def _save(hass: HomeAssistant) -> None:
    store = hass.data.get(DOMAIN, {}).get(DATA_STORE)
    if store is not None:
        await store.async_save(_tracks(hass))


def _title_overrides(hass: HomeAssistant) -> dict[str, str]:
    data = hass.data.setdefault(DOMAIN, {})
    overrides = data.get(DATA_TITLE_OVERRIDES)
    if not isinstance(overrides, dict):
        overrides = {}
        data[DATA_TITLE_OVERRIDES] = overrides
    return overrides


async def _save_title_overrides(hass: HomeAssistant) -> None:
    store = hass.data.get(DOMAIN, {}).get(DATA_TITLE_STORE)
    if store is not None:
        await store.async_save(_title_overrides(hass))


def _ignored_provider_ids(hass: HomeAssistant) -> set[str]:
    data = hass.data.setdefault(DOMAIN, {})
    ignored = data.get(DATA_IGNORED_PROVIDER_IDS)
    if not isinstance(ignored, set):
        ignored = set()
        data[DATA_IGNORED_PROVIDER_IDS] = ignored
    return ignored


async def _save_ignored_provider_ids(hass: HomeAssistant) -> None:
    store = hass.data.get(DOMAIN, {}).get(DATA_IGNORED_PROVIDER_STORE)
    if store is not None:
        await store.async_save(sorted(_ignored_provider_ids(hass)))


def provider_import_is_ignored(
    hass: HomeAssistant, provider: str, provider_id: str
) -> bool:
    """Return whether the user deleted and suppressed an automatic import."""
    return f"{provider}:{provider_id}" in _ignored_provider_ids(hass)


def provider_record(
    hass: HomeAssistant, provider: str, provider_id: str
) -> dict[str, Any] | None:
    """Return one provider-managed record by its stable remote identity."""
    return next(
        (
            item
            for item in _tracks(hass)
            if item.get("provider") == provider
            and str(item.get("provider_id")) == str(provider_id)
        ),
        None,
    )


async def async_set_provider_consumption(
    hass: HomeAssistant,
    *,
    provider: str,
    provider_id: str,
    consumption: dict[str, Any] | None,
) -> bool:
    """Set or clear matched consumption without re-downloading the GPX."""
    record = provider_record(hass, provider, provider_id)
    if record is None:
        return False
    old = record.get("consumption")
    if isinstance(consumption, dict):
        new_value = dict(consumption)
        if old == new_value:
            return False
        record["consumption"] = new_value
    else:
        if "consumption" not in record:
            return False
        record.pop("consumption", None)
    await _save(hass)
    return True


def _replace_provider_record(
    existing: dict[str, Any],
    replacement: dict[str, Any],
    consumption: dict[str, Any] | None,
) -> None:
    """Replace provider metadata without discarding confirmed consumption."""
    previous = existing.get("consumption")
    retained = (
        dict(consumption)
        if isinstance(consumption, dict)
        else dict(previous)
        if isinstance(previous, dict)
        else None
    )
    existing.clear()
    existing.update(replacement)
    if retained is not None:
        existing["consumption"] = retained


def _verified_manual_consumption(
    record: dict[str, Any],
    *,
    start_soc: float,
    end_soc: float,
    capacity_wh: float,
    session_distance_m: float,
) -> dict[str, Any] | None:
    """Build a manually verified value using the automatic safety bounds."""
    try:
        activity_distance_m = float(record.get("distance") or 0)
    except (TypeError, ValueError):
        return None
    percentage = start_soc - end_soc
    if (
        activity_distance_m <= 0
        or percentage < 1.0
        or percentage > 90.0
        or capacity_wh <= 0
        or session_distance_m < 300.0
    ):
        return None
    ratio = session_distance_m / activity_distance_m
    if not 0.55 <= ratio <= 1.8:
        return None
    return {
        "consumed_wh": round(capacity_wh * percentage / 100.0, 1),
        "percentage": round(percentage, 1),
        "capacity_wh": round(capacity_wh, 1),
        "start_soc": round(start_soc, 1),
        "end_soc": round(end_soc, 1),
        "session_distance_m": round(session_distance_m, 1),
        "source": "komoot_ble_journal",
        "verified_manual": True,
    }


def _record_storage_bytes(record: dict[str, Any]) -> int:
    """Return the compact UTF-8 size of one persisted record."""
    return len(
        json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )


def apply_activity_title_overrides(
    hass: HomeAssistant, activities: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Apply local display names without changing Bosch or Komoot source data."""
    overrides = _title_overrides(hass)
    result: list[dict[str, Any]] = []
    for item in activities:
        entry = dict(item)
        activity_id = str(entry.get("id") or entry.get("activity_id") or "")
        original_title = _clean_text(entry.get("title"), "Tour")
        custom_title = overrides.get(activity_id)
        entry["originalTitle"] = original_title
        entry["titleOverridden"] = bool(custom_title)
        if custom_title:
            entry["title"] = custom_title
        result.append(entry)
    return result


def _clean_text(value: Any, fallback: str = "", limit: int = 160) -> str:
    text = str(value or "").strip()
    return (text or fallback)[:limit]


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str:
    return value.isoformat().replace("+00:00", "Z") if value else ""


def _haversine_m(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
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


def _first_text(root: ET.Element, paths: tuple[str, ...]) -> str:
    for path in paths:
        node = root.find(path)
        if node is not None and node.text and node.text.strip():
            return node.text.strip()
    return ""


def _parse_gpx(
    content: str, filename: str, start_override: str | None
) -> dict[str, Any]:
    encoded = content.encode("utf-8")
    if not content or len(encoded) > MAX_FILE_BYTES:
        raise vol.Invalid("GPX file is empty or larger than 5 MB")
    if "<!DOCTYPE" in content.upper():
        raise vol.Invalid("GPX files with a DOCTYPE are not accepted")

    try:
        root = ET.fromstring(content)
    except ET.ParseError as err:
        raise vol.Invalid(f"Invalid GPX XML: {err}") from err

    nodes = root.findall(".//{*}trkpt")
    if len(nodes) < 2:
        raise vol.Invalid("GPX must contain at least two track points")
    if len(nodes) > MAX_RAW_POINTS:
        raise vol.Invalid(f"GPX contains more than {MAX_RAW_POINTS} track points")

    raw: list[dict[str, Any]] = []
    for node in nodes:
        try:
            lat = float(node.attrib["lat"])
            lon = float(node.attrib["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        ele_node = node.find("{*}ele")
        time_node = node.find("{*}time")
        try:
            ele = (
                float(ele_node.text)
                if ele_node is not None and ele_node.text
                else None
            )
        except (TypeError, ValueError):
            ele = None
        when = _parse_time(time_node.text if time_node is not None else None)
        raw.append({"lat": lat, "lon": lon, "ele": ele, "_time": when})

    if len(raw) < 2:
        raise vol.Invalid("GPX has fewer than two valid coordinate points")

    total_m = 0.0
    moving_s = 0.0
    elevation_gain = 0.0
    elevation_loss = 0.0
    max_kmh = 0.0
    previous = raw[0]
    previous_ele = previous.get("ele")
    previous["distance"] = 0.0
    previous["speed"] = None

    for point in raw[1:]:
        segment_m = _haversine_m(
            previous["lat"], previous["lon"], point["lat"], point["lon"]
        )
        if math.isfinite(segment_m) and segment_m < 20_000:
            total_m += segment_m
        point["distance"] = round(total_m, 1)

        speed_kmh = None
        before = previous.get("_time")
        current = point.get("_time")
        if before and current:
            seconds = (current - before).total_seconds()
            if 0 < seconds <= 600:
                speed_kmh = segment_m / seconds * 3.6
                if speed_kmh >= 1.8:
                    moving_s += seconds
        point["speed"] = round(speed_kmh, 2) if speed_kmh is not None else None

        ele = point.get("ele")
        if ele is not None and previous_ele is not None:
            rise = ele - previous_ele
            if 0.0 < rise <= 100.0:
                elevation_gain += rise
            elif -100.0 <= rise < 0.0:
                elevation_loss -= rise
        if ele is not None:
            previous_ele = ele
        previous = point

    # A single GPS jump can produce an unrealistic one-segment peak. Measure
    # maximum speed over the shortest available window of at least five seconds.
    for end_index in range(1, len(raw)):
        end_time = raw[end_index].get("_time")
        if end_time is None:
            continue
        start_index = end_index - 1
        while start_index >= 0:
            start_time = raw[start_index].get("_time")
            if start_time is None:
                start_index -= 1
                continue
            seconds = (end_time - start_time).total_seconds()
            if seconds >= SPEED_SMOOTHING_SECONDS:
                distance_delta = float(
                    raw[end_index].get("distance", 0.0)
                ) - float(raw[start_index].get("distance", 0.0))
                if 0 < seconds <= 600 and distance_delta >= 0:
                    max_kmh = max(max_kmh, distance_delta / seconds * 3.6)
                break
            start_index -= 1

    first_time = next((p.get("_time") for p in raw if p.get("_time")), None)
    last_time = next(
        (p.get("_time") for p in reversed(raw) if p.get("_time")), None
    )
    duration_s = (
        max(0.0, (last_time - first_time).total_seconds())
        if first_time and last_time
        else 0.0
    )
    if moving_s <= 0:
        moving_s = duration_s

    if len(raw) > MAX_STORED_POINTS:
        stride = math.ceil(len(raw) / MAX_STORED_POINTS)
        sampled = raw[::stride]
        if sampled[-1] is not raw[-1]:
            sampled.append(raw[-1])
    else:
        sampled = raw

    points = [
        {
            "lat": round(float(p["lat"]), 7),
            "lon": round(float(p["lon"]), 7),
            "ele": (
                round(float(p["ele"]), 1) if p.get("ele") is not None else None
            ),
            "speed": p.get("speed"),
            "distance": p.get("distance"),
        }
        for p in sampled
    ]

    fallback_name = PurePath(filename or "Komoot-Tour.gpx").stem
    title = _first_text(
        root,
        (
            ".//{*}trk/{*}name",
            ".//{*}metadata/{*}name",
            ".//{*}name",
        ),
    )
    override = _parse_time(start_override)
    start_time = first_time or override
    average_kmh = total_m / moving_s * 3.6 if moving_s > 0 else 0.0

    return {
        "title": _clean_text(title, fallback_name),
        "start_time": _iso(start_time),
        "end_time": _iso(last_time),
        "distance": round(total_m, 1),
        "duration_without_stops": round(moving_s),
        "speed": {
            "average": round(average_kmh, 2) if average_kmh > 0 else None,
            "maximum": round(max_kmh, 2) if max_kmh > 0 else None,
        },
        "elevation": {
            "gain": round(elevation_gain, 1),
            "loss": round(elevation_loss, 1),
        },
        "points": points,
        "point_count": len(points),
        "original_point_count": len(raw),
        "content_hash": hashlib.sha256(encoded).hexdigest(),
        "source_bytes": len(encoded),
    }


def _effective_activity_id(record: dict[str, Any]) -> str:
    return record.get("activity_id") or f"komoot:{record.get('id')}"


def _public_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": record.get("id"),
        "activity_id": record.get("activity_id"),
        "effective_activity_id": _effective_activity_id(record),
        "bike_id": record.get("bike_id"),
        "title": record.get("title"),
        "filename": record.get("filename"),
        "start_time": record.get("start_time"),
        "distance": record.get("distance"),
        "point_count": record.get(
            "point_count", len(record.get("points") or [])
        ),
        "source_bytes": record.get("source_bytes"),
        "storage_bytes": _record_storage_bytes(record),
        "imported_at": record.get("imported_at"),
        "source": record.get("source"),
        "provider": record.get("provider"),
        "provider_id": record.get("provider_id"),
        "provider_changed_at": record.get("provider_changed_at"),
        "has_consumption": isinstance(record.get("consumption"), dict),
        "linked": bool(record.get("activity_id")),
    }


def external_track_for_activity(
    hass: HomeAssistant, activity_id: str
) -> dict[str, Any] | None:
    """Find a linked or standalone imported track for a card activity id."""
    for record in _tracks(hass):
        if _effective_activity_id(record) == activity_id:
            return record
    return None


def external_activity_entries(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Expose standalone GPX imports as normal card activities."""
    result = []
    for record in _tracks(hass):
        if record.get("activity_id"):
            continue
        result.append(
            {
                "id": _effective_activity_id(record),
                "title": record.get("title", "Komoot-Tour"),
                "startTime": record.get("start_time", ""),
                "endTime": record.get("end_time", ""),
                "distance": record.get("distance", 0),
                "durationWithoutStops": record.get(
                    "duration_without_stops", 0
                ),
                "speed": record.get("speed", {}),
                "elevation": record.get("elevation", {}),
                "caloriesBurned": None,
                "accountId": "komoot-gpx",
                "accountLabel": "Komoot GPX",
                "bikeId": record.get("bike_id"),
                "source": "komoot_gpx",
            }
        )
    return result


def external_activity_consumption(
    hass: HomeAssistant,
) -> dict[str, dict[str, Any]]:
    """Consumption values derived from a matched bridge contact pair."""
    result: dict[str, dict[str, Any]] = {}
    for record in _tracks(hass):
        consumption = record.get("consumption")
        if isinstance(consumption, dict):
            result[_effective_activity_id(record)] = dict(consumption)
    return result


def external_activity_bikes(hass: HomeAssistant) -> dict[str, str]:
    """Bike attribution for standalone automatic/manual GPX activities."""
    result: dict[str, str] = {}
    for record in _tracks(hass):
        bike_id = record.get("bike_id")
        if isinstance(bike_id, str) and bike_id:
            result[_effective_activity_id(record)] = bike_id
    return result


def _metadata_number(metadata: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = metadata.get(key)
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(parsed):
            return parsed
    return None


def _apply_authoritative_metadata(
    parsed: dict[str, Any], metadata: dict[str, Any] | None
) -> dict[str, Any]:
    """Prefer Komoot's recorded-tour metrics over values recomputed from GPX."""
    result = dict(parsed)
    if not isinstance(metadata, dict):
        return result

    title = _clean_text(
        metadata.get("title") or metadata.get("name"),
        fallback=result.get("title", "Komoot-Tour"),
    )
    if title:
        result["title"] = title

    start_time = (
        metadata.get("start_time")
        or metadata.get("startTime")
        or metadata.get("date")
    )
    end_time = metadata.get("end_time") or metadata.get("endTime")
    if _parse_time(start_time):
        result["start_time"] = _iso(_parse_time(start_time))
    if _parse_time(end_time):
        result["end_time"] = _iso(_parse_time(end_time))

    distance = _metadata_number(metadata, "distance", "distance_m")
    if distance is not None and 0 < distance <= 500_000:
        result["distance"] = round(distance, 1)

    duration = _metadata_number(
        metadata,
        "duration_without_stops",
        "duration_active",
        "duration_moving",
    )
    if duration is not None and 0 < duration <= 7 * 24 * 3600:
        result["duration_without_stops"] = round(duration)

    speed = dict(result.get("speed") or {})
    average = _metadata_number(metadata, "speed_average", "average_speed")
    maximum = _metadata_number(metadata, "speed_maximum", "max_speed")
    if average is not None and 0 < average <= 150:
        speed["average"] = round(average, 2)
    if maximum is not None and 0 < maximum <= 200:
        speed["maximum"] = round(maximum, 2)
    result["speed"] = speed

    elevation = dict(result.get("elevation") or {})
    gain = _metadata_number(metadata, "elevation_gain", "elevation_up")
    loss = _metadata_number(metadata, "elevation_loss", "elevation_down")
    if gain is not None and 0 <= gain <= 20_000:
        elevation["gain"] = round(gain, 1)
    if loss is not None and 0 <= loss <= 20_000:
        elevation["loss"] = round(loss, 1)
    result["elevation"] = elevation
    return result


async def async_upsert_provider_gpx(
    hass: HomeAssistant,
    *,
    provider: str,
    provider_id: str,
    provider_changed_at: str | None,
    gpx_content: str,
    filename: str,
    bike_id: str,
    metadata: dict[str, Any] | None = None,
    activity_id: str | None = None,
    consumption: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create or update one automatic provider import idempotently."""
    provider = _clean_text(provider, limit=40)
    provider_id = _clean_text(provider_id, limit=120)
    if not provider or not provider_id:
        raise vol.Invalid("Provider and provider ID are required")
    if provider_import_is_ignored(hass, provider, provider_id):
        return {"status": "ignored", "record": None}

    tracks = _tracks(hass)
    existing = next(
        (
            item
            for item in tracks
            if item.get("provider") == provider
            and str(item.get("provider_id")) == provider_id
        ),
        None,
    )
    if (
        existing is not None
        and provider_changed_at
        and existing.get("provider_changed_at") == provider_changed_at
    ):
        return {"status": "unchanged", "record": existing}

    parsed = _parse_gpx(gpx_content, filename, None)
    parsed = _apply_authoritative_metadata(parsed, metadata)

    # A previously manual standalone import of the exact same GPX becomes the
    # provider-managed record instead of appearing twice on the dashboard.
    if existing is None:
        existing = next(
            (
                item
                for item in tracks
                if item.get("content_hash") == parsed["content_hash"]
                and (
                    not item.get("activity_id")
                    or item.get("activity_id") == activity_id
                )
            ),
            None,
        )
    if existing is None and activity_id:
        existing = next(
            (item for item in tracks if item.get("activity_id") == activity_id),
            None,
        )
    if existing is None:
        parsed_start = _parse_time(parsed.get("start_time"))
        parsed_distance = float(parsed.get("distance") or 0)
        close_matches = []
        for item in tracks:
            if item.get("provider") or item.get("activity_id"):
                continue
            item_start = _parse_time(item.get("start_time"))
            item_distance = float(item.get("distance") or 0)
            if parsed_start is None or item_start is None:
                continue
            if abs((parsed_start - item_start).total_seconds()) > 120:
                continue
            if abs(parsed_distance - item_distance) > max(
                300.0, parsed_distance * 0.03
            ):
                continue
            close_matches.append(item)
        if len(close_matches) == 1:
            existing = close_matches[0]

    now_iso = dt_util.utcnow().isoformat()
    status = "updated" if existing is not None else "created"
    if existing is None:
        if len(tracks) >= MAX_TRACKS:
            raise vol.Invalid(f"Maximum of {MAX_TRACKS} imports reached")
        existing = {
            "id": uuid.uuid4().hex[:12],
            "imported_at": now_iso,
        }
        tracks.append(existing)

    record_id = existing["id"]
    imported_at = existing.get("imported_at") or now_iso
    _replace_provider_record(
        existing,
        {
            "id": record_id,
            "activity_id": activity_id,
            "bike_id": _clean_text(bike_id, limit=80),
            "filename": _clean_text(filename, "Komoot-Tour.gpx"),
            "imported_at": imported_at,
            "updated_at": now_iso,
            "source": f"{provider}_auto",
            "provider": provider,
            "provider_id": provider_id,
            "provider_changed_at": provider_changed_at,
            **parsed,
        },
        consumption,
    )
    await _save(hass)
    return {"status": status, "record": existing}


def _within_range(
    start_value: str | None,
    max_age_days: int | None,
    date_from: str | None,
    date_to: str | None,
) -> bool:
    start = _parse_time(start_value)
    if start is None:
        return True
    try:
        if date_from:
            lower = datetime.fromisoformat(date_from).replace(
                tzinfo=timezone.utc
            )
            if start < lower:
                return False
        if date_to:
            upper = datetime.fromisoformat(date_to).replace(
                tzinfo=timezone.utc
            ) + timedelta(days=1)
            if start >= upper:
                return False
    except (TypeError, ValueError):
        pass
    if not date_from and not date_to and max_age_days and max_age_days > 0:
        return start >= datetime.now(timezone.utc) - timedelta(
            days=max_age_days
        )
    return True


def merge_external_track_entries(
    hass: HomeAssistant,
    existing: list[dict[str, Any]],
    *,
    max_age_days: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict[str, Any]]:
    """Append imported tracks only where Bosch has no track for that activity."""
    result = list(existing)
    existing_ids = {item.get("activity_id") for item in result}
    for record in _tracks(hass):
        activity_id = _effective_activity_id(record)
        if activity_id in existing_ids:
            continue
        if not _within_range(
            record.get("start_time"), max_age_days, date_from, date_to
        ):
            continue
        result.append(
            {
                "activity_id": activity_id,
                "account_id": "komoot-gpx",
                "account_label": "Komoot GPX",
                "bike_id": record.get("bike_id"),
                "title": record.get("title", "Komoot-Tour"),
                "start_time": record.get("start_time", ""),
                "distance": record.get("distance", 0),
                "points": record.get("points", []),
                "source": "komoot_gpx",
            }
        )
        existing_ids.add(activity_id)
    return result


@websocket_api.websocket_command(
    {
        vol.Required("type"): "bosch_ebike/import_gpx",
        vol.Required("gpx"): str,
        vol.Required("bike_id"): str,
        vol.Optional("activity_id"): vol.Any(str, None),
        vol.Optional("filename", default="Komoot-Tour.gpx"): str,
        vol.Optional("start_time"): vol.Any(str, None),
    }
)
@websocket_api.async_response
async def ws_import_gpx(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Import one GPX file, linked to a Bosch activity or standalone."""
    tracks = _tracks(hass)
    if len(tracks) >= MAX_TRACKS:
        connection.send_error(
            msg["id"],
            "limit_reached",
            f"Maximum of {MAX_TRACKS} imports reached",
        )
        return

    try:
        parsed = _parse_gpx(
            msg["gpx"], msg.get("filename", ""), msg.get("start_time")
        )
    except vol.Invalid as err:
        connection.send_error(msg["id"], "invalid_gpx", str(err))
        return

    activity_id = (msg.get("activity_id") or "").strip() or None
    if any(
        item.get("content_hash") == parsed["content_hash"]
        and (item.get("activity_id") or None) == activity_id
        for item in tracks
    ):
        connection.send_error(
            msg["id"],
            "duplicate",
            "This GPX file is already imported for the selected tour",
        )
        return
    if activity_id and any(
        item.get("activity_id") == activity_id for item in tracks
    ):
        connection.send_error(
            msg["id"],
            "target_exists",
            "The selected Bosch tour already has an imported GPX track",
        )
        return

    record = {
        "id": uuid.uuid4().hex[:12],
        "activity_id": activity_id,
        "bike_id": _clean_text(msg["bike_id"], limit=80),
        "filename": _clean_text(
            msg.get("filename"), "Komoot-Tour.gpx"
        ),
        "imported_at": dt_util.utcnow().isoformat(),
        "source": "komoot_gpx",
        **parsed,
    }
    tracks.append(record)
    await _save(hass)
    connection.send_result(
        msg["id"], {"track": _public_record(record), "count": len(tracks)}
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): "bosch_ebike/set_activity_title",
        vol.Required("activity_id"): str,
        vol.Optional("title", default=""): str,
    }
)
@websocket_api.async_response
async def ws_set_activity_title(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Set or clear a local display name for one activity."""
    activity_id = _clean_text(msg.get("activity_id"), limit=120)
    if not activity_id:
        connection.send_error(
            msg["id"], "invalid_activity_id", "Activity ID is required"
        )
        return

    title = _clean_text(msg.get("title"), limit=160)
    overrides = _title_overrides(hass)
    if title:
        overrides[activity_id] = title
    else:
        overrides.pop(activity_id, None)
    await _save_title_overrides(hass)
    connection.send_result(
        msg["id"],
        {
            "activity_id": activity_id,
            "title": title or None,
            "overridden": bool(title),
        },
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): "bosch_ebike/set_imported_gpx_consumption",
        vol.Required("track_id"): str,
        vol.Required("start_soc"): vol.All(
            vol.Coerce(float), vol.Range(min=0, max=100)
        ),
        vol.Required("end_soc"): vol.All(
            vol.Coerce(float), vol.Range(min=0, max=100)
        ),
        vol.Required("capacity_wh"): vol.All(
            vol.Coerce(float), vol.Range(min=1, max=2000)
        ),
        vol.Required("session_distance_m"): vol.All(
            vol.Coerce(float), vol.Range(min=300, max=500_000)
        ),
    }
)
@websocket_api.async_response
async def ws_set_imported_gpx_consumption(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Store an admin-verified consumption repair for one imported track."""
    connection.require_admin()
    record = next(
        (item for item in _tracks(hass) if item.get("id") == msg["track_id"]),
        None,
    )
    if record is None:
        connection.send_error(
            msg["id"], "not_found", "Imported GPX track not found"
        )
        return
    consumption = _verified_manual_consumption(
        record,
        start_soc=msg["start_soc"],
        end_soc=msg["end_soc"],
        capacity_wh=msg["capacity_wh"],
        session_distance_m=msg["session_distance_m"],
    )
    if consumption is None:
        connection.send_error(
            msg["id"],
            "invalid_consumption",
            "The verified values are not physically plausible for this track",
        )
        return
    record["consumption"] = consumption
    await _save(hass)
    connection.send_result(
        msg["id"],
        {
            "track": _public_record(record),
            "consumption": dict(consumption),
        },
    )


@websocket_api.websocket_command(
    {vol.Required("type"): "bosch_ebike/list_imported_gpx"}
)
@websocket_api.async_response
async def ws_list_imported_gpx(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """List imported GPX metadata without returning all point arrays."""
    connection.send_result(
        msg["id"],
        {
            "tracks": [_public_record(item) for item in _tracks(hass)],
            "count": len(_tracks(hass)),
            "storage_bytes_total": sum(
                _record_storage_bytes(item) for item in _tracks(hass)
            ),
        },
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): "bosch_ebike/delete_imported_gpx",
        vol.Required("track_id"): str,
    }
)
@websocket_api.async_response
async def ws_delete_imported_gpx(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Delete one imported GPX track."""
    tracks = _tracks(hass)
    deleted = next(
        (item for item in tracks if item.get("id") == msg["track_id"]), None
    )
    if deleted is None:
        connection.send_error(
            msg["id"], "not_found", "Imported GPX track not found"
        )
        return

    remaining = [item for item in tracks if item is not deleted]
    hass.data[DOMAIN][DATA_TRACKS] = remaining
    await _save(hass)

    provider = deleted.get("provider")
    provider_id = deleted.get("provider_id")
    if provider and provider_id:
        _ignored_provider_ids(hass).add(f"{provider}:{provider_id}")
        await _save_ignored_provider_ids(hass)

    # A standalone GPX has no Bosch activity behind it. Its local display-name
    # override would otherwise survive as an unreachable orphan after deletion.
    if not deleted.get("activity_id"):
        overrides = _title_overrides(hass)
        if overrides.pop(_effective_activity_id(deleted), None) is not None:
            await _save_title_overrides(hass)

    connection.send_result(
        msg["id"], {"deleted": msg["track_id"], "count": len(remaining)}
    )
