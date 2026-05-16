"""Button entities for Dragontree Irrigation."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, SIGNAL_STATIONS_UPDATED
from .coordinator import CONTROLLER_DEVICE_INFO, IrrigationCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: IrrigationCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[ButtonEntity] = []
    for station in coordinator.stations:
        entities.append(ResetFlowProfileButton(coordinator, station["id"]))

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
            uid = f"{DOMAIN}_{station['id']}_reset_flow_profile"
            if uid not in existing_ids:
                new_entities.append(ResetFlowProfileButton(coordinator, station["id"]))
        if new_entities:
            async_add_entities(new_entities)

    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_STATIONS_UPDATED, _stations_updated)
    )


class ResetFlowProfileButton(CoordinatorEntity, ButtonEntity):
    """Clears the learned flow baseline for a station so it re-enters learning mode.

    Use this after replacing a pipe, sprinkler head, or any hardware change that
    would legitimately alter the station's expected flow rate.
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:restore"

    def __init__(self, coordinator: IrrigationCoordinator, station_id: str) -> None:
        super().__init__(coordinator)
        self._station_id = station_id
        self._attr_unique_id = f"{DOMAIN}_{station_id}_reset_flow_profile"
        self._attr_device_info = CONTROLLER_DEVICE_INFO

    @property
    def name(self) -> str:
        return f"{self._station_id} Reset Flow Profile"

    async def async_press(self) -> None:
        await self.coordinator.async_reset_flow_profile(self._station_id)
