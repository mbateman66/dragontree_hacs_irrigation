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

This was a lot of manual, error-prone choreography for something that will keep
happening as stations get renamed on the physical controller. This design adds
a **rename detection + confirm-and-apply action** directly to the integration
so future renames are a two-click operation with no external tooling and no
restart.

## Key fact this design relies on

OpenSprinkler entity_ids are frozen at first discovery and never follow a
rename on the physical device. But the OpenSprinkler switch entity's **live
state attributes** already expose what we need, with no parsing required:

```
switch.1_front_upper_sprinkers_station_enabled attributes:
  name: "1 - Front Upper Sprinkers"   # live, current OS station name
  index: 0                            # stable physical slot, never changes
```

`dragontree_irrigation`'s `base_name` field must always equal the OpenSprinkler
entities' *current* `entity_id` (not just a cosmetic label) — `base_name` is
used to construct `switch.{base_name}_station_enabled` etc. throughout the
coordinator. So a "rename" fundamentally means: rename the OpenSprinkler
entities' `entity_id` *and* all of `dragontree_irrigation`'s own entity_ids for
that station to a new, matching slug — both, together, or `base_name` points
at a nonexistent entity.

## Scope

**In scope:**
- Detect when a tracked station's live OS name no longer matches what we last
  saw, using the existing reactive state-change plumbing (no new polling).
- Surface a "Rename" affordance in the Manage Stations tab for any station
  with a detected mismatch.
- A confirm step: suggested new `base_name`/`friendly_name` pre-filled and
  editable, not applied automatically.
- A new `rename_station` service that performs the full rename (OpenSprinkler
  entities + all `dragontree_irrigation` entities + stored fields) in-process,
  no restart required.

**Out of scope:**
- Fully automatic (zero-touch) renaming.
- Anything about performing the physical rename on the OpenSprinkler
  controller itself — still a manual step done by the user on the device.
- The Calendar View Jinja template — already fixed in v1.3.4 to read
  `base_name` dynamically, so it needs no further changes for renames.
- A general-purpose "undo rename" feature (a rename can be corrected by
  running the action again).

## Data model changes

Add one field to the station record (`DEFAULT_STATION_TEMPLATE` in
`coordinator.py`):

```python
"os_name": "",   # last-synced live OpenSprinkler station name
```

- Set at station creation time (`_make_station`, called from
  `_merge_discover_stations`) to the OpenSprinkler station's current live
  `name` attribute.
- Migration guard in `async_initialize` backfills `os_name` for existing
  stations from their current live OS name (best-effort — assumes no rename
  is silently pending at the moment of upgrade).
- Updated by `async_rename_station` (see below) whenever a rename is applied,
  clearing the detected mismatch.

`os_name` is intentionally distinct from both `base_name` (the entity_id slug)
and `friendly_name` (dashboard display text) — a user can edit the suggested
slug/name in the confirm step to something that doesn't match `os_name`
exactly, and that's fine; `os_name` only exists to detect the *next* rename.

## Detection

No new polling loop. `_setup_os_listeners` already subscribes to state-change
events for each station's 3 core OpenSprinkler entities (for schedule
regeneration); a rename on the controller updates the `name` attribute on the
next OpenSprinkler poll cycle, which already fires that listener today.

Detection is computed on read, not stored: wherever station dicts are
serialized for the frontend (`extra_state_attributes` of
`sensor.dragontree_irrigation_schedule`, and `_build_data()`), for each
station:

```python
live_name = _get_os_live_name(station["base_name"])  # reads
                                                       # switch.{base_name}_station_enabled
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
   (`switch/binary_sensor/sensor`) via
   `entity_registry.async_get(hass).async_update_entity(old, new_entity_id=new)`.
   This is a cross-integration edit via the shared entity registry — normal
   and supported, same mechanism HA's own UI uses.
5. **Enumerate** all `dragontree_irrigation` entities for this station by
   filtering the entity registry for `platform == DOMAIN and unique_id
   startswith f"{DOMAIN}_{station_id}_"` — using the station's *immutable*
   `id`, which unique_id is always built from and which never changes. This
   is more robust than matching on current `entity_id` text, since unique_id
   is guaranteed stable regardless of any manual entity_id customization.
   **Rename** each one by substituting the old `base_name` segment for the
   new one within its *current* `entity_id` (mirrors the pattern-matching
   logic used by today's external script). If an enumerated entity's current
   `entity_id` doesn't contain the expected old `base_name` segment (e.g. it
   was manually customized to something else via HA's UI), skip renaming
   that single entity and log a warning — never guess.
6. Update the station record: `base_name = new_base_name`,
   `friendly_name = new_friendly_name or new_base_name.replace('_', ' ').title()`,
   `os_name = <current live OS name>`.
7. Re-run the same re-registration `async_update_station` already does on a
   `base_name` change: `_setup_os_listeners`, `_setup_running_listeners`,
   `_setup_health_listeners`, `_flow_monitor.setup`, reload flow state if
   monitoring is on.
8. Save, `async_set_updated_data`, dispatch `SIGNAL_STATIONS_UPDATED`.

No restart is required — this is normal runtime service execution, not a code
change to the integration itself (the restarts we needed today were only
because we were editing `coordinator.py`'s source).

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

## Testing plan

No existing automated test suite in this repo. Manual verification on dev:

1. Rename a station on the OpenSprinkler controller.
2. Confirm the Manage Stations tab shows the "Rename" button for that station
   within moments, without any restart.
3. Click Rename, verify suggested slug/name are sensible, edit one of them,
   confirm.
4. Verify: OpenSprinkler entity_ids renamed, all ~35 `dragontree_irrigation`
   entity_ids renamed, `base_name`/`friendly_name`/`os_name` updated, no
   restart needed, no duplicate station created (regression check against the
   v1.3.1 bug), schedules/flow history/moisture association all preserved.
5. Test the collision guard: attempt to rename two different stations to the
   same target slug; confirm the second is rejected cleanly with no partial
   changes.
6. Confirm a station with no OS-side rename never shows `rename_pending`.
