# Station Rename Detection & Action — Design

Date: 2026-07-06

## Background

On 2026-07-06 we manually renamed 3 OpenSprinkler stations (`3_back_drippers` →
`3_driveway_drippers`, `2_back_sprinklers` → `2_back_drippers`,
`4_front_bubblers` → `4_front_drippers`). Doing this by hand required:

- An external Python script driving HA's WebSocket API to rename ~38 entity_ids
  per station (3 OpenSprinkler entities + ~35 `dragontree_irrigation` entities).
- Manually calling `update_station` to sync `base_name`/`friendly_name`.
- A full HA restart to pick up a coordinator bug fix (stale duplicate stations —
  fixed in v1.3.1).
- Discovering and fixing three more latent bugs where frontend code used a
  station's immutable `id` where it needed `base_name`, or vice versa (moisture
  panel — v1.3.2; Flow Monitor tab — v1.3.3; Calendar View template — v1.3.4).

All four of those bugs share one root cause: the coordinator finds and tracks
a station's OpenSprinkler entities by constructing `switch.{base_name}_...`
strings, and `base_name` is a slug that must be kept in perfect sync with
OpenSprinkler's actual (frozen-at-creation) `entity_id` — any drift between
the two silently breaks something. This design fixes that at the root by
switching the coordinator's internal station-lookup mechanism to the
OpenSprinkler station's **index** (the physical slot number — truly
immutable, never derived from any name), and then layers a rename
detection + confirm-and-apply action on top, so future renames are a
two-click operation with no external tooling, no restart, and — because
lookup no longer depends on name-derived text at all — no risk of
reintroducing this bug class.

## Key facts this design relies on

OpenSprinkler entity_ids are frozen at first discovery and never follow a
rename on the physical device. But every OpenSprinkler station entity — the
`switch`, `binary_sensor`, and `sensor` alike — already exposes what we need
as live state attributes, no parsing required:

```
switch.1_front_upper_sprinkers_station_enabled attributes:
  opensprinkler_type: station
  name: "1 - Front Upper Sprinkers"   # live, current OS station name
  index: 0                            # stable physical slot, never changes
```

`index` is assigned by the controller's physical wiring/slot position. It
never changes for a given station, regardless of any rename, and is exposed
identically across all three of that station's OpenSprinkler entities.

## Scope

This is one initiative delivered in two phases, because phase 2's design is
only simple and safe *because* phase 1 changes the lookup mechanism first.

**Phase 1 — index-based internal tracking (foundation):**
- Add `os_index` to the station record: the permanent internal pointer to a
  station's physical OpenSprinkler entities.
- Replace every place the coordinator currently constructs an OpenSprinkler
  entity_id from `base_name` with a lookup by `os_index` instead.
- Rewrite station discovery (`_merge_discover_stations`) to key on `os_index`
  instead of comparing name-derived strings — this also hardens the
  duplicate-station bug fixed in v1.3.1 (string comparison could still
  theoretically collide; index comparison cannot).
- `base_name`'s role narrows to just "the current/desired entity_id slug" —
  used only for constructing dragontree's own entity_id text and as the
  rename target for OpenSprinkler's entities. No longer used for lookup.
- `id` (today's immutable per-station key, used as unique_id suffix and the
  flow-history database key) **does not change** for any existing station —
  this is purely additive.

**Phase 2 — rename detection + `rename_station` action:**
- Detect when a tracked station's live OS name no longer matches what we
  last saw, using the existing reactive state-change plumbing (no new
  polling).
- Surface a "Rename" affordance in the Manage Stations tab for any station
  with a detected mismatch.
- A confirm step: suggested new `base_name`/`friendly_name` pre-filled and
  editable, not applied automatically.
- A new `rename_station` service that performs the full rename
  (OpenSprinkler entities' entity_id + all `dragontree_irrigation` entities'
  entity_id + stored fields) in-process, no restart required.

**Out of scope:**
- Fully automatic (zero-touch) renaming.
- Anything about performing the physical rename on the OpenSprinkler
  controller itself — still a manual step done by the user on the device.
- The Calendar View Jinja template — already fixed in v1.3.4 to read
  `base_name` dynamically, so it needs no further changes for renames.
- A general-purpose "undo rename" feature (a rename can be corrected by
  running the action again).
- Renaming dragontree's own ~35 entities to be index-based instead of
  name-based — considered and rejected (see below); they keep human-readable
  entity_ids and get renamed alongside OpenSprinkler's.

**Why not make dragontree's own entities index-based too?** It was
considered — it would mean *never* renaming those ~35 entities. But their
entity_id would then permanently read something like
`switch.dragontree_irrigation_station_3_flow_monitoring`, which is exactly
the "entity_id doesn't say anything meaningful" problem this whole effort
started to solve, just relocated from "shows the old name" to "shows no name
at all." Since the stated goal is that *all* entity_ids stay current and
readable, dragontree's own entities keep getting renamed too — `os_index`
only changes how the coordinator finds things internally, not what any
entity is named.

## Data model changes

Add two fields to the station record (`DEFAULT_STATION_TEMPLATE` in
`coordinator.py`):

```python
"os_index": None,  # permanent internal pointer: the OpenSprinkler station's
                    # physical slot number, from the `index` state attribute
"os_name": "",     # last-synced live OpenSprinkler station name, used only
                    # to detect that a rename has happened
```

- `os_index` is set once at station creation time (`_make_station`, called
  from `_merge_discover_stations`) from the OpenSprinkler station's live
  `index` attribute, and never changes afterward.
- `os_name` is set at creation time to the live `name` attribute, and updated
  by `async_rename_station` whenever a rename is applied (clearing the
  detected mismatch).
- Migration guard in `async_initialize` backfills both fields for existing
  stations, using each station's *current* `base_name` to look up its OS
  switch entity **one last time** (safe: no unknown rename is pending at the
  moment of upgrade, so `base_name` is still a trustworthy pointer for this
  one-time backfill). If a station's OS entity happens to be unavailable at
  that exact moment, `os_index` is left `None` and is not crash-inducing —
  confirmed by implementation testing to be the common case, since this
  integration doesn't declare `opensprinkler` as a manifest dependency, so
  there's no ordering guarantee at HA startup. The reliable fix: `os_index`
  is retried once HA has fully started (`async_at_started`, the same hook
  `_recover_running_station`/`_check_entity_health` already use), by which
  point OpenSprinkler is essentially guaranteed to be ready — see
  `_retry_merge_discover_stations` in the implementation plan.

`os_name` is intentionally distinct from both `base_name` (the entity_id
slug) and `friendly_name` (dashboard display text) — a user can edit the
suggested slug/name in the confirm step to something that doesn't match
`os_name` exactly, and that's fine; `os_name` only exists to detect the
*next* rename.

`id` is unchanged in meaning and value for every existing station — nothing
about it changes, so no entity gets recreated and no flow-history/moisture
association gets orphaned.

## Resolving OpenSprinkler entities by index

One shared helper:

```python
def _find_os_entity(hass: HomeAssistant, domain: str, os_index: int) -> str | None:
    """Find the current entity_id of an OpenSprinkler station entity by its
    physical index, regardless of what its entity_id currently says."""
    for state in hass.states.async_all(domain):
        if (state.attributes.get("opensprinkler_type") == "station"
                and state.attributes.get("index") == os_index):
            return state.entity_id
    return None
```

**Scope boundary (deliberately targeted, not exhaustive):** `coordinator.py`
constructs OpenSprinkler entity_ids from `base_name` in many more places than
originally scanned for this spec — including the queue-execution / start-stop
control flow (`_build_queue`, `_recover_running_station`, `_run_queue`,
`_stop_any_running_stations`, `_stop_station_if_running`,
`_wait_for_station`, `async_run_station_manual`,
`async_stop_station_manual`). That code is safety-critical (it's what turns
physical valves on and off) and is not currently broken — the staleness bugs
we hit were caused by the *manual, multi-step* rename process (external
script + separate `update_station` call, with a gap between them for things
to drift). Once `rename_station` renames entity_ids and updates `base_name`
atomically in one function call, there's no gap for `base_name` to go stale
in, so that code doesn't need to change.

`_find_os_entity` therefore replaces `base_name`-based construction in only
these places — the discovery/dedup logic and the "what do we watch for
changes" listener setup, not the "what do we command" control flow:
- `_merge_discover_stations` (coordinator.py) — see rewrite below.
- `_setup_os_listeners`, `_setup_running_listeners`, `_setup_health_listeners`
  (coordinator.py) — build their watched entity_id lists via `_find_os_entity`
  per station's `os_index`, once per (re)setup call, instead of formatting
  `f"switch.{base_name}_station_enabled"`.
- `StationStatusSensor.native_value`, `StationTimeRemainingSensor.native_value`
  (sensor.py) — resolve the running binary_sensor via `os_index` instead of
  `station['base_name']`.
- `flow_monitor.py`'s `setup()` — instead of building a
  `base_name -> station_id` map from entity_id text, build an
  `os_index -> station_id` map, and resolve the changed entity's index from
  its live state attributes in the state-changed callback rather than
  parsing its entity_id.

Everything else that constructs `switch.{base_name}_station_enabled` /
`binary_sensor.{base_name}_station_running` for queue execution and manual
start/stop is unchanged and continues to read `station['base_name']`
directly — correctness there now comes from `rename_station` keeping
`base_name` atomically accurate, not from index resolution.

Because this lookup is dynamic (computed fresh whenever needed, not cached as
a string), it self-heals: even if `base_name` and OpenSprinkler's actual
entity_id ever drift apart again for any reason, tracking, listening, and
control all keep working correctly. Only the human-readable entity_id text
(the thing `rename_station` fixes) would look stale — not the coordinator's
behavior.

## Discovery, rewritten (`_merge_discover_stations`)

Today's version parses `base_name` out of each `switch.*_station_enabled`
entity_id and compares that string against tracked stations' `id`s — the
exact mechanism that caused the v1.3.1 duplicate-station bug. The rewrite
compares the **live `index` attribute** against `{s["os_index"] for s in
self._stations}` instead:

```python
existing_indices = {s["os_index"] for s in self._stations}
for state in hass.states.async_all("switch"):
    if state.attributes.get("opensprinkler_type") != "station":
        continue
    os_index = state.attributes.get("index")
    if os_index in existing_indices:
        continue
    # new station: build its record from state.entity_id (for the initial
    # base_name/friendly_name) and state.attributes (name, index)
    ...
```

This can't have the same failure mode as before: indices are assigned once
by the hardware and never reused, so there's no string-comparison edge case
to get wrong.

## Action: `rename_station`

New service registered in `__init__.py`, backed by
`IrrigationCoordinator.async_rename_station`.

**Schema:**
```yaml
rename_station:
  fields:
    station_id:
      required: true
      selector: {text}
    new_base_name:
      required: true
      selector: {text}
    new_friendly_name:
      required: false
      selector: {text}
```

**Coordinator method behavior:**
1. Look up the station by `station_id` (immutable `id`); error if not found.
2. Reject if `new_base_name` collides with another tracked station's current
   `base_name`.
3. **Pre-flight collision check**: compute every target `entity_id` (3
   OpenSprinkler + all `dragontree_irrigation` entities for this station) and
   verify none of them already exist in the entity registry under a different
   unique_id. Abort with a clear `HomeAssistantError` before changing
   anything if any collision is found.
4. Rename the 3 OpenSprinkler entities' `entity_id`
   (`switch/binary_sensor/sensor`), found via `_find_os_entity(hass, domain,
   station["os_index"])`, via
   `entity_registry.async_get(hass).async_update_entity(old, new_entity_id=new)`.
   This is a cross-integration edit via the shared entity registry — normal
   and supported, same mechanism HA's own UI uses.
5. **Enumerate** all `dragontree_irrigation` entities for this station by
   filtering the entity registry for `platform == DOMAIN and unique_id
   startswith f"{DOMAIN}_{station_id}_"` — using the station's *immutable*
   `id`, which unique_id is always built from and which never changes.
   **Rename** each one by substituting the old `base_name` segment for the
   new one within its *current* `entity_id` (mirrors the pattern-matching
   logic used by today's external script). If an enumerated entity's current
   `entity_id` doesn't contain the expected old `base_name` segment (e.g. it
   was manually customized to something else via HA's UI), skip renaming
   that single entity and log a warning — never guess.
6. Update the station record: `base_name = new_base_name`,
   `friendly_name = new_friendly_name or new_base_name.replace('_', ' ').title()`,
   `os_name = <current live OS name>`. `os_index` and `id` are untouched.
7. Save, `async_set_updated_data`, dispatch `SIGNAL_STATIONS_UPDATED`.

Note what's *not* in this list, compared to the pre-Phase-1 version of this
design: there is no "re-run listener setup because base_name changed" step.
Since Phase 1 makes all internal lookup index-based, renaming entity_id text
is now purely cosmetic — nothing internal depends on it, so there's nothing
to re-wire.

No restart is required — this is normal runtime service execution, not a code
change to the integration itself (the restarts we needed today were only
because we were editing `coordinator.py`'s source directly).

## Frontend

**Manage Stations tab** (`DragontreeStationManager`):
- Each row already shows OS name and base name (`_patchRow`). Add: if
  `station.rename_pending`, show a "Rename" button/badge in the row.
- Clicking it reveals an inline editable confirm row (reusing the existing
  friendly-name inline-edit pattern already in this tab) with two fields
  pre-filled: suggested base name, suggested friendly name — both editable.
  "Confirm" / "Cancel" buttons.
- Confirm calls `dragontree_irrigation.rename_station` with
  `station_id`, `new_base_name`, `new_friendly_name` taken from the (possibly
  edited) fields at confirm time.
- On success, the row's `rename_pending` clears on the next data refresh
  (pushed automatically once the coordinator saves).
- On failure (service call raises), surface the error via HA's standard
  service-call error toast — no special handling needed client-side.

## Detection (Phase 2, built on Phase 1's lookup)

No new polling loop. The existing OS entity state-change listeners (now
resolved via `os_index`, per Phase 1) already fire when a rename updates the
`name` attribute on the next OpenSprinkler poll cycle.

Detection is computed on read, not stored: wherever station dicts are
serialized for the frontend (`extra_state_attributes` of
`sensor.dragontree_irrigation_schedule`, and `_build_data()`), for each
station:

```python
live_name = _get_os_live_name(station["os_index"])  # via _find_os_entity +
                                                       # attributes.name
if live_name and live_name != station["os_name"]:
    station["rename_pending"] = True
    station["suggested_base_name"] = slugify(live_name)
    station["suggested_friendly_name"] = live_name
else:
    station["rename_pending"] = False
```

If the OpenSprinkler entity is unavailable/missing at read time, treat as "no
live name available" — never show a false positive because data was
momentarily missing.

## Error handling

- Collision detection happens as a pre-flight pass over *all* target
  entity_ids before any registry writes — either the whole rename applies, or
  none of it does. No partial-rename state is possible.
- Duplicate `base_name` across stations is rejected before any registry
  changes.
- Missing/unavailable OpenSprinkler entities during detection just suppress
  the `rename_pending` flag for that station (no false positive), and during
  the rename action itself would surface as a "target entity not found"
  error, since the pre-flight check operates on the entity registry, not live
  state.
- If `_find_os_entity` can't find an entity for a station's `os_index` at all
  (e.g. a station was physically removed from the controller), that's
  surfaced the same way the existing health-check notification already
  handles missing/unavailable required entities — no new mechanism needed.

## Testing plan

No existing automated test suite in this repo. Manual verification on dev:

**Phase 1:**
1. After deploying, confirm all 24 stations still track/control correctly
   (station enable/disable, running detection, flow monitoring) with no
   restart-induced duplicates — the existing regression check for the
   v1.3.1 bug.
2. Confirm `os_index`/`os_name` were backfilled correctly for all existing
   stations (spot-check a few against their known OS entity `index`/`name`).

**Phase 2:**
3. Rename a station on the OpenSprinkler controller.
4. Confirm the Manage Stations tab shows the "Rename" button for that station
   within moments, without any restart.
5. Click Rename, verify suggested slug/name are sensible, edit one of them,
   confirm.
6. Verify: OpenSprinkler entity_ids renamed, all ~35 `dragontree_irrigation`
   entity_ids renamed, `base_name`/`friendly_name`/`os_name` updated, no
   restart needed, no duplicate station created, schedules/flow
   history/moisture association all preserved.
7. Test the collision guard: attempt to rename two different stations to the
   same target slug; confirm the second is rejected cleanly with no partial
   changes.
8. Confirm a station with no OS-side rename never shows `rename_pending`.
