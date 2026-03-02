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
      const key       = JSON.stringify(stations) + osStates.join(',');
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
          <div class="os-label"></div>
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
          ${this._panelHTML(sid, 'normal')}
          ${this._panelHTML(sid, 'hot')}
        </div>`;
      return el;
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
        if (el === active) return;
        const s = hass.states[el.dataset.entity];
        if (s) el.value = s.state;
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
  );

})();
