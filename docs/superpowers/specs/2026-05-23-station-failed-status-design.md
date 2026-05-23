# Station Failed Status — Design Spec

**Date:** 2026-05-23
**Status:** Approved

## Problem

When a station's queue turn arrives but it never starts (e.g. OpenSprinkler was briefly
unreachable and the service call was silently dropped), the coordinator currently marks the
station `complete` and updates `last_run` — as if it ran successfully. This is wrong in two
ways:

1. The calendar shows it as grey/done, indistinguishable from a successful run.
2. `last_run` is updated, which can cause the weekly-interval scheduler to skip the next
   real watering cycle.

## Goal

Introduce a `failed` status that is set whenever a station was scheduled, the queue tried to
start it, but its binary sensor never went `on`. Show this in the calendar as **red
strikethrough** so the user knows at a glance that watering didn't happen.

## Status Definitions (revised)

| Status      | Meaning                                                                 | Calendar style          |
|-------------|-------------------------------------------------------------------------|-------------------------|
| `scheduled` | Queued for today, not yet attempted                                     | Normal text             |
| `running`   | Binary sensor is currently `on`                                         | Blue, bold              |
| `complete`  | Station started and finished (binary sensor went on then off)           | Grey                    |
| `cancelled` | System gave up before trying: master off, station not found in OS, OS  did not recover within 60 s | Grey strikethrough |
| `failed`    | Queue tried to start it, but binary sensor never went `on` (timeout)   | Red strikethrough       |

## Changes

### `const.py`
Add one constant:
```python
STATUS_FAILED = "failed"
```

### `coordinator.py` — `_wait_for_station`
Change return type from `None` to `bool`:
- Returns `True` if the station actually started (binary sensor went `on`). This covers both
  the normal finish case and the finish-timeout case — the station ran.
- Returns `False` if it timed out before the binary sensor ever went `on` — the station did
  not run.

### `coordinator.py` — `_run_queue`
After `await self._wait_for_station(...)`, branch on the return value:

```
started = await self._wait_for_station(station["base_name"], duration + 60)
if started:
    station_entry["status"] = STATUS_COMPLETE
    station["last_run"] = date.today().isoformat()
else:
    station_entry["status"] = STATUS_FAILED
    # last_run intentionally NOT updated
```

`STATUS_FAILED` must also be added to the skip-check at the top of the loop alongside
`STATUS_CANCELLED` and `STATUS_COMPLETE`, so a failed station is not retried if the queue
is resumed.

### `dragontree-irrigation-cards.js` — calendar CSS
Add one rule in the calendar card's `<style>` block:
```css
.station.failed { color: var(--error-color, #db4437); text-decoration: line-through; }
```

No JS logic changes are needed — `_stationList` already maps `s.status` directly to a CSS
class name.

## What does NOT change
- `STATUS_CANCELLED` semantics and styling are unchanged.
- The `_wait_for_entity_available` pre-check added in the OS-unavailability bugfix is
  unchanged — it already sets `STATUS_CANCELLED` when OS doesn't recover within 60 s.
- No schema migrations needed; `failed` is a new possible value for the existing `status`
  field in the day-schedule data structure.

## Testing
- Simulate a station start timeout (e.g. temporarily break the entity ID) and confirm the
  calendar shows the station in red strikethrough.
- Confirm `last_run` is not updated for failed stations.
- Confirm a station that starts successfully is still marked `complete` (grey).
- Confirm a station cancelled due to master-off is still `cancelled` (grey strikethrough).
