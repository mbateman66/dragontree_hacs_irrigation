"""Number entities for Dragontree Irrigation."""
from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DEFAULT_LOOKAHEAD_DAYS, DOMAIN, SIGNAL_STATIONS_UPDATED
from .coordinator import CONTROLLER_DEVICE_INFO, IrrigationCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: IrrigationCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[NumberEntity] = [IrrigationLookaheadDays(coordinator)]
    for station in coordinator.stations:
        entities.extend(_station_numbers(coordinator, station["id"]))

    async_add_entities(entities)

    @callback
    def _stations_updated() -> None:
        existing_ids = {
            e.unique_id
            for platform in hass.data.get("entity_platform", {}).get(DOMAIN, [])
            for e in getattr(platform, "entities", {}).values()
        }
        new_entities = []
        for station in coordinator.stations:
            for uid, obj in _station_number_specs(coordinator, station["id"]):
                if uid not in existing_ids:
                    new_entities.append(obj)
        if new_entities:
            async_add_entities(new_entities)

    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_STATIONS_UPDATED, _stations_updated)
    )


def _station_numbers(coordinator: IrrigationCoordinator, station_id: str) -> list:
    return [obj for _, obj in _station_number_specs(coordinator, station_id)]


def _station_number_specs(
    coordinator: IrrigationCoordinator, station_id: str
) -> list[tuple[str, NumberEntity]]:
    specs = []
    for stype in ("normal", "hot"):
        uid_wi = f"{DOMAIN}_{station_id}_{stype}_week_interval"
        uid_dur = f"{DOMAIN}_{station_id}_{stype}_duration"
        specs.append((uid_wi, ScheduleWeekIntervalNumber(coordinator, station_id, stype)))
        specs.append((uid_dur, ScheduleDurationNumber(coordinator, station_id, stype)))
    return specs


# ---------------------------------------------------------------------------
# Global
# ---------------------------------------------------------------------------

class IrrigationLookaheadDays(CoordinatorEntity, NumberEntity):
    """Number of days ahead to generate and display queues."""

    _attr_has_entity_name = True
    _attr_name = "Lookahead Days"
    _attr_icon = "mdi:calendar-range"
    _attr_native_min_value = 1
    _attr_native_max_value = 7
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator: IrrigationCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_lookahead_days"
        self._attr_device_info = CONTROLLER_DEVICE_INFO

    @property
    def native_value(self) -> float:
        return float(
            self.coordinator.global_config.get("lookahead_days", DEFAULT_LOOKAHEAD_DAYS)
        )

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_update_global({"lookahead_days": int(value)})


# ---------------------------------------------------------------------------
# Per-station schedule: week interval
# ---------------------------------------------------------------------------

class ScheduleWeekIntervalNumber(CoordinatorEntity, NumberEntity):
    """How many weeks between runs for a station schedule."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:calendar-week"
    _attr_native_min_value = 1
    _attr_native_max_value = 8
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX

    def __init__(
        self,
        coordinator: IrrigationCoordinator,
        station_id: str,
        schedule_type: str,  # "normal" | "hot"
    ) -> None:
        super().__init__(coordinator)
        self._station_id = station_id
        self._schedule_type = schedule_type
        self._attr_unique_id = f"{DOMAIN}_{station_id}_{schedule_type}_week_interval"
        self._attr_device_info = CONTROLLER_DEVICE_INFO

    @property
    def name(self) -> str:
        return f"{self._station_id} {self._schedule_type.title()} Week Interval"

    def _get_schedule(self) -> dict:
        station = next(
            (s for s in self.coordinator.stations if s["id"] == self._station_id), None
        )
        if not station:
            return {}
        return station.get(f"{self._schedule_type}_schedule") or {}

    @property
    def native_value(self) -> float:
        return float(self._get_schedule().get("week_interval", 1))

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_update_station_schedule(
            self._station_id, self._schedule_type, {"week_interval": int(value)}
        )


# ---------------------------------------------------------------------------
# Per-station schedule: duration (stored as seconds, shown as minutes)
# ---------------------------------------------------------------------------

class ScheduleDurationNumber(CoordinatorEntity, NumberEntity):
    """Run duration in minutes for a station schedule."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:timer"
    _attr_native_min_value = 0
    _attr_native_max_value = 600  # 10 hours in minutes
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "min"
    _attr_mode = NumberMode.BOX

    def __init__(
        self,
        coordinator: IrrigationCoordinator,
        station_id: str,
        schedule_type: str,
    ) -> None:
        super().__init__(coordinator)
        self._station_id = station_id
        self._schedule_type = schedule_type
        self._attr_unique_id = f"{DOMAIN}_{station_id}_{schedule_type}_duration"
        self._attr_device_info = CONTROLLER_DEVICE_INFO

    @property
    def name(self) -> str:
        return f"{self._station_id} {self._schedule_type.title()} Duration"

    def _get_schedule(self) -> dict:
        station = next(
            (s for s in self.coordinator.stations if s["id"] == self._station_id), None
        )
        if not station:
            return {}
        return station.get(f"{self._schedule_type}_schedule") or {}

    @property
    def native_value(self) -> float:
        secs = self._get_schedule().get("duration", 600)
        return round(float(secs) / 60.0, 1)

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_update_station_schedule(
            self._station_id, self._schedule_type, {"duration": int(value * 60)}
        )
