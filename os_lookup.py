"""Helpers for finding OpenSprinkler station entities by their physical index.

OpenSprinkler entity_ids are frozen at first discovery and never follow a
rename on the physical device, but every station entity exposes `index` (the
physical slot number, immutable) and `name` (the live display name) as state
attributes. Resolving entities by index instead of by name-derived entity_id
text means lookups can't go stale.
"""
from __future__ import annotations

from homeassistant.core import HomeAssistant


def find_os_station_entity(hass: HomeAssistant, domain: str, os_index: int) -> str | None:
    """Find the current entity_id of an OpenSprinkler station entity by its
    physical index, regardless of what its entity_id currently says."""
    for state in hass.states.async_all(domain):
        if (
            state.attributes.get("opensprinkler_type") == "station"
            and state.attributes.get("index") == os_index
        ):
            return state.entity_id
    return None
