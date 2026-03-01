"""Binary sensor entities for Dragontree Irrigation."""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DEFAULT_AM_START_TIME, DEFAULT_PM_START_TIME, DOMAIN
from .coordinator import CONTROLLER_DEVICE_INFO, IrrigationCoordinator, _parse_time


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: IrrigationCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([QueueTimeConflictBinarySensor(coordinator)])


class QueueTimeConflictBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """True when AM queue start is not before PM queue start."""

    _attr_has_entity_name = True
    _attr_name = "Queue Time Conflict"
    _attr_icon = "mdi:clock-alert"

    def __init__(self, coordinator: IrrigationCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_queue_time_conflict"
        self._attr_device_info = CONTROLLER_DEVICE_INFO

    @property
    def is_on(self) -> bool:
        cfg = self.coordinator.global_config
        am_h, am_m = _parse_time(cfg.get("start_time_am", DEFAULT_AM_START_TIME))
        pm_h, pm_m = _parse_time(cfg.get("start_time_pm", DEFAULT_PM_START_TIME))
        return (am_h * 60 + am_m) >= (pm_h * 60 + pm_m)
