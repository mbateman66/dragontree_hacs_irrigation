"""Select entities for Dragontree Irrigation."""
from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DEFAULT_RAIN_MODE,
    DOMAIN,
    RAIN_MODES,
    SCHEDULE_MODE_NORMAL,
    SCHEDULE_MODES,
    SIGNAL_STATIONS_UPDATED,
)
from .coordinator import CONTROLLER_DEVICE_INFO, IrrigationCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: IrrigationCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[SelectEntity] = [IrrigationRainModeSelect(coordinator)]
    for station in coordinator.stations:
        entities.append(StationScheduleModeSelect(coordinator, station["id"]))

    async_add_entities(entities)

    @callback
    def _stations_updated() -> None:
        new_entities = []
        for station in coordinator.stations:
            uid = f"{DOMAIN}_{station['id']}_schedule_mode"
            # Simple check: try to find entity in registry
            existing = hass.states.get(f"select.dragontree_irrigation_{station['id']}_schedule_mode")
            if existing is None:
                new_entities.append(StationScheduleModeSelect(coordinator, station["id"]))
        if new_entities:
            async_add_entities(new_entities)

    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_STATIONS_UPDATED, _stations_updated)
    )


class IrrigationRainModeSelect(CoordinatorEntity, SelectEntity):
    """Select rain mode for the irrigation system."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:weather-rainy"
    _attr_options = RAIN_MODES
    _attr_name = "Rain Mode"

    def __init__(self, coordinator: IrrigationCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_rain_mode"
        self._attr_device_info = CONTROLLER_DEVICE_INFO

    @property
    def current_option(self) -> str:
        return self.coordinator.global_config.get("rain_mode", DEFAULT_RAIN_MODE)

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_update_global({"rain_mode": option})


class StationScheduleModeSelect(CoordinatorEntity, SelectEntity):
    """Select schedule mode (Off / Normal / Hot) for an individual station."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:calendar-edit"
    _attr_options = SCHEDULE_MODES

    def __init__(self, coordinator: IrrigationCoordinator, station_id: str) -> None:
        super().__init__(coordinator)
        self._station_id = station_id
        self._attr_unique_id = f"{DOMAIN}_{station_id}_schedule_mode"
        self._attr_device_info = CONTROLLER_DEVICE_INFO

    def _get_station(self) -> dict | None:
        return next(
            (s for s in self.coordinator.stations if s["id"] == self._station_id), None
        )

    @property
    def name(self) -> str:
        return f"{self._station_id} Schedule Mode"

    @property
    def current_option(self) -> str:
        s = self._get_station()
        return s.get("schedule_mode", SCHEDULE_MODE_NORMAL) if s else SCHEDULE_MODE_NORMAL

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_update_station(
            self._station_id, {"schedule_mode": option}
        )
