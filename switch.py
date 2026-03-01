"""Switch entities for Dragontree Irrigation."""
from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, SIGNAL_STATIONS_UPDATED
from .coordinator import CONTROLLER_DEVICE_INFO, IrrigationCoordinator

_LOGGER = logging.getLogger(__name__)

_DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: IrrigationCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[SwitchEntity] = [IrrigationMasterSwitch(coordinator)]
    for station in coordinator.stations:
        entities.extend(_station_switches(coordinator, station["id"]))

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
            for uid, cls_args in _station_switch_specs(coordinator, station["id"]):
                if uid not in existing_ids:
                    new_entities.append(cls_args)
        if new_entities:
            async_add_entities(new_entities)

    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_STATIONS_UPDATED, _stations_updated)
    )


def _station_switches(coordinator: IrrigationCoordinator, station_id: str) -> list:
    return [obj for _, obj in _station_switch_specs(coordinator, station_id)]


def _station_switch_specs(
    coordinator: IrrigationCoordinator, station_id: str
) -> list[tuple[str, SwitchEntity]]:
    specs = []
    sid = station_id

    specs.append(
        (f"{DOMAIN}_{sid}_tracked", StationTrackedSwitch(coordinator, sid))
    )
    specs.append(
        (f"{DOMAIN}_{sid}_sensitive", StationSensitiveSwitch(coordinator, sid))
    )

    for stype in ("normal", "hot"):
        for queue in ("am", "pm"):
            uid = f"{DOMAIN}_{sid}_{stype}_{queue}"
            specs.append((uid, ScheduleQueueSwitch(coordinator, sid, stype, queue)))

        for day_idx in range(7):
            uid = f"{DOMAIN}_{sid}_{stype}_day_{day_idx}"
            specs.append((uid, ScheduleDaySwitch(coordinator, sid, stype, day_idx)))

    return specs


# ---------------------------------------------------------------------------
# Global
# ---------------------------------------------------------------------------

class IrrigationMasterSwitch(CoordinatorEntity, SwitchEntity):
    """Master enable/disable for the irrigation system."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:water-pump"
    _attr_name = "Master Enable"

    def __init__(self, coordinator: IrrigationCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_master_enable"
        self._attr_device_info = CONTROLLER_DEVICE_INFO

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.global_config.get("master_enable", False))

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_update_global({"master_enable": True})

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_update_global({"master_enable": False})


# ---------------------------------------------------------------------------
# Per-station: tracked toggle (included in scheduling)
# ---------------------------------------------------------------------------

class StationTrackedSwitch(CoordinatorEntity, SwitchEntity):
    """Toggle whether a station is tracked (included in scheduling).

    When off the station still appears in the management view but is skipped
    by the scheduler — useful for OS stations that haven't been named yet or
    that the user doesn't want to manage through this component.
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:eye"

    def __init__(self, coordinator: IrrigationCoordinator, station_id: str) -> None:
        super().__init__(coordinator)
        self._station_id = station_id
        self._attr_unique_id = f"{DOMAIN}_{station_id}_tracked"
        self._attr_device_info = CONTROLLER_DEVICE_INFO

    def _get_station(self) -> dict | None:
        return next(
            (s for s in self.coordinator.stations if s["id"] == self._station_id), None
        )

    @property
    def name(self) -> str:
        return f"{self._station_id} Tracked"

    @property
    def is_on(self) -> bool:
        s = self._get_station()
        return bool(s.get("tracked", True)) if s else False

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_update_station(self._station_id, {"tracked": True})

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_update_station(self._station_id, {"tracked": False})


# ---------------------------------------------------------------------------
# Per-station: sensitive toggle
# ---------------------------------------------------------------------------

class StationSensitiveSwitch(CoordinatorEntity, SwitchEntity):
    """Toggle whether a station is sensitive (runs in light rain)."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:water-alert"

    def __init__(self, coordinator: IrrigationCoordinator, station_id: str) -> None:
        super().__init__(coordinator)
        self._station_id = station_id
        self._attr_unique_id = f"{DOMAIN}_{station_id}_sensitive"
        self._attr_device_info = CONTROLLER_DEVICE_INFO

    def _get_station(self) -> dict | None:
        return next(
            (s for s in self.coordinator.stations if s["id"] == self._station_id), None
        )

    @property
    def name(self) -> str:
        return f"{self._station_id} Sensitive"

    @property
    def is_on(self) -> bool:
        s = self._get_station()
        return bool(s.get("sensitive", False)) if s else False

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_update_station(self._station_id, {"sensitive": True})

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_update_station(self._station_id, {"sensitive": False})


# ---------------------------------------------------------------------------
# Per-station schedule: AM / PM queue membership
# ---------------------------------------------------------------------------

class ScheduleQueueSwitch(CoordinatorEntity, SwitchEntity):
    """Toggle AM or PM queue membership for a station schedule."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: IrrigationCoordinator,
        station_id: str,
        schedule_type: str,  # "normal" | "hot"
        queue: str,          # "am" | "pm"
    ) -> None:
        super().__init__(coordinator)
        self._station_id = station_id
        self._schedule_type = schedule_type
        self._queue = queue
        self._attr_unique_id = f"{DOMAIN}_{station_id}_{schedule_type}_{queue}"
        self._attr_device_info = CONTROLLER_DEVICE_INFO
        self._attr_icon = (
            "mdi:weather-sunny" if queue == "am" else "mdi:weather-night"
        )

    @property
    def name(self) -> str:
        return (
            f"{self._station_id} {self._schedule_type.title()} {self._queue.upper()}"
        )

    def _get_schedule(self) -> dict:
        station = next(
            (s for s in self.coordinator.stations if s["id"] == self._station_id), None
        )
        if not station:
            return {}
        return station.get(f"{self._schedule_type}_schedule") or {}

    @property
    def is_on(self) -> bool:
        default = self._queue == "am"  # AM defaults True, PM defaults False
        return bool(self._get_schedule().get(self._queue, default))

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_update_station_schedule(
            self._station_id, self._schedule_type, {self._queue: True}
        )

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_update_station_schedule(
            self._station_id, self._schedule_type, {self._queue: False}
        )


# ---------------------------------------------------------------------------
# Per-station schedule: day-of-week toggles
# ---------------------------------------------------------------------------

class ScheduleDaySwitch(CoordinatorEntity, SwitchEntity):
    """Toggle a specific day of week for a station schedule."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:calendar-today"

    def __init__(
        self,
        coordinator: IrrigationCoordinator,
        station_id: str,
        schedule_type: str,
        day_index: int,  # 0=Mon … 6=Sun
    ) -> None:
        super().__init__(coordinator)
        self._station_id = station_id
        self._schedule_type = schedule_type
        self._day_index = day_index
        self._attr_unique_id = (
            f"{DOMAIN}_{station_id}_{schedule_type}_day_{day_index}"
        )
        self._attr_device_info = CONTROLLER_DEVICE_INFO

    @property
    def name(self) -> str:
        return (
            f"{self._station_id} {self._schedule_type.title()} "
            f"{_DAY_NAMES[self._day_index]}"
        )

    def _get_schedule(self) -> dict:
        station = next(
            (s for s in self.coordinator.stations if s["id"] == self._station_id), None
        )
        if not station:
            return {}
        return station.get(f"{self._schedule_type}_schedule") or {}

    @property
    def is_on(self) -> bool:
        return self._day_index in self._get_schedule().get("days_of_week", [])

    async def async_turn_on(self, **kwargs) -> None:
        days = list(self._get_schedule().get("days_of_week", []))
        if self._day_index not in days:
            days.append(self._day_index)
            days.sort()
        await self.coordinator.async_update_station_schedule(
            self._station_id, self._schedule_type, {"days_of_week": days}
        )

    async def async_turn_off(self, **kwargs) -> None:
        days = [
            d
            for d in self._get_schedule().get("days_of_week", [])
            if d != self._day_index
        ]
        await self.coordinator.async_update_station_schedule(
            self._station_id, self._schedule_type, {"days_of_week": days}
        )
