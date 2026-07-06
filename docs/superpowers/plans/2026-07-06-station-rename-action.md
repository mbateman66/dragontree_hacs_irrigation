# Station Rename Detection & Action Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `dragontree_irrigation` track OpenSprinkler stations by their immutable physical index instead of name-derived text, then add a "Rename" button that renames a station's entity_ids (both OpenSprinkler's and dragontree's own) to match a change made on the physical controller — no external tooling, no restart.

**Architecture:** Two phases. Phase 1 (Tasks 1–6) introduces `os_index` as the coordinator's internal pointer to a station's OpenSprinkler entities, used only for discovery/dedup and listener setup — safety-critical queue-execution code is untouched. Phase 2 (Tasks 7–12) adds rename detection (computed on read, no new polling) and a `rename_station` service, surfaced via a confirm-and-apply button in the Manage Stations tab.

**Tech Stack:** Python 3 (Home Assistant custom component), vanilla JS (Lovelace custom card, no build step), YAML (services.yaml, HACS manifest/changelog).

## Global Constraints

- Repo: `/home/mdb/dev/dragontree_irrigation` (git repo). Deployed/tested copy is mounted at `/mnt/ha-dev/config/custom_components/dragontree_irrigation` on the dev HA instance — copy files there to test, never edit the mounted copy directly and forget to copy back.
- No existing automated test suite. Every task's verification step is manual: copy the changed file(s) to the dev mount, reload/restart dev's HA as needed, and check behavior via `curl` against dev's REST API (credentials in `~/.config/ha-instances.env`, IP/token as `$HA_DEV_IP` / `$HA_DEV_TOKEN`).
- Python source changes need a **full HA restart** on dev to take effect (`homeassistant.restart` service) — HA caches custom component modules in `sys.modules`; reloading the config entry alone does not re-import changed `.py` files.
- JS-only changes do NOT need a restart, but the integration cache-busts its Lovelace resource URL using `manifest.json`'s version, read once at Python import time — so a JS change won't visibly take effect in the browser until the *next* restart that also bumps the version. Practically: bundle JS changes into the same restart cycle as any Python changes in the same task group.
- Follow the existing release process (see `release-hacs-component` skill / this repo's `CHANGELOG.md` + `manifest.json` pattern) for the two release checkpoints in this plan (end of Phase 1, end of Phase 2): bump `version` in `manifest.json`, add a `CHANGELOG.md` entry, commit, `git tag vX.Y.Z`, push, `gh release create`.
- Match existing code style exactly: this codebase has no type-checked strictness beyond inline annotations already present; follow the patterns already in each file (docstrings, `_LOGGER` usage, etc.) rather than introducing new conventions.

---

## Phase 1: Index-based internal tracking

### Task 1: `os_lookup.py` helper + `os_index`/`os_name` data model

**Files:**
- Create: `os_lookup.py`
- Modify: `coordinator.py:1-21` (imports), `coordinator.py:92-116` (`DEFAULT_STATION_TEMPLATE` / `_make_station`), `coordinator.py:162-181` (migration guards in `async_initialize`)

**Interfaces:**
- Produces: `find_os_station_entity(hass: HomeAssistant, domain: str, os_index: int) -> str | None` — importable from `.os_lookup` by `coordinator.py`, `sensor.py`, `flow_monitor.py` with no circular-import risk (this new module imports nothing from any of them).
- Produces: station dicts now always have `os_index: int | None` and `os_name: str` keys.

- [ ] **Step 1: Create `os_lookup.py`**

```python
"""Helpers for finding OpenSprinkler station entities by their physical index.

OpenSprinkler entity_ids are frozen at first discovery and never follow a
rename on the physical device, but every station entity exposes `index` (the
physical slot number, immutable) and `name` (the live display name) as state
attributes. Resolving entities by index instead of by name-derived entity_id
text means lookups can't go stale.
"""
from __future__ import annotations

from homeassistant.core import HomeAssistant


def find_os_station_entity(hass: HomeAssistant, domain: str, os_index: int) -> str | None:
    """Find the current entity_id of an OpenSprinkler station entity by its
    physical index, regardless of what its entity_id currently says."""
    for state in hass.states.async_all(domain):
        if (
            state.attributes.get("opensprinkler_type") == "station"
            and state.attributes.get("index") == os_index
        ):
            return state.entity_id
    return None
```

- [ ] **Step 2: Verify syntax**

Run: `python3 -m py_compile os_lookup.py`
Expected: no output, exit code 0.

- [ ] **Step 3: Add the import to `coordinator.py`**

At `coordinator.py:54` (right after the existing `from .flow_database import FlowDatabase` / `from .flow_monitor import FlowMonitor` lines), add:

```python
from .os_lookup import find_os_station_entity
```

- [ ] **Step 4: Add `os_index`/`os_name` to the station template and `_make_station`**

Replace `coordinator.py:92-116`:

```python
DEFAULT_STATION_TEMPLATE = {
    "id": "",
    "base_name": "",
    "friendly_name": "",
    "os_index": None,  # permanent internal pointer: OpenSprinkler's physical
                       # slot number (from the `index` state attribute).
                       # Never changes once set. Used for lookup only —
                       # entity_id text is never derived from it.
    "os_name": "",     # last-synced live OpenSprinkler station name, used
                       # only to detect that a rename has happened.
    "schedule_mode": SCHEDULE_MODE_NORMAL,
    "sensitive": False,
    "tracked": True,
    "normal_schedule": None,
    "hot_schedule": None,
    "last_run": None,
    "moisture_sensor": None,
    "moisture_max": None,
    "flow_monitoring": False,
    "flow_fill_time": DEFAULT_FLOW_FILL_TIME,
}


def _make_station(
    base_name: str,
    friendly_name: str,
    os_index: int | None = None,
    os_name: str = "",
) -> dict:
    s = deepcopy(DEFAULT_STATION_TEMPLATE)
    s["id"] = base_name
    s["base_name"] = base_name
    s["friendly_name"] = friendly_name
    s["os_index"] = os_index
    s["os_name"] = os_name
    s["normal_schedule"] = deepcopy(DEFAULT_SCHEDULE)
    s["hot_schedule"] = deepcopy(DEFAULT_SCHEDULE)
    return s
```

(`os_index`/`os_name` default to `None`/`""` so `async_add_station`'s existing call — `_make_station(data.get("base_name", ""), data.get("friendly_name", ""))` at `coordinator.py:969` — keeps working unchanged.)

- [ ] **Step 5: Backfill `os_index`/`os_name` for existing stations at startup**

In `coordinator.py`'s `async_initialize`, the migration-guard loop currently ends at line 180 with `s.setdefault("manual_duration", DEFAULT_STATION_MANUAL_DURATION)`. Add immediately after that line (still inside the `for s in self._stations:` loop):

```python
                s.setdefault("manual_duration", DEFAULT_STATION_MANUAL_DURATION)
                # os_index/os_name backfill: use the station's current
                # base_name to find its OS switch entity one last time (safe
                # — no unknown rename is pending at upgrade time). If the
                # entity happens to be unavailable right now, leave os_index
                # as None; _merge_discover_stations or a later health check
                # will pick it up once OpenSprinkler finishes loading.
                s.setdefault("os_index", None)
                s.setdefault("os_name", "")
                if s["os_index"] is None:
                    switch_state = self.hass.states.get(
                        f"switch.{s['base_name']}_station_enabled"
                    )
                    if switch_state is not None:
                        s["os_index"] = switch_state.attributes.get("index")
                        if not s["os_name"]:
                            s["os_name"] = switch_state.attributes.get("name", "")
```

- [ ] **Step 6: Verify syntax**

Run: `python3 -m py_compile coordinator.py`
Expected: no output, exit code 0.

- [ ] **Step 7: Deploy to dev and verify the backfill**

```bash
cp os_lookup.py coordinator.py /mnt/ha-dev/config/custom_components/dragontree_irrigation/
source ~/.config/ha-instances.env
curl -s -X POST -H "Authorization: Bearer $HA_DEV_TOKEN" "http://$HA_DEV_IP:8123/api/services/homeassistant/restart"
```

Wait for dev to come back up (poll `GET /api/` until HTTP 200), then check the storage file directly:

```bash
python3 -c "
import json
d = json.load(open('/mnt/ha-dev/config/.storage/dragontree_irrigation'))
for s in d['data']['stations'][:3]:
    print(s['id'], '| os_index:', s.get('os_index'), '| os_name:', s.get('os_name'))
"
```

Expected: all 3 sampled stations show a non-`None` integer `os_index` and a non-empty `os_name` matching their current OS display name.

- [ ] **Step 8: Commit**

```bash
git add os_lookup.py coordinator.py
git commit -m "Add os_index/os_name fields and OS-entity-by-index lookup helper"
```

---

### Task 2: Rewrite `_merge_discover_stations` to key on `os_index`

**Files:**
- Modify: `coordinator.py:212-254` (`_merge_discover_stations`), `coordinator.py:237-242` (`_schedule_recovery`, inside `async_initialize`)

**Interfaces:**
- Consumes: `find_os_station_entity` (Task 1, not used directly here but the pattern it establishes), `_make_station(base_name, friendly_name, os_index, os_name)` (Task 1).
- Produces: discovery no longer creates a duplicate station for one whose `entity_id` was renamed elsewhere (hardens the class of bug fixed narrowly in v1.3.1). Also produces `IrrigationCoordinator._retry_merge_discover_stations()`, which fixes the startup race Task 1's testing surfaced (OpenSprinkler not yet ready when this integration's own `os_index` backfill runs).

- [ ] **Step 1: Replace `_merge_discover_stations`**

Replace `coordinator.py:212-254`:

```python
    async def _merge_discover_stations(self) -> None:
        """Scan live OpenSprinkler station entities and add any not yet tracked.

        Default-named stations (s1, s2, …) are added with tracked=False so they
        appear in the management view but don't affect scheduling until the user
        explicitly enables them. Custom-named stations are added with tracked=True.

        Matches on each OpenSprinkler station's physical `index` (a state
        attribute on its switch entity), not on any name-derived string. The
        index is assigned once by the controller's wiring and never changes,
        so — unlike matching on entity_id text — this can't misidentify an
        already-tracked station as new after it's been renamed.
        """
        default_re = re.compile(r"^s\d+$")
        existing_indices = {
            s["os_index"] for s in self._stations if s.get("os_index") is not None
        }
        # Stations that predate os_index, or whose migration backfill couldn't
        # find their OS entity yet (e.g. OpenSprinkler wasn't ready at startup)
        # — bridge these to their now-available OS entity by base_name, once.
        # Once os_index is set this map is never consulted for that station
        # again, so this narrow use of base_name never causes a duplicate.
        by_base_name = {
            s["base_name"]: s for s in self._stations if s.get("os_index") is None
        }

        added = 0
        backfilled = 0
        for state in self.hass.states.async_all("switch"):
            if state.attributes.get("opensprinkler_type") != "station":
                continue
            os_index = state.attributes.get("index")
            if os_index is None or os_index in existing_indices:
                continue
            base_name = state.entity_id.removeprefix("switch.").removesuffix(
                "_station_enabled"
            )

            stale = by_base_name.get(base_name)
            if stale is not None:
                stale["os_index"] = os_index
                if not stale.get("os_name"):
                    stale["os_name"] = state.attributes.get("name", "")
                existing_indices.add(os_index)
                backfilled += 1
                continue

            os_name = state.attributes.get("name", "")
            is_default_name = bool(default_re.match(base_name))
            friendly = base_name.replace("_", " ").title()
            station = _make_station(base_name, friendly, os_index=os_index, os_name=os_name)
            station["tracked"] = not is_default_name
            self._stations.append(station)
            existing_indices.add(os_index)
            added += 1

        if added:
            _LOGGER.info(
                "Dragontree Irrigation: added %d new station(s) from OpenSprinkler",
                added,
            )
        if added or backfilled:
            await self._save()
```

Note: this drops the entity-registry scan (`er.async_get(self.hass)` / `registry.entities.values()`) in favor of `hass.states.async_all("switch")` filtered by the `opensprinkler_type` attribute — simpler, and consistent with how Task 1's `find_os_station_entity` works. The `er` import stays in `coordinator.py` (Task 8 uses it for `async_rename_station`).

- [ ] **Step 2: Retry the backfill once HA has fully started**

Task 1's testing surfaced a real race: `async_initialize` (and this rewritten
`_merge_discover_stations`) run during this integration's own setup, which
can easily happen *before* OpenSprinkler's own config entry has finished its
first refresh (`dragontree_irrigation`'s `manifest.json` doesn't declare
`opensprinkler` as a dependency, so HA gives no ordering guarantee between
them). When that happens, `os_index`/`os_name` are left `None`/`""` for
every station — safe (no crash, no duplicate), but not actually backfilled,
and nothing currently retries it.

`async_initialize` already has the right hook for exactly this: it registers
`_schedule_recovery` via `async_at_started`, which fires only after *all* of
Home Assistant — not just this integration — has finished starting, by which
point OpenSprinkler is essentially guaranteed to be ready. Add a call to
`_merge_discover_stations` there too, so a station left with `os_index: None`
from the first pass gets a second, much more reliable chance via this same
function's `by_base_name` fallback (Step 1).

In `coordinator.py`, `_schedule_recovery` currently reads:

```python
        @callback
        def _schedule_recovery(_hass: HomeAssistant) -> None:
            self.hass.async_create_task(self._recover_running_station())
            self.hass.async_create_task(self._check_entity_health())

        async_at_started(self.hass, _schedule_recovery)
```

Replace it with:

```python
        @callback
        def _schedule_recovery(_hass: HomeAssistant) -> None:
            self.hass.async_create_task(self._retry_merge_discover_stations())
            self.hass.async_create_task(self._recover_running_station())
            self.hass.async_create_task(self._check_entity_health())

        async_at_started(self.hass, _schedule_recovery)
```

Add this new method directly above `_merge_discover_stations`:

```python
    async def _retry_merge_discover_stations(self) -> None:
        """Re-run discovery once HA has fully started, to backfill os_index
        for any station that predated it or whose migration backfill raced
        ahead of OpenSprinkler's own startup (see _merge_discover_stations).
        Also re-registers listeners in case anything was backfilled, since
        _setup_os_listeners/_setup_running_listeners/_setup_health_listeners
        skip any station whose os_index is still None.
        """
        before = {s["id"]: s.get("os_index") for s in self._stations}
        await self._merge_discover_stations()
        if any(s.get("os_index") != before.get(s["id"]) for s in self._stations):
            self._setup_os_listeners()
            self._setup_running_listeners()
            self._setup_health_listeners()
            self._flow_monitor.setup(self._stations)
```

- [ ] **Step 4: Deploy to dev and verify no duplicates on restart, and that the backfill retry actually populates os_index**

```bash
cp coordinator.py /mnt/ha-dev/config/custom_components/dragontree_irrigation/
source ~/.config/ha-instances.env
curl -s -X POST -H "Authorization: Bearer $HA_DEV_TOKEN" "http://$HA_DEV_IP:8123/api/services/homeassistant/restart"
```

After dev is back up, **wait about 30 seconds** for the `async_at_started` callback to fire (it runs after HA reports fully started, not immediately on process start), then check:

```bash
python3 -c "
import json
d = json.load(open('/mnt/ha-dev/config/.storage/dragontree_irrigation'))
print('station count:', len(d['data']['stations']))
none_count = sum(1 for s in d['data']['stations'] if s.get('os_index') is None)
print('stations still missing os_index:', none_count)
for s in d['data']['stations'][:3]:
    print(s['id'], '| os_index:', s.get('os_index'), '| os_name:', s.get('os_name'))
"
curl -s -H "Authorization: Bearer $HA_DEV_TOKEN" "http://$HA_DEV_IP:8123/api/states" | python3 -c "
import json, sys
states = json.load(sys.stdin)
dupes = [s['entity_id'] for s in states if 'system_dragontree_irrigation' in s['entity_id']]
print('duplicate entities:', len(dupes))
"
```

Expected: station count unchanged from before this task (24 on dev at time of writing), duplicate entities count is 0, **`stations still missing os_index` is 0** (this is the actual fix for the race Task 1 surfaced — if this is still nonzero after waiting 30s, something is wrong and this task is not done), and the 3 sampled stations show real integer `os_index` values and non-empty `os_name`.

- [ ] **Step 5: Commit**

```bash
git add coordinator.py
git commit -m "Key station discovery on OpenSprinkler's physical index, not entity_id text"
```

---

### Task 3: Index-based listener setup (`_setup_os_listeners`, `_setup_running_listeners`, `_setup_health_listeners`)

**Files:**
- Modify: `coordinator.py:294-383`

**Interfaces:**
- Consumes: `find_os_station_entity(hass, domain, os_index)` (Task 1).

- [ ] **Step 1: Replace `_setup_os_listeners`**

Replace `coordinator.py:294-314`:

```python
    def _setup_os_listeners(self) -> None:
        """Watch OpenSprinkler station enable/disable switches for state changes."""
        for unsub in self._os_unsubs:
            unsub()
        self._os_unsubs.clear()

        entity_ids = []
        for s in self._stations:
            if s.get("os_index") is None:
                continue
            eid = find_os_station_entity(self.hass, "switch", s["os_index"])
            if eid:
                entity_ids.append(eid)
        if not entity_ids:
            return

        @callback
        def _os_state_changed(_event: Any) -> None:
            self._regenerate_schedules()
            self.async_set_updated_data(self._build_data())

        self._os_unsubs.append(
            async_track_state_change_event(self.hass, entity_ids, _os_state_changed)
        )
```

- [ ] **Step 2: Replace `_setup_running_listeners`**

Replace `coordinator.py:316-335`:

```python
    def _setup_running_listeners(self) -> None:
        """Watch station running binary sensors to keep coordinator data current."""
        for unsub in self._running_unsubs:
            unsub()
        self._running_unsubs.clear()

        entity_ids = []
        for s in self._stations:
            if s.get("os_index") is None:
                continue
            eid = find_os_station_entity(self.hass, "binary_sensor", s["os_index"])
            if eid:
                entity_ids.append(eid)
        if not entity_ids:
            return

        @callback
        def _station_running_changed(_event: Any) -> None:
            self.async_set_updated_data(self._build_data())

        self._running_unsubs.append(
            async_track_state_change_event(self.hass, entity_ids, _station_running_changed)
        )
```

- [ ] **Step 3: Replace `_setup_health_listeners`**

Replace `coordinator.py:360-383`:

```python
    def _setup_health_listeners(self) -> None:
        """Watch the entity registry for additions/removals/renames of required OS entities."""
        for unsub in self._health_unsubs:
            unsub()
        self._health_unsubs.clear()

        entity_ids = []
        for s in self._stations:
            if s.get("os_index") is None:
                continue
            for domain in ("switch", "binary_sensor", "sensor"):
                eid = find_os_station_entity(self.hass, domain, s["os_index"])
                if eid:
                    entity_ids.append(eid)
        if not entity_ids:
            return

        @callback
        def _registry_updated(_event: Any) -> None:
            self.hass.async_create_task(self._check_entity_health())

        self._health_unsubs.append(
            self.hass.bus.async_listen("entity_registry_updated", _registry_updated)
        )
```

- [ ] **Step 4: Verify syntax**

Run: `python3 -m py_compile coordinator.py`
Expected: no output, exit code 0.

- [ ] **Step 5: Deploy to dev and verify listeners still work**

```bash
cp coordinator.py /mnt/ha-dev/config/custom_components/dragontree_irrigation/
source ~/.config/ha-instances.env
curl -s -X POST -H "Authorization: Bearer $HA_DEV_TOKEN" "http://$HA_DEV_IP:8123/api/services/homeassistant/restart"
```

After dev is back up, pick any tracked station and toggle its OS switch off then on, confirming the coordinator reacts (schedule sensor's `stations` attribute reflects the change, and no errors appear in the log):

```bash
curl -s -X POST -H "Authorization: Bearer $HA_DEV_TOKEN" -H "Content-Type: application/json" \
  -d '{"entity_id": "switch.1_front_upper_sprinkers_station_enabled"}' \
  "http://$HA_DEV_IP:8123/api/services/homeassistant/turn_off"
sleep 2
curl -s -X POST -H "Authorization: Bearer $HA_DEV_TOKEN" -H "Content-Type: application/json" \
  -d '{"entity_id": "switch.1_front_upper_sprinkers_station_enabled"}' \
  "http://$HA_DEV_IP:8123/api/services/homeassistant/turn_on"
ha-logs dev dragontree_irrigation | tail -20
```

Expected: no tracebacks/errors in the log from this sequence.

- [ ] **Step 6: Commit**

```bash
git add coordinator.py
git commit -m "Resolve OS listener entity_ids via os_index instead of base_name"
```

---

### Task 4: Index-based lookup in `sensor.py`

**Files:**
- Modify: `sensor.py:18-31` (imports), `sensor.py:209-231` (`StationStatusSensor.native_value`), `sensor.py:262-282` (`StationTimeRemainingSensor.native_value`)

**Interfaces:**
- Consumes: `find_os_station_entity(hass, domain, os_index)` (Task 1).

- [ ] **Step 1: Add the import**

At `sensor.py:31`, change:

```python
from .coordinator import CONTROLLER_DEVICE_INFO, IrrigationCoordinator
```

to:

```python
from .coordinator import CONTROLLER_DEVICE_INFO, IrrigationCoordinator
from .os_lookup import find_os_station_entity
```

- [ ] **Step 2: Update `StationStatusSensor.native_value`**

Replace `sensor.py:209-231`:

```python
    @property
    def native_value(self) -> str:
        rt = self.coordinator.runtime
        station = self._get_station()
        # Check whether the physical station is currently running
        is_running = False
        if station and station.get("os_index") is not None:
            bs = find_os_station_entity(self.hass, "binary_sensor", station["os_index"])
            bs_state = self.hass.states.get(bs) if bs else None
            is_running = bs_state is not None and bs_state.state == "on"

        if is_running:
            # If this station is the one the queue started, it's a scheduled run
            if rt.get("current_station_id") == self._station_id and rt.get("running_queue"):
                return STATUS_RUNNING
            return STATUS_MANUAL

        # Not physically running — reflect the schedule entry status
        for q in ("am", "pm"):
            entry = self._get_station_entry_today(q)
            if entry:
                return entry["status"]
        return "idle"
```

- [ ] **Step 3: Update `StationTimeRemainingSensor.native_value`**

Replace `sensor.py:262-282`:

```python
    @property
    def native_value(self) -> str:
        station = self._get_station()
        if not station or station.get("os_index") is None:
            return "n/a"
        bs = find_os_station_entity(self.hass, "binary_sensor", station["os_index"])
        state = self.hass.states.get(bs) if bs else None
        if state and state.state == "on":
            end_time_str = state.attributes.get("end_time")
            if end_time_str:
                try:
                    end_time = dt_util.parse_datetime(end_time_str)
                    if end_time:
                        remaining = int((end_time - dt_util.utcnow()).total_seconds())
                        if remaining > 0:
                            mins, secs = divmod(remaining, 60)
                            hrs, mins = divmod(mins, 60)
                            return f"{hrs:02d}:{mins:02d}:{secs:02d}"
                except (ValueError, TypeError):
                    pass
        return "n/a"
```

- [ ] **Step 4: Verify syntax**

Run: `python3 -m py_compile sensor.py`
Expected: no output, exit code 0.

- [ ] **Step 5: Deploy to dev and verify**

```bash
cp sensor.py /mnt/ha-dev/config/custom_components/dragontree_irrigation/
source ~/.config/ha-instances.env
curl -s -X POST -H "Authorization: Bearer $HA_DEV_TOKEN" "http://$HA_DEV_IP:8123/api/services/homeassistant/restart"
```

After dev is back up, check a station's status sensor still reports correctly, and (if a station happens to be running) that time-remaining shows a real countdown rather than always `n/a`:

```bash
curl -s -H "Authorization: Bearer $HA_DEV_TOKEN" "http://$HA_DEV_IP:8123/api/states/sensor.dragontree_irrigation_1_front_upper_sprinkers_status" | python3 -c "import json,sys; print(json.load(sys.stdin)['state'])"
```

Expected: a valid status string (`idle`, `scheduled`, `running`, `manual`, etc.), no error.

- [ ] **Step 6: Commit**

```bash
git add sensor.py
git commit -m "Resolve station status/time-remaining sensors via os_index"
```

---

### Task 5: Index-based lookup in `flow_monitor.py`

**Files:**
- Modify: `flow_monitor.py:18-33` (imports), `flow_monitor.py:65-114` (`setup`)

**Interfaces:**
- Consumes: `find_os_station_entity(hass, domain, os_index)` (Task 1).
- Note: `flow_monitor.py` cannot import anything at module level from `coordinator.py` (coordinator.py imports `FlowMonitor` from this module — a module-level reverse import would be circular). `os_lookup.py` has no dependency on either, so this import is safe.

- [ ] **Step 1: Add the import**

At `flow_monitor.py:30` (right after `from .flow_database import FlowDatabase`), add:

```python
from .os_lookup import find_os_station_entity
```

- [ ] **Step 2: Replace `setup()`**

Replace `flow_monitor.py:65-114`:

```python
    def setup(self, stations: list[dict]) -> None:
        """Register state listeners for all flow-monitored stations."""
        self._teardown_listeners()

        monitored = [
            s for s in stations
            if s.get("flow_monitoring", False) and s.get("os_index") is not None
        ]
        if not monitored:
            return

        self._index_to_id: dict[int, str] = {
            s["os_index"]: s["id"] for s in monitored
        }
        entity_ids = []
        for s in monitored:
            eid = find_os_station_entity(self.hass, "binary_sensor", s["os_index"])
            if eid:
                entity_ids.append(eid)
        if not entity_ids:
            return

        @callback
        def _state_changed(event: Any) -> None:
            new_state = event.data.get("new_state")
            old_state = event.data.get("old_state")
            if not new_state:
                return

            os_index = new_state.attributes.get("index")
            station_id = self._index_to_id.get(os_index)
            if not station_id:
                return

            if old_state is None:
                return  # ignore synthetic events during initial HA state load

            old_s = old_state.state
            new_s = new_state.state

            if new_s == "on" and old_s != "on":
                self._start_monitoring(station_id)
            elif old_s == "on" and new_s != "on":
                self._stop_monitoring(station_id)

        self._unsubs.append(
            async_track_state_change_event(self.hass, entity_ids, _state_changed)
        )
```

(This replaces the old `self._base_name_to_id` map — built from parsing `binary_sensor.{base_name}_station_running` entity_id text — with `self._index_to_id`, built from `os_index` directly and resolved from the changed state's own `index` attribute rather than parsing its entity_id.)

- [ ] **Step 3: Verify syntax**

Run: `python3 -m py_compile flow_monitor.py`
Expected: no output, exit code 0.

- [ ] **Step 4: Deploy to dev and verify flow monitoring still starts/stops correctly**

```bash
cp flow_monitor.py /mnt/ha-dev/config/custom_components/dragontree_irrigation/
source ~/.config/ha-instances.env
curl -s -X POST -H "Authorization: Bearer $HA_DEV_TOKEN" "http://$HA_DEV_IP:8123/api/services/homeassistant/restart"
```

After dev is back up, pick a station with `flow_monitoring: true` (e.g. `1_front_upper_sprinkers` per this session's history) and confirm its flow status sensor is still reachable and not stuck in an error state:

```bash
curl -s -H "Authorization: Bearer $HA_DEV_TOKEN" "http://$HA_DEV_IP:8123/api/states/sensor.dragontree_irrigation_1_front_upper_sprinkers_flow_status" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['state'], d['attributes'].get('run_count'))"
```

Expected: a valid status (`normal`, `learning`, etc.) and the same `run_count` as before this change (no data loss). If a run happens to be in progress during testing, confirm the status reads `monitoring` while it's active — the same live check used earlier in this project's history.

- [ ] **Step 5: Commit**

```bash
git add flow_monitor.py
git commit -m "Resolve flow monitor's watched entities and callback lookup via os_index"
```

---

### Task 6: Phase 1 regression verification + release

**Files:** none (verification and release only)

- [ ] **Step 1: Full regression pass on dev**

With all of Tasks 1–5 deployed and dev already restarted (from Task 5's step 4), run through:

```bash
source ~/.config/ha-instances.env
# 1. Station count unchanged, no duplicates
python3 -c "
import json
d = json.load(open('/mnt/ha-dev/config/.storage/dragontree_irrigation'))
print('stations:', len(d['data']['stations']))
"
curl -s -H "Authorization: Bearer $HA_DEV_TOKEN" "http://$HA_DEV_IP:8123/api/states" | python3 -c "
import json, sys
states = json.load(sys.stdin)
print('duplicate entities:', len([s for s in states if 'system_dragontree_irrigation' in s['entity_id']]))
"
# 2. No new errors since restart
ha-logs dev dragontree_irrigation | tail -40
```

Expected: station count and 0 duplicates match Task 2's baseline; no tracebacks attributable to this change (the log's known pre-existing "attributes exceed maximum size" recorder warning for `sensor.dragontree_irrigation_schedule` is unrelated and expected if the recorder-exclude config from earlier isn't present on this environment).

- [ ] **Step 2: Bump version and update changelog**

In `manifest.json`, bump `version` (this is an internal hardening change with no user-visible feature yet — a patch or minor bump per semver judgment at release time; use the `release-hacs-component` skill's guidance).

In `CHANGELOG.md`, add an entry under today's date describing: station tracking now keyed on OpenSprinkler's physical station index instead of name-derived entity_id text, which further hardens the duplicate-station bug fixed in v1.3.1 and lays groundwork for the upcoming rename action.

- [ ] **Step 3: Commit, tag, push, release**

```bash
git add manifest.json CHANGELOG.md
git commit -m "Release vX.Y.Z — index-based station tracking"
git tag vX.Y.Z
git push origin main
git push origin vX.Y.Z
gh release create vX.Y.Z --repo mbateman66/dragontree_hacs_irrigation \
  --title "vX.Y.Z — Index-based station tracking" \
  --notes "Station tracking now keyed on OpenSprinkler's physical station index instead of name-derived entity_id text. Internal change, no user-visible behavior change; lays the groundwork for an upcoming station-rename action."
```

(Replace `X.Y.Z` with the version chosen in Step 2.)

---

## Phase 2: Rename detection + action

### Task 7: Rename-pending detection, computed on read

**Files:**
- Modify: `coordinator.py` (new method, imports), `sensor.py:141-171` (`IrrigationScheduleSensor.extra_state_attributes`)

**Interfaces:**
- Produces: `IrrigationCoordinator._rename_suggestion(station: dict) -> dict` returning `{"rename_pending": False}` or `{"rename_pending": True, "suggested_base_name": str, "suggested_friendly_name": str}`.
- Produces: each station dict in `sensor.dragontree_irrigation_schedule`'s `stations` attribute now includes these 1 or 3 keys, merged in fresh on every read (not stored).

- [ ] **Step 1: Add the `slugify` import to `coordinator.py`**

At `coordinator.py:20` (after the `from homeassistant.helpers.update_coordinator import DataUpdateCoordinator` line), add:

```python
from homeassistant.util import slugify
```

- [ ] **Step 2: Add `_rename_suggestion` to `IrrigationCoordinator`**

Add this method in the "Internal helpers" section of `coordinator.py`, directly above `_get_station` (currently at line 1152):

```python
    def _rename_suggestion(self, station: dict) -> dict:
        """Compute rename-pending status for a station by comparing its
        last-synced OS name (os_name) against the live OS name. Computed
        fresh on every call — nothing here is persisted."""
        os_index = station.get("os_index")
        if os_index is None:
            return {"rename_pending": False}
        eid = find_os_station_entity(self.hass, "switch", os_index)
        state = self.hass.states.get(eid) if eid else None
        live_name = state.attributes.get("name") if state else None
        if not live_name or live_name == station.get("os_name"):
            return {"rename_pending": False}
        return {
            "rename_pending": True,
            "suggested_base_name": slugify(live_name),
            "suggested_friendly_name": live_name,
        }
```

- [ ] **Step 3: Verify syntax**

Run: `python3 -m py_compile coordinator.py`
Expected: no output, exit code 0.

- [ ] **Step 4: Merge the computed fields into the schedule sensor's station list**

Replace `sensor.py:150-171` (inside `IrrigationScheduleSensor.extra_state_attributes`):

```python
        cfg = self.coordinator.global_config
        return {
            "day_schedules": [
                {
                    **day,
                    "queues": {
                        q_name: {
                            **q_data,
                            "stations": [dict(s) for s in q_data.get("stations", [])],
                        }
                        for q_name, q_data in day.get("queues", {}).items()
                    },
                }
                for day in self.coordinator.day_schedules
            ],
            "stations": [
                {**dict(s), **self.coordinator._rename_suggestion(s)}
                for s in self.coordinator.stations
            ],
            "flow_config": {
                "flow_sensor_entity": cfg.get("flow_sensor_entity"),
                "flow_alert_threshold": cfg.get("flow_alert_threshold", 0.25),
                "flow_min_runs": cfg.get("flow_min_runs", 5),
                "flow_sample_interval": cfg.get("flow_sample_interval", 10),
            },
        }
```

- [ ] **Step 5: Verify syntax**

Run: `python3 -m py_compile sensor.py`
Expected: no output, exit code 0.

- [ ] **Step 6: Deploy to dev and verify detection**

```bash
cp coordinator.py sensor.py /mnt/ha-dev/config/custom_components/dragontree_irrigation/
source ~/.config/ha-instances.env
curl -s -X POST -H "Authorization: Bearer $HA_DEV_TOKEN" "http://$HA_DEV_IP:8123/api/services/homeassistant/restart"
```

After dev is back up, confirm no station shows `rename_pending: true` yet (nothing has been renamed):

```bash
curl -s -H "Authorization: Bearer $HA_DEV_TOKEN" "http://$HA_DEV_IP:8123/api/states/sensor.dragontree_irrigation_schedule" | python3 -c "
import json, sys
d = json.load(sys.stdin)
pending = [s['id'] for s in d['attributes']['stations'] if s.get('rename_pending')]
print('rename_pending stations:', pending)
"
```

Expected: `rename_pending stations: []`.

Now rename a station on the OpenSprinkler controller (or, to test without touching real hardware, temporarily edit that station's `os_name` in dev's storage file directly to something other than its live name, then trigger a coordinator refresh by toggling its OS switch):

```bash
python3 -c "
import json
p = '/mnt/ha-dev/config/.storage/dragontree_irrigation'
d = json.load(open(p))
for s in d['data']['stations']:
    if s['id'] == '1_front_upper_sprinkers':
        s['os_name'] = 'deliberately stale test name'
json.dump(d, open(p, 'w'))
"
curl -s -X POST -H "Authorization: Bearer $HA_DEV_TOKEN" "http://$HA_DEV_IP:8123/api/services/homeassistant/restart"
```

After dev is back up, re-check:

```bash
curl -s -H "Authorization: Bearer $HA_DEV_TOKEN" "http://$HA_DEV_IP:8123/api/states/sensor.dragontree_irrigation_schedule" | python3 -c "
import json, sys
d = json.load(sys.stdin)
s = next(s for s in d['attributes']['stations'] if s['id'] == '1_front_upper_sprinkers')
print(s.get('rename_pending'), s.get('suggested_base_name'), s.get('suggested_friendly_name'))
"
```

Expected: `True`, a slugified version of the station's real live name, and the real live name itself. Then restore the correct `os_name` (repeat the Python snippet above with the station's actual current live name, or just let Task 8's rename action fix it later) and restart once more before moving on, so dev isn't left in a deliberately-broken state.

- [ ] **Step 7: Commit**

```bash
git add coordinator.py sensor.py
git commit -m "Add computed rename-pending detection to the schedule sensor"
```

---

### Task 8: `async_rename_station` coordinator method

**Files:**
- Modify: `const.py` (new service constant), `coordinator.py` (new method)

**Interfaces:**
- Consumes: `find_os_station_entity` (Task 1), `er` (already imported), `DOMAIN` (already imported).
- Produces: `IrrigationCoordinator.async_rename_station(station_id: str, new_base_name: str, new_friendly_name: str | None = None) -> None`, raising `HomeAssistantError` on a missing station, a `new_base_name` collision, or a target entity_id collision. Called by Task 9's service handler.

- [ ] **Step 1: Add the service name constant**

In `const.py`, after `SERVICE_DISCARD_FLOW_RUNS_BEFORE = "discard_flow_runs_before"` (line 83), add:

```python
SERVICE_RENAME_STATION = "rename_station"
```

- [ ] **Step 2: Add `async_rename_station` to `IrrigationCoordinator`**

Add this method in `coordinator.py`, directly after `async_remove_station` (which ends at line 1034):

```python
    _OS_ENTITY_SUFFIXES = {
        "switch": "_station_enabled",
        "binary_sensor": "_station_running",
        "sensor": "_station_status",
    }

    async def async_rename_station(
        self,
        station_id: str,
        new_base_name: str,
        new_friendly_name: str | None = None,
    ) -> None:
        """Rename a station's OpenSprinkler + dragontree_irrigation entity_ids
        to a new slug, and update base_name/friendly_name/os_name to match.

        Renaming entity_id text here is purely cosmetic: internal tracking
        (discovery, listener setup) is index-based, and queue-execution code
        reads base_name fresh from the station record on every use — so
        nothing needs re-wiring afterward, unlike the pre-os_index design.
        """
        station = self._get_station(station_id)
        if not station:
            raise HomeAssistantError(f"Station '{station_id}' not found")

        old_base_name = station["base_name"]
        if new_base_name == old_base_name:
            return

        for s in self._stations:
            if s["id"] != station_id and s["base_name"] == new_base_name:
                raise HomeAssistantError(
                    f"Another station already uses base_name '{new_base_name}'"
                )

        registry = er.async_get(self.hass)
        renames: list[tuple[str, str]] = []

        # The 3 OpenSprinkler entities, found by physical index.
        os_index = station.get("os_index")
        if os_index is not None:
            for domain, suffix in self._OS_ENTITY_SUFFIXES.items():
                old_eid = find_os_station_entity(self.hass, domain, os_index)
                if not old_eid:
                    continue
                if old_eid != f"{domain}.{old_base_name}{suffix}":
                    _LOGGER.warning(
                        "Skipping rename of %s: entity_id doesn't match the "
                        "expected pattern for base_name '%s'",
                        old_eid, old_base_name,
                    )
                    continue
                renames.append((old_eid, f"{domain}.{new_base_name}{suffix}"))

        # Every dragontree_irrigation entity for this station, found by the
        # immutable id embedded in unique_id (never by entity_id text).
        old_obj_prefix = f"{DOMAIN}_{old_base_name}_"
        unique_prefix = f"{DOMAIN}_{station_id}_"
        for entry in registry.entities.values():
            if entry.platform != DOMAIN or not entry.unique_id.startswith(unique_prefix):
                continue
            entity_id = entry.entity_id
            domain, _, obj_id = entity_id.partition(".")
            if not obj_id.startswith(old_obj_prefix):
                _LOGGER.warning(
                    "Skipping rename of %s: entity_id doesn't start with "
                    "expected '%s'",
                    entity_id, old_obj_prefix,
                )
                continue
            suffix = obj_id[len(old_obj_prefix):]
            renames.append((entity_id, f"{domain}.{DOMAIN}_{new_base_name}_{suffix}"))

        # Pre-flight: verify every target entity_id is free before changing
        # anything, so a rename either fully applies or doesn't touch
        # anything at all.
        for old_eid, new_eid in renames:
            if old_eid != new_eid and registry.async_get(new_eid) is not None:
                raise HomeAssistantError(
                    f"Cannot rename {old_eid} to {new_eid}: entity already exists"
                )

        for old_eid, new_eid in renames:
            if old_eid != new_eid:
                registry.async_update_entity(old_eid, new_entity_id=new_eid)

        live_name = station.get("os_name", "")
        if os_index is not None:
            new_switch_eid = find_os_station_entity(self.hass, "switch", os_index)
            state = self.hass.states.get(new_switch_eid) if new_switch_eid else None
            if state:
                live_name = state.attributes.get("name", live_name)

        station["base_name"] = new_base_name
        station["friendly_name"] = (
            new_friendly_name or new_base_name.replace("_", " ").title()
        )
        station["os_name"] = live_name

        await self._save()
        async_dispatcher_send(self.hass, SIGNAL_STATIONS_UPDATED)
        self.async_set_updated_data(self._build_data())
```

- [ ] **Step 3: Verify syntax**

Run: `python3 -m py_compile const.py coordinator.py`
Expected: no output, exit code 0.

- [ ] **Step 4: Deploy to dev and verify via direct coordinator exercise**

This task adds the coordinator method only; the HA service that calls it is Task 9. To verify this method directly before wiring the service, temporarily test it via a one-off Python script against dev is not practical (no direct process access) — skip live verification here and verify end-to-end in Task 9's step, which exercises this method through the registered service. Commit now; Task 9 confirms correctness.

- [ ] **Step 5: Commit**

```bash
git add const.py coordinator.py
git commit -m "Add async_rename_station: renames OS + dragontree entity_ids atomically"
```

---

### Task 9: Register the `rename_station` service

**Files:**
- Modify: `__init__.py:23-38` (imports), `__init__.py:118-134` (service cleanup list), `__init__.py` (new handler + registration), `services.yaml` (new entry)

**Interfaces:**
- Consumes: `IrrigationCoordinator.async_rename_station` (Task 8).
- Produces: HA service `dragontree_irrigation.rename_station`, callable from Developer Tools > Actions, automations, or (Task 10) the Manage Stations tab.

- [ ] **Step 1: Add the import**

In `__init__.py:23-38`, add `SERVICE_RENAME_STATION` to the `from .const import (...)` block (alphabetically, after `SERVICE_REORDER_STATIONS`):

```python
from .const import (
    DOMAIN,
    PLATFORMS,
    SERVICE_ADD_STATION,
    SERVICE_DISCARD_FLOW_RUN,
    SERVICE_DISCARD_FLOW_RUNS_BEFORE,
    SERVICE_MOVE_STATION,
    SERVICE_REMOVE_STATION,
    SERVICE_RENAME_STATION,
    SERVICE_REORDER_STATIONS,
    SERVICE_RESET_FLOW_PROFILE,
    SERVICE_START_STATION,
    SERVICE_STOP_STATION,
    SERVICE_UPDATE_FLOW_CONFIG,
    SERVICE_UPDATE_SCHEDULE,
    SERVICE_UPDATE_STATION,
)
```

- [ ] **Step 2: Add it to the unload cleanup list**

In `__init__.py:120-133` (the list of services removed in `async_unload_entry`), add `SERVICE_RENAME_STATION` after `SERVICE_REMOVE_STATION`:

```python
        for service in [
            SERVICE_ADD_STATION,
            SERVICE_UPDATE_STATION,
            SERVICE_REMOVE_STATION,
            SERVICE_RENAME_STATION,
            SERVICE_REORDER_STATIONS,
            SERVICE_UPDATE_SCHEDULE,
            SERVICE_MOVE_STATION,
            SERVICE_RESET_FLOW_PROFILE,
            SERVICE_UPDATE_FLOW_CONFIG,
            SERVICE_DISCARD_FLOW_RUN,
            SERVICE_DISCARD_FLOW_RUNS_BEFORE,
            SERVICE_START_STATION,
            SERVICE_STOP_STATION,
        ]:
            hass.services.async_remove(DOMAIN, service)
```

- [ ] **Step 3: Add the handler**

In `__init__.py`, after `handle_remove_station` (ends at `__init__.py:227`), add:

```python
    async def handle_rename_station(call: ServiceCall) -> None:
        await coordinator.async_rename_station(
            call.data["station_id"],
            call.data["new_base_name"],
            call.data.get("new_friendly_name"),
        )
```

- [ ] **Step 4: Register the service**

After the `SERVICE_REMOVE_STATION` registration block (ends at `__init__.py:315`), add:

```python
    hass.services.async_register(
        DOMAIN,
        SERVICE_RENAME_STATION,
        handle_rename_station,
        schema=vol.Schema(
            {
                vol.Required("station_id"): cv.string,
                vol.Required("new_base_name"): cv.string,
                vol.Optional("new_friendly_name"): cv.string,
            }
        ),
    )
```

- [ ] **Step 5: Add the `services.yaml` entry**

In `services.yaml`, after the `remove_station:` block (ends at line 111), add:

```yaml
rename_station:
  name: Rename Station
  description: >
    Rename a station's OpenSprinkler and dragontree_irrigation entity_ids to
    match a new name (e.g. after renaming the station on the OpenSprinkler
    controller). Renames every entity_id for the station and updates
    base_name/friendly_name to match. No restart required.
  fields:
    station_id:
      name: Station ID
      required: true
      selector:
        text:
    new_base_name:
      name: New Base Name
      description: >
        The new entity_id slug (e.g. "3_driveway_drippers"). Applied to both
        the OpenSprinkler entities and all dragontree_irrigation entities for
        this station.
      required: true
      selector:
        text:
    new_friendly_name:
      name: New Friendly Name
      description: >
        Human-readable display name. Defaults to a title-cased version of
        new_base_name if omitted.
      required: false
      selector:
        text:
```

- [ ] **Step 6: Verify syntax**

Run: `python3 -m py_compile __init__.py`
Expected: no output, exit code 0.

- [ ] **Step 7: Deploy to dev and verify end-to-end**

```bash
cp __init__.py services.yaml /mnt/ha-dev/config/custom_components/dragontree_irrigation/
source ~/.config/ha-instances.env
curl -s -X POST -H "Authorization: Bearer $HA_DEV_TOKEN" "http://$HA_DEV_IP:8123/api/services/homeassistant/restart"
```

After dev is back up, pick a real, currently-untracked-by-anything-important station to rename as a live test (or reuse one already renamed earlier this session, e.g. rename `4_front_drippers` to something and back). Example using a low-stakes default station:

```bash
curl -s -X POST -H "Authorization: Bearer $HA_DEV_TOKEN" -H "Content-Type: application/json" \
  -d '{"station_id": "s12", "new_base_name": "s12_test_rename", "new_friendly_name": "S12 Test Rename"}' \
  "http://$HA_DEV_IP:8123/api/services/dragontree_irrigation/rename_station"
```

Verify:

```bash
curl -s -H "Authorization: Bearer $HA_DEV_TOKEN" "http://$HA_DEV_IP:8123/api/states/switch.s12_test_rename_station_enabled" | python3 -c "import json,sys; print(json.load(sys.stdin)['state'])"
curl -s -H "Authorization: Bearer $HA_DEV_TOKEN" "http://$HA_DEV_IP:8123/api/states/switch.dragontree_irrigation_s12_test_rename_tracked" | python3 -c "import json,sys; print(json.load(sys.stdin)['state'])"
curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $HA_DEV_TOKEN" "http://$HA_DEV_IP:8123/api/states/switch.s12_station_enabled"
```

Expected: the renamed OS switch and a renamed dragontree entity both resolve with valid states, and the old `switch.s12_station_enabled` now 404s. **No restart was required for this rename to take effect** — that's the core deliverable. Then rename it back to `s12` to restore dev's baseline:

```bash
curl -s -X POST -H "Authorization: Bearer $HA_DEV_TOKEN" -H "Content-Type: application/json" \
  -d '{"station_id": "s12", "new_base_name": "s12", "new_friendly_name": "S12"}' \
  "http://$HA_DEV_IP:8123/api/services/dragontree_irrigation/rename_station"
```

- [ ] **Step 8: Commit**

```bash
git add __init__.py services.yaml
git commit -m "Register the rename_station service"
```

---

### Task 10: Manage Stations tab — Rename button and confirm panel

**Files:**
- Modify: `js/dragontree-irrigation-cards.js:18-130` (styles), `js/dragontree-irrigation-cards.js:227-318` (`_makeRow`), `js/dragontree-irrigation-cards.js:323-357` (`_patchRow`)

**Interfaces:**
- Consumes: `station.rename_pending` / `station.suggested_base_name` / `station.suggested_friendly_name` (Task 7), the `dragontree_irrigation.rename_station` service (Task 9).

- [ ] **Step 1: Add rename-panel styles**

In `js/dragontree-irrigation-cards.js`, inside the `STYLES` template literal, after the `.health-warn[hidden] { display: none; }` rule (line 124), add:

```css
    .rename-btn {
      margin-top: 4px; padding: 3px 10px; font-size: 0.75em; cursor: pointer;
      border-radius: 6px; border: 1px solid var(--warning-color, #ff9800);
      background: transparent; color: var(--warning-color, #ff9800); font-weight: 600;
    }
    .rename-btn:hover { background: var(--warning-color, #ff9800); color: white; }
    .rename-panel {
      margin-top: 6px; display: flex; flex-direction: column; gap: 4px;
      padding: 8px; border-radius: 6px;
      background: var(--secondary-background-color, #f5f5f5);
    }
    .rename-panel input {
      padding: 4px 8px; font-size: 0.85em;
      border: 1px solid var(--divider-color, #e0e0e0); border-radius: 5px;
      background: var(--card-background-color, white); color: var(--primary-text-color);
    }
    .rename-actions { display: flex; gap: 6px; margin-top: 2px; }
    .rename-confirm-btn, .rename-cancel-btn {
      padding: 4px 10px; font-size: 0.78em; cursor: pointer;
      border-radius: 5px; border: 1px solid transparent;
    }
    .rename-confirm-btn { background: var(--primary-color, #03a9f4); color: white; }
    .rename-cancel-btn {
      background: var(--secondary-background-color, #f5f5f5);
      border-color: var(--divider-color, #e0e0e0); color: var(--primary-text-color);
    }
```

- [ ] **Step 2: Add the button/panel markup to `_makeRow`**

In `_makeRow()` (`js/dragontree-irrigation-cards.js:227-257`), replace the `<td class="col-station">` block:

```html
        <td class="col-station">
          <div class="os-name-row">
            <span class="os-label"></span>
            <span class="health-warn" title="" hidden>⚠</span>
          </div>
          <div class="base-label"></div>
          <button class="rename-btn" hidden>Rename</button>
          <div class="rename-panel" hidden>
            <input class="rename-base-input" type="text" placeholder="base_name" />
            <input class="rename-friendly-input" type="text" placeholder="Friendly Name" />
            <div class="rename-actions">
              <button class="rename-confirm-btn">Confirm</button>
              <button class="rename-cancel-btn">Cancel</button>
            </div>
          </div>
        </td>
```

- [ ] **Step 3: Add the rename listeners in `_makeRow`**

In `_makeRow()`, immediately before the `return tr;` line (`js/dragontree-irrigation-cards.js:317`), add:

```js
      // Rename button — reveals the confirm panel pre-filled with suggestions
      const renameBtn      = tr.querySelector('.rename-btn');
      const renamePanel    = tr.querySelector('.rename-panel');
      const baseInput      = tr.querySelector('.rename-base-input');
      const friendlyInput  = tr.querySelector('.rename-friendly-input');

      renameBtn.addEventListener('click', () => {
        const s = this._stationById(tr.dataset.sid);
        if (!s) return;
        baseInput.value     = s.suggested_base_name || '';
        friendlyInput.value = s.suggested_friendly_name || '';
        renamePanel.hidden  = false;
        this._editing = true;
      });

      tr.querySelector('.rename-cancel-btn').addEventListener('click', () => {
        renamePanel.hidden = true;
        this._editing = false;
      });

      tr.querySelector('.rename-confirm-btn').addEventListener('click', () => {
        const sid         = tr.dataset.sid;
        const newBase     = baseInput.value.trim();
        const newFriendly = friendlyInput.value.trim();
        if (!newBase) return;
        this._hass.callService(DOMAIN, 'rename_station', {
          station_id: sid,
          new_base_name: newBase,
          new_friendly_name: newFriendly || undefined,
        });
        renamePanel.hidden = true;
        this._editing = false;
      });
```

- [ ] **Step 4: Show/hide the button in `_patchRow`**

In `_patchRow()` (`js/dragontree-irrigation-cards.js:323-357`), immediately after the line `tr.querySelector('.base-label').textContent = station.base_name || '';`, add:

```js
      const renameBtn = tr.querySelector('.rename-btn');
      renameBtn.hidden = !station.rename_pending;
      if (!station.rename_pending) {
        tr.querySelector('.rename-panel').hidden = true;
      }
```

- [ ] **Step 5: Deploy to dev and verify visually**

```bash
cp js/dragontree-irrigation-cards.js /mnt/ha-dev/config/custom_components/dragontree_irrigation/js/
source ~/.config/ha-instances.env
curl -s -X POST -H "Authorization: Bearer $HA_DEV_TOKEN" "http://$HA_DEV_IP:8123/api/services/homeassistant/restart"
```

After dev is back up, open the Manage Stations tab in a browser (hard-refresh to bypass the JS cache-busting version, per this project's known caching behavior). Confirm no station shows a Rename button yet.

Trigger a detectable rename the same way Task 7's Step 6 did (temporarily set a station's stored `os_name` to something stale, restart), then confirm:
- The Rename button appears on that station's row.
- Clicking it reveals the panel pre-filled with the suggested base name/friendly name.
- Cancel hides the panel without calling any service.
- Confirm calls `rename_station` (watch Developer Tools > Actions logs, or just verify the row updates — the entity_ids and `base_name`/`friendly_name` should change, and the Rename button should disappear once `os_name` catches up).

Restore dev to its clean baseline afterward (rename back if a real slug changed) and restart once more.

- [ ] **Step 6: Commit**

```bash
git add js/dragontree-irrigation-cards.js
git commit -m "Add Rename button + confirm panel to the Manage Stations tab"
```

---

### Task 11: Phase 2 end-to-end verification

**Files:** none (verification only)

- [ ] **Step 1: Full scenario test on dev**

With all of Tasks 7–10 deployed:

1. Rename a real station on dev's OpenSprinkler controller (or simulate as in prior tasks).
2. Confirm the Rename button appears in the Manage Stations tab without a restart (detection is reactive, per Task 7).
3. Click Rename, edit the suggested friendly name to something different from the raw OS name, confirm.
4. Verify: the OpenSprinkler entities' entity_id changed, all `dragontree_irrigation` entities for that station changed, `base_name`/`friendly_name`/`os_name` updated correctly, and the edited friendly name (not the raw OS name) was actually applied.
5. Verify schedules, flow-monitoring history, and moisture-sensor association for that station are all unchanged (spot-check via the same station's flow status sensor `run_count` and its `moisture_sensor`/`moisture_max` fields in storage).
6. Verify no restart was needed at any point in this flow.
7. Test the collision guard: attempt `rename_station` twice with the same `new_base_name` targeting two different stations; confirm the second call raises an error and neither station's entities were partially renamed.

- [ ] **Step 2: Bump version and update changelog**

In `manifest.json`, bump `version` (minor bump — this adds a user-facing feature). In `CHANGELOG.md`, add an entry describing the new Rename button in the Manage Stations tab and the `rename_station` service.

- [ ] **Step 3: Commit, tag, push, release**

```bash
git add manifest.json CHANGELOG.md
git commit -m "Release vX.Y.Z — station rename detection and action"
git tag vX.Y.Z
git push origin main
git push origin vX.Y.Z
gh release create vX.Y.Z --repo mbateman66/dragontree_hacs_irrigation \
  --title "vX.Y.Z — Station rename detection and action" \
  --notes "The Manage Stations tab now detects when a station has been renamed on the OpenSprinkler controller and shows a Rename button. Confirming renames every entity_id for that station (both OpenSprinkler's and dragontree_irrigation's own) to match, with no restart required — replaces the manual external-script process used before this release."
```

(Replace `X.Y.Z` with the version chosen in Step 2.)
