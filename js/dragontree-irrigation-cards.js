/**
 * Dragontree Station Manager — custom Lovelace card
 *
 * Architecture notes:
 *  - Each <tr> is created ONCE; event listeners live on the row forever.
 *    _updateRow() patches individual cells in-place so listeners are never lost.
 *  - Move buttons do optimistic updates: swap locally + re-render immediately,
 *    then fire the service. No waiting for the backend round-trip.
 *  - An _editing flag blocks hass-driven re-renders while a text field is focused.
 */

(function () {
  const SENSOR = 'sensor.dragontree_irrigation_schedule';
  const DOMAIN  = 'dragontree_irrigation';

  if (customElements.get('dragontree-irrigation-station-schedules')) return;

  const STYLES = `
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
      font-size: 1.5em;
      font-weight: 500;
      color: var(--ha-card-header-color, var(--primary-text-color));
    }
    .card-content { padding: 0 16px 16px; overflow-x: auto; }

    table { width: 100%; border-collapse: collapse; font-size: 0.9em; }
    thead th {
      padding: 6px 10px;
      text-align: left;
      font-size: 0.75em;
      font-weight: 600;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      color: var(--secondary-text-color);
      border-bottom: 2px solid var(--divider-color, #e0e0e0);
      white-space: nowrap;
    }
    tbody td { padding: 8px 10px; vertical-align: middle; }
    tbody tr + tr td { border-top: 1px solid var(--divider-color, #e0e0e0); }

    .col-order { width: 44px; }
    .btn-group  { display: flex; flex-direction: column; gap: 3px; }
    .move-btn {
      display: flex; align-items: center; justify-content: center;
      width: 26px; height: 22px; padding: 0;
      background: var(--secondary-background-color, #f5f5f5);
      border: 1px solid var(--divider-color, #e0e0e0);
      border-radius: 4px;
      cursor: pointer;
      color: var(--primary-text-color);
      font-size: 0.7em;
      transition: background 0.15s, border-color 0.15s, color 0.15s;
    }
    .move-btn:hover:not(:disabled) {
      background: var(--primary-color, #03a9f4);
      border-color: var(--primary-color, #03a9f4);
      color: white;
    }
    .move-btn:disabled { opacity: 0.25; cursor: default; }

    .col-station { min-width: 120px; }
    .os-label  { font-size: 0.95em; color: var(--primary-text-color); }
    .base-label {
      font-size: 0.72em; font-family: monospace;
      color: var(--secondary-text-color); margin-top: 2px;
    }

    .col-friendly { min-width: 150px; }
    .name-input {
      width: 100%; padding: 5px 9px;
      border: 1px solid var(--divider-color, #e0e0e0);
      border-radius: 6px;
      background: var(--secondary-background-color, #f5f5f5);
      color: var(--primary-text-color);
      font-size: 0.9em; box-sizing: border-box;
    }
    .name-input:focus {
      outline: none;
      border-color: var(--primary-color, #03a9f4);
    }

    .col-tracked  { width: 68px; text-align: center; }
    .col-os       { width: 68px; text-align: center; }
    .toggle {
      position: relative; display: inline-block;
      width: 40px; height: 22px; cursor: pointer;
    }
    .toggle input { opacity: 0; width: 0; height: 0; position: absolute; }
    .slider {
      position: absolute; inset: 0;
      background: #bdbdbd;
      border-radius: 22px;
      transition: background 0.2s;
    }
    .slider::before {
      content: ""; position: absolute;
      width: 16px; height: 16px; left: 3px; bottom: 3px;
      background: white;
      border-radius: 50%;
      transition: transform 0.2s; box-shadow: 0 1px 3px rgba(0,0,0,0.3);
    }
    .toggle input:checked + .slider { background: var(--primary-color, #03a9f4); }
    .toggle input:checked + .slider::before { transform: translateX(18px); }

    .row-tracked td { opacity: 0.5; }
    .row-tracked td.col-order,
    .row-tracked td.col-tracked,
    .row-tracked td.col-os { opacity: 1; }

    .os-name-row { display: flex; align-items: center; gap: 5px; }
    .health-warn {
      display: inline-block; font-size: 1em; cursor: default; flex-shrink: 0;
      color: var(--warning-color, #ff9800);
    }
    .health-warn[hidden] { display: none; }

    .empty {
      text-align: center; padding: 32px 0;
      color: var(--secondary-text-color); font-style: italic;
    }
  `;

  class DragontreeStationManager extends HTMLElement {

    setConfig(config) {
      this._config   = config || {};
      this._stations = [];
      this._editing  = false;
      this._lastKey  = null;

      if (!this.shadowRoot) {
        this.attachShadow({ mode: 'open' });
        this.shadowRoot.innerHTML = `
          <style>${STYLES}</style>
          <div class="card">
            <div class="card-header">Manage Stations</div>
            <div class="card-content">
              <table>
                <thead>
                  <tr>
                    <th class="col-order"></th>
                    <th class="col-station">OpenSprinkler Station</th>
                    <th class="col-friendly">Friendly Name</th>
                    <th class="col-tracked">Tracked</th>
                    <th class="col-os">OS Enabled</th>
                  </tr>
                </thead>
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
      const stations  = stateObj && stateObj.attributes && stateObj.attributes.stations || [];
      const osStates  = stations.map(s => (hass.states['switch.' + s.base_name + '_station_enabled'] || {}).state || '');
      const healthKeys = stations.map(s => [
        'switch.'        + s.base_name + '_station_enabled',
        'binary_sensor.' + s.base_name + '_station_running',
        'sensor.'        + s.base_name + '_station_status',
      ].map(eid => { const e = hass.states[eid]; return e ? e.state : 'X'; }).join(','));
      const key       = JSON.stringify(stations) + osStates.join(',') + '|' + healthKeys.join(';');
      if (key === this._lastKey) return;

      this._lastKey  = key;
      this._stations = stations.map(s => Object.assign({}, s)); // shallow clone each
      this._sync();
    }

    // -------------------------------------------------------------------------
    // DOM sync — creates missing rows, removes extra rows, updates all cells.
    // Rows are never recreated; only their content is patched.
    // -------------------------------------------------------------------------
    _sync() {
      const tbody    = this.shadowRoot.getElementById('sbody');
      const stations = this._stations;

      if (!stations.length) {
        while (tbody.firstChild) tbody.removeChild(tbody.firstChild);
        const tr = document.createElement('tr');
        const td = document.createElement('td');
        td.colSpan = 5;
        td.className = 'empty';
        td.textContent = 'No stations found — reload the Dragontree Irrigation integration.';
        tr.appendChild(td);
        tbody.appendChild(tr);
        return;
      }

      // Add rows until we have enough
      while (tbody.children.length < stations.length) {
        tbody.appendChild(this._makeRow());
      }
      // Remove excess rows
      while (tbody.children.length > stations.length) {
        tbody.removeChild(tbody.lastChild);
      }
      // Patch each row in-place
      for (let i = 0; i < stations.length; i++) {
        this._patchRow(tbody.children[i], stations[i], i, stations.length);
      }
    }

    // -------------------------------------------------------------------------
    // Create a single <tr> with all its child elements and permanent listeners.
    // Listeners read station ID from the row's data-sid attribute at fire time,
    // so they remain correct even after _patchRow changes the row's content.
    // -------------------------------------------------------------------------
    _makeRow() {
      const tr  = document.createElement('tr');
      tr.innerHTML = `
        <td class="col-order">
          <div class="btn-group">
            <button class="move-btn" data-dir="up"   title="Move up">▲</button>
            <button class="move-btn" data-dir="down" title="Move down">▼</button>
          </div>
        </td>
        <td class="col-station">
          <div class="os-name-row">
            <span class="os-label"></span>
            <span class="health-warn" title="" hidden>⚠</span>
          </div>
          <div class="base-label"></div>
        </td>
        <td class="col-friendly">
          <input class="name-input" type="text" />
        </td>
        <td class="col-tracked">
          <label class="toggle">
            <input type="checkbox" class="tracked-check" />
            <span class="slider"></span>
          </label>
        </td>
        <td class="col-os">
          <label class="toggle">
            <input type="checkbox" class="os-check" />
            <span class="slider"></span>
          </label>
        </td>`;

      // Move up / down — reads station ID from row at click time
      tr.querySelectorAll('.move-btn').forEach(btn => {
        btn.addEventListener('click', () => {
          this._move(tr.dataset.sid, btn.dataset.dir);
        });
      });

      // Friendly name
      const input = tr.querySelector('.name-input');
      input.addEventListener('focus',   () => { this._editing = true; });
      input.addEventListener('keydown', e  => {
        if (e.key === 'Enter')  { input.blur(); }
        if (e.key === 'Escape') {
          const s = this._stationById(tr.dataset.sid);
          if (s) input.value = s.friendly_name;
          input.blur();
        }
      });
      input.addEventListener('blur', () => {
        this._editing = false;
        const sid    = tr.dataset.sid;
        const s      = this._stationById(sid);
        const newVal = input.value.trim();
        if (s && newVal && newVal !== s.friendly_name) {
          s.friendly_name = newVal;                // update local copy
          // Do NOT update _lastKey here — it must keep reflecting the last HA
          // state so that stale hass calls (from other entities) are skipped
          // until the backend confirms this change.
          this._hass.callService(DOMAIN, 'update_station', {
            station_id: sid, friendly_name: newVal,
          });
        }
      });

      // Tracked toggle
      const check = tr.querySelector('.tracked-check');
      check.addEventListener('change', () => {
        const sid = tr.dataset.sid;
        const s   = this._stationById(sid);
        if (!s) return;
        s.tracked = check.checked;
        tr.className = s.tracked ? '' : 'row-tracked';
        // Do NOT update _lastKey — same reason as the blur handler above.
        this._hass.callService(DOMAIN, 'update_station', {
          station_id: sid, tracked: check.checked,
        });
      });

      // OS enabled toggle
      const osCheck = tr.querySelector('.os-check');
      osCheck.addEventListener('change', () => {
        const s = this._stationById(tr.dataset.sid);
        if (!s) return;
        this._hass.callService('homeassistant', osCheck.checked ? 'turn_on' : 'turn_off', {
          entity_id: 'switch.' + s.base_name + '_station_enabled',
        });
      });

      return tr;
    }

    // -------------------------------------------------------------------------
    // Patch a row's visible content without touching its event listeners.
    // -------------------------------------------------------------------------
    _patchRow(tr, station, index, total) {
      tr.dataset.sid = station.id;
      tr.className   = station.tracked === false ? 'row-tracked' : '';

      const [upBtn, downBtn] = tr.querySelectorAll('.move-btn');
      upBtn.disabled   = (index === 0);
      downBtn.disabled = (index === total - 1);

      tr.querySelector('.os-label').textContent   = this._osName(station.base_name);
      tr.querySelector('.base-label').textContent = station.base_name || '';

      // Only overwrite the input if it isn't focused (user might be editing)
      const input = tr.querySelector('.name-input');
      if (!this._editing) {
        input.value = station.friendly_name || '';
      }

      tr.querySelector('.tracked-check').checked = station.tracked !== false;

      const osState = this._hass && this._hass.states['switch.' + station.base_name + '_station_enabled'];
      tr.querySelector('.os-check').checked = osState ? osState.state === 'on' : true;

      const requiredEntities = [
        'switch.'         + station.base_name + '_station_enabled',
        'binary_sensor.'  + station.base_name + '_station_running',
        'sensor.'         + station.base_name + '_station_status',
      ];
      const missing = requiredEntities.filter(eid => {
        const e = this._hass && this._hass.states[eid];
        return !e || e.state === 'unavailable' || e.state === 'unknown';
      });
      const warn = tr.querySelector('.health-warn');
      warn.hidden = missing.length === 0;
      warn.title  = missing.length ? 'Missing or unavailable:\n' + missing.join('\n') : '';
    }

    // -------------------------------------------------------------------------
    // Optimistic move: swap locally, re-sync DOM, then fire service.
    // -------------------------------------------------------------------------
    _move(sid, direction) {
      const arr = this._stations;
      const idx = arr.findIndex(s => s.id === sid);
      if (idx < 0) return;

      if (direction === 'up' && idx > 0) {
        [arr[idx], arr[idx - 1]] = [arr[idx - 1], arr[idx]];
      } else if (direction === 'down' && idx < arr.length - 1) {
        [arr[idx], arr[idx + 1]] = [arr[idx + 1], arr[idx]];
      } else {
        return;
      }

      // Do NOT update _lastKey — it stays as the last HA-confirmed state so
      // that any stale hass calls before the backend confirms are skipped.
      this._sync();

      this._hass.callService(DOMAIN, 'move_station', {
        station_id: sid, direction: direction,
      });
    }

    // -------------------------------------------------------------------------
    // Helpers
    // -------------------------------------------------------------------------
    _stationById(sid) {
      return this._stations.find(s => s.id === sid) || null;
    }

    _osName(baseName) {
      try {
        const state = this._hass.states['switch.' + baseName + '_station_enabled'];
        if (state && state.attributes && state.attributes.friendly_name) {
          return state.attributes.friendly_name
            .replace(/\s+station\s+enabled$/i, '')
            .replace(/\s+station$/i, '')
            .trim();
        }
      } catch (e) { /* fall through */ }
      return (baseName || '')
        .replace(/_/g, ' ')
        .replace(/\b\w/g, c => c.toUpperCase());
    }
  }

  customElements.define('dragontree-irrigation-station-manager', DragontreeStationManager);


  // =========================================================================
  // dragontree-station-schedules  (Schedules view)
  // =========================================================================
  const _DAYS = ['mon','tue','wed','thu','fri','sat','sun'];

  const SCHED_STYLES = `
    :host { display: block; }
    .page-header {
      display: flex; align-items: center; justify-content: space-between;
      padding: 16px 20px 14px;
      // background: var(--ha-card-background, var(--card-background-color, white));
      // border-radius: var(--ha-card-border-radius, 12px);
      // border: 1px solid var(--ha-card-border-color, var(--divider-color, #e0e0e0));
      // border: 0;
      // box-shadow: var(--ha-card-box-shadow, none);
      // margin-bottom: 8px;
    }
    .page-title {
      font-size: 1.5em; font-weight: 500;
      color: var(--ha-card-header-color, var(--primary-text-color));
    }
    .btn-group { display: flex; gap: 6px; }
    .tool-btn {
      padding: 5px 12px; font-size: 0.82em; cursor: pointer;
      border: 1px solid var(--primary-color, #03a9f4); border-radius: 6px;
      background: transparent; color: var(--primary-color, #03a9f4);
      transition: background 0.15s, color 0.15s;
    }
    .tool-btn:hover { background: var(--primary-color, #03a9f4); color: white; }
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
      gap: 10px;
      padding: 10px;
    }
    .card {
      background: var(--ha-card-background, var(--card-background-color, white));
      box-shadow: var(--ha-card-box-shadow, none);
      border: 3px solid var(--ha-card-border-color, var(--divider-color, #e0e0e0));
      border-radius: 40px;
      overflow: hidden;
    }
    .card-header {
      display: flex; align-items: center; gap: 10px;
      padding: 14px 16px; font-size: 1.1em; font-weight: 500;
      color: var(--ha-card-header-color, var(--primary-text-color));
      border-bottom: 1px solid var(--divider-color, #e0e0e0);
    }
    .card-header ha-icon {
      --mdc-icon-size: 22px;
      color: var(--state-icon-color, var(--secondary-text-color));
    }
    .card-body { padding: 0 16px 12px; }
    .entity-row {
      display: flex; align-items: center; min-height: 44px;
      border-top: 1px solid var(--divider-color, #e0e0e0);
    }
    .card-body > .entity-row:first-child { border-top: none; }
    .row-label { flex: 1; font-size: 0.9em; color: var(--primary-text-color); }
    .mode-select {
      padding: 4px 8px; font-size: 0.88em;
      border: 1px solid var(--divider-color, #e0e0e0);
      border-radius: 6px;
      background: var(--secondary-background-color, #f5f5f5);
      color: var(--primary-text-color); cursor: pointer;
    }
    .toggle {
      position: relative; display: inline-block;
      width: 40px; height: 22px; cursor: pointer;
    }
    .toggle input { opacity: 0; width: 0; height: 0; position: absolute; }
    .slider {
      position: absolute; inset: 0;
      background: #bdbdbd;
      border-radius: 12px;
      transition: background 0.2s;
    }
    .slider::before {
      content: ""; position: absolute;
      width: 16px; height: 16px; left: 3px; bottom: 3px;
      background: white;
      border-radius: 50%;
      transition: transform 0.2s; box-shadow: 0 1px 3px rgba(0,0,0,0.3);
    }
    .toggle input:checked + .slider { background: var(--primary-color, #03a9f4); }
    .toggle input:checked + .slider::before { transform: translateX(18px); }
    details.panel { border-top: 2px solid var(--divider-color, #e0e0e0); }
    details.panel > summary {
      padding: 9px 0; font-size: 0.82em; font-weight: 600;
      letter-spacing: 0.05em; text-transform: uppercase;
      color: var(--secondary-text-color);
      cursor: pointer; list-style: none; user-select: none;
      display: flex; align-items: center; gap: 6px;
    }
    details.panel > summary::before {
      content: "▶"; font-size: 0.7em; transition: transform 0.15s;
      display: inline-block;
    }
    details.panel[open] > summary::before { transform: rotate(90deg); }
    .panel-body .entity-row:first-child { border-top: none; }
    .pips { display: flex; gap: 5px; }
    .pip-group { display: flex; flex-direction: column; align-items: center; gap: 3px; }
    .pip-label {
      font-size: 0.68em; font-weight: 600;
      color: var(--secondary-text-color); line-height: 1; user-select: none;
    }
    .pip {
      width: 22px;
      height: 22px;
      border-radius: 50%;
      border: none;
      background: var(--divider-color, #e0e0e0);
      cursor: pointer;
      transition: background 0.15s;
    }
    .pip.on { background: var(--primary-color, #03a9f4); }
    .num-input {
      width: 64px; padding: 4px 8px; text-align: right;
      border: 1px solid var(--divider-color, #e0e0e0);
      border-radius: 6px;
      background: var(--secondary-background-color, #f5f5f5);
      color: var(--primary-text-color);
      font-size: 0.88em;
    }
    .num-input:focus { outline: none; border-color: var(--primary-color, #03a9f4); }

    .moisture-current {
      font-size: 0.88em; font-weight: 500;
      color: var(--primary-text-color);
      padding: 3px 8px;
      background: var(--secondary-background-color, #f5f5f5);
      border: 1px solid var(--divider-color, #e0e0e0);
      border-radius: 6px;
    }
    .moisture-current.overmax {
      color: var(--error-color, #db4437);
      border-color: var(--error-color, #db4437);
    }
  `;

  class DragontreeStationSchedules extends HTMLElement {
    setConfig(config) {
      this._config  = config || {};
      this._lastKey = null;
      if (!this.shadowRoot) {
        this.attachShadow({ mode: 'open' });
        this.shadowRoot.innerHTML = `
          <style>${SCHED_STYLES}</style>
          <div class="page-header">
            <span class="page-title">Station Schedules</span>
            <div class="btn-group">
              <button class="tool-btn" id="btn-expand">Expand All</button>
              <button class="tool-btn" id="btn-collapse">Collapse All</button>
            </div>
          </div>
          <div class="grid" id="grid"></div>`;
        this.shadowRoot.getElementById('btn-expand').addEventListener('click', () => {
          this.shadowRoot.querySelectorAll('details.panel').forEach(d => { d.open = true; });
        });
        this.shadowRoot.getElementById('btn-collapse').addEventListener('click', () => {
          this.shadowRoot.querySelectorAll('details.panel').forEach(d => { d.open = false; });
        });
      }
    }

    getCardSize() { return Math.max(3, (this._stationCount || 0) * 6); }

    set hass(hass) {
      this._hass = hass;
      if (!this.shadowRoot) return;
      const stations = (hass.states[SENSOR]?.attributes?.stations || [])
        .filter(s => s.tracked !== false);
      const key = JSON.stringify(stations.map(s => s.id + '|' + s.friendly_name));
      if (key !== this._lastKey) {
        this._lastKey      = key;
        this._stationCount = stations.length;
        this._buildCards(stations);
      }
      this._updateStates();
    }

    _buildCards(stations) {
      const grid = this.shadowRoot.getElementById('grid');
      grid.innerHTML = '';
      for (const station of stations) {
        grid.appendChild(this._makeCard(station));
      }
      this._attachListeners();
    }

    _makeCard(station) {
      const sid = station.base_name;
      const el  = document.createElement('div');
      el.className = 'card';
      el.innerHTML = `
        <div class="card-header">${this._esc(station.friendly_name)}</div>
        <div class="card-body">
          <div class="entity-row">
            <span class="row-label">Schedule Mode</span>
            <select class="mode-select" data-entity="select.dragontree_irrigation_${sid}_schedule_mode">
              <option value="Off">Off</option>
              <option value="Normal">Normal</option>
              <option value="Hot">Hot</option>
            </select>
          </div>
          <div class="entity-row">
            <span class="row-label">Sensitive (run on light rain)</span>
            <label class="toggle">
              <input type="checkbox" data-toggle data-entity="switch.dragontree_irrigation_${sid}_sensitive" />
              <span class="slider"></span>
            </label>
          </div>
          ${this._moisturePanelHTML(sid)}
          ${this._panelHTML(sid, 'normal')}
          ${this._panelHTML(sid, 'hot')}
        </div>`;
      return el;
    }

    _moisturePanelHTML(sid) {
      return `
        <details class="panel">
          <summary>Moisture Sensor</summary>
          <div class="panel-body">
            <div class="entity-row">
              <span class="row-label">Sensor</span>
              <select class="mode-select" data-moisture-select data-sid="${sid}">
                <option value="">None</option>
              </select>
            </div>
            <div class="entity-row moisture-value-row" style="display:none">
              <span class="row-label">Current Reading</span>
              <span class="moisture-current"></span>
            </div>
            <div class="entity-row moisture-max-row" style="display:none">
              <span class="row-label">Skip if above (%)</span>
              <input class="num-input" type="number" min="0" max="100" step="1"
                     data-moisture-max data-sid="${sid}" />
            </div>
          </div>
        </details>`;
    }

    _panelHTML(sid, type) {
      const title = type === 'normal' ? 'Normal Schedule' : 'Hot Schedule';
      return `
        <details class="panel">
          <summary>${title}</summary>
          <div class="panel-body">
            ${this._pipRowHTML('Queues',
              [`switch.dragontree_irrigation_${sid}_${type}_am`, `switch.dragontree_irrigation_${sid}_${type}_pm`],
              ['AM', 'PM'])}
            ${this._pipRowHTML('Days',
              _DAYS.map(d => `switch.dragontree_irrigation_${sid}_${type}_${d}`),
              ['Mo','Tu','We','Th','Fr','Sa','Su'])}
            <div class="entity-row">
              <span class="row-label">Every N Weeks</span>
              <input class="num-input" type="number" min="1" max="8" step="1"
                     data-entity="number.dragontree_irrigation_${sid}_${type}_week_interval" />
            </div>
            <div class="entity-row">
              <span class="row-label">Duration (min)</span>
              <input class="num-input" type="number" min="0" max="600" step="1"
                     data-entity="number.dragontree_irrigation_${sid}_${type}_duration" />
            </div>
          </div>
        </details>`;
    }

    _pipRowHTML(label, entities, labels) {
      return `
        <div class="entity-row">
          <span class="row-label">${label}</span>
          <div class="pips">
            ${entities.map((e, i) => `
              <div class="pip-group">
                <span class="pip-label">${labels[i]}</span>
                <button class="pip" data-entity="${e}"></button>
              </div>`).join('')}
          </div>
        </div>`;
    }

    _attachListeners() {
      this.shadowRoot.querySelectorAll('select[data-entity]').forEach(el => {
        el.addEventListener('change', () => {
          this._hass.callService('select', 'select_option', {
            entity_id: el.dataset.entity, option: el.value,
          });
        });
      });

      this.shadowRoot.querySelectorAll('input[data-toggle]').forEach(el => {
        el.addEventListener('change', () => {
          this._hass.callService('homeassistant', el.checked ? 'turn_on' : 'turn_off', {
            entity_id: el.dataset.entity,
          });
        });
      });

      this.shadowRoot.querySelectorAll('.pip[data-entity]').forEach(btn => {
        btn.addEventListener('click', () => {
          const isOn = btn.classList.contains('on');
          this._hass.callService('homeassistant', isOn ? 'turn_off' : 'turn_on', {
            entity_id: btn.dataset.entity,
          });
        });
      });

      this.shadowRoot.querySelectorAll('.num-input').forEach(el => {
        if (el.dataset.moistureMax !== undefined) return; // handled separately below
        el.addEventListener('keydown', e => {
          if (e.key === 'Enter')  el.blur();
          if (e.key === 'Escape') {
            const s = this._hass?.states[el.dataset.entity];
            if (s) el.value = s.state;
            el.blur();
          }
        });
        el.addEventListener('blur', () => {
          const val = parseFloat(el.value);
          if (!isNaN(val)) {
            this._hass.callService('number', 'set_value', {
              entity_id: el.dataset.entity, value: val,
            });
          }
        });
      });

      this.shadowRoot.querySelectorAll('[data-moisture-select]').forEach(el => {
        el.addEventListener('change', () => {
          this._hass.callService(DOMAIN, 'update_station', {
            station_id: el.dataset.sid,
            moisture_sensor: el.value,  // empty string clears the association
          });
        });
      });

      this.shadowRoot.querySelectorAll('[data-moisture-max]').forEach(el => {
        el.addEventListener('keydown', e => {
          if (e.key === 'Enter')  el.blur();
          if (e.key === 'Escape') {
            const stations = this._hass?.states[SENSOR]?.attributes?.stations || [];
            const station  = stations.find(s => s.base_name === el.dataset.sid);
            el.value = station?.moisture_max ?? '';
            el.blur();
          }
        });
        el.addEventListener('blur', () => {
          const val = parseFloat(el.value);
          if (!isNaN(val)) {
            this._hass.callService(DOMAIN, 'update_station', {
              station_id: el.dataset.sid,
              moisture_max: val,
            });
          }
        });
      });
    }

    _updateStates() {
      const hass   = this._hass;
      const active = this.shadowRoot.activeElement;
      this.shadowRoot.querySelectorAll('select[data-entity]').forEach(el => {
        const s = hass.states[el.dataset.entity];
        if (s) el.value = s.state;
      });
      this.shadowRoot.querySelectorAll('input[data-toggle]').forEach(el => {
        const s = hass.states[el.dataset.entity];
        if (s) el.checked = s.state === 'on';
      });
      this.shadowRoot.querySelectorAll('.pip[data-entity]').forEach(btn => {
        const s = hass.states[btn.dataset.entity];
        btn.classList.toggle('on', !!s && s.state === 'on');
      });
      this.shadowRoot.querySelectorAll('.num-input').forEach(el => {
        if (el.dataset.moistureMax !== undefined) return; // handled below
        if (el === active) return;
        const s = hass.states[el.dataset.entity];
        if (s) el.value = s.state;
      });

      // Moisture sensor panels
      const allStations = hass.states[SENSOR]?.attributes?.stations || [];
      const moistureEntities = Object.values(hass.entities || {}).filter(e =>
        Array.isArray(e.labels) &&
        e.labels.includes('soil') &&
        e.labels.includes('moisture')
      );

      this.shadowRoot.querySelectorAll('[data-moisture-select]').forEach(select => {
        const sid          = select.dataset.sid;
        const station      = allStations.find(s => s.base_name === sid);
        const currentValue = station?.moisture_sensor || '';

        // Rebuild options: keep "None" first, then one per moisture entity
        const existingValues = new Set(
          Array.from(select.options).map(o => o.value).filter(v => v)
        );
        const targetValues = new Set(moistureEntities.map(e => e.entity_id));

        for (const e of moistureEntities) {
          if (!existingValues.has(e.entity_id)) {
            const opt       = document.createElement('option');
            opt.value       = e.entity_id;
            opt.textContent = e.name ||
              hass.states[e.entity_id]?.attributes?.friendly_name ||
              e.entity_id;
            select.appendChild(opt);
          }
        }
        for (const opt of Array.from(select.options)) {
          if (opt.value && !targetValues.has(opt.value)) opt.remove();
        }

        if (select !== active) select.value = currentValue;

        // Show/hide value and max rows based on whether a sensor is selected
        const card      = select.closest('.card');
        const valueRow  = card.querySelector('.moisture-value-row');
        const maxRow    = card.querySelector('.moisture-max-row');
        const reading   = card.querySelector('.moisture-current');
        const maxInput  = card.querySelector('[data-moisture-max]');

        const hasSensor = !!currentValue;
        valueRow.style.display = hasSensor ? '' : 'none';
        maxRow.style.display   = hasSensor ? '' : 'none';

        if (hasSensor && reading) {
          const sensorState = hass.states[currentValue];
          const raw         = sensorState?.state;
          const unit        = sensorState?.attributes?.unit_of_measurement || '%';
          const moistureMax = station?.moisture_max;
          const isValid     = raw && raw !== 'unavailable' && raw !== 'unknown';
          const isOverMax   = isValid && moistureMax != null && parseFloat(raw) > parseFloat(moistureMax);

          reading.textContent = isValid ? `${raw} ${unit}` : '—';
          reading.classList.toggle('overmax', isOverMax);
        }

        if (hasSensor && maxInput && maxInput !== active) {
          const moistureMax = station?.moisture_max;
          maxInput.value = moistureMax != null ? moistureMax : '';
        }
      });
    }

    _esc(str) {
      return String(str || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }
  }

  customElements.define('dragontree-irrigation-station-schedules', DragontreeStationSchedules);

  // =========================================================================
  // dragontree-irrigation-schedule-calendar  (Calendar view)
  // =========================================================================
  const CALENDAR_STYLES = `
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
      font-size: 1.5em;
      font-weight: 500;
      color: var(--ha-card-header-color, var(--primary-text-color));
    }
    .card-content { padding: 0 16px 16px; overflow-x: auto; }

    table { width: 100%; border-collapse: collapse; font-size: 1em; }
    thead th {
      padding: 6px 6px;
      text-align: left;
      font-size: 0.75em;
      font-weight: 600;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      color: var(--secondary-text-color);
      border-bottom: 2px solid var(--divider-color, #e0e0e0);
      white-space: nowrap;
    }
    tbody tr + tr { border-top: 1px solid var(--divider-color, #e0e0e0); }
    tbody tr.today {
      background: rgba(var(--rgb-primary-color, 3, 169, 244), 0.06);
    }
    tbody td { padding: 6px 6px; vertical-align: top; }

    .col-day  { white-space: nowrap; font-weight: 600; }
    .col-date { white-space: nowrap; color: var(--secondary-text-color); font-size: 0.88em; }
    .col-time {
      white-space: nowrap;
      color: var(--secondary-text-color);
      font-size: 0.88em;
      min-width: 50px;
    }
    .col-time.overrun { color: var(--warning-color, #f57c00); }
    .col-stations { min-width: 120px; }

    .station { padding: 1px 0; line-height: 1.6; }
    .station.scheduled { color: var(--primary-text-color); }
    .station.running   { color: var(--primary-color, #03a9f4); font-weight: bold; }
    .station.complete  { color: var(--disabled-text-color, #9e9e9e); }
    .station.cancelled { color: var(--disabled-text-color, #9e9e9e); text-decoration: line-through; }
    .station.failed    { color: var(--error-color, #db4437); text-decoration: line-through; }
    .no-stations       { color: var(--secondary-text-color); font-size: 0.85em; }
  `;

  class DragontreeScheduleCalendar extends HTMLElement {
    setConfig(config) {
      this._config   = config || {};
      this._lastKey  = null;
      this._rowCount = 7;

      if (!this.shadowRoot) {
        this.attachShadow({ mode: 'open' });
        this.shadowRoot.innerHTML = `
          <style>${CALENDAR_STYLES}</style>
          <div class="card">
            <div class="card-header">Schedule Calendar</div>
            <div class="card-content">
              <table>
                <thead><tr>
                  <th class="col-day">Day</th>
                  <th class="col-date">Date</th>
                  <th class="col-time">AM Time</th>
                  <th class="col-stations">AM Stations</th>
                  <th class="col-time">PM Time</th>
                  <th class="col-stations">PM Stations</th>
                </tr></thead>
                <tbody id="tbody"></tbody>
              </table>
            </div>
          </div>`;
      }
    }

    getCardSize() { return Math.max(3, this._rowCount + 2); }

    set hass(hass) {
      this._hass = hass;
      if (!this.shadowRoot) return;
      const schedules = hass.states[SENSOR]?.attributes?.day_schedules || [];
      const key = JSON.stringify(schedules);
      if (key === this._lastKey) return;
      this._lastKey = key;
      this._render(schedules);
    }

    _render(schedules) {
      const tbody = this.shadowRoot.getElementById('tbody');
      if (!schedules.length) {
        tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;padding:32px 0;' +
          'color:var(--secondary-text-color);font-style:italic;">No schedule data available.</td></tr>';
        this._rowCount = 1;
        return;
      }

      // Build today string in local time (matches Python date.today().isoformat())
      const now = new Date();
      const today = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;

      this._rowCount = schedules.length;
      tbody.innerHTML = schedules.map(day => {
        const am = day.queues?.am;
        const pm = day.queues?.pm;
        const [, mo, dy] = day.date.split('-');
        const rowClass = day.date === today ? ' class="today"' : '';
        return `<tr${rowClass}>
          <td class="col-day">${this._esc(day.day_of_week)}</td>
          <td class="col-date">${mo}/${dy}</td>
          <td class="col-time${am?.overrun ? ' overrun' : ''}">${this._timeRange(am)}${am?.overrun ? '&nbsp;⚠️' : ''}</td>
          <td class="col-stations">${this._stationList(am?.stations)}</td>
          <td class="col-time">${this._timeRange(pm)}</td>
          <td class="col-stations">${this._stationList(pm?.stations)}</td>
        </tr>`;
      }).join('');
    }

    _timeRange(queue) {
      if (!queue?.stations?.length) return '';
      return `${queue.start_time}&thinsp;–&thinsp;${queue.end_time}`;
    }

    _stationList(stations) {
      if (!stations?.length) return '<span class="no-stations">—</span>';
      return stations.map(s => {
        const cls = (s.status || 'scheduled').toLowerCase();
        return `<div class="station ${cls}">${this._esc(s.friendly_name)}</div>`;
      }).join('');
    }

    _esc(str) {
      return String(str || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }
  }

  customElements.define('dragontree-irrigation-schedule-calendar', DragontreeScheduleCalendar);

  // =========================================================================
  // dragontree-irrigation-station-control  (Run Stations view)
  // =========================================================================

  const CONTROL_STYLES = `
    :host { display: block; width: fit-content; }
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
    .card-content { padding: 0 16px 16px; }
    table { width: auto; border-collapse: collapse; font-size: 0.9em; }
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

  class DragontreeStationControl extends HTMLElement {

    setConfig(config) {
      this._config                = config || {};
      this._stations              = [];
      this._editing               = false;
      this._lastKey               = null;
      this._optimisticRunningBase = null;
      this._optimisticStopped     = false;
      this._sawOnAfterStop        = false;
      this._stopClickTime         = 0;
      this._lastQueueActive       = false;

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

      // Clear optimistic states once real HA state catches up.
      // _optimisticStopped must survive the binary sensor's delayed ON fire after a
      // fast Start→Stop: only clear it after seeing ON→OFF, or after a 10s fallback.
      if (runningBase) {
        this._optimisticRunningBase = null;
        if (this._optimisticStopped) this._sawOnAfterStop = true;
      }
      if (this._optimisticStopped && !runningBase) {
        if (this._sawOnAfterStop || Date.now() - this._stopClickTime > 10000) {
          this._optimisticStopped = false;
          this._sawOnAfterStop    = false;
        }
      }
      const effectiveRunningBase = this._optimisticStopped ? null
        : (this._optimisticRunningBase || runningBase);
      this._lastQueueActive = queueActive;

      const key = stations.map(s =>
        `${s.id}|${s.friendly_name}|${s.manual_duration}`
      ).join(',') + '|' + (effectiveRunningBase || '') + '|' + (queueActive ? 'q' : 'i');

      if (key === this._lastKey) return;
      this._lastKey  = key;
      this._stations = stations;
      this._sync(!!effectiveRunningBase, queueActive, effectiveRunningBase);
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

      // Clear if transitioning from the empty-state message to real rows
      if (tbody.children.length && !tbody.children[0].dataset.sid) {
        while (tbody.firstChild) tbody.removeChild(tbody.firstChild);
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
        this._optimisticRunningBase = null;
        this._optimisticStopped     = true;
        this._sawOnAfterStop        = false;
        this._stopClickTime         = Date.now();
        this._lastKey               = null;
        this._sync(false, this._lastQueueActive, null);
      });

      tr.querySelector('.start-btn').addEventListener('click', () => {
        const sid = tr.dataset.sid;
        const dur = parseInt(tr.querySelector('.dur-input').value, 10) || 5;
        this._hass.callService(DOMAIN, 'start_station', {
          station_id:       sid,
          duration_seconds: dur * 60,
        });
        // Clear any stop optimistic, then set start optimistic
        this._optimisticStopped     = false;
        this._sawOnAfterStop        = false;
        this._optimisticRunningBase = this._stationById(sid)?.base_name || null;
        if (this._optimisticRunningBase) {
          this._lastKey = null;
          this._sync(true, this._lastQueueActive, this._optimisticRunningBase);
        }
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

  // =========================================================================
  // dragontree-irrigation-flow-monitor  (Flow Monitor view)
  // =========================================================================

  const FLOW_STYLES = `
    :host { display: block; }

    /* ── page chrome ── */
    .page-header {
      display: flex; align-items: center; justify-content: space-between;
      padding: 16px 20px 14px;
    }
    .page-title { font-size: 1.5em; font-weight: 500; color: var(--primary-text-color); }

    /* ── global config panel ── */
    .global-config {
      margin: 0 10px 10px;
      background: var(--ha-card-background, var(--card-background-color, white));
      border: 1px solid var(--ha-card-border-color, var(--divider-color, #e0e0e0));
      border-radius: 12px;
      overflow: hidden;
    }
    .global-config summary {
      padding: 12px 16px; cursor: pointer; list-style: none; user-select: none;
      font-size: 0.85em; font-weight: 600; letter-spacing: 0.05em; text-transform: uppercase;
      color: var(--secondary-text-color);
      display: flex; align-items: center; gap: 6px;
    }
    .global-config summary::before {
      content: "▶"; font-size: 0.7em; transition: transform 0.15s; display: inline-block;
    }
    .global-config[open] summary::before { transform: rotate(90deg); }
    .global-config .cfg-body { padding: 0 16px 12px; }
    .cfg-row {
      display: flex; align-items: center; min-height: 40px;
      border-top: 1px solid var(--divider-color, #e0e0e0);
      gap: 8px;
    }
    .cfg-label { flex: 1; font-size: 0.88em; color: var(--primary-text-color); }
    .cfg-hint  { font-size: 0.75em; color: var(--secondary-text-color); }
    .cfg-input {
      padding: 4px 8px; font-size: 0.88em;
      border: 1px solid var(--divider-color, #e0e0e0); border-radius: 6px;
      background: var(--secondary-background-color, #f5f5f5);
      color: var(--primary-text-color);
    }
    .cfg-input:focus { outline: none; border-color: var(--primary-color, #03a9f4); }
    .cfg-entity { width: 240px; }
    .cfg-num    { width: 72px; text-align: right; }
    .cfg-select { padding: 4px 8px; font-size: 0.88em; cursor: pointer;
      border: 1px solid var(--divider-color, #e0e0e0); border-radius: 6px;
      background: var(--secondary-background-color, #f5f5f5);
      color: var(--primary-text-color);
    }
    .save-btn {
      margin-top: 10px; padding: 6px 16px; font-size: 0.82em; cursor: pointer;
      border: 1px solid var(--primary-color, #03a9f4); border-radius: 6px;
      background: var(--primary-color, #03a9f4); color: white;
      transition: opacity 0.15s, background 0.15s, border-color 0.15s, color 0.15s;
    }
    .save-btn:hover:not(:disabled) { opacity: 0.85; }
    .save-btn:disabled {
      cursor: default;
      background: var(--secondary-background-color, #f5f5f5);
      border-color: var(--divider-color, #e0e0e0);
      color: var(--secondary-text-color);
    }

    /* ── station grid ── */
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
      gap: 10px; padding: 10px;
    }

    /* ── station card ── */
    .scard {
      background: var(--ha-card-background, var(--card-background-color, white));
      border: 3px solid var(--ha-card-border-color, var(--divider-color, #e0e0e0));
      border-radius: 28px;
      overflow: hidden;
      transition: border-color 0.25s;
    }
    .scard.anomaly { border-color: #ef5350; }
    .scard-header {
      display: flex; align-items: center; gap: 10px;
      padding: 14px 16px 10px;
    }
    .scard-name { flex: 1; font-size: 1.05em; font-weight: 500; color: var(--primary-text-color); }
    .status-badge {
      display: inline-flex; align-items: center; gap: 5px;
      padding: 3px 10px; border-radius: 20px;
      font-size: 0.72em; font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase;
      white-space: nowrap;
    }
    .status-dot { width: 7px; height: 7px; border-radius: 50%; }
    .status-learning  { background: #eceff1; color: #546e7a; }
    .status-learning .status-dot  { background: #90a4ae; }
    .status-normal    { background: #e8f5e9; color: #2e7d32; }
    .status-normal .status-dot    { background: #66bb6a; }
    .status-high      { background: #fce4ec; color: #c62828; }
    .status-high .status-dot      { background: #ef5350; }
    .status-low       { background: #fff3e0; color: #e65100; }
    .status-low .status-dot       { background: #ffa726; }
    .status-monitoring { background: #e3f2fd; color: #1565c0; }
    .status-monitoring .status-dot { background: #42a5f5; }
    .status-unknown   { background: #f5f5f5; color: #757575; }
    .status-unknown .status-dot   { background: #bdbdbd; }

    /* ── progress bar ── */
    .progress-wrap { padding: 0 16px 8px; }
    .progress-label { font-size: 0.75em; color: var(--secondary-text-color); margin-bottom: 4px; }
    .progress-track {
      height: 6px; background: var(--divider-color, #e0e0e0); border-radius: 3px; overflow: hidden;
    }
    .progress-fill {
      height: 100%; background: #90a4ae; border-radius: 3px;
      transition: width 0.4s;
    }

    /* ── body rows ── */
    .scard-body { padding: 0 16px 12px; }
    .row {
      display: flex; align-items: center; min-height: 40px;
      border-top: 1px solid var(--divider-color, #e0e0e0);
      gap: 8px;
    }
    .scard-body > .row:first-child { border-top: none; }
    .row-label { flex: 1; font-size: 0.88em; color: var(--primary-text-color); }
    .baseline-val {
      font-size: 0.95em; font-weight: 600; color: var(--primary-color, #03a9f4);
      font-family: monospace;
    }

    /* ── toggle ── */
    .toggle { position: relative; display: inline-block; width: 40px; height: 22px; cursor: pointer; }
    .toggle input { opacity: 0; width: 0; height: 0; position: absolute; }
    .slider {
      position: absolute; inset: 0; background: #bdbdbd;
      border-radius: 22px; transition: background 0.2s;
    }
    .slider::before {
      content: ""; position: absolute;
      width: 16px; height: 16px; left: 3px; bottom: 3px;
      background: white; border-radius: 50%;
      transition: transform 0.2s; box-shadow: 0 1px 3px rgba(0,0,0,0.3);
    }
    .toggle input:checked + .slider { background: var(--primary-color, #03a9f4); }
    .toggle input:checked + .slider::before { transform: translateX(18px); }

    /* ── number input ── */
    .num-input {
      width: 64px; padding: 4px 8px; text-align: right;
      border: 1px solid var(--divider-color, #e0e0e0); border-radius: 6px;
      background: var(--secondary-background-color, #f5f5f5);
      color: var(--primary-text-color); font-size: 0.88em;
    }
    .num-input:focus { outline: none; border-color: var(--primary-color, #03a9f4); }

    /* ── reset button ── */
    .reset-btn {
      padding: 4px 12px; font-size: 0.78em; cursor: pointer;
      border: 1px solid var(--divider-color, #e0e0e0); border-radius: 6px;
      background: var(--secondary-background-color, #f5f5f5);
      color: var(--secondary-text-color);
      transition: border-color 0.15s, color 0.15s;
    }
    .reset-btn:hover {
      border-color: var(--error-color, #db4437);
      color: var(--error-color, #db4437);
    }

    /* ── chart area ── */
    .chart-wrap { padding: 8px 16px 4px; cursor: default; }
    .no-runs { font-size: 0.78em; color: var(--secondary-text-color); font-style: italic; text-align: center; padding: 8px 0; }
    .run-bar { cursor: pointer; }
    .run-bar:hover { opacity: 1 !important; filter: brightness(1.15); }

    /* ── run action panel ── */
    .run-panel {
      margin: 0 16px 8px; padding: 8px 12px;
      background: var(--secondary-background-color, #f5f5f5);
      border-radius: 10px; border: 1px solid var(--divider-color, #e0e0e0);
    }
    .run-panel-date {
      font-size: 0.78em; color: var(--secondary-text-color); margin-bottom: 5px;
    }
    .run-panel-grid {
      display: grid; grid-template-columns: auto 1fr;
      gap: 2px 10px; margin-bottom: 8px;
    }
    .run-detail-label {
      font-size: 0.75em; color: var(--secondary-text-color); white-space: nowrap;
      display: flex; align-items: center;
    }
    .run-detail-val {
      font-size: 0.78em; color: var(--primary-text-color); font-family: monospace;
      display: flex; align-items: center;
    }
    .run-deviation-high { color: #ef5350; }
    .run-deviation-low  { color: #ffa726; }
    .run-panel-btns { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; border-top: 1px solid var(--divider-color, #e0e0e0); padding-top: 7px; }
    .run-btn {
      padding: 4px 10px; font-size: 0.75em; cursor: pointer; border-radius: 6px;
      border: 1px solid transparent; font-weight: 500; white-space: nowrap;
      background: transparent;
    }
    .run-btn.run-discard {
      background: var(--error-color, #db4437); color: white;
      border-color: var(--error-color, #db4437);
    }
    .run-btn.run-discard:hover { opacity: 0.85; }
    .run-btn.run-before { color: #e65100; border-color: #e65100; }
    .run-btn.run-before:hover { background: #fff3e0; }
    .run-btn.run-cancel {
      color: var(--secondary-text-color); border-color: var(--divider-color, #e0e0e0);
      margin-left: auto;
    }
    .run-btn.run-cancel:hover { border-color: var(--secondary-text-color); }

    /* ── disabled overlay ── */
    .scard.disabled [data-enabled-only] { opacity: 0.4; pointer-events: none; }
    .scard.disabled .chart-wrap { display: none; }
    .scard.disabled .progress-wrap { display: none; }
  `;

  class DragontreeFlowMonitor extends HTMLElement {

    setConfig(config) {
      this._config  = config || {};
      this._lastKey = null;
      this._editingFill = false;

      if (!this.shadowRoot) {
        this.attachShadow({ mode: 'open' });
        this.shadowRoot.innerHTML = `
          <style>${FLOW_STYLES}</style>
          <div class="page-header">
            <span class="page-title">Flow Monitor</span>
          </div>
          <details class="global-config" id="globalConfig">
            <summary>Global Configuration</summary>
            <div class="cfg-body" id="cfgBody"></div>
          </details>
          <div class="grid" id="grid"></div>`;
      }
      this._buildGlobalConfig();
    }

    getCardSize() { return Math.max(3, (this._stationCount || 0) * 4); }

    // -----------------------------------------------------------------------
    // hass setter — called on every HA state update
    // -----------------------------------------------------------------------
    set hass(hass) {
      this._hass = hass;
      if (!this.shadowRoot) return;

      const stations = (hass.states[SENSOR]?.attributes?.stations || [])
        .filter(s => s.tracked !== false);
      const flowCfg  = hass.states[SENSOR]?.attributes?.flow_config || {};

      // Key: station list + all flow entity states
      const key = this._stateKey(hass, stations);
      if (key !== this._lastKey) {
        this._lastKey      = key;
        this._stationCount = stations.length;
        this._rebuildGrid(stations);
      }

      this._updateStates(hass, stations, flowCfg);
    }

    _stateKey(hass, stations) {
      return stations.map(s => {
        const status  = hass.states[`sensor.dragontree_irrigation_${s.id}_flow_status`];
        const monitor = hass.states[`switch.dragontree_irrigation_${s.id}_flow_monitoring`];
        const anomaly = hass.states[`binary_sensor.dragontree_irrigation_${s.id}_flow_anomaly`];
        const runs    = status?.attributes?.recent_runs?.join(',') || '';
        return `${s.id}:${s.friendly_name}:${monitor?.state}:${status?.state}:${anomaly?.state}:${runs}`;
      }).join('|') + '|' + JSON.stringify(hass.states[SENSOR]?.attributes?.flow_config);
    }

    // -----------------------------------------------------------------------
    // Global config panel (built once in setConfig, updated in _updateStates)
    // -----------------------------------------------------------------------
    _buildGlobalConfig() {
      const body = this.shadowRoot.getElementById('cfgBody');
      if (!body) return;
      this._savedFormState = null;  // null = not yet initialised from HA
      this._sensorDirty = false;

      body.innerHTML = `
        <div class="cfg-row">
          <span class="cfg-label">Flow Sensor</span>
          <select class="cfg-select cfg-entity" id="cfgSensor">
            <option value="">— select sensor —</option>
          </select>
        </div>
        <div class="cfg-row">
          <span class="cfg-label">Alert Threshold
            <span class="cfg-hint"> (% deviation to trigger alarm)</span>
          </span>
          <input class="cfg-input cfg-num" id="cfgThreshold" type="number" min="5" max="100" step="1" />
          <span style="font-size:0.82em">%</span>
        </div>
        <div class="cfg-row">
          <span class="cfg-label">Min Runs to Learn
            <span class="cfg-hint"> (before alerting)</span>
          </span>
          <input class="cfg-input cfg-num" id="cfgMinRuns" type="number" min="1" max="30" step="1" />
        </div>
        <div class="cfg-row">
          <span class="cfg-label">Sample Interval
            <span class="cfg-hint"> (seconds between readings)</span>
          </span>
          <input class="cfg-input cfg-num" id="cfgInterval" type="number" min="5" max="60" step="5" />
          <span style="font-size:0.82em">s</span>
        </div>
        <button class="save-btn" id="cfgSave" disabled>Save Configuration</button>`;

      // Enable save button whenever a field changes
      ['cfgSensor', 'cfgThreshold', 'cfgMinRuns', 'cfgInterval'].forEach(id => {
        const el = this.shadowRoot.getElementById(id);
        el.addEventListener('change', () => {
          if (id === 'cfgSensor') this._sensorDirty = true;
          this._updateSaveButton();
        });
        el.addEventListener('input',  () => this._updateSaveButton());
      });

      this.shadowRoot.getElementById('cfgSave').addEventListener('click', () => {
        const sensorVal = this.shadowRoot.getElementById('cfgSensor').value;
        const threshold = parseFloat(this.shadowRoot.getElementById('cfgThreshold').value);
        const minRuns   = parseInt(this.shadowRoot.getElementById('cfgMinRuns').value, 10);
        const interval  = parseInt(this.shadowRoot.getElementById('cfgInterval').value, 10);
        const data = {};
        if (sensorVal !== undefined) data.flow_sensor_entity   = sensorVal;
        if (!isNaN(threshold))       data.flow_alert_threshold = threshold / 100;
        if (!isNaN(minRuns))         data.flow_min_runs        = minRuns;
        if (!isNaN(interval))        data.flow_sample_interval = interval;
        this._hass.callService(DOMAIN, 'update_flow_config', data);
        // Optimistically commit — HA will confirm via the next hass update
        this._commitFormState();
        this._updateSaveButton();
      });
    }

    _readFormState() {
      return {
        sensor:    this.shadowRoot.getElementById('cfgSensor')?.value    ?? '',
        threshold: this.shadowRoot.getElementById('cfgThreshold')?.value ?? '',
        minRuns:   this.shadowRoot.getElementById('cfgMinRuns')?.value   ?? '',
        interval:  this.shadowRoot.getElementById('cfgInterval')?.value  ?? '',
      };
    }

    _commitFormState() {
      this._savedFormState = this._readFormState();
    }

    _updateSaveButton() {
      const btn = this.shadowRoot.getElementById('cfgSave');
      if (!btn) return;
      if (!this._savedFormState) { btn.disabled = true; return; }
      const cur   = this._readFormState();
      const saved = this._savedFormState;
      const dirty = Object.keys(cur).some(k => cur[k] !== saved[k]);
      btn.disabled = !dirty;
    }

    // -----------------------------------------------------------------------
    // Grid rebuild — creates all station card DOM, attaches permanent listeners
    // -----------------------------------------------------------------------
    _rebuildGrid(stations) {
      const grid = this.shadowRoot.getElementById('grid');
      grid.innerHTML = '';
      for (const station of stations) {
        grid.appendChild(this._makeStationCard(station));
      }
    }

    _makeStationCard(station) {
      const sid = station.id;
      const el  = document.createElement('div');
      el.className = 'scard';
      el.dataset.sid = sid;
      el.innerHTML = `
        <div class="scard-header">
          <span class="scard-name">${this._esc(station.friendly_name)}</span>
          <span class="status-badge status-unknown" data-badge>
            <span class="status-dot"></span>
            <span data-badge-text>unknown</span>
          </span>
        </div>
        <div class="progress-wrap" data-progress style="display:none">
          <div class="progress-label" data-progress-label>Learning 0 of 5 runs</div>
          <div class="progress-track"><div class="progress-fill" data-progress-fill style="width:0%"></div></div>
        </div>
        <div class="chart-wrap" data-chart></div>
        <div class="run-panel" data-run-panel style="display:none">
          <div class="run-panel-date" data-run-date></div>
          <div class="run-panel-grid">
            <span class="run-detail-label">Flow</span>
            <span class="run-detail-val" data-run-flow></span>
            <span class="run-detail-label">Range</span>
            <span class="run-detail-val" data-run-range></span>
            <span class="run-detail-label">Duration</span>
            <span class="run-detail-val" data-run-duration></span>
            <span class="run-detail-label" data-run-dev-label style="display:none">vs. baseline</span>
            <span class="run-detail-val" data-run-deviation style="display:none"></span>
          </div>
          <div class="run-panel-btns">
            <button class="run-btn run-discard" data-discard-btn>Discard run</button>
            <button class="run-btn run-before" data-before-btn>Discard earlier runs</button>
            <button class="run-btn run-cancel" data-cancel-btn>Cancel</button>
          </div>
        </div>
        <div class="scard-body">
          <div class="row">
            <span class="row-label">Flow Monitoring</span>
            <label class="toggle">
              <input type="checkbox" data-monitor-toggle />
              <span class="slider"></span>
            </label>
          </div>
          <div class="row" data-enabled-only>
            <span class="row-label">Baseline</span>
            <span class="baseline-val" data-baseline>—</span>
            <span style="font-size:0.78em;color:var(--secondary-text-color)">GPM</span>
          </div>
          <div class="row" data-enabled-only>
            <span class="row-label">Fill Time</span>
            <input class="num-input" type="number" min="0" max="300" step="10"
                   data-fill-input title="Seconds to ignore at start of run" />
            <span style="font-size:0.78em;color:var(--secondary-text-color)">s</span>
          </div>
          <div class="row" style="justify-content:flex-end" data-enabled-only>
            <button class="reset-btn" data-reset-btn>Reset Profile</button>
          </div>
        </div>`;

      // Monitor toggle
      el.querySelector('[data-monitor-toggle]').addEventListener('change', e => {
        this._hass.callService('homeassistant', e.target.checked ? 'turn_on' : 'turn_off', {
          entity_id: `switch.dragontree_irrigation_${sid}_flow_monitoring`,
        });
      });

      // Fill time input
      const fillInput = el.querySelector('[data-fill-input]');
      fillInput.addEventListener('focus',   () => { this._editingFill = true; });
      fillInput.addEventListener('keydown', e => {
        if (e.key === 'Enter')  fillInput.blur();
        if (e.key === 'Escape') {
          const s = this._hass?.states[`number.dragontree_irrigation_${sid}_flow_fill_time`];
          if (s) fillInput.value = s.state;
          fillInput.blur();
        }
      });
      fillInput.addEventListener('blur', () => {
        this._editingFill = false;
        const val = parseInt(fillInput.value, 10);
        if (!isNaN(val)) {
          this._hass.callService('number', 'set_value', {
            entity_id: `number.dragontree_irrigation_${sid}_flow_fill_time`,
            value: val,
          });
        }
      });

      // Reset button
      el.querySelector('[data-reset-btn]').addEventListener('click', () => {
        this._hass.callService('button', 'press', {
          entity_id: `button.dragontree_irrigation_${sid}_reset_flow_profile`,
        });
      });

      // Chart bar click — event delegation so it survives innerHTML rebuilds
      el.querySelector('[data-chart]').addEventListener('click', e => {
        const bar = e.target.closest('[data-run-idx]');
        if (!bar) return;
        const idx = parseInt(bar.dataset.runIdx, 10);
        this._showRunPanel(el, sid, idx);
      });

      // Run panel buttons
      const runPanel = el.querySelector('[data-run-panel]');
      el.querySelector('[data-discard-btn]').addEventListener('click', () => {
        const { runId, stationId } = runPanel.dataset;
        if (runId && stationId) {
          this._hass.callService(DOMAIN, 'discard_flow_run', { station_id: stationId, run_id: runId });
        }
        this._hideRunPanel(el);
      });
      el.querySelector('[data-before-btn]').addEventListener('click', () => {
        const { runId, stationId } = runPanel.dataset;
        if (runId && stationId) {
          this._hass.callService(DOMAIN, 'discard_flow_runs_before', { station_id: stationId, run_id: runId });
        }
        this._hideRunPanel(el);
      });
      el.querySelector('[data-cancel-btn]').addEventListener('click', () => {
        this._hideRunPanel(el);
      });

      return el;
    }

    // -----------------------------------------------------------------------
    // State updates — patches live values without rebuilding DOM
    // -----------------------------------------------------------------------
    _updateStates(hass, stations, flowCfg) {
      // ── global config inputs ──
      const sensorSelect = this.shadowRoot.getElementById('cfgSensor');
      if (sensorSelect) {
        // Clear the dirty flag once HA confirms the value the user saved
        if (this._sensorDirty && (flowCfg.flow_sensor_entity || '') === sensorSelect.value) {
          this._sensorDirty = false;
        }
        this._populateSensorOptions(
          hass,
          sensorSelect,
          this._sensorDirty ? sensorSelect.value : flowCfg.flow_sensor_entity
        );
      }
      const shadowActive = this.shadowRoot.activeElement;
      const thresholdEl = this.shadowRoot.getElementById('cfgThreshold');
      if (thresholdEl && shadowActive !== thresholdEl) {
        thresholdEl.value = Math.round((flowCfg.flow_alert_threshold ?? 0.25) * 100);
      }
      const minRunsEl = this.shadowRoot.getElementById('cfgMinRuns');
      if (minRunsEl && shadowActive !== minRunsEl) {
        minRunsEl.value = flowCfg.flow_min_runs ?? 5;
      }
      const intervalEl = this.shadowRoot.getElementById('cfgInterval');
      if (intervalEl && shadowActive !== intervalEl) {
        intervalEl.value = flowCfg.flow_sample_interval ?? 10;
      }
      // First load: snapshot form state so the button starts disabled.
      // Subsequent loads after HA confirms a save: re-snapshot so the button
      // disables again even if the user had made further edits in the meantime.
      if (!this._savedFormState) {
        this._commitFormState();
      }
      this._updateSaveButton();

      // ── per-station cards ──
      for (const station of stations) {
        const sid  = station.id;
        const card = this.shadowRoot.querySelector(`.scard[data-sid="${sid}"]`);
        if (!card) continue;

        const monitorState = hass.states[`switch.dragontree_irrigation_${sid}_flow_monitoring`];
        const statusState  = hass.states[`sensor.dragontree_irrigation_${sid}_flow_status`];
        const baselineState= hass.states[`sensor.dragontree_irrigation_${sid}_flow_baseline`];
        const anomalyState = hass.states[`binary_sensor.dragontree_irrigation_${sid}_flow_anomaly`];
        const fillState    = hass.states[`number.dragontree_irrigation_${sid}_flow_fill_time`];

        const isMonitoring = monitorState?.state === 'on';
        const status       = statusState?.state || 'unknown';
        const runCount     = statusState?.attributes?.run_count ?? 0;
        const minRuns      = statusState?.attributes?.min_runs ?? flowCfg.flow_min_runs ?? 5;
        const alertThr     = statusState?.attributes?.alert_threshold ?? flowCfg.flow_alert_threshold ?? 0.25;
        const recentRuns   = statusState?.attributes?.recent_runs || [];
        const runDetails   = statusState?.attributes?.recent_run_details || [];
        const baseline     = baselineState?.state && baselineState.state !== 'unavailable'
                             ? parseFloat(baselineState.state) : null;
        const isAnomaly    = anomalyState?.state === 'on';

        card._runDetails = runDetails;

        // Card-level border
        card.classList.toggle('anomaly', isAnomaly);
        card.classList.toggle('disabled', !isMonitoring);

        // Status badge
        const badge     = card.querySelector('[data-badge]');
        const badgeText = card.querySelector('[data-badge-text]');
        badge.className = `status-badge status-${status}`;
        badgeText.textContent = isAnomaly ? `⚠ ${status}` : status;

        // Learning progress bar
        const progressWrap = card.querySelector('[data-progress]');
        const progressLabel= card.querySelector('[data-progress-label]');
        const progressFill = card.querySelector('[data-progress-fill]');
        const isLearning   = isMonitoring && (status === 'learning' || status === 'monitoring');
        progressWrap.style.display = isLearning ? '' : 'none';
        if (isLearning) {
          const pct = Math.min(100, (runCount / minRuns) * 100);
          progressFill.style.width = pct + '%';
          progressLabel.textContent = `Learning: ${runCount} of ${minRuns} runs`;
        }

        // Monitor toggle
        const toggle = card.querySelector('[data-monitor-toggle]');
        toggle.checked = isMonitoring;

        // Baseline
        const baselineEl = card.querySelector('[data-baseline]');
        baselineEl.textContent = baseline !== null ? baseline.toFixed(3) : '—';

        // Fill time (skip if currently editing)
        const fillInput = card.querySelector('[data-fill-input]');
        if (fillInput && !this._editingFill) {
          fillInput.value = fillState?.state != null ? parseInt(fillState.state, 10) : 120;
        }

        // Show/hide enabled-only rows
        card.querySelectorAll('[data-enabled-only]').forEach(r => {
          r.style.display = isMonitoring ? '' : 'none';
        });

        // Bar chart
        const chartWrap = card.querySelector('[data-chart]');
        chartWrap.innerHTML = isMonitoring
          ? this._renderChart(recentRuns, baseline, alertThr, runDetails)
          : '';
        // Re-apply bar highlight if the action panel is open
        if (card._selectedRunIdx != null) {
          this._applyBarHighlight(card, card._selectedRunIdx);
        }
      }
    }

    // -----------------------------------------------------------------------
    // Sensor selector — finds flow-rate sensors in hass.states
    // -----------------------------------------------------------------------
    _populateSensorOptions(hass, select, currentValue) {
      const candidates = Object.values(hass.states)
        .filter(s => {
          const id   = s.entity_id;
          const attr = s.attributes;
          if (!id.startsWith('sensor.')) return false;
          const uom  = (attr?.unit_of_measurement || '').toLowerCase();
          if (uom.includes('gal') || uom.includes('l/min') || uom.includes('m³/h')) return true;
          const name = (attr?.friendly_name || id).toLowerCase();
          return name.includes('flow') || name.includes('droplet');
        })
        .sort((a, b) => a.entity_id.localeCompare(b.entity_id));

      const existing = new Set(
        Array.from(select.options).map(o => o.value).filter(v => v)
      );
      const target = new Set(candidates.map(s => s.entity_id));

      for (const s of candidates) {
        if (!existing.has(s.entity_id)) {
          const opt       = document.createElement('option');
          opt.value       = s.entity_id;
          opt.textContent = s.attributes?.friendly_name || s.entity_id;
          select.appendChild(opt);
        }
      }
      for (const opt of Array.from(select.options)) {
        if (opt.value && !target.has(opt.value)) opt.remove();
      }

      if (select !== document.activeElement) {
        select.value = currentValue || '';
      }
    }

    // -----------------------------------------------------------------------
    // SVG bar chart
    // -----------------------------------------------------------------------
    _renderChart(recentRuns, baseline, alertThreshold, runDetails) {
      if (!recentRuns || recentRuns.length === 0) {
        return '<div class="no-runs">No completed runs yet</div>';
      }

      const W = 220, H = 80, PX = 8, PY_TOP = 14, PY_BOT = 6;
      const allVals = baseline !== null ? [...recentRuns, baseline] : [...recentRuns];
      const rawMax  = Math.max(...allVals);
      const minV    = 0;
      const maxV    = rawMax * 1.15;
      const range   = maxV;

      const n       = recentRuns.length;
      const barW    = Math.floor((W - PX * 2 - (n - 1) * 2) / n);
      const startX  = PX + Math.floor(((W - PX * 2) - (n * barW + (n - 1) * 2)) / 2);
      const chartH  = H - PY_TOP - PY_BOT;

      const toY = v => PY_TOP + chartH * (1 - (v - minV) / range);

      const bars = recentRuns.map((v, i) => {
        const x      = startX + i * (barW + 2);
        const y      = toY(v);
        const bH     = H - PY_BOT - y;
        const dev    = baseline !== null ? Math.abs(v - baseline) / baseline : 0;
        const color  = baseline === null ? '#90a4ae'
                     : dev > alertThreshold ? '#ef5350'
                     : '#42a5f5';
        const cx     = x + barW / 2;
        const labelY = Math.max(PY_TOP - 2, y - 2);

        const detail  = runDetails && runDetails[i];
        const tipDate = detail
          ? new Date(detail.start_time).toLocaleString(undefined, {
              month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
            })
          : '';
        const tip = tipDate ? `${tipDate} · ${v.toFixed(3)} GPM` : `${v.toFixed(3)} GPM`;

        const valueLabel = n <= 8
          ? `<text x="${cx.toFixed(1)}" y="${labelY.toFixed(1)}" font-size="8" fill="${color}" font-family="monospace" text-anchor="middle">${v.toFixed(2)}</text>`
          : '';

        return `<rect data-run-idx="${i}" x="${x}" y="${y.toFixed(1)}" width="${barW}" height="${Math.max(1, bH).toFixed(1)}" rx="2" fill="${color}" opacity="0.85"><title>${this._esc(tip)}</title></rect>${valueLabel}`;
      }).join('');

      const baseLine = baseline !== null
        ? (() => {
            const by = toY(baseline);
            return `<line x1="${PX}" y1="${by.toFixed(1)}" x2="${W - PX}" y2="${by.toFixed(1)}"
                      stroke="var(--primary-color,#03a9f4)" stroke-width="1.5" stroke-dasharray="4,3" opacity="0.7"/>`;
          })()
        : '';

      return `<svg width="${W}" height="${H}" style="display:block;margin:0 auto" viewBox="0 0 ${W} ${H}">
        ${bars}${baseLine}
      </svg>`;
    }

    // -----------------------------------------------------------------------
    // Run action panel
    // -----------------------------------------------------------------------
    _showRunPanel(card, sid, idx) {
      const details = card._runDetails || [];
      const run     = details[idx];
      if (!run) return;

      const panel = card.querySelector('[data-run-panel]');

      // Date header
      card.querySelector('[data-run-date]').textContent =
        new Date(run.start_time).toLocaleString(undefined, {
          weekday: 'short', month: 'short', day: 'numeric',
          hour: '2-digit', minute: '2-digit',
        });

      // Flow
      card.querySelector('[data-run-flow]').textContent = `${run.median.toFixed(3)} GPM`;

      // Range (Q1–Q3)
      const rangeEl = card.querySelector('[data-run-range]');
      rangeEl.textContent = (run.q1 != null && run.q3 != null)
        ? `${run.q1.toFixed(3)} – ${run.q3.toFixed(3)} GPM`
        : `${run.median.toFixed(3)} GPM`;

      // Duration
      const durEl = card.querySelector('[data-run-duration]');
      if (run.end_time) {
        const secs = Math.round((new Date(run.end_time) - new Date(run.start_time)) / 1000);
        const m = Math.floor(secs / 60), s = secs % 60;
        durEl.textContent = m > 0 ? `${m} min ${s} sec` : `${s} sec`;
      } else {
        durEl.textContent = '—';
      }

      // Deviation from baseline (only when available)
      const devLabel = card.querySelector('[data-run-dev-label]');
      const devEl    = card.querySelector('[data-run-deviation]');
      if (run.anomaly_score != null && run.baseline_median != null) {
        const pct = (run.anomaly_score * 100).toFixed(1);
        const high = run.median > run.baseline_median;
        devEl.textContent  = `${high ? '↑' : '↓'} ${pct}% ${high ? 'above' : 'below'} baseline`;
        devEl.className    = `run-detail-val ${high ? 'run-deviation-high' : 'run-deviation-low'}`;
        devLabel.style.display = '';
        devEl.style.display    = '';
      } else {
        devLabel.style.display = 'none';
        devEl.style.display    = 'none';
      }

      // Hide "Discard earlier runs" when this is the oldest non-discarded run
      card.querySelector('[data-before-btn]').style.display = idx === 0 ? 'none' : '';

      card._selectedRunIdx = idx;
      this._applyBarHighlight(card, idx);

      panel.dataset.runId     = run.run_id;
      panel.dataset.stationId = sid;
      panel.style.display     = '';
    }

    _hideRunPanel(card) {
      const panel = card.querySelector('[data-run-panel]');
      if (panel) panel.style.display = 'none';
      card._selectedRunIdx = null;
      this._clearBarHighlight(card);
    }

    _applyBarHighlight(card, idx) {
      card.querySelector('[data-chart]')
        ?.querySelectorAll('[data-run-idx]')
        .forEach(r => {
          const selected = parseInt(r.dataset.runIdx, 10) === idx;
          r.setAttribute('opacity', selected ? '1' : '0.35');
          if (selected) {
            r.setAttribute('stroke', 'rgba(255,255,255,0.85)');
            r.setAttribute('stroke-width', '1.5');
          } else {
            r.removeAttribute('stroke');
            r.removeAttribute('stroke-width');
          }
        });
    }

    _clearBarHighlight(card) {
      card.querySelector('[data-chart]')
        ?.querySelectorAll('[data-run-idx]')
        .forEach(r => {
          r.setAttribute('opacity', '0.85');
          r.removeAttribute('stroke');
          r.removeAttribute('stroke-width');
        });
    }

    _esc(str) {
      return String(str || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    }
  }

  customElements.define('dragontree-irrigation-flow-monitor', DragontreeFlowMonitor);

  // =========================================================================
  // dragontree-irrigation-moisture-status  (Moisture Sensors view)
  // =========================================================================

  const MOISTURE_STYLES = `
    :host { display: block; width: fit-content; }
    .card {
      background: var(--ha-card-background, var(--card-background-color, white));
      border-radius: var(--ha-card-border-radius, 12px);
      box-shadow: var(--ha-card-box-shadow, none);
      border: 1px solid var(--ha-card-border-color, var(--divider-color, #e0e0e0));
      overflow: hidden;
      min-width: 320px;
    }
    .card-header {
      padding: 16px 16px 8px;
      font-size: 1.5em; font-weight: 500;
      color: var(--ha-card-header-color, var(--primary-text-color));
    }
    .card-content { padding: 0; }
    .sensor-row {
      display: flex; align-items: center;
      padding: 10px 16px; gap: 16px; cursor: pointer;
      border-top: 1px solid var(--divider-color, #e0e0e0);
      transition: background 0.1s;
    }
    .sensor-row:hover { background: var(--secondary-background-color, rgba(0,0,0,0.04)); }
    .sensor-info { flex: 1; min-width: 0; }
    .sensor-name {
      font-size: 0.95em; color: var(--primary-text-color);
      white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    }
    .sensor-stations { font-size: 0.78em; color: var(--secondary-text-color); margin-top: 2px; }
    .sensor-value {
      font-size: 1.3em; font-weight: 500;
      color: var(--primary-text-color); white-space: nowrap; text-align: right;
    }
    .sensor-unit { font-size: 0.65em; color: var(--secondary-text-color); margin-left: 2px; }
    .empty {
      padding: 32px 16px; text-align: center;
      color: var(--secondary-text-color); font-style: italic;
    }
  `;

  class DragontreeMoistureStatus extends HTMLElement {

    setConfig(config) {
      this._config  = config || {};
      this._lastKey = null;

      if (!this.shadowRoot) {
        this.attachShadow({ mode: 'open' });
        this.shadowRoot.innerHTML = `
          <style>${MOISTURE_STYLES}</style>
          <div class="card">
            <div class="card-header">Moisture Sensors</div>
            <div class="card-content" id="content"></div>
          </div>`;
      }
    }

    getCardSize() { return Math.max(2, (this._sensorCount || 1) + 1); }

    set hass(hass) {
      this._hass = hass;
      if (!this.shadowRoot) return;

      const stateObj = hass.states[SENSOR];
      const stations = stateObj?.attributes?.stations || [];

      // Collect unique sensors across all stations (tracked or not)
      const seen    = new Set();
      const sensors = [];
      for (const s of stations) {
        if (s.moisture_sensor && !seen.has(s.moisture_sensor)) {
          seen.add(s.moisture_sensor);
          const assocNames = stations
            .filter(t => t.moisture_sensor === s.moisture_sensor)
            .map(t => t.friendly_name || t.base_name);
          sensors.push({ entityId: s.moisture_sensor, stations: assocNames });
        }
      }
      this._sensorCount = sensors.length;

      const key = sensors.map(s => {
        const st = hass.states[s.entityId];
        return `${s.entityId}:${st?.state || 'u'}`;
      }).join(',');
      if (key === this._lastKey) return;
      this._lastKey = key;

      const content = this.shadowRoot.getElementById('content');
      content.innerHTML = '';

      if (!sensors.length) {
        const div = document.createElement('div');
        div.className = 'empty';
        div.textContent = 'No moisture sensors configured on any station.';
        content.appendChild(div);
        return;
      }

      for (const sensor of sensors) {
        const st    = hass.states[sensor.entityId];
        const state = st?.state;
        const unit  = st?.attributes?.unit_of_measurement || '%';
        const name  = st?.attributes?.friendly_name || sensor.entityId;

        const row = document.createElement('div');
        row.className = 'sensor-row';
        row.innerHTML = `
          <div class="sensor-info">
            <div class="sensor-name">${this._esc(name)}</div>
            <div class="sensor-stations">${this._esc(sensor.stations.join(', '))}</div>
          </div>
          <div class="sensor-value">
            ${state != null && state !== 'unavailable' && state !== 'unknown'
              ? `${this._esc(state)}<span class="sensor-unit">${this._esc(unit)}</span>`
              : '<span style="font-size:0.7em;color:var(--secondary-text-color)">unavailable</span>'}
          </div>`;

        row.addEventListener('click', () => {
          this.dispatchEvent(new CustomEvent('hass-more-info', {
            bubbles: true, composed: true,
            detail: { entityId: sensor.entityId },
          }));
        });

        content.appendChild(row);
      }
    }

    _esc(s) {
      return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    }
  }

  customElements.define('dragontree-irrigation-moisture-status', DragontreeMoistureStatus);


  window.customCards = window.customCards || [];
  window.customCards.push(
    {
      type:        'dragontree-irrigation-station-manager',
      name:        'Dragontree Station Manager',
      description: 'Manage irrigation stations: reorder, rename, and toggle tracking.',
    },
    {
      type:        'dragontree-irrigation-station-configs',
      name:        'Dragontree Station Configs',
      description: 'Per-station status and config cards for all tracked stations.',
    },
    {
      type:        'dragontree-irrigation-station-schedules',
      name:        'Dragontree Station Schedules',
      description: 'Per-station schedule cards for all tracked stations.',
    },
    {
      type:        'dragontree-irrigation-schedule-calendar',
      name:        'Dragontree Schedule Calendar',
      description: 'Lookahead schedule table with per-station status styling.',
    },
    {
      type:        'dragontree-irrigation-station-control',
      name:        'Dragontree Station Control',
      description: 'Manually start and stop irrigation stations with per-station duration.',
    },
    {
      type:        'dragontree-irrigation-flow-monitor',
      name:        'Dragontree Flow Monitor',
      description: 'Per-station flow learning, baseline chart, anomaly status, and config.',
    },
    {
      type:        'dragontree-irrigation-moisture-status',
      name:        'Dragontree Moisture Status',
      description: 'All configured moisture sensors deduplicated; click a row to see history.',
    },
  );

})();
