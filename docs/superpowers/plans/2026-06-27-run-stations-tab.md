# Run Stations Tab — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a "Run Stations" dashboard tab that lets users manually start/stop individual stations, with per-station duration stored server-side.

**Architecture:** Two new HA services (`start_station`, `stop_station`) on the `dragontree_irrigation` domain handle all run/stop logic in the coordinator. A new `DragontreeStationControl` custom Lovelace card reads station state from existing binary sensors and the running-queue sensor, and persists per-station durations via the existing `update_station` service. A new Lovelace view wires the card into the dashboard.

**Tech Stack:** Python (Home Assistant custom component), vanilla JS (Shadow DOM custom element), Lovelace YAML.

## Global Constraints

- Python files live at `/home/mdb/dev/dragontree_irrigation/` (git repo); deploy by copying to `/mnt/ha-dev/config/custom_components/dragontree_irrigation/`
- JS file: `js/dragontree-irrigation-cards.js` — single bundled file, all card classes inside one IIFE
- Lovelace views loaded via `!include_dir_merge_list views/` — file sort order determines tab order
- `manual_duration` is stored in **minutes** (1–120); converted to seconds on service call
- No test suite exists; verification is manual via HA dev instance at SSH host `.50`
- Reload integration after Python/YAML changes: HA UI → Settings → Devices & Services → Dragontree Irrigation → ⋮ → Reload
- Hard-refresh browser (Shift+Reload) after JS changes

---

## File Map

| File | Change |
|------|--------|
| `const.py` | Add `DEFAULT_STATION_MANUAL_DURATION = 5` |
| `coordinator.py` | Add `_manual_stop_requested` flag; add `OS_SERVICE_STOP` import (bug fix); add `async_run_station_manual`; add `async_stop_station_manual`; modify `_run_queue` for cancellation; add migration guard |
| `__init__.py` | Import `SERVICE_START_STATION`, `SERVICE_STOP_STATION`; register two new service handlers; add `manual_duration` to `update_station` schema |
| `services.yaml` | Document `start_station`, `stop_station`; add `manual_duration` field to `update_station` |
| `js/dragontree-irrigation-cards.js` | Add `DragontreeStationControl` class + `customElements.define` + `customCards` entry |
| `lovelace/views/05.run.yaml` | **Create** — new Run Stations view |
| `lovelace/views/05.flow.yaml` | **Rename** → `06.flow.yaml` |

---

## Task 1: `manual_duration` field — constant, migration, schema, services.yaml

**Files:**
- Modify: `const.py`
- Modify: `coordinator.py` (migration guard only)
- Modify: `__init__.py` (schema addition only)
- Modify: `services.yaml`

**Interfaces:**
- Produces: `station.manual_duration` (int, minutes) available in `sensor.dragontree_irrigation_schedule` attributes; `update_station` service accepts `manual_duration`

- [ ] **Step 1: Add default constant to `const.py`**

  After the existing `DEFAULT_MANUAL_DURATION = 600` line, add:

  ```python
  DEFAULT_STATION_MANUAL_DURATION = 5  # minutes, per-station manual run default
  ```

- [ ] **Step 2: Add migration guard in `coordinator.py`**

  In `async_initialize`, in the `for s in self._stations:` migration block (around line 159), add after the last `s.setdefault(...)` call:

  ```python
              s.setdefault("manual_duration", DEFAULT_STATION_MANUAL_DURATION)
  ```

  Also add `DEFAULT_STATION_MANUAL_DURATION` to the `from .const import (...)` block in coordinator.py.

- [ ] **Step 3: Add `manual_duration` to `update_station` schema in `__init__.py`**

  In `_register_services`, find the `SERVICE_UPDATE_STATION` schema (the `vol.Schema({...})` block). Add this line inside it after the `moisture_max` entry:

  ```python
                  vol.Optional("manual_duration"): vol.All(
                      vol.Coerce(int), vol.Range(min=1, max=120)
                  ),
  ```

- [ ] **Step 4: Document `manual_duration` in `services.yaml`**

  In the `update_station:` block, add after the `moisture_max` field:

  ```yaml
      manual_duration:
        name: Manual Run Duration (minutes)
        description: Default duration shown in the Run Stations tab for this station.
        required: false
        selector:
          number:
            min: 1
            max: 120
            unit_of_measurement: min
  ```

- [ ] **Step 5: Deploy and verify**

  ```bash
  cp /home/mdb/dev/dragontree_irrigation/const.py /mnt/ha-dev/config/custom_components/dragontree_irrigation/const.py
  cp /home/mdb/dev/dragontree_irrigation/coordinator.py /mnt/ha-dev/config/custom_components/dragontree_irrigation/coordinator.py
  cp /home/mdb/dev/dragontree_irrigation/__init__.py /mnt/ha-dev/config/custom_components/dragontree_irrigation/__init__.py
  cp /home/mdb/dev/dragontree_irrigation/services.yaml /mnt/ha-dev/config/custom_components/dragontree_irrigation/services.yaml
  ```

  Reload integration in HA UI. Then in Developer Tools → States, open `sensor.dragontree_irrigation_schedule` and confirm each station in `attributes.stations` has a `manual_duration: 5` key.

  In Developer Tools → Services, call `dragontree_irrigation.update_station` with:
  ```yaml
  station_id: <any station id>
  manual_duration: 10
  ```
  Re-check the sensor — that station should now show `manual_duration: 10`.

- [ ] **Step 6: Commit**

  ```bash
  cd /home/mdb/dev/dragontree_irrigation
  git add const.py coordinator.py __init__.py services.yaml
  git commit -m "feat: add manual_duration field to stations (default 5 min)"
  ```

---

## Task 2: `start_station` service

**Files:**
- Modify: `coordinator.py` (new method + import fix)
- Modify: `__init__.py` (handler + registration)
- Modify: `services.yaml`

**Interfaces:**
- Consumes: `SERVICE_START_STATION = "start_station"` (already in `const.py`); `OPENSPRINKLER_DOMAIN`, `OS_SERVICE_RUN_STATION` (already imported in coordinator)
- Produces: `coordinator.async_run_station_manual(station_id: str, duration_seconds: int)` raises `HomeAssistantError` if a queue or station is already running

- [ ] **Step 1: Add `HomeAssistantError` import to `coordinator.py`**

  Add to the homeassistant imports block (after `from homeassistant.helpers.update_coordinator import DataUpdateCoordinator`):

  ```python
  from homeassistant.exceptions import HomeAssistantError
  ```

- [ ] **Step 2: Add `async_run_station_manual` to coordinator**

  Add this method in `coordinator.py` after `async_update_flow_config` (around line 1031), before `_build_data`:

  ```python
      async def async_run_station_manual(self, station_id: str, duration_seconds: int) -> None:
          """Start a station manually outside the queue."""
          if self._runtime.get("running_queue") or self._runtime.get("current_station_id"):
              raise HomeAssistantError(
                  "Cannot start a manual run while a queue or station is already running."
              )
          station = self._get_station(station_id)
          if not station:
              raise HomeAssistantError(f"Station '{station_id}' not found")
          entity_id = f"switch.{station['base_name']}_station_enabled"
          await self.hass.services.async_call(
              OPENSPRINKLER_DOMAIN,
              OS_SERVICE_RUN_STATION,
              {"run_seconds": duration_seconds},
              target={"entity_id": entity_id},
              blocking=True,
          )
          if station.get("flow_monitoring"):
              self._flow_monitor._start_monitoring(station["id"])
  ```

- [ ] **Step 3: Register the service in `__init__.py`**

  Add `SERVICE_START_STATION` to the `from .const import (...)` block.

  In `_register_services`, add the handler after `handle_discard_flow_runs_before`:

  ```python
      async def handle_start_station(call: ServiceCall) -> None:
          await coordinator.async_run_station_manual(
              call.data["station_id"], call.data["duration_seconds"]
          )
  ```

  Add the registration after the last `hass.services.async_register(...)` call:

  ```python
      hass.services.async_register(
          DOMAIN,
          SERVICE_START_STATION,
          handle_start_station,
          schema=vol.Schema(
              {
                  vol.Required("station_id"): cv.string,
                  vol.Required("duration_seconds"): vol.All(
                      vol.Coerce(int), vol.Range(min=60, max=7200)
                  ),
              }
          ),
      )
  ```

- [ ] **Step 4: Document in `services.yaml`**

  Add at the end of `services.yaml`:

  ```yaml
  start_station:
    name: Start Station
    description: Manually start a station outside the scheduled queue. Blocked if a queue or any station is already running.
    fields:
      station_id:
        name: Station ID
        required: true
        selector:
          text:
      duration_seconds:
        name: Duration (seconds)
        required: true
        selector:
          number:
            min: 60
            max: 7200
            unit_of_measurement: s
  ```

- [ ] **Step 5: Deploy and verify**

  ```bash
  cp /home/mdb/dev/dragontree_irrigation/coordinator.py /mnt/ha-dev/config/custom_components/dragontree_irrigation/coordinator.py
  cp /home/mdb/dev/dragontree_irrigation/__init__.py /mnt/ha-dev/config/custom_components/dragontree_irrigation/__init__.py
  cp /home/mdb/dev/dragontree_irrigation/services.yaml /mnt/ha-dev/config/custom_components/dragontree_irrigation/services.yaml
  ```

  Reload integration. In Developer Tools → Services, call `dragontree_irrigation.start_station` with a real station ID and `duration_seconds: 60`. The corresponding `binary_sensor.<base>_station_running` should go `on` within a few seconds. Verify it shuts off after 60 s.

  Also verify the guard: with a station running, call `start_station` again — HA should show an error notification "Cannot start a manual run while a queue or station is already running."

- [ ] **Step 6: Commit**

  ```bash
  cd /home/mdb/dev/dragontree_irrigation
  git add coordinator.py __init__.py services.yaml
  git commit -m "feat: add start_station service for manual station runs"
  ```

---

## Task 3: `stop_station` service + queue cancellation + import bug fix

**Files:**
- Modify: `coordinator.py`
- Modify: `__init__.py`
- Modify: `services.yaml`

**Interfaces:**
- Consumes: `SERVICE_STOP_STATION = "stop_station"` (already in `const.py`); `OS_SERVICE_STOP` (in `const.py` but not yet imported in coordinator — **this is a bug fix**)
- Produces: `coordinator.async_stop_station_manual()` — no-op if nothing is running; marks queue station as `STATUS_CANCELLED` if stop is requested during a queue run

- [ ] **Step 1: Fix `OS_SERVICE_STOP` import in `coordinator.py`**

  Add `OS_SERVICE_STOP` to the `from .const import (...)` block (it is already defined in `const.py` but missing from the coordinator's imports — the existing `_stop_any_running_stations` and `_stop_station_if_running` methods reference it and would throw `NameError` if those paths were hit):

  ```python
      OS_SERVICE_STOP,
  ```

- [ ] **Step 2: Add `_manual_stop_requested` flag to coordinator `__init__`**

  In `IrrigationCoordinator.__init__`, add after `self._queue_task: asyncio.Task | None = None`:

  ```python
          self._manual_stop_requested: bool = False
  ```

- [ ] **Step 3: Modify `_run_queue` to handle manual stop → cancelled**

  Find this block in `_run_queue` (around line 783):

  ```python
                  if station_entry["status"] == STATUS_RUNNING:
                      if started:
                          station_entry["status"] = STATUS_COMPLETE
                          station["last_run"] = date.today().isoformat()
                      else:
  ```

  Replace it with:

  ```python
                  if station_entry["status"] == STATUS_RUNNING:
                      if self._manual_stop_requested:
                          station_entry["status"] = STATUS_CANCELLED
                          self._manual_stop_requested = False
                      elif started:
                          station_entry["status"] = STATUS_COMPLETE
                          station["last_run"] = date.today().isoformat()
                      else:
  ```

- [ ] **Step 4: Add `async_stop_station_manual` to coordinator**

  Add after `async_run_station_manual` (before `_build_data`):

  ```python
      async def async_stop_station_manual(self) -> None:
          """Stop whatever station is currently running. No-op if nothing is running."""
          current_sid = self._runtime.get("current_station_id")
          if current_sid:
              # Queue is running — signal cancellation then stop OS
              self._manual_stop_requested = True
              station = self._get_station(current_sid)
              if station:
                  await self.hass.services.async_call(
                      OPENSPRINKLER_DOMAIN,
                      OS_SERVICE_STOP,
                      {},
                      target={"entity_id": f"switch.{station['base_name']}_station_enabled"},
                      blocking=True,
                  )
          else:
              # Manual run — find whichever station is physically on and stop it
              for s in self._stations:
                  bs_id = f"binary_sensor.{s['base_name']}_station_running"
                  bs_state = self.hass.states.get(bs_id)
                  if bs_state and bs_state.state == "on":
                      await self.hass.services.async_call(
                          OPENSPRINKLER_DOMAIN,
                          OS_SERVICE_STOP,
                          {},
                          target={"entity_id": f"switch.{s['base_name']}_station_enabled"},
                          blocking=True,
                      )
                      break
  ```

- [ ] **Step 5: Register the service in `__init__.py`**

  Add `SERVICE_STOP_STATION` to the `from .const import (...)` block.

  In `_register_services`, add handler after `handle_start_station`:

  ```python
      async def handle_stop_station(call: ServiceCall) -> None:
          await coordinator.async_stop_station_manual()
  ```

  Add registration after the `start_station` registration:

  ```python
      hass.services.async_register(
          DOMAIN,
          SERVICE_STOP_STATION,
          handle_stop_station,
          schema=vol.Schema({}),
      )
  ```

- [ ] **Step 6: Document in `services.yaml`**

  Add after `start_station:` block:

  ```yaml
  stop_station:
    name: Stop Station
    description: Stop whatever station is currently running. If a queue is active, the current station is marked cancelled and the queue advances to the next station. No-op if nothing is running.
  ```

- [ ] **Step 7: Deploy and verify**

  ```bash
  cp /home/mdb/dev/dragontree_irrigation/coordinator.py /mnt/ha-dev/config/custom_components/dragontree_irrigation/coordinator.py
  cp /home/mdb/dev/dragontree_irrigation/__init__.py /mnt/ha-dev/config/custom_components/dragontree_irrigation/__init__.py
  cp /home/mdb/dev/dragontree_irrigation/services.yaml /mnt/ha-dev/config/custom_components/dragontree_irrigation/services.yaml
  ```

  Reload integration. Test two scenarios:

  **Manual stop:** Call `start_station` (60 s). While running, call `stop_station`. The binary sensor should go `off` within seconds. Calling `stop_station` again (nothing running) should succeed silently.

  **Queue stop:** Trigger an AM or PM queue manually (or wait for scheduled run). While a station is running, call `stop_station`. The station should stop, the calendar view should show that station as `cancelled`, and the queue should continue to the next station.

- [ ] **Step 8: Commit**

  ```bash
  cd /home/mdb/dev/dragontree_irrigation
  git add coordinator.py __init__.py services.yaml
  git commit -m "feat: add stop_station service; fix OS_SERVICE_STOP import; cancelled status on manual queue interrupt"
  ```

---

## Task 4: `DragontreeStationControl` JS card

**Files:**
- Modify: `js/dragontree-irrigation-cards.js`

**Interfaces:**
- Consumes: `SENSOR = 'sensor.dragontree_irrigation_schedule'` (already defined in the file's IIFE scope); `DOMAIN = 'dragontree_irrigation'` (same scope)
- Consumes services: `dragontree_irrigation.start_station`, `dragontree_irrigation.stop_station`, `dragontree_irrigation.update_station`
- Consumes state: `sensor.dragontree_irrigation_running_queue` (state is `"idle"`, `"AM"`, or `"PM"`); `binary_sensor.<base_name>_station_running` per station

- [ ] **Step 1: Add `CONTROL_STYLES` constant**

  In `dragontree-irrigation-cards.js`, inside the IIFE and after the last existing `const ... = \`...\`` styles block (just before `class DragontreeFlowMonitor`), add:

  ```javascript
    const CONTROL_STYLES = `
      :host { display: block; }
      .card {
        background: var(--ha-card-background, var(--card-background-color, white));
        border-radius: var(--ha-card-border-radius, 12px);
        box-shadow: var(--ha-card-box-shadow, none);
        border: 1px solid var(--ha-card-border-color, var(--divider-color, #e0e0e0));
        overflow: hidden;
      }
      .card-header {
        padding: 16px 16px 8px;
        font-size: 1.5em; font-weight: 500;
        color: var(--ha-card-header-color, var(--primary-text-color));
      }
      .card-content { padding: 0 16px 16px; overflow-x: auto; }
      table { width: 100%; border-collapse: collapse; font-size: 0.9em; }
      thead th {
        padding: 6px 10px; text-align: left;
        font-size: 0.75em; font-weight: 600; letter-spacing: 0.06em;
        text-transform: uppercase; color: var(--secondary-text-color);
        border-bottom: 2px solid var(--divider-color, #e0e0e0);
        white-space: nowrap;
      }
      tbody td { padding: 8px 10px; vertical-align: middle; }
      tbody tr + tr td { border-top: 1px solid var(--divider-color, #e0e0e0); }
      .col-name { min-width: 150px; }
      .col-stop, .col-start { width: 90px; }
      .col-dur { width: 130px; }
      .station-name { font-size: 0.95em; color: var(--primary-text-color); }
      .action-btn {
        padding: 5px 12px; font-size: 0.82em; cursor: pointer;
        border-radius: 6px; border: 1px solid transparent; font-weight: 500;
        transition: opacity 0.15s;
      }
      .stop-btn {
        background: var(--error-color, #db4437); color: white;
        border-color: var(--error-color, #db4437);
      }
      .start-btn {
        background: var(--primary-color, #03a9f4); color: white;
        border-color: var(--primary-color, #03a9f4);
      }
      .action-btn:disabled {
        background: var(--secondary-background-color, #f5f5f5);
        border-color: var(--divider-color, #e0e0e0);
        color: var(--secondary-text-color); cursor: default; opacity: 0.45;
      }
      .action-btn:hover:not(:disabled) { opacity: 0.82; }
      .dur-input {
        width: 52px; padding: 4px 8px; text-align: right;
        border: 1px solid var(--divider-color, #e0e0e0); border-radius: 6px;
        background: var(--secondary-background-color, #f5f5f5);
        color: var(--primary-text-color); font-size: 0.88em;
      }
      .dur-input:focus { outline: none; border-color: var(--primary-color, #03a9f4); }
      .dur-unit { font-size: 0.78em; color: var(--secondary-text-color); margin-left: 4px; }
      .row-running { background: rgba(var(--rgb-primary-color, 3, 169, 244), 0.08); }
      .empty {
        text-align: center; padding: 32px 0;
        color: var(--secondary-text-color); font-style: italic;
      }
    `;
  ```

- [ ] **Step 2: Add the `DragontreeStationControl` class**

  Immediately after the `CONTROL_STYLES` const, add:

  ```javascript
    class DragontreeStationControl extends HTMLElement {

      setConfig(config) {
        this._config   = config || {};
        this._stations = [];
        this._editing  = false;
        this._lastKey  = null;

        if (!this.shadowRoot) {
          this.attachShadow({ mode: 'open' });
          this.shadowRoot.innerHTML = `
            <style>${CONTROL_STYLES}</style>
            <div class="card">
              <div class="card-header">Run Stations</div>
              <div class="card-content">
                <table>
                  <thead><tr>
                    <th class="col-name">Station</th>
                    <th class="col-stop">Stop</th>
                    <th class="col-start">Start</th>
                    <th class="col-dur">Duration</th>
                  </tr></thead>
                  <tbody id="sbody"></tbody>
                </table>
              </div>
            </div>`;
        }
      }

      getCardSize() {
        return Math.max(3, this._stations.length + 2);
      }

      set hass(hass) {
        this._hass = hass;
        if (!this.shadowRoot || this._editing) return;

        const stateObj  = hass.states[SENSOR];
        const stations  = (stateObj?.attributes?.stations || [])
          .filter(s => s.tracked !== false);

        const queueState   = hass.states['sensor.dragontree_irrigation_running_queue'];
        const queueActive  = queueState && queueState.state !== 'idle';

        const runningBase  = stations.find(s => {
          const bs = hass.states[`binary_sensor.${s.base_name}_station_running`];
          return bs && bs.state === 'on';
        })?.base_name || null;

        const key = stations.map(s =>
          `${s.id}|${s.friendly_name}|${s.manual_duration}`
        ).join(',') + '|' + (runningBase || '') + '|' + (queueActive ? 'q' : 'i');

        if (key === this._lastKey) return;
        this._lastKey  = key;
        this._stations = stations;
        this._sync(!!runningBase, queueActive, runningBase);
      }

      _sync(anyRunning, queueActive, runningBase) {
        const tbody    = this.shadowRoot.getElementById('sbody');
        const stations = this._stations;

        if (!stations.length) {
          while (tbody.firstChild) tbody.removeChild(tbody.firstChild);
          const tr = document.createElement('tr');
          const td = document.createElement('td');
          td.colSpan = 4;
          td.className = 'empty';
          td.textContent = 'No stations found — reload the Dragontree Irrigation integration.';
          tr.appendChild(td);
          tbody.appendChild(tr);
          return;
        }

        while (tbody.children.length < stations.length) tbody.appendChild(this._makeRow());
        while (tbody.children.length > stations.length) tbody.removeChild(tbody.lastChild);

        for (let i = 0; i < stations.length; i++) {
          this._patchRow(tbody.children[i], stations[i], anyRunning, queueActive, runningBase);
        }
      }

      _makeRow() {
        const tr = document.createElement('tr');
        tr.innerHTML = `
          <td class="col-name"><span class="station-name"></span></td>
          <td class="col-stop"><button class="action-btn stop-btn">Stop</button></td>
          <td class="col-start"><button class="action-btn start-btn">Start</button></td>
          <td class="col-dur">
            <input class="dur-input" type="number" min="1" max="120" step="1" />
            <span class="dur-unit">min</span>
          </td>`;

        tr.querySelector('.stop-btn').addEventListener('click', () => {
          this._hass.callService(DOMAIN, 'stop_station', {});
        });

        tr.querySelector('.start-btn').addEventListener('click', () => {
          const sid = tr.dataset.sid;
          const dur = parseInt(tr.querySelector('.dur-input').value, 10) || 5;
          this._hass.callService(DOMAIN, 'start_station', {
            station_id:       sid,
            duration_seconds: dur * 60,
          });
        });

        const durInput = tr.querySelector('.dur-input');
        durInput.addEventListener('focus',   () => { this._editing = true; });
        durInput.addEventListener('keydown', e => {
          if (e.key === 'Enter')  durInput.blur();
          if (e.key === 'Escape') {
            const s = this._stationById(tr.dataset.sid);
            if (s) durInput.value = s.manual_duration || 5;
            durInput.blur();
          }
        });
        durInput.addEventListener('blur', () => {
          this._editing = false;
          const sid    = tr.dataset.sid;
          const s      = this._stationById(sid);
          const newVal = parseInt(durInput.value, 10);
          if (s && !isNaN(newVal) && newVal >= 1 && newVal <= 120
              && newVal !== (s.manual_duration || 5)) {
            s.manual_duration = newVal;
            this._hass.callService(DOMAIN, 'update_station', {
              station_id:      sid,
              manual_duration: newVal,
            });
          }
        });

        return tr;
      }

      _patchRow(tr, station, anyRunning, queueActive, runningBase) {
        tr.dataset.sid = station.id;
        tr.className   = station.base_name === runningBase ? 'row-running' : '';

        tr.querySelector('.station-name').textContent = station.friendly_name || station.base_name;

        const stopBtn  = tr.querySelector('.stop-btn');
        const startBtn = tr.querySelector('.start-btn');
        const durInput = tr.querySelector('.dur-input');

        stopBtn.disabled  = station.base_name !== runningBase;
        startBtn.disabled = anyRunning || queueActive;

        if (!this._editing) {
          durInput.value = station.manual_duration || 5;
        }
      }

      _stationById(sid) {
        return this._stations.find(s => s.id === sid) || null;
      }
    }

    customElements.define('dragontree-irrigation-station-control', DragontreeStationControl);
  ```

- [ ] **Step 3: Add entry to `window.customCards`**

  In the `window.customCards.push(...)` array at the bottom of the file, add:

  ```javascript
      {
        type:        'dragontree-irrigation-station-control',
        name:        'Dragontree Station Control',
        description: 'Manually start and stop irrigation stations with per-station duration.',
      },
  ```

- [ ] **Step 4: Deploy and verify**

  ```bash
  cp /home/mdb/dev/dragontree_irrigation/js/dragontree-irrigation-cards.js \
     /mnt/ha-dev/config/custom_components/dragontree_irrigation/js/dragontree-irrigation-cards.js
  ```

  Hard-refresh the browser (Shift+Reload). In HA Lovelace, add a manual card:
  ```yaml
  type: custom:dragontree-irrigation-station-control
  ```
  Verify:
  - Table renders with all tracked stations
  - Stop buttons are disabled, Start buttons enabled (when nothing is running)
  - Change a duration and blur — the updated value should persist after page reload (confirming the `update_station` service call saved it)
  - Start a station — its row highlights, its Stop button enables, all Start buttons disable
  - Click Stop — row un-highlights, Stop disables, Starts re-enable

- [ ] **Step 5: Commit**

  ```bash
  cd /home/mdb/dev/dragontree_irrigation
  git add js/dragontree-irrigation-cards.js
  git commit -m "feat: add DragontreeStationControl card for manual station run/stop"
  ```

---

## Task 5: New Lovelace view

**Files:**
- Rename: `lovelace/views/05.flow.yaml` → `lovelace/views/06.flow.yaml`
- Create: `lovelace/views/05.run.yaml`

**Interfaces:**
- Consumes: `custom:dragontree-irrigation-station-control` (registered in Task 4)

- [ ] **Step 1: Rename the flow view file**

  ```bash
  cd /home/mdb/dev/dragontree_irrigation
  git mv lovelace/views/05.flow.yaml lovelace/views/06.flow.yaml
  ```

- [ ] **Step 2: Create the new Run Stations view**

  Create `lovelace/views/05.run.yaml` with:

  ```yaml

    ##########################################################################
    # VIEW: Run Stations (manual start/stop)
    ##########################################################################
    - title: Run Stations
      path: run-stations
      icon: mdi:play-circle
      type: panel
      cards:
        - type: custom:dragontree-irrigation-station-control
  ```

  (The leading blank line and indentation match the other view files — each view file contributes items to a merged YAML list.)

- [ ] **Step 3: Deploy and verify**

  ```bash
  cp /home/mdb/dev/dragontree_irrigation/lovelace/views/06.flow.yaml \
     /mnt/ha-dev/config/custom_components/dragontree_irrigation/lovelace/views/06.flow.yaml
  cp /home/mdb/dev/dragontree_irrigation/lovelace/views/05.run.yaml \
     /mnt/ha-dev/config/custom_components/dragontree_irrigation/lovelace/views/05.run.yaml
  # Remove old filename from mounted location
  rm /mnt/ha-dev/config/custom_components/dragontree_irrigation/lovelace/views/05.flow.yaml
  ```

  Reload integration (or full HA restart if lovelace doesn't pick up the new view). Hard-refresh browser. Confirm:
  - "Run Stations" tab appears between "Manage Stations" and "Flow Monitor"
  - "Flow Monitor" tab still present and functional
  - The Run Stations tab shows the station-control card

- [ ] **Step 4: Commit**

  ```bash
  cd /home/mdb/dev/dragontree_irrigation
  git add lovelace/views/05.run.yaml lovelace/views/06.flow.yaml
  git commit -m "feat: add Run Stations dashboard tab; rename flow view to 06"
  ```

---

## Self-Review

**Spec coverage check:**
- ✅ New tab with Run Stations label — Task 5
- ✅ Stations listed in same order as Manage Stations — read from `stations` attribute which preserves coordinator order
- ✅ Column order: Name | Stop | Start | Duration — Task 4 `_makeRow`
- ✅ Stop enabled only when station is running — `stopBtn.disabled = station.base_name !== runningBase`
- ✅ Stop cancels queue entry and moves to next — Task 3 `_manual_stop_requested` + `_run_queue` modification
- ✅ Start disabled when any station running — `startBtn.disabled = anyRunning || queueActive`
- ✅ Start disabled when queue active — `startBtn.disabled = anyRunning || queueActive`
- ✅ Start calls `start_station` with `duration_seconds = minutes * 60` — Task 4 start button handler
- ✅ `run_station_manual` validates no queue and no station running — Task 2 coordinator method
- ✅ Duration default 5 min — `DEFAULT_STATION_MANUAL_DURATION = 5` + `setdefault`
- ✅ Duration remembered per station, stored in HA — `manual_duration` field on station, persisted via `update_station`
- ✅ Running row highlighted — `.row-running` CSS + `tr.className` in `_patchRow`
- ✅ Untracked stations excluded — `.filter(s => s.tracked !== false)` in `set hass()`
- ✅ `stop_station` no-op if nothing running — Task 3 `async_stop_station_manual`
- ✅ `OS_SERVICE_STOP` import bug fixed — Task 3 Step 1
