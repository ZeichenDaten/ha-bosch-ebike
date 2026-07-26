"""Automatic Komoot-to-dashboard synchronisation."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging
from typing import Any, Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import (
    CONF_KOMOOT_BIKE_ID,
    CONF_KOMOOT_EMAIL,
    CONF_KOMOOT_PASSWORD,
    CONF_KOMOOT_SCAN_INTERVAL,
    DEFAULT_KOMOOT_SCAN_INTERVAL,
    EVENT_KOMOOT_SYNC_COMPLETED,
    DOMAIN,
)
from .external_gpx import (
    async_set_provider_consumption,
    async_upsert_provider_gpx,
    provider_import_is_ignored,
    provider_record,
)
from .komoot_api import (
    KomootApiClient,
    KomootApiError,
    KomootAuthenticationError,
    KomootRateLimitError,
)
from .komoot_gpx import detail_to_gpx
from .ride_journal import RideContactJournal
from .ride_matcher import (
    consumption_from_match,
    match_contact_windows,
    parse_datetime,
)

_LOGGER = logging.getLogger(__name__)

PROVIDER = "komoot"
MAX_SYNC_TOURS = 100
INITIAL_SYNC_DELAY_SECONDS = 15
DATA_KOMOOT_MANAGERS = "komoot_sync_managers"
DATA_RIDE_JOURNALS = "ride_contact_journals"


def get_komoot_manager(
    hass: HomeAssistant, entry_id: str
) -> "KomootSyncManager | None":
    managers = hass.data.get(DOMAIN, {}).get(DATA_KOMOOT_MANAGERS, {})
    return managers.get(entry_id) if isinstance(managers, dict) else None


def _number(source: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = source.get(key)
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if parsed == parsed:
            return parsed
    return None


def _text(source: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def normalise_komoot_metadata(
    summary: dict[str, Any], detail: dict[str, Any]
) -> dict[str, Any]:
    """Map stable, observed v007 fields to the local activity shape."""
    merged = dict(summary)
    merged.update(detail)
    distance = _number(merged, "distance")
    duration = _number(
        merged, "duration_active", "duration_moving", "duration"
    )
    start_time = _text(merged, "date", "start_time", "startTime")
    metadata: dict[str, Any] = {
        "title": _text(merged, "name", "title") or "Komoot-Tour",
        "start_time": start_time,
        "distance": distance,
        "duration_without_stops": duration,
        "elevation_gain": _number(merged, "elevation_up", "elevation_gain"),
        "elevation_loss": _number(
            merged, "elevation_down", "elevation_loss"
        ),
        "speed_maximum": _number(
            merged, "speed_max", "max_speed", "maximum_speed"
        ),
    }
    parsed_start = parse_datetime(start_time)
    if parsed_start is not None and duration is not None and duration > 0:
        metadata["end_time"] = (
            parsed_start + timedelta(seconds=duration)
        ).isoformat()
    if distance and duration and distance > 0 and duration > 0:
        metadata["speed_average"] = distance / duration * 3.6
    return metadata


def komoot_changed_at(summary: dict[str, Any], detail: dict[str, Any]) -> str | None:
    """Best available provider revision marker."""
    return _text(detail, "changed_at", "updated_at") or _text(
        summary, "changed_at", "updated_at"
    )


def find_matching_bosch_activity(
    activities: list[dict[str, Any]],
    *,
    start_time: str | None,
    distance_m: float,
) -> str | None:
    """Link to one obvious Bosch summary; ambiguous matches stay standalone."""
    wanted_start = parse_datetime(start_time)
    if wanted_start is None or distance_m <= 0:
        return None

    candidates: list[tuple[float, str]] = []
    for activity in activities:
        activity_id = activity.get("id")
        if not isinstance(activity_id, str) or not activity_id:
            continue
        if activity_id.startswith("komoot:") or str(
            activity.get("source") or ""
        ).startswith("komoot"):
            continue
        candidate_start = parse_datetime(activity.get("startTime"))
        if candidate_start is None:
            continue
        time_delta = abs((candidate_start - wanted_start).total_seconds())
        if time_delta > 20 * 60:
            continue
        try:
            candidate_distance = float(activity.get("distance") or 0)
        except (TypeError, ValueError):
            continue
        distance_delta = abs(candidate_distance - distance_m)
        if distance_delta > max(1_000.0, distance_m * 0.15):
            continue
        score = time_delta + distance_delta / 10.0
        candidates.append((score, activity_id))

    candidates.sort()
    if not candidates:
        return None
    if len(candidates) > 1 and candidates[1][0] - candidates[0][0] < 300:
        return None
    return candidates[0][1]


class KomootSyncManager:
    """Poll recorded tours, upsert tracks, and enrich them conservatively."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        coordinator: Any,
        client: KomootApiClient,
        journal: RideContactJournal | None,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator
        self.client = client
        self.journal = journal
        self.bike_id = str(entry.options.get(CONF_KOMOOT_BIKE_ID) or "")
        self._lock = asyncio.Lock()
        self._unsub_interval: Callable[[], None] | None = None
        self._unsub_initial: Callable[[], None] | None = None
        self.last_sync: str | None = None
        self.last_error: str | None = None
        self.last_result: dict[str, Any] = {}

    @callback
    def async_start(self) -> None:
        """Schedule an initial sync and subsequent fixed-interval polls."""
        interval_minutes = int(
            self.entry.options.get(
                CONF_KOMOOT_SCAN_INTERVAL, DEFAULT_KOMOOT_SCAN_INTERVAL
            )
            or DEFAULT_KOMOOT_SCAN_INTERVAL
        )
        interval_minutes = min(360, max(15, interval_minutes))
        self._unsub_interval = async_track_time_interval(
            self.hass, self._async_interval, timedelta(minutes=interval_minutes)
        )
        self._unsub_initial = async_call_later(
            self.hass, INITIAL_SYNC_DELAY_SECONDS, self._async_initial
        )

    @callback
    def async_stop(self) -> None:
        """Cancel scheduled work on config-entry unload."""
        if self._unsub_interval is not None:
            self._unsub_interval()
            self._unsub_interval = None
        if self._unsub_initial is not None:
            self._unsub_initial()
            self._unsub_initial = None

    async def _async_initial(self, _now: datetime) -> None:
        self._unsub_initial = None
        await self.async_sync(reason="startup")

    async def _async_interval(self, _now: datetime) -> None:
        await self.async_sync(reason="scheduled")

    async def async_sync(self, *, reason: str = "manual") -> dict[str, Any]:
        """Synchronise at most one batch; overlapping triggers coalesce."""
        if self._lock.locked():
            return {"status": "busy"}

        async with self._lock:
            created = 0
            updated = 0
            consumption_added = 0
            ignored = 0
            failed = 0
            latest_title: str | None = None
            latest_distance = 0.0
            changed_anything = False
            try:
                tours = await self.client.async_list_tours()
                tours.sort(
                    key=lambda item: str(
                        item.get("date") or item.get("changed_at") or ""
                    ),
                    reverse=True,
                )
                for summary in tours[:MAX_SYNC_TOURS]:
                    provider_id = str(summary.get("id") or "")
                    if not provider_id:
                        failed += 1
                        continue
                    if provider_import_is_ignored(
                        self.hass, PROVIDER, provider_id
                    ):
                        ignored += 1
                        continue

                    existing = provider_record(
                        self.hass, PROVIDER, provider_id
                    )
                    summary_changed_at = _text(
                        summary, "changed_at", "updated_at"
                    )
                    if (
                        existing is not None
                        and summary_changed_at
                        and existing.get("provider_changed_at")
                        == summary_changed_at
                    ):
                        if await self._async_enrich_consumption(
                            provider_id, existing
                        ):
                            consumption_added += 1
                            changed_anything = True
                        continue

                    try:
                        detail = await self.client.async_get_tour_detail(
                            provider_id, language="de"
                        )
                        # Komoot's recorded-tour detail already contains the
                        # coordinate array. Building GPX locally avoids a
                        # second undocumented endpoint and halves the request
                        # count during the initial import.
                        gpx_text = detail_to_gpx(detail)
                        metadata = normalise_komoot_metadata(summary, detail)
                        distance_m = float(metadata.get("distance") or 0)
                        activity_id = find_matching_bosch_activity(
                            (
                                self.coordinator.data.get("all_activities", [])
                                if self.coordinator.data
                                else []
                            ),
                            start_time=metadata.get("start_time"),
                            distance_m=distance_m,
                        )
                        result = await async_upsert_provider_gpx(
                            self.hass,
                            provider=PROVIDER,
                            provider_id=provider_id,
                            provider_changed_at=komoot_changed_at(
                                summary, detail
                            ),
                            gpx_content=gpx_text,
                            filename=f"komoot-{provider_id}.gpx",
                            bike_id=self.bike_id,
                            metadata=metadata,
                            activity_id=activity_id,
                        )
                    except (
                        KomootApiError,
                        ValueError,
                    ) as err:
                        if isinstance(
                            err,
                            (
                                KomootAuthenticationError,
                                KomootRateLimitError,
                            ),
                        ):
                            raise
                        failed += 1
                        _LOGGER.warning(
                            "Komoot tour %s could not be imported: %s",
                            provider_id,
                            type(err).__name__,
                        )
                        continue

                    status = result["status"]
                    record = result.get("record")
                    if status == "created":
                        created += 1
                        changed_anything = True
                    elif status == "updated":
                        updated += 1
                        changed_anything = True
                    elif status == "ignored":
                        ignored += 1
                        continue
                    if isinstance(record, dict):
                        latest_title = latest_title or str(
                            record.get("title") or "Komoot-Tour"
                        )
                        latest_distance = latest_distance or float(
                            record.get("distance") or 0
                        )
                        if await self._async_enrich_consumption(
                            provider_id, record
                        ):
                            consumption_added += 1
                            changed_anything = True

                if changed_anything:
                    await self.coordinator.async_request_refresh()

                self.last_sync = dt_util.now().isoformat()
                self.last_error = None
                self.last_result = {
                    "status": "ok",
                    "reason": reason,
                    "created": created,
                    "updated": updated,
                    "consumption_added": consumption_added,
                    "ignored": ignored,
                    "failed": failed,
                    "tour_count": len(tours),
                }
                if created or updated:
                    self.hass.bus.async_fire(
                        EVENT_KOMOOT_SYNC_COMPLETED,
                        {
                            "config_entry_id": self.entry.entry_id,
                            "bike_id": self.bike_id,
                            "created": created,
                            "updated": updated,
                            "consumption_added": consumption_added,
                            "failed": failed,
                            "latest_title": latest_title,
                            "latest_distance_km": round(
                                latest_distance / 1000.0, 1
                            ),
                        },
                    )
                return dict(self.last_result)
            except KomootRateLimitError as err:
                self.last_error = "rate_limited"
                _LOGGER.warning(
                    "Komoot sync rate-limited%s",
                    (
                        f"; retry after about {int(err.retry_after)} seconds"
                        if err.retry_after is not None
                        else ""
                    ),
                )
            except KomootAuthenticationError:
                self.last_error = "authentication_failed"
                _LOGGER.warning(
                    "Komoot sync authentication failed; update the integration options"
                )
            except KomootApiError as err:
                self.last_error = type(err).__name__
                _LOGGER.warning("Komoot sync failed: %s", type(err).__name__)
            except Exception:  # noqa: BLE001
                self.last_error = "unexpected_error"
                _LOGGER.exception("Unexpected Komoot sync failure")

            self.last_sync = dt_util.now().isoformat()
            self.last_result = {
                "status": "error",
                "reason": reason,
                "error": self.last_error,
            }
            return dict(self.last_result)

    async def _async_enrich_consumption(
        self, provider_id: str, record: dict[str, Any]
    ) -> bool:
        if self.journal is None:
            return False
        start = parse_datetime(record.get("start_time"))
        end = parse_datetime(record.get("end_time"))
        try:
            distance_m = float(record.get("distance") or 0)
        except (TypeError, ValueError):
            return False
        if start is None or end is None:
            return False

        decision = match_contact_windows(
            tour_start=start,
            tour_end=end,
            tour_distance_m=distance_m,
            windows=self.journal.reliable_windows(),
        )
        if decision.match is None:
            return False
        consumption = consumption_from_match(
            decision.match,
            capacity_wh=self.coordinator.battery_capacity_wh(
                self.bike_id
            ),
            activity_distance_m=distance_m,
        )
        if consumption is None:
            return False
        return await async_set_provider_consumption(
            self.hass,
            provider=PROVIDER,
            provider_id=provider_id,
            consumption=consumption,
        )

    def diagnostics(self) -> dict[str, Any]:
        """Non-secret manager status for downloaded diagnostics."""
        return {
            "enabled": True,
            "bike_id": self.bike_id,
            "last_sync": self.last_sync,
            "last_error": self.last_error,
            "last_result": dict(self.last_result),
            "journal": self.journal.diagnostics() if self.journal else None,
        }


def build_komoot_client(
    hass: HomeAssistant, entry: ConfigEntry
) -> KomootApiClient | None:
    """Create a client only when all private options are present."""
    email = entry.options.get(CONF_KOMOOT_EMAIL)
    password = entry.options.get(CONF_KOMOOT_PASSWORD)
    if not isinstance(email, str) or not email or not isinstance(password, str) or not password:
        return None
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    return KomootApiClient(
        async_get_clientsession(hass),
        email,
        password,
    )
