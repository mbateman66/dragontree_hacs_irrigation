"""Time entities for Dragontree Irrigation (AM/PM queue start times)."""
from __future__ import annotations

import logging
from datetime import time

from homeassistant.components.time import TimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DEFAULT_AM_START_TIME, DEFAULT_PM_START_TIME, DOMAIN
from .coordinator import CONTROLLER_DEVICE_INFO, IrrigationCoordinator, _parse_time

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: IrrigationCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            IrrigationStartTime(coordinator, "am", "AM Queue Start", DEFAULT_AM_START_TIME),
            IrrigationStartTime(coordinator, "pm", "PM Queue Start", DEFAULT_PM_START_TIME),
        ]
    )


class IrrigationStartTime(CoordinatorEntity, TimeEntity):
    """Configurable start time for AM or PM queue."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:clock-start"

    def __init__(
        self,
        coordinator: IrrigationCoordinator,
        queue: str,
        name: str,
        default: str,
    ) -> None:
        super().__init__(coordinator)
        self._queue = queue
        self._config_key = f"start_time_{queue}"
        self._default = default
        self._attr_name = name
        self._attr_unique_id = f"{DOMAIN}_start_time_{queue}"
        self._attr_device_info = CONTROLLER_DEVICE_INFO

    @property
    def native_value(self) -> time:
        time_str = self.coordinator.global_config.get(self._config_key, self._default)
        h, m = _parse_time(time_str)
        return time(h, m)

    async def async_set_value(self, value: time) -> None:
        time_str = value.strftime("%H:%M")
        await self.coordinator.async_update_global({self._config_key: time_str})
