# Run Stations Tab — Design Spec
**Date:** 2026-06-27

## Overview

Add a new "Run Stations" tab to the irrigation dashboard that lets the user manually start and stop individual stations. Only one station can run at a time. Start is blocked when any station is running or a queue is active. Stop cancels the current station (and advances the queue if one is running). Each station remembers its own last-used manual run duration, stored server-side so it persists across browsers and devices.

---

## Backend

### New services

**`dragontree_irrigation.run_station_manual`**
- Fields: `station_id` (required), `duration_seconds` (required, int, 60–7200)
- Validates that `_runtime["running_queue"]` is `None` AND `_runtime["current_station_id"]` is `None`; raises `HomeAssistantError` if either is set
- Calls `opensprinkler.run_station` with `run_seconds: duration_seconds` targeting `switch.<base_name>_station_enabled`
- Starts flow monitoring for the station if `station.flow_monitoring` is enabled

**`dragontree_irrigation.stop_station_manual`**
- No fields
- Two cases:
  - **Queue running** (`current_station_id` is set): sets `_manual_stop_requested = True` on the coordinator, then calls `opensprinkler.stop` on the current station's switch entity
  - **Manual run** (no queue): scans `binary_sensor.<base>_station_running` across all tracked stations to find the one that is `on`, then calls `opensprinkler.stop` on it

### Queue cancellation on manual stop

`_run_queue` checks `_manual_stop_requested` immediately after `_wait_for_station` returns. If the flag is set:
- Marks `station_entry["status"] = STATUS_CANCELLED` (instead of `STATUS_COMPLETE`)
- Clears `_manual_stop_requested`
- Continues to the next station in the queue as normal

The flag is initialised to `False` in `IrrigationCoordinator.__init__`.

### Per-station manual duration

`manual_duration` (integer, minutes, default `5`) is added to each station dict. Added to the migration guard in `async_initialize` via `s.setdefault("manual_duration", 5)`.

Exposed automatically through the existing `stations` list in `sensor.dragontree_irrigation_schedule` attributes (already copies each station via `dict(s)`).

Persisted by adding `manual_duration` as an optional field to the existing `update_station` service schema:
```
vol.Optional("manual_duration"): vol.All(vol.Coerce(int), vol.Range(min=1, max=120))
```
The card calls this service on blur when the user changes the duration input.

### New constants (`const.py`)

```python
SERVICE_RUN_STATION_MANUAL = "run_station_manual"
SERVICE_STOP_STATION_MANUAL = "stop_station_manual"
```

---

## Frontend

### New custom element: `DragontreeStationControl`

Added to `js/dragontree-irrigation-cards.js`. Follows the same patterns as `DragontreeStationManager`:
- Shadow DOM created once in `setConfig`
- Rows created once in `_makeRow`, listeners attached permanently
- `_patchRow` updates content in-place on each `hass` update
- Change-key diffing in `set hass()` to avoid unnecessary DOM work

**Lovelace config:**
```yaml
type: custom:dragontree-irrigation-station-control
```

### Data sources

| Data | Source |
|------|--------|
| Station list (order, names, `manual_duration`) | `sensor.dragontree_irrigation_schedule` → `attributes.stations` |
| Which station is physically running | `binary_sensor.<base_name>_station_running` per station |
| Whether a queue is active | `sensor.dragontree_irrigation_running_queue` state (`"idle"` or `"AM"` / `"PM"`) |

### Change key

Built from: stations list JSON + per-station binary sensor states + running queue sensor state. Re-patches rows only when this key changes.

### Table layout

Columns (in order): **Station Name | Stop | Start | Duration**

| Column | Details |
|--------|---------|
| Station Name | `station.friendly_name`; same order as Manage Stations (order from `stations` attribute) |
| Stop | Button (red/error color). Enabled only when `binary_sensor.<base>_station_running` is `on` for this specific station. Calls `stop_station_manual()` |
| Start | Button (green/primary color). Disabled when any binary sensor is `on` OR `sensor.dragontree_irrigation_running_queue` ≠ `"idle"`. Calls `run_station_manual(station_id, manual_duration * 60)` |
| Duration | `<input type="number">`, range 1–120 min, value from `station.manual_duration`. Updates on blur via `update_station(station_id, manual_duration)`. Skips HA update if value unchanged. Uses `_editing` flag to block hass-driven re-renders while focused |

### Visual treatment

- The row for the currently running station gets a highlighted style (primary-color left border or background tint) so it is visually obvious which station is active.
- Untracked stations (`station.tracked === false`) are excluded from the list (same as schedules view).

### New Lovelace view

File: `lovelace/views/05.run.yaml`

The existing `lovelace/views/05.flow.yaml` is renamed to `06.flow.yaml` to preserve logical tab ordering:

```
01.calendar → 02.controller → 03.schedules → 04.stations → 05.run → 06.flow
```

View config:
```yaml
- title: Run Stations
  path: run-stations
  icon: mdi:play-circle
  type: panel
  cards:
    - type: custom:dragontree-irrigation-station-control
```

---

## Out of scope

- No support for running multiple stations simultaneously
- No queue-position manipulation (stop-and-skip vs stop-and-cancel is not configurable; it always cancels the current entry and advances)
- No automation-visible entities for `manual_duration` (stored in coordinator, readable from sensor attributes only)
