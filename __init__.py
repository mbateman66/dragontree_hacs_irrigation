"""Dragontree Irrigation custom component.

The Lovelace cards (dragontree-irrigation-cards.js) are served automatically
from the integration package and registered as a frontend module on first setup.
The Irrigation dashboard is registered in the HA sidebar automatically.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import voluptuous as vol
from homeassistant.components.frontend import async_remove_panel
from homeassistant.components.http import StaticPathConfig
from homeassistant.components.lovelace import _register_panel
from homeassistant.components.lovelace.dashboard import LovelaceYAML
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

if TYPE_CHECKING:
    pass

_LOGGER = logging.getLogger(__name__)

# URL path where the bundled JS directory is served
_JS_URL_BASE = f"/{DOMAIN}/js"
_JS_DIR = Path(__file__).parent / "js"

# Lovelace dashboard slug and YAML path (relative to HA config dir)
_DASHBOARD_URL = "dragontree-irrigation"
_DASHBOARD_YAML = f"custom_components/{DOMAIN}/lovelace/ui-lovelace.yaml"

# Read version once at import time — manifest.json is static while HA is running.
try:
    _VERSION = json.loads((Path(__file__).parent / "manifest.json").read_text()).get("version", "0.0.0")
except Exception:
    _VERSION = "0.0.0"


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Serve the bundled JS directory as a static HTTP path.

    This runs once when HA loads the integration domain, before any config
    entries are set up.
    """
    if _JS_DIR.exists():
        await hass.http.async_register_static_paths(
            [StaticPathConfig(_JS_URL_BASE, str(_JS_DIR), cache_headers=True)]
        )
        _LOGGER.debug("Registered static path %s → %s", _JS_URL_BASE, _JS_DIR)
    else:
        _LOGGER.warning(
            "JS directory not found at %s — Lovelace card will not be available",
            _JS_DIR,
        )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Dragontree Irrigation from a config entry."""
    coordinator = IrrigationCoordinator(hass)
    await coordinator.async_initialize()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    _register_services(hass, coordinator)

    # Register the Lovelace card JS in lovelace_resources (Lovelace waits for these before rendering)
    await _ensure_lovelace_resource(hass)

    # Register the Irrigation dashboard in the HA sidebar
    _register_dashboard(hass)

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


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove the dashboard panel when the integration is deleted."""
    try:
        async_remove_panel(hass, _DASHBOARD_URL)
    except Exception:
        pass
    lovelace = hass.data.get("lovelace")
    if lovelace is not None:
        lovelace.dashboards.pop(_DASHBOARD_URL, None)


async def _ensure_lovelace_resource(hass: HomeAssistant) -> None:
    """Register the card JS via Lovelace's ResourceStorageCollection.

    Writing directly to the Store file only updates the JSON on disk — it
    bypasses HA's in-memory resource collection and never sends a WebSocket
    push to connected frontends.  Using the collection API keeps the in-memory
    state, the storage file, and all connected clients in sync.

    The collection's async_create_item() expects "res_type" (the WS field name),
    which is internally converted to "type" when stored.
    """
    url = f"{_JS_URL_BASE}/dragontree-irrigation-cards.js?v={_VERSION}"
    lovelace = hass.data.get("lovelace")
    if lovelace is None:
        _LOGGER.warning("Lovelace not initialised — cannot register resource")
        return

    resources = getattr(lovelace, "resources", None)
    if resources is None or not hasattr(resources, "async_create_item"):
        _LOGGER.warning("Lovelace resource collection not available (resource_mode may not be storage)")
        return

    # Remove any existing entries for this integration (old version or old path)
    for item in list(resources.async_items()):
        item_url = item.get("url", "")
        if _JS_URL_BASE in item_url or "dragontree-irrigation" in item_url:
            try:
                await resources.async_delete_item(item["id"])
            except Exception:
                pass

    await resources.async_create_item({"res_type": "module", "url": url})
    _LOGGER.info("Registered Lovelace resource: %s", url)


def _register_dashboard(hass: HomeAssistant) -> None:
    """Register the Irrigation YAML dashboard in the HA sidebar.

    Uses LovelaceYAML to point directly at the bundled YAML inside the
    integration package — no file copying required.  _register_panel with
    update=False is a no-op if the panel is already registered, so this is
    safe to call on every entry reload.
    """
    lovelace = hass.data.get("lovelace")
    if lovelace is None:
        _LOGGER.warning("Lovelace not initialised — Irrigation dashboard not registered")
        return

    config = {
        "mode": "yaml",
        "icon": "mdi:sprinkler",
        "title": "Irrigation",
        "filename": _DASHBOARD_YAML,
        "show_in_sidebar": True,
        "require_admin": False,
    }

    lovelace.dashboards[_DASHBOARD_URL] = LovelaceYAML(hass, _DASHBOARD_URL, config)
    _register_panel(hass, _DASHBOARD_URL, "yaml", config, False)
    _LOGGER.info("Irrigation dashboard registered at /%s", _DASHBOARD_URL)


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
