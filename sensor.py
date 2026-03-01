"""Sensor entities for Dragontree Irrigation."""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN, SIGNAL_STATIONS_UPDATED, STATUS_MANUAL, STATUS_RUNNING
from .coordinator import CONTROLLER_DEVICE_INFO, IrrigationCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: IrrigationCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[SensorEntity] = [
        IrrigationStatusSensor(coordinator),
        IrrigationScheduleSensor(coordinator),
    ]
    for station in coordinator.stations:
        entities.extend(_station_sensors(coordinator, station))

    async_add_entities(entities)

    @callback
    def _stations_updated() -> None:
        """Add sensors for newly added stations."""
        existing_ids = {e.unique_id for e in hass.data[DOMAIN].get("sensor_entities", [])}
        new_entities = []
        for station in coordinator.stations:
            for cls in (StationStatusSensor, StationTimeRemainingSensor, StationLastRunSensor):
                uid = f"{DOMAIN}_{station['id']}_{cls.__name__.lower()}"
                if uid not in existing_ids:
                    new_entities.append(cls(coordinator, station["id"]))
        if new_entities:
            async_add_entities(new_entities)

    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_STATIONS_UPDATED, _stations_updated)
    )


def _station_sensors(coordinator: IrrigationCoordinator, station: dict) -> list:
    sid = station["id"]
    return [
        StationStatusSensor(coordinator, sid),
        StationTimeRemainingSensor(coordinator, sid),
        StationLastRunSensor(coordinator, sid),
    ]


class IrrigationStatusSensor(CoordinatorEntity, SensorEntity):
    """Overall irrigation status — which queue is running."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:sprinkler"

    def __init__(self, coordinator: IrrigationCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_running_queue"
        self._attr_name = "Running Queue"
        self._attr_device_info = CONTROLLER_DEVICE_INFO

    @property
    def native_value(self) -> str:
        rt = self.coordinator.runtime
        queue = rt.get("running_queue")
        return queue.upper() if queue else "idle"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        rt = self.coordinator.runtime
        return {
            "current_station_id": rt.get("current_station_id"),
        }


class IrrigationScheduleSensor(CoordinatorEntity, SensorEntity):
    """Exposes the full lookahead schedule as JSON for dashboards."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, coordinator: IrrigationCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_schedule"
        self._attr_name = "Schedule"
        self._attr_device_info = CONTROLLER_DEVICE_INFO

    @property
    def native_value(self) -> str:
        schedules = self.coordinator.day_schedules
        if not schedules:
            return "empty"
        # Return count of non-empty days for a simple state
        active = sum(
            1
            for d in schedules
            if any(
                d["queues"][q]["stations"]
                for q in d["queues"]
            )
        )
        return f"{active} active days"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        # Copy at every level that is mutated in-place so HA's old-vs-new
        # attribute comparison always sees distinct objects:
        #   - stations list: async_update_station / async_move_station mutate in-place
        #   - day_schedules: _run_queue mutates station_entry["status"] and
        #     _recalculate_queue_end_time mutates queue["end_time"] in-place
        #     without calling _regenerate_schedules (which would replace the list).
        return {
            "day_schedules": [
                {
                    **day,
                    "queues": {
                        q_name: {
                            **q_data,
                            "stations": [dict(s) for s in q_data.get("stations", [])],
                        }
                        for q_name, q_data in day.get("queues", {}).items()
                    },
                }
                for day in self.coordinator.day_schedules
            ],
            "stations": [dict(s) for s in self.coordinator.stations],
        }


class _StationBaseSensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: IrrigationCoordinator, station_id: str) -> None:
        super().__init__(coordinator)
        self._station_id = station_id
        self._attr_device_info = CONTROLLER_DEVICE_INFO

    def _get_station(self) -> dict | None:
        return next(
            (s for s in self.coordinator.stations if s["id"] == self._station_id), None
        )

    def _get_station_entry_today(self, queue_name: str) -> dict | None:
        today_str = date.today().isoformat()
        for day in self.coordinator.day_schedules:
            if day["date"] == today_str:
                for entry in day["queues"].get(queue_name, {}).get("stations", []):
                    if entry["station_id"] == self._station_id:
                        return entry
        return None


class StationStatusSensor(_StationBaseSensor):
    """Status of a specific station."""

    def __init__(self, coordinator: IrrigationCoordinator, station_id: str) -> None:
        super().__init__(coordinator, station_id)
        self._attr_unique_id = f"{DOMAIN}_{station_id}_status"
        self._attr_icon = "mdi:water"

    @property
    def name(self) -> str:
        return f"{self._station_id} Status"

    @property
    def native_value(self) -> str:
        rt = self.coordinator.runtime
        station = self._get_station()
        # Check whether the physical station is currently running
        is_running = False
        if station:
            bs = f"binary_sensor.{station['base_name']}_station_running"
            bs_state = self.hass.states.get(bs)
            is_running = bs_state is not None and bs_state.state == "on"

        if is_running:
            # If this station is the one the queue started, it's a scheduled run
            if rt.get("current_station_id") == self._station_id and rt.get("running_queue"):
                return STATUS_RUNNING
            return STATUS_MANUAL

        # Not physically running — reflect the schedule entry status
        for q in ("am", "pm"):
            entry = self._get_station_entry_today(q)
            if entry:
                return entry["status"]
        return "idle"


class StationTimeRemainingSensor(_StationBaseSensor):
    """Time remaining for the currently running station."""

    def __init__(self, coordinator: IrrigationCoordinator, station_id: str) -> None:
        super().__init__(coordinator, station_id)
        self._attr_unique_id = f"{DOMAIN}_{station_id}_time_remaining"
        self._attr_icon = "mdi:timer"
        self._tick_unsub = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._tick_unsub = async_track_time_interval(
            self.hass, self._handle_tick, timedelta(seconds=5)
        )

    async def async_will_remove_from_hass(self) -> None:
        if self._tick_unsub:
            self._tick_unsub()
            self._tick_unsub = None

    @callback
    def _handle_tick(self, _now: Any) -> None:
        self.async_write_ha_state()

    @property
    def name(self) -> str:
        return f"{self._station_id} Time Remaining"

    @property
    def native_value(self) -> str:
        station = self._get_station()
        if not station:
            return "n/a"
        bs = f"binary_sensor.{station['base_name']}_station_running"
        state = self.hass.states.get(bs)
        if state and state.state == "on":
            end_time_str = state.attributes.get("end_time")
            if end_time_str:
                try:
                    end_time = dt_util.parse_datetime(end_time_str)
                    if end_time:
                        remaining = int((end_time - dt_util.utcnow()).total_seconds())
                        if remaining > 0:
                            mins, secs = divmod(remaining, 60)
                            hrs, mins = divmod(mins, 60)
                            return f"{hrs:02d}:{mins:02d}:{secs:02d}"
                except (ValueError, TypeError):
                    pass
        return "n/a"


class StationLastRunSensor(_StationBaseSensor):
    """Date of last run for a station."""

    def __init__(self, coordinator: IrrigationCoordinator, station_id: str) -> None:
        super().__init__(coordinator, station_id)
        self._attr_unique_id = f"{DOMAIN}_{station_id}_last_run"
        self._attr_icon = "mdi:calendar-check"

    @property
    def name(self) -> str:
        return f"{self._station_id} Last Run"

    @property
    def native_value(self) -> str:
        station = self._get_station()
        if not station:
            return "unknown"
        return station.get("last_run") or "never"
