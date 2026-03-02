# Changelog

All notable changes to this project will be documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

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
