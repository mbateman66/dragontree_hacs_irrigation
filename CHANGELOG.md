# Changelog

All notable changes to this project will be documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

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
