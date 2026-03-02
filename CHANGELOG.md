# Changelog

All notable changes to this project will be documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

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
