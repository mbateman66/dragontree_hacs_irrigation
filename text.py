"""Text entities for Dragontree Irrigation."""
from __future__ import annotations

import logging

from homeassistant.components.text import TextEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, SIGNAL_STATIONS_UPDATED
from .coordinator import CONTROLLER_DEVICE_INFO, IrrigationCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: IrrigationCoordinator = hass.data[DOMAIN][entry.entry_id]

    known_ids: set[str] = set()
    entities = []
    for station in coordinator.stations:
        known_ids.add(station["id"])
        entities.append(StationFriendlyNameText(coordinator, station["id"]))

    async_add_entities(entities)

    @callback
    def _stations_updated() -> None:
        new_entities = []
        for station in coordinator.stations:
            if station["id"] not in known_ids:
                known_ids.add(station["id"])
                new_entities.append(StationFriendlyNameText(coordinator, station["id"]))
        if new_entities:
            async_add_entities(new_entities)

    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_STATIONS_UPDATED, _stations_updated)
    )


class StationFriendlyNameText(CoordinatorEntity, TextEntity):
    """Editable friendly name for a station."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:rename"
    _attr_native_max = 64

    def __init__(self, coordinator: IrrigationCoordinator, station_id: str) -> None:
        super().__init__(coordinator)
        self._station_id = station_id
        self._attr_unique_id = f"{DOMAIN}_{station_id}_friendly_name"
        self._attr_device_info = CONTROLLER_DEVICE_INFO

    @property
    def name(self) -> str:
        return f"{self._station_id} Friendly Name"

    def _get_station(self) -> dict | None:
        return next(
            (s for s in self.coordinator.stations if s["id"] == self._station_id), None
        )

    @property
    def native_value(self) -> str:
        s = self._get_station()
        return s.get("friendly_name", self._station_id) if s else self._station_id

    async def async_set_value(self, value: str) -> None:
        await self.coordinator.async_update_station(
            self._station_id, {"friendly_name": value}
        )
