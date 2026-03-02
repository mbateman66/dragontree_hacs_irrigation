# Dragontree Irrigation

A Home Assistant custom integration for managing an OpenSprinkler irrigation
controller, with AM/PM scheduling queues, rain modes, and a built-in Lovelace
dashboard.

## Features

- AM and PM run queues with configurable start times
- Per-station schedule modes: **Off**, **Normal**, **Hot**
- Rain modes (**None**, **Light**, **Heavy**) — sensitive stations skip watering
  in Light or Heavy rain
- Week-interval scheduling (run every 1–8 weeks)
- Services for adding, updating, removing, and reordering stations
- **Irrigation dashboard registered automatically** in the HA sidebar — no
  `configuration.yaml` edits required
- Lovelace cards served and registered automatically on setup

## Requirements

- Home Assistant 2024.1 or newer
- An [OpenSprinkler](https://opensprinkler.com/) controller accessible on your
  local network

## Installation via HACS

1. In HACS, go to **Integrations → Custom repositories**
2. Add `https://github.com/mbateman66/dragontree_hacs_irrigation` with category
   **Integration**
3. Search for **Dragontree Irrigation** and install it
4. Restart Home Assistant

## Post-install setup

**Settings → Devices & Services → Add Integration → Dragontree Irrigation**

After restarting, the **Irrigation** entry appears automatically in the HA
sidebar. No `configuration.yaml` changes are needed.

## Services

| Service | Description |
|---|---|
| `dragontree_irrigation.add_station` | Add a new station |
| `dragontree_irrigation.update_station` | Update station name, mode, or flags |
| `dragontree_irrigation.remove_station` | Remove a station |
| `dragontree_irrigation.reorder_stations` | Set the full run order |
| `dragontree_irrigation.move_station` | Shift a station one position up or down |
| `dragontree_irrigation.update_schedule` | Update normal or hot schedule for a station |
