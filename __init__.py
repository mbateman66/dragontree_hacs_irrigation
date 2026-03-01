"""Dragontree Irrigation custom component."""
from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv

from .const import (
    DOMAIN,
    PLATFORMS,
    SERVICE_ADD_STATION,
    SERVICE_MOVE_STATION,
    SERVICE_REMOVE_STATION,
    SERVICE_REORDER_STATIONS,
    SERVICE_UPDATE_SCHEDULE,
    SERVICE_UPDATE_STATION,
)
from .coordinator import IrrigationCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Dragontree Irrigation from a config entry."""
    coordinator = IrrigationCoordinator(hass)
    await coordinator.async_initialize()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    _register_services(hass, coordinator)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    coordinator: IrrigationCoordinator = hass.data[DOMAIN][entry.entry_id]
    coordinator.cleanup()

    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id)

    # Remove services if no more entries
    if not hass.data[DOMAIN]:
        for service in [
            SERVICE_ADD_STATION,
            SERVICE_UPDATE_STATION,
            SERVICE_REMOVE_STATION,
            SERVICE_REORDER_STATIONS,
            SERVICE_UPDATE_SCHEDULE,
            SERVICE_MOVE_STATION,
        ]:
            hass.services.async_remove(DOMAIN, service)

    return unloaded


def _register_services(hass: HomeAssistant, coordinator: IrrigationCoordinator) -> None:
    """Register component services."""

    async def handle_add_station(call: ServiceCall) -> None:
        await coordinator.async_add_station(dict(call.data))

    async def handle_update_station(call: ServiceCall) -> None:
        station_id = call.data["station_id"]
        data = {k: v for k, v in call.data.items() if k != "station_id"}
        await coordinator.async_update_station(station_id, data)

    async def handle_remove_station(call: ServiceCall) -> None:
        await coordinator.async_remove_station(call.data["station_id"])

    async def handle_reorder_stations(call: ServiceCall) -> None:
        await coordinator.async_reorder_stations(call.data["station_ids"])

    async def handle_update_schedule(call: ServiceCall) -> None:
        station_id = call.data["station_id"]
        schedule_type = call.data["schedule_type"]  # "normal" or "hot"
        data = {
            k: v
            for k, v in call.data.items()
            if k not in ("station_id", "schedule_type")
        }
        await coordinator.async_update_station_schedule(station_id, schedule_type, data)

    async def handle_move_station(call: ServiceCall) -> None:
        await coordinator.async_move_station(
            call.data["station_id"], call.data["direction"]
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_ADD_STATION,
        handle_add_station,
        schema=vol.Schema(
            {
                vol.Required("base_name"): cv.string,
                vol.Required("friendly_name"): cv.string,
                vol.Optional("schedule_mode"): vol.In(["Off", "Normal", "Hot"]),
                vol.Optional("sensitive"): cv.boolean,
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_UPDATE_STATION,
        handle_update_station,
        schema=vol.Schema(
            {
                vol.Required("station_id"): cv.string,
                vol.Optional("friendly_name"): cv.string,
                vol.Optional("schedule_mode"): vol.In(["Off", "Normal", "Hot"]),
                vol.Optional("sensitive"): cv.boolean,
                vol.Optional("tracked"): cv.boolean,
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_REMOVE_STATION,
        handle_remove_station,
        schema=vol.Schema({vol.Required("station_id"): cv.string}),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_REORDER_STATIONS,
        handle_reorder_stations,
        schema=vol.Schema({vol.Required("station_ids"): vol.All(cv.ensure_list, [cv.string])}),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_UPDATE_SCHEDULE,
        handle_update_schedule,
        schema=vol.Schema(
            {
                vol.Required("station_id"): cv.string,
                vol.Required("schedule_type"): vol.In(["normal", "hot"]),
                vol.Optional("am"): cv.boolean,
                vol.Optional("pm"): cv.boolean,
                vol.Optional("days_of_week"): vol.All(
                    cv.ensure_list,
                    [vol.All(vol.Coerce(int), vol.Range(min=0, max=6))],
                ),
                vol.Optional("week_interval"): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=8)
                ),
                vol.Optional("duration"): vol.All(
                    vol.Coerce(int), vol.Range(min=0, max=36000)
                ),
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_MOVE_STATION,
        handle_move_station,
        schema=vol.Schema(
            {
                vol.Required("station_id"): cv.string,
                vol.Required("direction"): vol.In(["up", "down"]),
            }
        ),
    )
