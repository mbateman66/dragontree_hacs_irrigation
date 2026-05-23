# Station Failed Status Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Introduce `STATUS_FAILED` so stations that were supposed to run but never started are shown as red strikethrough in the calendar instead of being silently marked complete; also ship the OS-unavailability pre-check that was already coded but not yet committed.

**Architecture:** Three-layer change — one new constant, two coordinator method changes (return type on `_wait_for_station`, branch logic in `_run_queue`), one new CSS rule in the frontend card. The OS availability pre-check (`_wait_for_entity_available`) is already in `coordinator.py` as an uncommitted change and is included in the first commit.

**Tech Stack:** Python (HA DataUpdateCoordinator), vanilla JS custom element, HA CSS variables.

---

## File Map

| File | Change |
|------|--------|
| `const.py` | Add `STATUS_FAILED = "failed"` |
| `coordinator.py` | Import `STATUS_FAILED`; add it to skip-check; change `_wait_for_station` to return `bool`; branch on return value in `_run_queue` |
| `js/dragontree-irrigation-cards.js` | Add `.station.failed` CSS rule in calendar card |

---

### Task 1: Add `STATUS_FAILED` constant and commit OS pre-check

**Files:**
- Modify: `const.py:25`
- Modify: `coordinator.py` (import block ~line 43, skip-check ~line 642)

The OS availability pre-check (`_wait_for_entity_available` method + the unavailability guard in `_run_queue`) is already in `coordinator.py` as an uncommitted change. This task absorbs it into the same commit.

- [ ] **Step 1: Add the constant to `const.py`**

In `const.py`, add `STATUS_FAILED` after `STATUS_COMPLETE`:

```python
# Station statuses
STATUS_SCHEDULED = "scheduled"
STATUS_RUNNING = "running"
STATUS_MANUAL = "manual"
STATUS_COMPLETE = "complete"
STATUS_PAUSED = "paused"
STATUS_CANCELLED = "cancelled"
STATUS_FAILED = "failed"
```

- [ ] **Step 2: Import `STATUS_FAILED` in `coordinator.py`**

In `coordinator.py`, in the `from .const import (` block (around line 43), add `STATUS_FAILED`:

```python
from .const import (
    ...
    STATUS_CANCELLED,
    STATUS_COMPLETE,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SCHEDULED,
    ...
)
```

- [ ] **Step 3: Add `STATUS_FAILED` to the queue skip-check**

In `_run_queue` (around line 642), the loop skips stations that are already done.
Update it to also skip `STATUS_FAILED` so a resumed queue doesn't retry failed stations:

```python
if station_entry["status"] in (STATUS_CANCELLED, STATUS_COMPLETE, STATUS_FAILED):
    continue
```

- [ ] **Step 4: Verify the file loads cleanly**

```bash
cd /home/mdb/dev/dragontree_irrigation
python3 -c "import ast; ast.parse(open('coordinator.py').read()); print('OK')"
python3 -c "import ast; ast.parse(open('const.py').read()); print('OK')"
```

Expected: `OK` for both.

- [ ] **Step 5: Commit**

```bash
git add const.py coordinator.py
git commit -m "feat: add STATUS_FAILED constant and OS-unavailability recovery pre-check

- STATUS_FAILED = 'failed' in const.py
- _wait_for_entity_available waits up to 60 s for OS entity to recover
  before issuing run_station service call (avoids silent drops when OS
  is briefly unreachable)
- STATUS_FAILED added to _run_queue skip-check alongside CANCELLED/COMPLETE

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 2: Make `_wait_for_station` return `bool`

**Files:**
- Modify: `coordinator.py:741-784` (`_wait_for_station`)

- [ ] **Step 1: Change the return type and early-return path**

Replace the entire `_wait_for_station` method with the version below.
The only logic changes are:
- Return type annotation `-> None` → `-> bool`
- Early return on start-timeout: `return` → `return False`
- Normal path (station started, whether it finished cleanly or timed out finishing): `return True`

```python
async def _wait_for_station(self, base_name: str, timeout_seconds: int) -> bool:
    """Wait for a station to start and then finish running.

    Sets up the state-change listener BEFORE sampling current state to
    avoid a race condition where the binary sensor turns on between the
    sample and the listener being registered.

    Returns True if the station started (binary sensor went on), False if
    it timed out before starting.
    """
    binary_sensor_id = f"binary_sensor.{base_name}_station_running"
    started = asyncio.Event()
    done = asyncio.Event()

    @callback
    def _state_changed(event: Any) -> None:
        new_state = event.data.get("new_state")
        if not new_state:
            return
        if new_state.state == "on":
            started.set()
        elif new_state.state in ("off", "unavailable", "unknown"):
            if started.is_set():
                done.set()

    # Register listener first, then sample, to avoid missing transitions.
    unsub = async_track_state_change_event(self.hass, [binary_sensor_id], _state_changed)
    try:
        current = self.hass.states.get(binary_sensor_id)
        if current and current.state == "on":
            started.set()

        if not started.is_set():
            # Station hasn't started yet — wait up to 15 s for it to turn on.
            try:
                await asyncio.wait_for(started.wait(), timeout=15.0)
            except asyncio.TimeoutError:
                _LOGGER.warning("Timeout waiting for station %s to start", base_name)
                return False

        # Station is on — now wait for it to finish.
        try:
            await asyncio.wait_for(done.wait(), timeout=float(timeout_seconds))
        except asyncio.TimeoutError:
            _LOGGER.warning("Timeout waiting for station %s to finish", base_name)
        return True
    finally:
        unsub()
```

- [ ] **Step 2: Verify the file parses cleanly**

```bash
python3 -c "import ast; ast.parse(open('coordinator.py').read()); print('OK')"
```

Expected: `OK`.

- [ ] **Step 3: Commit**

```bash
git add coordinator.py
git commit -m "feat: _wait_for_station returns bool indicating whether station started

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 3: Use return value in `_run_queue` to set `STATUS_FAILED`

**Files:**
- Modify: `coordinator.py:696-700` (post-wait branch in `_run_queue`)

- [ ] **Step 1: Replace the post-wait status assignment**

Find this block in `_run_queue` (around line 696):

```python
                await self._wait_for_station(station["base_name"], duration + 60)

                if station_entry["status"] == STATUS_RUNNING:
                    station_entry["status"] = STATUS_COMPLETE
                    station["last_run"] = date.today().isoformat()
```

Replace with:

```python
                started = await self._wait_for_station(station["base_name"], duration + 60)

                if station_entry["status"] == STATUS_RUNNING:
                    if started:
                        station_entry["status"] = STATUS_COMPLETE
                        station["last_run"] = date.today().isoformat()
                    else:
                        station_entry["status"] = STATUS_FAILED
```

- [ ] **Step 2: Verify the file parses cleanly**

```bash
python3 -c "import ast; ast.parse(open('coordinator.py').read()); print('OK')"
```

Expected: `OK`.

- [ ] **Step 3: Manual smoke-check logic**

Trace through the three cases mentally:

| Scenario | `started` | `station_entry["status"]` after wait | Result |
|----------|-----------|--------------------------------------|--------|
| Station ran normally | `True` | `STATUS_RUNNING` | → `STATUS_COMPLETE`, `last_run` updated ✓ |
| Station ran, timed out finishing | `True` | `STATUS_RUNNING` | → `STATUS_COMPLETE`, `last_run` updated ✓ |
| Station never started (OS blip) | `False` | `STATUS_RUNNING` | → `STATUS_FAILED`, `last_run` unchanged ✓ |
| Station cancelled before wait | n/a | `STATUS_CANCELLED` | → skip branch, stays CANCELLED ✓ |

- [ ] **Step 4: Commit**

```bash
git add coordinator.py
git commit -m "feat: set STATUS_FAILED when station never starts instead of STATUS_COMPLETE

Stations that time out waiting for their binary sensor to go on are now
marked failed rather than complete. last_run is not updated, so the
weekly-interval scheduler correctly treats them as unwatered.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 4: Add `failed` CSS rule to calendar card

**Files:**
- Modify: `js/dragontree-irrigation-cards.js:889`

- [ ] **Step 1: Add the CSS rule after `.station.cancelled`**

Find this block (around line 886):

```css
    .station.scheduled { color: var(--primary-text-color); }
    .station.running   { color: var(--primary-color, #03a9f4); font-weight: bold; }
    .station.complete  { color: var(--disabled-text-color, #9e9e9e); }
    .station.cancelled { color: var(--disabled-text-color, #9e9e9e); text-decoration: line-through; }
```

Replace with:

```css
    .station.scheduled { color: var(--primary-text-color); }
    .station.running   { color: var(--primary-color, #03a9f4); font-weight: bold; }
    .station.complete  { color: var(--disabled-text-color, #9e9e9e); }
    .station.cancelled { color: var(--disabled-text-color, #9e9e9e); text-decoration: line-through; }
    .station.failed    { color: var(--error-color, #db4437); text-decoration: line-through; }
```

- [ ] **Step 2: Verify `_stationList` needs no changes**

Confirm `_stationList` (around line 969) still reads:

```js
const cls = (s.status || 'scheduled').toLowerCase();
return `<div class="station ${cls}">${this._esc(s.friendly_name)}</div>`;
```

`"failed".toLowerCase()` → `"failed"` → matches `.station.failed`. No JS changes needed.

- [ ] **Step 3: Commit**

```bash
git add js/dragontree-irrigation-cards.js
git commit -m "feat: show failed stations as red strikethrough in calendar view

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 5: Deploy and verify end-to-end

**Files:**
- Deploy: sync dev → HA config, reload integration

- [ ] **Step 1: Sync to HA**

Use the release skill (`/release-hacs-component`) or manually copy:

```bash
cp coordinator.py const.py /mnt/ha-dev/config/custom_components/dragontree_irrigation/
cp js/dragontree-irrigation-cards.js /mnt/ha-dev/config/custom_components/dragontree_irrigation/js/
```

- [ ] **Step 2: Reload the integration in HA**

In HA: Settings → Devices & Services → Dragontree Irrigation → Reload.

- [ ] **Step 3: Simulate a failed station start**

In HA Developer Tools → Template, verify the schedule sensor has the `failed` status for a known station.

To force a failure without waiting for the real scenario: temporarily rename `switch.{station}_station_enabled` by disabling HA's OpenSprinkler integration, then trigger the AM queue manually via the irrigation service. After 15 s the station should appear as `failed` (red strikethrough) in the calendar.

- [ ] **Step 4: Verify happy path unchanged**

Run a normal irrigation queue. Confirm stations that complete normally still show as grey (`complete`), not red.

- [ ] **Step 5: Verify `last_run` not updated on failure**

In HA Developer Tools → States, check `sensor.dragontree_irrigation_schedule` attributes. A `failed` station's `last_run` in the stations list should be unchanged from before the run.
