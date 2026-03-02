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
from homeassistant.components.frontend import add_extra_js_url, async_remove_panel
from homeassistant.components.http import StaticPathConfig
from homeassistant.components.lovelace import _register_panel
from homeassistant.components.lovelace.dashboard import LovelaceYAML
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.storage import Store

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

    # Register the Lovelace card JS module
    _register_frontend(hass)

    # Register the Irrigation dashboard in the HA sidebar
    _register_dashboard(hass)

    # One-time migration: remove stale /local/* entries from lovelace_resources store
    await _cleanup_old_lovelace_resource(hass)

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


def _register_frontend(hass: HomeAssistant) -> None:
    """Register the bundled Lovelace card JS as a frontend module.

    add_extra_js_url injects the URL into every Lovelace page load — equivalent
    to a resources entry but done entirely at runtime, no storage file needed.
    """
    url = f"{_JS_URL_BASE}/dragontree-irrigation-cards.js?v={_VERSION}"
    add_extra_js_url(hass, url)
    _LOGGER.debug("Registered Lovelace card module: %s", url)


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


async def _cleanup_old_lovelace_resource(hass: HomeAssistant) -> None:
    """Remove stale /local/* resource entries for this integration.

    Previous versions stored the card URL in .storage/lovelace_resources.
    That approach is replaced by add_extra_js_url — this function removes
    any leftover entries on the first run after upgrading.
    """
    store = Store(hass, 1, "lovelace_resources", minor_version=1)
    data = await store.async_load()
    if data is None:
        return

    items: list[dict] = data.get("items", [])
    cleaned = [
        i for i in items
        if "dragontree-irrigation" not in i.get("url", "")
    ]
    if len(cleaned) != len(items):
        await store.async_save({"items": cleaned})
        _LOGGER.info(
            "Removed %d stale lovelace_resources entry(s) for %s",
            len(items) - len(cleaned),
            DOMAIN,
        )


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
