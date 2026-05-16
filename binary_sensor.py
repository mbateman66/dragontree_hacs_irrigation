"""Binary sensor entities for Dragontree Irrigation."""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DEFAULT_AM_START_TIME, DEFAULT_PM_START_TIME, DOMAIN, SIGNAL_STATIONS_UPDATED
from .coordinator import CONTROLLER_DEVICE_INFO, IrrigationCoordinator, _parse_time


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: IrrigationCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[BinarySensorEntity] = [QueueTimeConflictBinarySensor(coordinator)]
    for station in coordinator.stations:
        entities.append(StationFlowAnomalyBinarySensor(coordinator, station["id"]))

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
            uid = f"{DOMAIN}_{station['id']}_flow_anomaly"
            if uid not in existing_ids:
                new_entities.append(
                    StationFlowAnomalyBinarySensor(coordinator, station["id"])
                )
        if new_entities:
            async_add_entities(new_entities)

    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_STATIONS_UPDATED, _stations_updated)
    )


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


class StationFlowAnomalyBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """True when the most recent flow reading for a station was anomalous."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_icon = "mdi:pipe-leak"

    def __init__(self, coordinator: IrrigationCoordinator, station_id: str) -> None:
        super().__init__(coordinator)
        self._station_id = station_id
        self._attr_unique_id = f"{DOMAIN}_{station_id}_flow_anomaly"
        self._attr_device_info = CONTROLLER_DEVICE_INFO

    @property
    def name(self) -> str:
        return f"{self._station_id} Flow Anomaly"

    @property
    def is_on(self) -> bool:
        station = next(
            (s for s in self.coordinator.stations if s["id"] == self._station_id),
            None,
        )
        if not station or not station.get("flow_monitoring"):
            return False
        return bool(
            self.coordinator.flow_monitor.get_station_flow_state(
                self._station_id
            ).get("last_anomaly", False)
        )
