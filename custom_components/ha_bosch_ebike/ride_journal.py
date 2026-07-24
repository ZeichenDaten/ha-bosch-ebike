"""Persistent BLE contact journal used to enrich automatically imported rides."""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Any, Callable
import uuid

from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

STORE_VERSION = 1
MAX_WINDOWS = 100
RETENTION = timedelta(days=30)
FRESH_SAMPLE = timedelta(minutes=2)
STALE_ACTIVE = timedelta(minutes=15)
INVALID_STATES = {None, "", "unknown", "unavailable"}


def _as_float(state: State | None) -> float | None:
    if state is None or state.state in INVALID_STATES:
        return None
    try:
        value = float(state.state)
    except (TypeError, ValueError):
        return None
    if value != value:
        return None
    return value


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return result if result.tzinfo is not None else None


def _discover_contact_entities(
    hass: HomeAssistant, soc_entity_id: str
) -> tuple[str | None, str | None]:
    """Find the bridge's connectivity and charger sensors on the SoC device."""
    registry = er.async_get(hass)
    soc_entry = registry.async_get(soc_entity_id)
    if soc_entry is None or soc_entry.device_id is None:
        return None, None

    connected: str | None = None
    charger: str | None = None
    for entry in er.async_entries_for_device(registry, soc_entry.device_id):
        if entry.disabled_by is not None:
            continue
        device_class = entry.original_device_class or entry.device_class
        if entry.domain == "binary_sensor" and device_class == "connectivity":
            connected = entry.entity_id
        elif entry.domain == "binary_sensor" and device_class == "plug":
            charger = entry.entity_id
    return connected, charger


class RideContactJournal:
    """Record bridge contact windows without depending on garage automations."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        entry_id: str,
        bike_id: str,
        soc_entity_id: str,
        odometer_entity_id: str,
    ) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self.bike_id = bike_id
        self.soc_entity_id = soc_entity_id
        self.odometer_entity_id = odometer_entity_id
        self.connected_entity_id, self.charger_entity_id = (
            _discover_contact_entities(hass, soc_entity_id)
        )
        self._store = Store(
            hass,
            STORE_VERSION,
            f"{DOMAIN}_ride_contact_journal_{entry_id}_{bike_id}",
        )
        self._windows: list[dict[str, Any]] = []
        self._active: dict[str, Any] | None = None
        self._unsub: Callable[[], None] | None = None

    @property
    def available(self) -> bool:
        """Whether all mandatory source entities were discovered."""
        return self.connected_entity_id is not None

    @property
    def windows(self) -> list[dict[str, Any]]:
        """Return closed windows plus the active window for diagnostics."""
        result = [dict(window) for window in self._windows]
        if self._active is not None:
            result.append(dict(self._active))
        return result

    async def async_setup(self) -> bool:
        """Load state and subscribe to the four direct bridge entities."""
        if not self.available:
            _LOGGER.warning(
                "Komoot ride journal for bike %s is disabled: no connectivity "
                "binary sensor was found on the configured live-SoC device",
                self.bike_id,
            )
            return False

        loaded = await self._store.async_load() or {}
        if isinstance(loaded, dict):
            windows = loaded.get("windows")
            active = loaded.get("active")
            if isinstance(windows, list):
                self._windows = [
                    item for item in windows if isinstance(item, dict)
                ][-MAX_WINDOWS:]
            if isinstance(active, dict):
                self._active = active
        self._purge()

        entity_ids = [
            self.connected_entity_id,
            self.soc_entity_id,
            self.odometer_entity_id,
        ]
        if self.charger_entity_id:
            entity_ids.append(self.charger_entity_id)
        self._unsub = async_track_state_change_event(
            self.hass, entity_ids, self._async_state_changed
        )

        connected = self.hass.states.get(self.connected_entity_id)
        if connected is not None and connected.state == "on":
            self._ensure_active(dt_util.now(), reliable_start=False)
            self._capture_sample(dt_util.now())
        elif self._active is not None:
            # HA restarted while the bridge was absent/off. Preserve the data
            # for diagnostics, but never use the guessed end for matching.
            self._close_active(
                self._last_seen(self._active) or dt_util.now(),
                reliable_end=False,
            )
        self._schedule_save()
        return True

    @callback
    def async_stop(self) -> None:
        """Unsubscribe on config-entry unload."""
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    @callback
    def _async_state_changed(self, event: Event) -> None:
        entity_id = event.data.get("entity_id")
        old_state: State | None = event.data.get("old_state")
        new_state: State | None = event.data.get("new_state")
        now = dt_util.now()

        if entity_id == self.connected_entity_id:
            old_value = old_state.state if old_state else None
            new_value = new_state.state if new_state else None
            if new_value == "on":
                if self._active is not None:
                    last_seen = self._last_seen(self._active)
                    if last_seen is not None and now - last_seen > STALE_ACTIVE:
                        self._close_active(last_seen, reliable_end=False)
                self._ensure_active(now, reliable_start=old_value == "off")
                self._capture_sample(now)
            elif old_value == "on" and new_value == "off":
                self._capture_sample(now)
                self._close_active(now, reliable_end=True)
            elif (
                new_value == "off"
                and old_value in INVALID_STATES
                and self._active is not None
            ):
                self._close_active(
                    self._last_seen(self._active) or now,
                    reliable_end=False,
                )
            return

        connected = self.hass.states.get(self.connected_entity_id)
        if connected is None or connected.state != "on":
            return
        self._ensure_active(now, reliable_start=False)
        self._capture_sample(now)

    @callback
    def _ensure_active(self, now: datetime, *, reliable_start: bool) -> None:
        if self._active is None:
            self._active = {
                "id": uuid.uuid4().hex,
                "started_at": now.isoformat(),
                "reliable_start": reliable_start,
                "ended_at": None,
                "reliable_end": False,
                "last_seen_at": now.isoformat(),
                "first_sample": None,
                "last_sample": None,
            }
        else:
            self._active["last_seen_at"] = now.isoformat()
        self._schedule_save()

    @callback
    def _capture_sample(self, now: datetime) -> None:
        if self._active is None:
            return
        # Never use a reading taken while the bike is charging. Especially on
        # arrival, the first SoC update may otherwise happen only after the
        # charger was connected and would make the ride look artificially
        # efficient. Keeping no automatic consumption is safer than guessing.
        if self.charger_entity_id:
            charger_state = self.hass.states.get(self.charger_entity_id)
            if charger_state is not None and charger_state.state == "on":
                return
        soc_state = self.hass.states.get(self.soc_entity_id)
        odometer_state = self.hass.states.get(self.odometer_entity_id)
        soc = _as_float(soc_state)
        odometer_km = _as_float(odometer_state)
        if soc is None or odometer_km is None:
            return
        if not 0 <= soc <= 100 or odometer_km < 0:
            return
        if (
            now - soc_state.last_updated > FRESH_SAMPLE
            or now - odometer_state.last_updated > FRESH_SAMPLE
        ):
            return

        sample = {
            "at": now.isoformat(),
            "soc": soc,
            "odometer_km": odometer_km,
        }
        if self._active.get("first_sample") is None:
            self._active["first_sample"] = sample
        self._active["last_sample"] = sample
        self._active["last_seen_at"] = now.isoformat()
        self._schedule_save()

    @callback
    def _close_active(self, ended_at: datetime, *, reliable_end: bool) -> None:
        if self._active is None:
            return
        self._active["ended_at"] = ended_at.isoformat()
        self._active["reliable_end"] = reliable_end
        self._windows.append(self._active)
        self._active = None
        self._purge()
        self._schedule_save()

    @staticmethod
    def _last_seen(window: dict[str, Any]) -> datetime | None:
        return _parse_datetime(window.get("last_seen_at"))

    @callback
    def _purge(self) -> None:
        cutoff = dt_util.now() - RETENTION
        retained = []
        for window in self._windows:
            ended = _parse_datetime(window.get("ended_at"))
            if ended is not None and ended >= cutoff:
                retained.append(window)
        self._windows = retained[-MAX_WINDOWS:]

    @callback
    def _schedule_save(self) -> None:
        self._store.async_delay_save(self._serialise, 2)

    @callback
    def _serialise(self) -> dict[str, Any]:
        return {
            "windows": self._windows[-MAX_WINDOWS:],
            "active": self._active,
        }

    def reliable_windows(self) -> list[dict[str, Any]]:
        """Return only windows safe for automatic tour attribution."""
        result = [
            dict(window)
            for window in self._windows
            if (
                window.get("reliable_end") is True
                or window.get("reliable_start") is True
            )
        ]
        if self._active is not None and self._active.get("reliable_start") is True:
            result.append(dict(self._active))
        return result

    def diagnostics(self) -> dict[str, Any]:
        """Return non-secret status for integration diagnostics."""
        return {
            "available": self.available,
            "bike_id": self.bike_id,
            "soc_entity_id": self.soc_entity_id,
            "odometer_entity_id": self.odometer_entity_id,
            "connected_entity_id": self.connected_entity_id,
            "charger_entity_id": self.charger_entity_id,
            "closed_windows": len(self._windows),
            "reliable_windows": sum(
                1 for item in self._windows if item.get("reliable_end") is True
            ),
            "active": self._active is not None,
        }
