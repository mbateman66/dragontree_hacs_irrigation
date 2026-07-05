# Changelog

All notable changes to this project will be documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

## [1.3.2] - 2026-07-05

### Fixed
- **Moisture sensor/threshold updates failed on renamed stations** — the Station
  Schedules card's moisture panel sent `station.base_name` as the `update_station`
  service's `station_id`, but the backend looks stations up by their immutable `id`.
  This was invisible for never-renamed stations (`id == base_name`) but broke as
  soon as a station was renamed. The panel now sends `station.id`.

## [1.3.1] - 2026-07-05

### Fixed
- **Renaming a station in OS/HA created a duplicate station on the next restart** —
  `_merge_discover_stations` matched newly-discovered OpenSprinkler entities against
  each tracked station's immutable `id`, but renaming only updates `base_name`. On the
  next restart/reload the renamed station's new entity_id no longer matched any known
  `id`, so it was re-added as a brand-new station, duplicating every entity for that
  station. Discovery now matches against current `base_name` instead.

## [1.3.0] - 2026-06-28

### Added
- **Run Stations tab** — new dashboard tab for manually starting and stopping individual
  stations. Each station has a Start button (disabled when any station or queue is running),
  a Stop button (enabled only when that station is running), and a per-station duration
  input (in minutes, stored server-side so it persists across browsers/devices). Stopping a
  station mid-queue marks it as cancelled and advances to the next entry.
- **Moisture Sensors tab** — new dashboard tab listing all unique moisture sensors
  configured across stations. Sensors shared by multiple stations are deduplicated. Each
  row shows the current value and associated station names; clicking a row opens the HA
  more-info dialog with full sensor history.
- **`start_station` service** — manually start a station by ID with a specified duration
  in seconds. Blocked if any station or queue is already running.
- **`stop_station` service** — stop whatever station is currently running. If a scheduled
  queue is active, the current station is marked `cancelled` and the queue advances.
- **`manual_duration` field on stations** — per-station default duration for manual runs
  (integer, minutes, default 5). Exposed via `sensor.dragontree_irrigation_schedule`
  attributes and updatable via the existing `update_station` service.

### Fixed
- **Stop did not work when clicked before binary sensor confirmed start** — the stop
  handler scanned binary sensors to find the running station, but sensors take 5–6 seconds
  to update after a start command. If Stop was clicked first, no station was found and no
  stop was sent to OpenSprinkler. Fixed by tracking `_manual_station_id` in the coordinator
  so the correct station can be stopped immediately.
- **Start→Stop UI race** — clicking Stop immediately after Start caused the UI to briefly
  revert to showing the station as running (delayed binary sensor ON firing after the
  optimistic stop). Fixed with `_sawOnAfterStop` flag: optimistic stopped state now
  persists until the binary sensor transitions ON→OFF, confirming the stop.
- **`stop_station` service not removed on integration unload** — the service handler
  remained registered after the integration was unloaded.
- **`OS_SERVICE_STOP` not imported in coordinator** — would have caused a `NameError` on
  queue timeout paths that call `_stop_any_running_stations`.

### Changed
- **Dashboard reorganised** — Controller tab removed; global settings moved to new Config
  tab (alongside station manager). Irrigation Status card moved to top of Calendar tab and
  now also shows the active station during manual runs. Tab order: Calendar | Run |
  Moisture | Station | Flow | Config.

## [1.2.6] - 2026-06-25

### Fixed
- **Flow status shows "idle" during active monitoring** — when the coordinator starts a
  station, it proactively calls `_start_monitoring` before the OpenSprinkler binary sensor
  turns on. When the sensor fires moments later, `_start_monitoring` is called a second time,
  cancelling the first task. The cancelled task's `_analyze_run` cleanup then overwrote the
  new task's "monitoring" status with "idle". Fixed by checking whether a live monitoring
  task is already running before overwriting status in `_analyze_run`.
- **Global flow config inputs revert on every HA state update** — the alert threshold, min
  runs, and sample interval inputs in the flow monitoring config panel used
  `document.activeElement` to detect user focus, which always returns the shadow host element
  (not the inner input) in Shadow DOM. Inputs were therefore reset to the saved value on
  every HA state update, making them impossible to edit. Fixed by using
  `this.shadowRoot.activeElement` instead.

### Changed
- **Default flow baseline runs reduced from 5 to 3** — anomaly detection now activates after
  3 valid station runs instead of 5.

## [1.2.5] - 2026-06-21

### Added
- **Entity health warning icon** — each station row in the station manager card now shows
  a ⚠ icon inline next to the station name when any of its required OpenSprinkler entities
  (`_station_enabled`, `_station_running`, `_station_status`) are missing or unavailable.
  Hovering the icon lists the specific problem entities.
- **Persistent notification for missing entities** — on startup and whenever the entity
  registry changes, the coordinator checks all station entities and creates a HA persistent
  notification listing any that are missing or unavailable. The notification is dismissed
  automatically once all entities are healthy.

### Fixed
- **ImportError on HA restart** — `async_track_entity_registry_updated_event` was imported
  from `homeassistant.helpers.entity_registry` but does not exist in HA 2026.6.4, causing
  the integration to fail to load entirely. Replaced with `hass.bus.async_listen(
  "entity_registry_updated", ...)` which is the correct API for this HA version.

## [1.2.4] - 2026-06-08

### Fixed
- **`update_station` action now accepts `base_name`** — the field was wired in the
  coordinator but missing from the service schema and `services.yaml`, so HA rejected
  it. Adding `base_name` to the action lets you remap a station to its renamed entity
  from Developer Tools → Actions without editing the storage file.

## [1.2.3] - 2026-06-08

### Fixed
- **Flow monitoring not starting when binary sensor is delayed** — flow monitoring now
  starts explicitly when the run command is confirmed sent (blocking=True), rather than
  relying solely on the binary sensor on-transition. If the binary sensor later fires
  normally, it resets the fill timer to the actual start time. If the binary sensor never
  confirms the start (15 s timeout), monitoring is cleanly stopped so no stale task runs.
- **Station renamed in OS/HA breaks monitoring and start detection** — if a station's
  entity IDs change because it was renamed in OpenSprinkler or HA (e.g. base_name
  `3_back_drippers` → `3_back_sprinklers_north`), updating the station's `base_name`
  via the `update_station` service now re-registers all entity listeners (OS enable
  switch, running binary sensor, and flow monitor) against the new entity IDs. Previously
  the listeners stayed bound to the old (now stale) entity IDs after a rename.

## [1.2.2] - 2026-06-08

### Fixed
- **Broader pre-start overlap guard** — before starting any station the coordinator now
  checks every tracked station's binary sensor and stops any that are physically running
  in OpenSprinkler (excluding the station about to start). This catches unexpected
  running stations regardless of cause — not just the immediately preceding timed-out
  station covered by the v1.2.1 fix.

## [1.2.1] - 2026-06-08

### Fixed
- **Double-station overlap** — when a station's start-confirmation timed out (binary
  sensor did not turn on within 15 s), the station was marked failed and the next
  station started immediately, leaving both running simultaneously in OpenSprinkler.
  Root cause: `run_station` was called with `blocking=False`, so the HTTP command to
  OpenSprinkler and the subsequent coordinator refresh raced against the 15-second
  countdown. Under any OS polling or network pressure the refresh could arrive after
  the timeout. Fixed by switching to `blocking=True` so the HTTP command completes
  before the countdown starts, and by adding a safety stop: if the timed-out station
  is physically running in OS when the failure is detected, it is stopped before the
  next station begins.

## [1.2.0] - 2026-05-23

### Added
- **Failed station status** — stations that were scheduled but never started (e.g. due to
  a brief OpenSprinkler outage) are now marked `failed` instead of being silently marked
  complete. Failed stations appear as red strikethrough in the calendar view so you can
  see at a glance that watering didn't happen.
- `last_run` is no longer updated for failed stations, so the weekly-interval scheduler
  correctly treats them as unwatered.

### Fixed
- **OpenSprinkler unavailability recovery** — before starting each station the coordinator
  now checks if the OpenSprinkler entity is unavailable (e.g. a brief network blip). If
  so, it waits up to 60 seconds for it to recover before issuing the start command.
  Previously the service call was silently dropped and the station timed out.

## [1.1.2] - 2026-05-18

### Fixed
- Flow monitoring is now started correctly when Home Assistant restarts while a
  station is mid-run. Previously `_recover_running_station` resumed the irrigation
  queue but never notified the flow monitor, so the entire run was silently
  skipped. The station's run is now monitored from the point of recovery.
- Added error handling around the post-run flow analysis so that any unexpected
  exception logs clearly and sets the station status to `idle` rather than leaving
  it permanently stuck at `monitoring`.

## [1.1.1] - 2026-05-16

### Fixed
- Enabling flow monitoring on a station now immediately loads its run history from
  the database. Previously the history bars were empty until the user toggled
  monitoring off and back on.

## [1.1.0] - 2026-05-16

### Added
- **Flow monitoring** — Droplet flow sensor integration with per-station anomaly
  detection. Configurable fill time, sample interval, minimum baseline runs, and
  alert threshold. Learns each station's normal flow profile over time and fires a
  persistent HA notification when a run deviates from the baseline.
- **Flow Monitor dashboard tab** — new tab in the irrigation dashboard showing the
  learning curve for each monitored station, recent run history with median flow and
  anomaly scores, and a toggle to enable/disable monitoring per station.
- **`discard_flow_run` service** — mark a specific run as discarded so it is excluded
  from the station's baseline. Run IDs are available in the `recent_run_details`
  attribute of each station's Flow Status sensor.
- **`discard_flow_runs_before` service** — discard all runs for a station that
  predate a given run. Useful after hardware changes (adding/removing drippers)
  to start the baseline fresh from a known-good run.
- Flow Status sensor now exposes `recent_run_details` — a list of up to 10 recent
  non-discarded runs including run ID, timestamps, median flow, IQR, steady sample
  count, anomaly score, and baseline median at time of run.

### Fixed
- Flow monitor now ignores synthetic state events that HA fires during initial state
  load, preventing spurious run-start events on integration startup.
- Off-by-one in the learning-phase run counter: a station with `N-1` runs now
  correctly transitions to the baseline-comparison phase after its `N`th run.

## [1.0.3] - 2026-03-24

### Fixed
- If a station was running when Home Assistant restarted, the integration now
  detects it at startup and resumes the queue from that point instead of
  treating the run as finished. The remaining stations continue in order once
  the in-progress station completes.

## [1.0.2] - 2026-03-02

### Added
- Per-station **Moisture Sensor** panel in the Schedules view. Each station card
  has a new collapsible panel (above Normal/Hot Schedule) where a soil moisture
  sensor can be associated. Eligible sensors are filtered automatically to those
  labelled both `soil` and `moisture` in the HA entity registry.
- When a sensor is selected, the panel shows the live reading and a configurable
  **Skip if above (%)** threshold. If the sensor reading exceeds the threshold the
  station is excluded from all queues exactly as if Schedule Mode were **Off** —
  reflected in the lookahead calendar and enforced at queue-build time.
- The schedule updates live as moisture changes via a dedicated state-change
  listener in the coordinator.

### Fixed
- Dashboard panel registration now uses `update=True` so the integration can be
  reloaded without crashing with `ValueError: Overwriting panel`.
- If post-platform setup fails, platforms are now torn down immediately so a
  subsequent reload does not encounter "already been setup" errors.
- `async_unload_entry` is now fully defensive and handles being called on a
  partially-loaded entry.

## [1.0.1] - 2026-03-02

### Fixed
- Lovelace card JS is now registered via Lovelace's `ResourceStorageCollection` API
  instead of `add_extra_js_url`. This keeps the in-memory resource collection, the
  storage file, and all connected clients in sync via WebSocket push — previously the
  card could fail to load after a fresh install without a full browser reload.
- Any stale `/local/*` resource entries written by earlier versions are cleaned up
  automatically on first run after upgrading.

## [1.0.0] - 2026-02-28

### Added
- Initial release
- OpenSprinkler integration with full station, program, and schedule control
- Binary sensors for rain delay and sensor status
- Automatic dashboard and Lovelace card registration
- Bundled card JS served automatically; Lovelace resource auto-registered on setup
