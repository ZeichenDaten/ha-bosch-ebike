"""Regression tests for unchanged ESPHome values reported on reconnect."""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace


SOURCE_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "ha_bosch_ebike"
    / "ride_journal.py"
)
tree = ast.parse(SOURCE_PATH.read_text(encoding="utf-8"))
as_float_node = next(
    node
    for node in tree.body
    if isinstance(node, ast.FunctionDef)
    and node.name == "_as_float"
)
reported_at_node = next(
    node
    for node in tree.body
    if isinstance(node, ast.FunctionDef)
    and node.name == "_state_reported_at"
)
timestamp_fresh_node = next(
    node
    for node in tree.body
    if isinstance(node, ast.FunctionDef)
    and node.name == "_timestamp_is_fresh"
)
parse_datetime_node = next(
    node
    for node in tree.body
    if isinstance(node, ast.FunctionDef)
    and node.name == "_parse_datetime"
)
capture_node = next(
    node
    for node in ast.walk(tree)
    if isinstance(node, ast.FunctionDef)
    and node.name == "_capture_sample"
)
cancel_delayed_node = next(
    node
    for node in ast.walk(tree)
    if isinstance(node, ast.FunctionDef)
    and node.name == "_cancel_delayed_capture"
)
schedule_delayed_node = next(
    node
    for node in ast.walk(tree)
    if isinstance(node, ast.FunctionDef)
    and node.name == "_schedule_delayed_capture"
)
scheduled_callbacks = []


def fake_async_call_later(_hass, delay, callback_fn):
    scheduled_callbacks.append((delay, callback_fn))

    def cancel():
        scheduled_callbacks.append(("cancelled", callback_fn))

    return cancel


namespace = {
    "State": object,
    "datetime": datetime,
    "callback": lambda function: function,
    "FRESH_SAMPLE": timedelta(minutes=2),
    "MAX_SAMPLE_SKEW": timedelta(seconds=10),
    "MAX_FUTURE_SKEW": timedelta(seconds=5),
    "CONTACT_SETTLE_DELAY": timedelta(seconds=3),
    "INVALID_STATES": {None, "", "unknown", "unavailable"},
    "async_call_later": fake_async_call_later,
    "dt_util": SimpleNamespace(now=lambda: NOW),
}
exec(
    compile(
        ast.Module(
            body=[
                as_float_node,
                reported_at_node,
                timestamp_fresh_node,
                parse_datetime_node,
                capture_node,
                cancel_delayed_node,
                schedule_delayed_node,
            ],
            type_ignores=[],
        ),
        str(SOURCE_PATH),
        "exec",
    ),
    namespace,
)
state_reported_at = namespace["_state_reported_at"]
timestamp_is_fresh = namespace["_timestamp_is_fresh"]
capture_sample = namespace["_capture_sample"]
cancel_delayed_capture = namespace["_cancel_delayed_capture"]
schedule_delayed_capture = namespace["_schedule_delayed_capture"]

UTC = timezone.utc
NOW = datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC)


def test_same_value_report_uses_last_reported_not_last_updated():
    state = SimpleNamespace(
        last_updated=NOW - timedelta(hours=2),
        last_reported=NOW - timedelta(seconds=2),
    )

    assert state_reported_at(state) == NOW - timedelta(seconds=2)


def test_older_state_like_objects_fall_back_to_last_updated():
    state = SimpleNamespace(last_updated=NOW - timedelta(seconds=3))

    assert state_reported_at(state) == NOW - timedelta(seconds=3)


def test_missing_state_has_no_report_timestamp():
    assert state_reported_at(None) is None


def test_timestamp_validation_rejects_stale_future_and_naive_values():
    assert timestamp_is_fresh(NOW, NOW - timedelta(seconds=30)) is True
    assert timestamp_is_fresh(NOW, NOW - timedelta(minutes=3)) is False
    assert timestamp_is_fresh(NOW, NOW + timedelta(seconds=6)) is False
    assert timestamp_is_fresh(NOW, NOW.replace(tzinfo=None)) is False


class FakeStates:
    def __init__(self, values):
        self.values = values

    def get(self, entity_id):
        return self.values.get(entity_id)


class FakeJournal:
    charger_entity_id = "binary_sensor.charger"
    soc_entity_id = "sensor.soc"
    odometer_entity_id = "sensor.odometer"

    def __init__(self, states):
        self.hass = SimpleNamespace(states=FakeStates(states))
        self._active = {
            "started_at": NOW.isoformat(),
            "first_sample": None,
            "last_sample": None,
        }
        self.saved = 0

    def _schedule_save(self):
        self.saved += 1


class FakeDelayedJournal:
    connected_entity_id = "binary_sensor.connected"

    def __init__(self):
        self._active = {"id": "window-1"}
        self._delayed_capture_unsub = None
        self.hass = SimpleNamespace(
            states=FakeStates(
                {
                    self.connected_entity_id: SimpleNamespace(state="on"),
                }
            )
        )
        self.captures = 0

    def _cancel_delayed_capture(self):
        cancel_delayed_capture(self)

    def _capture_sample(self, _now):
        self.captures += 1


def sensor_state(value, *, updated, reported=None):
    return SimpleNamespace(
        state=str(value),
        last_updated=updated,
        last_reported=reported,
    )


def test_capture_accepts_unchanged_values_reported_on_reconnect():
    states = {
        "binary_sensor.charger": sensor_state(
            "off", updated=NOW - timedelta(hours=2), reported=NOW
        ),
        "sensor.soc": sensor_state(
            100, updated=NOW - timedelta(hours=2), reported=NOW
        ),
        "sensor.odometer": sensor_state(
            100.125, updated=NOW - timedelta(seconds=1), reported=NOW
        ),
    }
    journal = FakeJournal(states)

    capture_sample(journal, NOW)

    assert journal._active["first_sample"]["soc"] == 100
    assert journal._active["first_sample"]["odometer_km"] == 100.125
    assert journal._active["last_sample"] == journal._active["first_sample"]


def test_capture_replaces_stale_reconnect_snapshot_during_settle_delay():
    states = {
        "binary_sensor.charger": sensor_state(
            "off", updated=NOW, reported=NOW
        ),
        "sensor.soc": sensor_state(100, updated=NOW, reported=NOW),
        "sensor.odometer": sensor_state(
            323.379, updated=NOW, reported=NOW
        ),
    }
    journal = FakeJournal(states)

    capture_sample(journal, NOW)
    assert journal._active["first_sample"]["soc"] == 100

    refreshed_at = NOW + timedelta(seconds=2)
    states["binary_sensor.charger"] = sensor_state(
        "off", updated=refreshed_at, reported=refreshed_at
    )
    states["sensor.soc"] = sensor_state(
        55, updated=refreshed_at, reported=refreshed_at
    )
    states["sensor.odometer"] = sensor_state(
        341.7, updated=refreshed_at, reported=refreshed_at
    )
    capture_sample(journal, refreshed_at)

    assert journal._active["first_sample"]["soc"] == 55
    assert journal._active["first_sample"]["odometer_km"] == 341.7

    after_settle = NOW + timedelta(seconds=4)
    states["binary_sensor.charger"] = sensor_state(
        "off", updated=after_settle, reported=after_settle
    )
    states["sensor.soc"] = sensor_state(
        54, updated=after_settle, reported=after_settle
    )
    states["sensor.odometer"] = sensor_state(
        341.702, updated=after_settle, reported=after_settle
    )
    capture_sample(journal, after_settle)

    assert journal._active["first_sample"]["soc"] == 55
    assert journal._active["last_sample"]["soc"] == 54


def test_capture_rejects_unsafe_charger_states():
    base = {
        "sensor.soc": sensor_state(100, updated=NOW, reported=NOW),
        "sensor.odometer": sensor_state(100.125, updated=NOW, reported=NOW),
    }
    for charger in (
        None,
        sensor_state("on", updated=NOW, reported=NOW),
        sensor_state("unknown", updated=NOW, reported=NOW),
        sensor_state("unavailable", updated=NOW, reported=NOW),
        sensor_state(
            "off",
            updated=NOW - timedelta(hours=1),
            reported=NOW - timedelta(hours=1),
        ),
    ):
        values = dict(base)
        if charger is not None:
            values["binary_sensor.charger"] = charger
        journal = FakeJournal(values)

        capture_sample(journal, NOW)

        assert journal._active["first_sample"] is None
        assert journal.saved == 0


def test_capture_rejects_values_reported_too_far_apart():
    states = {
        "binary_sensor.charger": sensor_state(
            "off", updated=NOW, reported=NOW
        ),
        "sensor.soc": sensor_state(
            100, updated=NOW, reported=NOW - timedelta(seconds=11)
        ),
        "sensor.odometer": sensor_state(
            100.125, updated=NOW, reported=NOW
        ),
    }
    journal = FakeJournal(states)

    capture_sample(journal, NOW)

    assert journal._active["first_sample"] is None
    assert journal.saved == 0


def test_capture_rejects_invalid_or_stale_measurements():
    valid = {
        "binary_sensor.charger": sensor_state(
            "off", updated=NOW, reported=NOW
        ),
        "sensor.soc": sensor_state(100, updated=NOW, reported=NOW),
        "sensor.odometer": sensor_state(
            100.125, updated=NOW, reported=NOW
        ),
    }
    invalid_values = (
        ("sensor.soc", "unknown"),
        ("sensor.soc", "unavailable"),
        ("sensor.soc", -1),
        ("sensor.soc", 101),
        ("sensor.odometer", "unknown"),
        ("sensor.odometer", "unavailable"),
        ("sensor.odometer", -1),
    )
    for entity_id, value in invalid_values:
        states = dict(valid)
        states[entity_id] = sensor_state(
            value, updated=NOW, reported=NOW
        )
        journal = FakeJournal(states)

        capture_sample(journal, NOW)

        assert journal._active["first_sample"] is None
        assert journal.saved == 0

    for missing_entity_id in ("sensor.soc", "sensor.odometer"):
        states = dict(valid)
        states.pop(missing_entity_id)
        journal = FakeJournal(states)

        capture_sample(journal, NOW)

        assert journal._active["first_sample"] is None
        assert journal.saved == 0

    for stale_entity_id in ("sensor.soc", "sensor.odometer"):
        states = dict(valid)
        states[stale_entity_id] = sensor_state(
            states[stale_entity_id].state,
            updated=NOW - timedelta(minutes=3),
            reported=NOW - timedelta(minutes=3),
        )
        journal = FakeJournal(states)

        capture_sample(journal, NOW)

        assert journal._active["first_sample"] is None
        assert journal.saved == 0


def test_delayed_capture_runs_only_for_the_same_connected_window():
    scheduled_callbacks.clear()
    journal = FakeDelayedJournal()

    schedule_delayed_capture(journal)

    delay, callback_fn = scheduled_callbacks[0]
    assert delay == timedelta(seconds=3)
    callback_fn(NOW)
    assert journal.captures == 1
    assert journal._delayed_capture_unsub is None

    scheduled_callbacks.clear()
    journal = FakeDelayedJournal()
    schedule_delayed_capture(journal)
    journal.hass.states.values[journal.connected_entity_id].state = "off"
    scheduled_callbacks[0][1](NOW)
    assert journal.captures == 0

    scheduled_callbacks.clear()
    journal = FakeDelayedJournal()
    schedule_delayed_capture(journal)
    journal._active = {"id": "window-2"}
    scheduled_callbacks[0][1](NOW)
    assert journal.captures == 0


def test_rescheduling_and_stopping_cancel_delayed_capture():
    scheduled_callbacks.clear()
    journal = FakeDelayedJournal()

    schedule_delayed_capture(journal)
    first_callback = scheduled_callbacks[0][1]
    schedule_delayed_capture(journal)

    assert ("cancelled", first_callback) in scheduled_callbacks
    second_callback = next(
        callback_fn
        for delay, callback_fn in scheduled_callbacks
        if delay == timedelta(seconds=3) and callback_fn is not first_callback
    )
    cancel_delayed_capture(journal)
    assert ("cancelled", second_callback) in scheduled_callbacks
    assert journal._delayed_capture_unsub is None


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("ALL TESTS PASSED")
