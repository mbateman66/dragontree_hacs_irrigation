"""Coordinator for Dragontree Irrigation."""
from __future__ import annotations

import asyncio
import logging
import re
from copy import deepcopy
from datetime import date, datetime, time, timedelta
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_change
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    DAYS_OF_WEEK,
    DEFAULT_AM_START_TIME,
    DEFAULT_FLOW_ALERT_THRESHOLD,
    DEFAULT_FLOW_FILL_TIME,
    DEFAULT_FLOW_MIN_RUNS,
    DEFAULT_FLOW_SAMPLE_INTERVAL,
    DEFAULT_LOOKAHEAD_DAYS,
    DEFAULT_PM_START_TIME,
    DEFAULT_RAIN_MODE,
    DEFAULT_STATION_MANUAL_DURATION,
    DEFAULT_WEEK_INTERVAL,
    DOMAIN,
    OPENSPRINKLER_DOMAIN,
    OS_SERVICE_RUN_STATION,
    OS_SERVICE_STOP,
    QUEUE_AM,
    QUEUE_PM,
    RAIN_MODE_HEAVY,
    RAIN_MODE_LIGHT,
    SCHEDULE_MODE_HOT,
    SCHEDULE_MODE_NORMAL,
    SCHEDULE_MODE_OFF,
    SIGNAL_STATIONS_UPDATED,
    STATUS_CANCELLED,
    STATUS_COMPLETE,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SCHEDULED,
    STORAGE_KEY,
    STORAGE_VERSION,
)
from .flow_database import FlowDatabase
from .flow_monitor import FlowMonitor
from .os_lookup import find_os_station_entity

_LOGGER = logging.getLogger(__name__)

# Shared device info for all irrigation entities.
# With _attr_has_entity_name = True, HA prefixes entity IDs with the device name slug.
# "Dragontree Irrigation" → "dragontree_irrigation_" prefix, e.g.:
#   switch.dragontree_irrigation_master_enable
#   select.dragontree_irrigation_rain_mode
CONTROLLER_DEVICE_INFO = DeviceInfo(
    identifiers={(DOMAIN, "controller")},
    name="Dragontree Irrigation",
    manufacturer="Dragontree",
    entry_type=DeviceEntryType.SERVICE,
)

DEFAULT_GLOBAL = {
    "master_enable": False,
    "rain_mode": DEFAULT_RAIN_MODE,
    "start_time_am": DEFAULT_AM_START_TIME,
    "start_time_pm": DEFAULT_PM_START_TIME,
    "lookahead_days": DEFAULT_LOOKAHEAD_DAYS,
    # Flow monitoring (Droplet sensor integration)
    "flow_sensor_entity": None,
    "flow_alert_threshold": DEFAULT_FLOW_ALERT_THRESHOLD,
    "flow_min_runs": DEFAULT_FLOW_MIN_RUNS,
    "flow_sample_interval": DEFAULT_FLOW_SAMPLE_INTERVAL,
}

DEFAULT_SCHEDULE = {
    "am": True,
    "pm": False,
    "days_of_week": [],
    "week_interval": DEFAULT_WEEK_INTERVAL,
    "duration": 600,
}

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


class IrrigationCoordinator(DataUpdateCoordinator):
    """Manages all irrigation state, scheduling, and queue execution."""

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN)
        self._store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._global: dict = deepcopy(DEFAULT_GLOBAL)
        self._stations: list[dict] = []
        self._runtime: dict = {
            "running_queue": None,  # "am" | "pm" | None
            "current_station_id": None,
        }
        self._day_schedules: list[dict] = []
        self._time_unsubs: list = []
        self._os_unsubs: list = []
        self._running_unsubs: list = []
        self._moisture_unsubs: list = []
        self._health_unsubs: list = []
        self._queue_task: asyncio.Task | None = None
        self._manual_stop_requested: bool = False
        self._manual_station_id: str | None = None  # station started via async_run_station_manual

        db_path = hass.config.path(".storage", "dragontree_flow.db")
        self._flow_db = FlowDatabase(db_path)
        self._flow_monitor = FlowMonitor(hass, self._flow_db, self)

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    async def async_initialize(self) -> None:
        """Load persisted data, merge-discover OS stations, setup triggers."""
        self._flow_db.initialize()

        stored = await self._store.async_load()
        if stored:
            self._global = stored.get("global", deepcopy(DEFAULT_GLOBAL))
            # Migration guard: add flow global keys added in this version
            self._global.setdefault("flow_sensor_entity", None)
            self._global.setdefault("flow_alert_threshold", DEFAULT_FLOW_ALERT_THRESHOLD)
            self._global.setdefault("flow_min_runs", DEFAULT_FLOW_MIN_RUNS)
            self._global.setdefault("flow_sample_interval", DEFAULT_FLOW_SAMPLE_INTERVAL)

            self._stations = stored.get("stations", [])
            # Migration guards for fields added in later versions
            for s in self._stations:
                s.setdefault("normal_schedule", deepcopy(DEFAULT_SCHEDULE))
                s.setdefault("hot_schedule", deepcopy(DEFAULT_SCHEDULE))
                s.setdefault("sensitive", False)
                s.setdefault("last_run", None)
                # Migrate old "ignored" field to "tracked" (inverted semantics)
                if "ignored" in s and "tracked" not in s:
                    s["tracked"] = not s.pop("ignored")
                else:
                    s.pop("ignored", None)
                s.setdefault("tracked", True)
                s.setdefault("moisture_sensor", None)
                s.setdefault("moisture_max", None)
                # Flow monitoring fields
                s.setdefault("flow_monitoring", False)
                s.setdefault("flow_fill_time", DEFAULT_FLOW_FILL_TIME)
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
            # Persist backfilled values
            await self._save()

        # Always merge-discover: add any OS stations not yet tracked.
        # On first run (no stored data) this populates _stations from scratch.
        await self._merge_discover_stations()

        if not stored:
            # Save global defaults on first run (stations already saved in merge-discover)
            await self._save()

        self._regenerate_schedules()
        self._setup_time_triggers()
        self._setup_os_listeners()
        self._setup_running_listeners()
        self._setup_moisture_listeners()
        self._setup_health_listeners()
        self._flow_monitor.setup(self._stations)

        # Load persisted flow state for all monitored stations
        for s in self._stations:
            if s.get("flow_monitoring"):
                self.hass.async_create_task(
                    self._flow_monitor.async_load_station_state(s["id"])
                )

        @callback
        def _schedule_recovery(_hass: HomeAssistant) -> None:
            self.hass.async_create_task(self._retry_merge_discover_stations())
            self.hass.async_create_task(self._recover_running_station())
            self.hass.async_create_task(self._check_entity_health())

        async_at_started(self.hass, _schedule_recovery)

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

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    async def _save(self) -> None:
        await self._store.async_save(
            {
                "global": self._global,
                "stations": self._stations,
            }
        )

    # ------------------------------------------------------------------
    # Scheduling
    # ------------------------------------------------------------------

    def _setup_time_triggers(self) -> None:
        for unsub in self._time_unsubs:
            unsub()
        self._time_unsubs.clear()

        am_h, am_m = _parse_time(self._global.get("start_time_am", DEFAULT_AM_START_TIME))
        pm_h, pm_m = _parse_time(self._global.get("start_time_pm", DEFAULT_PM_START_TIME))

        self._time_unsubs.extend(
            [
                async_track_time_change(
                    self.hass, self._handle_am_trigger, hour=am_h, minute=am_m, second=0
                ),
                async_track_time_change(
                    self.hass, self._handle_pm_trigger, hour=pm_h, minute=pm_m, second=0
                ),
                async_track_time_change(
                    self.hass, self._handle_midnight, hour=0, minute=0, second=0
                ),
            ]
        )

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

    def _setup_moisture_listeners(self) -> None:
        """Watch associated moisture sensors so schedule updates live as moisture changes."""
        for unsub in self._moisture_unsubs:
            unsub()
        self._moisture_unsubs.clear()

        entity_ids = [
            s["moisture_sensor"]
            for s in self._stations
            if s.get("moisture_sensor")
        ]
        if not entity_ids:
            return

        @callback
        def _moisture_changed(_event: Any) -> None:
            self._regenerate_schedules()
            self.async_set_updated_data(self._build_data())

        self._moisture_unsubs.append(
            async_track_state_change_event(self.hass, entity_ids, _moisture_changed)
        )

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

    @callback
    def _handle_am_trigger(self, _now: datetime) -> None:
        if not self._global.get("master_enable"):
            return
        self.hass.async_create_task(self._start_queue(QUEUE_AM))

    @callback
    def _handle_pm_trigger(self, _now: datetime) -> None:
        if not self._global.get("master_enable"):
            return
        self.hass.async_create_task(self._start_queue(QUEUE_PM))

    @callback
    def _handle_midnight(self, _now: datetime) -> None:
        self._regenerate_schedules()
        self.async_set_updated_data(self._build_data())

    def _regenerate_schedules(self) -> None:
        """Build or rebuild the lookahead schedule.

        For today's queues, any queue that has already been started (at least one
        station with a non-scheduled status) is preserved unchanged.  This covers
        both a completed AM queue and a queue that is currently mid-run, so that
        regenerations triggered by settings changes or OS state updates mid-day
        never overwrite execution history.
        """
        lookahead = int(self._global.get("lookahead_days", DEFAULT_LOOKAHEAD_DAYS))
        today = date.today()
        today_str = today.isoformat()
        rain_mode = self._global.get("rain_mode", DEFAULT_RAIN_MODE)
        am_start = self._global.get("start_time_am", DEFAULT_AM_START_TIME)
        pm_start = self._global.get("start_time_pm", DEFAULT_PM_START_TIME)

        # Snapshot today's existing queues once so we can selectively restore them.
        existing_today: dict | None = next(
            (d for d in self._day_schedules if d["date"] == today_str), None
        )
        running_queue = self._runtime.get("running_queue")

        new_schedules: list[dict] = []
        for offset in range(lookahead):
            target = today + timedelta(days=offset)
            dow = target.weekday()  # 0=Mon

            am_entry = self._build_queue(target, dow, QUEUE_AM, rain_mode, am_start)
            pm_entry = self._build_queue(target, dow, QUEUE_PM, rain_mode, pm_start)

            # Overrun detection: AM queue ends after PM queue is scheduled to start.
            # Guard against the case where AM start >= PM start (inverted config),
            # which would produce a spurious overrun on every day.
            if am_entry["stations"] and pm_entry["stations"]:
                am_st_h, am_st_m = _parse_time(am_start)
                am_end_h, am_end_m = _parse_time(am_entry["end_time"])
                pm_st_h, pm_st_m = _parse_time(pm_start)
                am_st_mins = am_st_h * 60 + am_st_m
                am_end_mins = am_end_h * 60 + am_end_m
                pm_st_mins = pm_st_h * 60 + pm_st_m
                if am_st_mins < pm_st_mins and am_end_mins > pm_st_mins:
                    am_entry["overrun"] = True

            day_entry: dict = {
                "date": target.isoformat(),
                "day_of_week": DAYS_OF_WEEK[dow],
                "queues": {QUEUE_AM: am_entry, QUEUE_PM: pm_entry},
            }

            if offset == 0 and existing_today:
                for q_name in (QUEUE_AM, QUEUE_PM):
                    existing_q = existing_today["queues"].get(q_name, {})
                    # Preserve if currently running (safety net) OR if any station
                    # has already been touched (running/complete/cancelled).
                    is_running = q_name == running_queue
                    has_started = any(
                        s.get("status", STATUS_SCHEDULED) != STATUS_SCHEDULED
                        for s in existing_q.get("stations", [])
                    )
                    if is_running or has_started:
                        day_entry["queues"][q_name] = existing_q

            new_schedules.append(day_entry)

        self._day_schedules = new_schedules

    def _build_queue(
        self,
        target: date,
        dow: int,
        queue_name: str,
        rain_mode: str,
        start_time: str,
    ) -> dict:
        stations_out: list[dict] = []

        if not self._global.get("master_enable"):
            return {
                "name": queue_name.upper(),
                "start_time": start_time,
                "end_time": start_time,
                "overrun": False,
                "stations": [],
            }

        for station in self._stations:
            # Skip if the OpenSprinkler station is explicitly disabled.
            # Treat None/unavailable/unknown as enabled so queues aren't
            # emptied when OpenSprinkler hasn't loaded yet (e.g. on restart).
            os_switch = self.hass.states.get(
                f"switch.{station['base_name']}_station_enabled"
            )
            if os_switch is not None and os_switch.state == "off":
                continue

            if not station.get("tracked", True):
                continue

            mode = station.get("schedule_mode", SCHEDULE_MODE_NORMAL)
            if mode == SCHEDULE_MODE_OFF:
                continue

            # Moisture sensor override: skip station if sensor reading > threshold
            moisture_sensor = station.get("moisture_sensor")
            moisture_max = station.get("moisture_max")
            if moisture_sensor and moisture_max is not None:
                sensor_state = self.hass.states.get(moisture_sensor)
                if sensor_state is not None:
                    try:
                        if float(sensor_state.state) > float(moisture_max):
                            continue
                    except (ValueError, TypeError):
                        pass

            schedule = (
                station.get("hot_schedule") or deepcopy(DEFAULT_SCHEDULE)
                if mode == SCHEDULE_MODE_HOT
                else station.get("normal_schedule") or deepcopy(DEFAULT_SCHEDULE)
            )

            # AM/PM membership
            if queue_name == QUEUE_AM and not schedule.get("am", True):
                continue
            if queue_name == QUEUE_PM and not schedule.get("pm", False):
                continue

            # Day of week
            if dow not in schedule.get("days_of_week", []):
                continue

            # Rain mode / sensitivity filter
            sensitive = station.get("sensitive", False)
            if rain_mode == RAIN_MODE_HEAVY:
                continue
            if rain_mode == RAIN_MODE_LIGHT and not sensitive:
                continue

            # Week interval
            week_interval = int(schedule.get("week_interval", DEFAULT_WEEK_INTERVAL))
            last_run = station.get("last_run")
            if week_interval > 1 and last_run:
                try:
                    lr_date = date.fromisoformat(last_run)
                    if (target - lr_date).days < week_interval * 7:
                        continue
                except ValueError:
                    pass

            duration = int(schedule.get("duration", 600))
            stations_out.append(
                {
                    "station_id": station["id"],
                    "friendly_name": station.get("friendly_name", station["id"]),
                    "status": STATUS_SCHEDULED,
                    "duration": duration,
                    "time_remaining": None,
                }
            )

        # Calculate end time
        h, m = _parse_time(start_time)
        total_secs = sum(s["duration"] for s in stations_out)
        end_dt = datetime.combine(target, time(h, m)) + timedelta(seconds=total_secs)

        return {
            "name": queue_name.upper(),
            "start_time": start_time,
            "end_time": end_dt.strftime("%H:%M"),
            "overrun": False,
            "stations": stations_out,
        }

    # ------------------------------------------------------------------
    # Queue execution
    # ------------------------------------------------------------------

    async def _recover_running_station(self) -> None:
        """Detect a station that was running when HA restarted and resume the queue.

        Called once after HA finishes starting so that all OpenSprinkler binary
        sensors have their current states populated.  If a station's binary sensor
        is already 'on', we mark prior stations in that queue as complete, set the
        running station's status to RUNNING, and resume the queue task from there.
        """
        if self._runtime.get("running_queue"):
            return
        if self._queue_task and not self._queue_task.done():
            return

        today_str = date.today().isoformat()
        today_sched = next((d for d in self._day_schedules if d["date"] == today_str), None)
        if not today_sched:
            return

        for queue_name in (QUEUE_AM, QUEUE_PM):
            queue = today_sched["queues"].get(queue_name, {})
            stations = queue.get("stations", [])

            for idx, station_entry in enumerate(stations):
                if station_entry["status"] in (STATUS_CANCELLED, STATUS_COMPLETE, STATUS_FAILED):
                    continue

                station = self._get_station(station_entry["station_id"])
                if not station:
                    continue

                bs_id = f"binary_sensor.{station['base_name']}_station_running"
                bs_state = self.hass.states.get(bs_id)
                if not bs_state or bs_state.state != "on":
                    continue

                _LOGGER.info(
                    "Dragontree Irrigation: resuming %s queue after restart — %s is still running",
                    queue_name,
                    station["base_name"],
                )

                # Mark all scheduled stations before this one as complete.
                for prior in stations[:idx]:
                    if prior["status"] == STATUS_SCHEDULED:
                        prior["status"] = STATUS_COMPLETE

                station_entry["status"] = STATUS_RUNNING
                self._runtime["running_queue"] = queue_name
                self._runtime["current_station_id"] = station["id"]
                self._recalculate_queue_end_time(queue_name)
                self.async_set_updated_data(self._build_data())

                if station.get("flow_monitoring"):
                    self._flow_monitor._start_monitoring(station["id"])

                self._queue_task = self.hass.async_create_task(
                    self._run_queue(queue_name, stations)
                )
                return

    async def _check_entity_health(self) -> None:
        """Create or dismiss a persistent notification for missing/unavailable OS entities."""
        _NOTIFICATION_ID = "dragontree_irrigation_entity_health"
        _REQUIRED = [
            ("switch",         "{base}_station_enabled"),
            ("binary_sensor",  "{base}_station_running"),
            ("sensor",         "{base}_station_status"),
        ]

        problems: list[str] = []
        for s in self._stations:
            base = s["base_name"]
            missing: list[str] = []
            for domain, tpl in _REQUIRED:
                entity_id = f"{domain}.{tpl.format(base=base)}"
                state = self.hass.states.get(entity_id)
                if state is None:
                    missing.append(f"`{entity_id}` — not found")
                elif state.state in ("unavailable", "unknown"):
                    missing.append(f"`{entity_id}` — {state.state}")
            if missing:
                name = s.get("friendly_name") or base
                problems.append(
                    f"**{name}** (`{base}`):\n" + "\n".join(f"- {m}" for m in missing)
                )

        if problems:
            message = (
                "The following stations have missing or unavailable "
                "OpenSprinkler entities:\n\n" + "\n\n".join(problems)
            )
            await self.hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "notification_id": _NOTIFICATION_ID,
                    "title": "Dragontree Irrigation: Missing Entities",
                    "message": message,
                },
            )
        else:
            await self.hass.services.async_call(
                "persistent_notification",
                "dismiss",
                {"notification_id": _NOTIFICATION_ID},
            )

    async def _start_queue(self, queue_name: str) -> None:
        if self._runtime.get("running_queue"):
            _LOGGER.warning("Queue already running: %s", self._runtime["running_queue"])
            return

        today_str = date.today().isoformat()
        today_sched = next((d for d in self._day_schedules if d["date"] == today_str), None)
        if not today_sched:
            return

        queue = today_sched["queues"].get(queue_name, {})
        stations = queue.get("stations", [])
        if not stations:
            _LOGGER.debug("No stations in %s queue today", queue_name)
            return

        self._runtime["running_queue"] = queue_name
        await self._save()
        self.async_set_updated_data(self._build_data())

        self._queue_task = self.hass.async_create_task(
            self._run_queue(queue_name, stations)
        )

    def _recalculate_queue_end_time(self, queue_name: str) -> None:
        """Update the queue end_time based on how long remaining stations will take."""
        today_str = date.today().isoformat()
        today_sched = next((d for d in self._day_schedules if d["date"] == today_str), None)
        if not today_sched:
            return
        queue = today_sched["queues"].get(queue_name)
        if not queue:
            return
        remaining_secs = sum(
            s["duration"]
            for s in queue.get("stations", [])
            if s["status"] == STATUS_SCHEDULED
        )
        end_dt = datetime.now() + timedelta(seconds=remaining_secs)
        queue["end_time"] = end_dt.strftime("%H:%M")

    async def _run_queue(self, queue_name: str, stations: list[dict]) -> None:
        try:
            for station_entry in stations:
                if station_entry["status"] in (STATUS_CANCELLED, STATUS_COMPLETE, STATUS_FAILED):
                    continue
                if not self._global.get("master_enable"):
                    break

                station = self._get_station(station_entry["station_id"])
                if not station:
                    station_entry["status"] = STATUS_CANCELLED
                    continue

                already_running = station_entry["status"] == STATUS_RUNNING
                station_entry["status"] = STATUS_RUNNING
                self._runtime["current_station_id"] = station["id"]
                self.async_set_updated_data(self._build_data())

                duration = station_entry["duration"]

                if not already_running:
                    entity_id = f"switch.{station['base_name']}_station_enabled"

                    # If OpenSprinkler entities are temporarily unavailable (e.g. brief
                    # network blip), wait for them to recover before issuing the start
                    # command — otherwise the service call is silently dropped and the
                    # station never runs.
                    entity_state = self.hass.states.get(entity_id)
                    if entity_state and entity_state.state in ("unavailable", "unknown"):
                        _LOGGER.warning(
                            "OpenSprinkler entity %s is unavailable; waiting up to 60 s for recovery before starting %s",
                            entity_id,
                            station["base_name"],
                        )
                        if not await self._wait_for_entity_available(entity_id, timeout=60.0):
                            _LOGGER.error(
                                "OpenSprinkler did not recover within 60 s; skipping station %s",
                                station["base_name"],
                            )
                            station_entry["status"] = STATUS_CANCELLED
                            self._runtime["current_station_id"] = None
                            continue

                    await self._stop_any_running_stations(except_station_id=station["id"])

                    try:
                        await self.hass.services.async_call(
                            OPENSPRINKLER_DOMAIN,
                            OS_SERVICE_RUN_STATION,
                            {"run_seconds": duration},
                            target={"entity_id": entity_id},
                            blocking=True,
                        )
                    except Exception as err:
                        _LOGGER.error("Failed to start %s: %s", station["base_name"], err)
                        station_entry["status"] = STATUS_CANCELLED
                        self._runtime["current_station_id"] = None
                        continue

                    # Start flow monitoring immediately after the run command is confirmed
                    # sent, so sampling begins even if the binary sensor is slow to update.
                    # If the binary sensor later fires its own on-transition, _start_monitoring
                    # will restart the task from the actual on-time (resetting the fill timer).
                    if station.get("flow_monitoring"):
                        self._flow_monitor._start_monitoring(station["id"])

                started = await self._wait_for_station(station["base_name"], duration + 60)

                if station_entry["status"] == STATUS_RUNNING:
                    if self._manual_stop_requested:
                        station_entry["status"] = STATUS_CANCELLED
                        self._manual_stop_requested = False
                    elif started:
                        station_entry["status"] = STATUS_COMPLETE
                        station["last_run"] = date.today().isoformat()
                    else:
                        _LOGGER.warning("Station %s never started; marking failed", station["base_name"])
                        station_entry["status"] = STATUS_FAILED
                        # If the binary sensor never confirmed the start, stop any
                        # proactive flow monitoring we started above.
                        if station.get("flow_monitoring"):
                            self._flow_monitor._stop_monitoring(station["id"])
                        # Safety: if the station is physically running in OS despite
                        # our timeout, stop it before the next station starts.
                        await self._stop_station_if_running(station)

                self._runtime["current_station_id"] = None
                self._recalculate_queue_end_time(queue_name)
                await self._save()
                self.async_set_updated_data(self._build_data())

                # Brief pause between stations (OS rate-limit workaround)
                await asyncio.sleep(2)

        except asyncio.CancelledError:
            _LOGGER.debug("Queue %s cancelled", queue_name)
        finally:
            self._runtime["running_queue"] = None
            self._runtime["current_station_id"] = None
            await self._save()
            self.async_set_updated_data(self._build_data())

    async def _stop_any_running_stations(self, except_station_id: str) -> None:
        """Stop every tracked station that is physically running in OS except the one about to start.

        Catches any station — not just the immediately preceding one — that may still be
        on due to a prior timeout, a manual trigger, or any other unexpected state.
        """
        for s in self._stations:
            if s["id"] == except_station_id:
                continue
            bs_id = f"binary_sensor.{s['base_name']}_station_running"
            bs_state = self.hass.states.get(bs_id)
            if not (bs_state and bs_state.state == "on"):
                continue
            _LOGGER.warning(
                "Station %s is physically running before starting %s — stopping it first",
                s["base_name"],
                except_station_id,
            )
            try:
                await self.hass.services.async_call(
                    OPENSPRINKLER_DOMAIN,
                    OS_SERVICE_STOP,
                    {},
                    target={"entity_id": f"switch.{s['base_name']}_station_enabled"},
                    blocking=True,
                )
            except Exception as err:
                _LOGGER.error("Failed to stop station %s: %s", s["base_name"], err)

    async def _stop_station_if_running(self, station: dict) -> None:
        """Stop a station in OpenSprinkler if its binary sensor still reports running.

        Called after a start-timeout failure to prevent two stations running at once:
        the station may have started in OS hardware after our 15-second window closed,
        so we check the binary sensor and issue a stop if it is physically on.
        """
        bs_id = f"binary_sensor.{station['base_name']}_station_running"
        bs_state = self.hass.states.get(bs_id)
        if not (bs_state and bs_state.state == "on"):
            return
        _LOGGER.warning(
            "Station %s is physically running despite being marked failed — stopping to prevent overlap with next station",
            station["base_name"],
        )
        try:
            await self.hass.services.async_call(
                OPENSPRINKLER_DOMAIN,
                OS_SERVICE_STOP,
                {},
                target={"entity_id": f"switch.{station['base_name']}_station_enabled"},
                blocking=True,
            )
        except Exception as err:
            _LOGGER.error("Failed to stop station %s: %s", station["base_name"], err)

    async def _wait_for_entity_available(self, entity_id: str, timeout: float = 60.0) -> bool:
        """Wait until an entity leaves unavailable/unknown state. Returns True if recovered."""
        available = asyncio.Event()

        @callback
        def _state_changed(event: Any) -> None:
            new_state = event.data.get("new_state")
            if new_state and new_state.state not in ("unavailable", "unknown"):
                available.set()

        unsub = async_track_state_change_event(self.hass, [entity_id], _state_changed)
        try:
            current = self.hass.states.get(entity_id)
            if current and current.state not in ("unavailable", "unknown"):
                return True
            try:
                await asyncio.wait_for(available.wait(), timeout=timeout)
                return True
            except asyncio.TimeoutError:
                return False
        finally:
            unsub()

    async def _wait_for_station(self, base_name: str, timeout_seconds: int) -> bool:
        """Wait for a station to start and then finish running.

        Sets up the state-change listener BEFORE sampling current state to
        avoid a race condition where the binary sensor turns on between the
        sample and the listener being registered.

        Returns True if the station started (binary sensor went on), regardless of
        whether it finished within timeout_seconds. Returns False if the station
        never started within the 15-second start timeout.
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

    # ------------------------------------------------------------------
    # Configuration helpers (called by entities and services)
    # ------------------------------------------------------------------

    async def async_update_global(self, updates: dict) -> None:
        self._global.update(updates)
        self._regenerate_schedules()
        if "start_time_am" in updates or "start_time_pm" in updates:
            self._setup_time_triggers()
        await self._save()
        self.async_set_updated_data(self._build_data())

    async def async_add_station(self, data: dict) -> None:
        station = _make_station(data.get("base_name", ""), data.get("friendly_name", ""))
        station.update(data)
        if not station.get("id"):
            station["id"] = station["base_name"]
        self._stations.append(station)
        self._regenerate_schedules()
        self._setup_os_listeners()
        self._setup_running_listeners()
        self._setup_moisture_listeners()
        self._setup_health_listeners()
        self._flow_monitor.setup(self._stations)
        await self._save()
        async_dispatcher_send(self.hass, SIGNAL_STATIONS_UPDATED)
        self.async_set_updated_data(self._build_data())
        await self._check_entity_health()

    async def async_update_station(self, station_id: str, data: dict) -> None:
        station = self._get_station(station_id)
        if not station:
            raise ValueError(f"Station '{station_id}' not found")
        station.update(data)
        self._regenerate_schedules()
        self._setup_moisture_listeners()
        # Re-register all entity listeners if base_name changed (station was renamed in OS/HA).
        if "base_name" in data:
            self._setup_os_listeners()
            self._setup_running_listeners()
            self._setup_health_listeners()
            self._flow_monitor.setup(self._stations)
            if station.get("flow_monitoring"):
                await self._flow_monitor.async_load_station_state(station["id"])
            await self._check_entity_health()
        elif "flow_monitoring" in data:
            self._flow_monitor.setup(self._stations)
            if data.get("flow_monitoring"):
                await self._flow_monitor.async_load_station_state(station_id)
        await self._save()
        self.async_set_updated_data(self._build_data())

    async def async_update_station_schedule(
        self, station_id: str, schedule_type: str, data: dict
    ) -> None:
        """Update normal_schedule or hot_schedule for a station."""
        station = self._get_station(station_id)
        if not station:
            raise ValueError(f"Station '{station_id}' not found")
        key = f"{schedule_type}_schedule"
        existing = station.get(key) or deepcopy(DEFAULT_SCHEDULE)
        existing.update(data)
        station[key] = existing
        self._regenerate_schedules()
        await self._save()
        self.async_set_updated_data(self._build_data())

    async def async_remove_station(self, station_id: str) -> None:
        self._stations = [s for s in self._stations if s["id"] != station_id]
        self._regenerate_schedules()
        self._setup_os_listeners()
        self._setup_running_listeners()
        self._setup_moisture_listeners()
        self._setup_health_listeners()
        self._flow_monitor.setup(self._stations)
        await self._save()
        async_dispatcher_send(self.hass, SIGNAL_STATIONS_UPDATED)
        self.async_set_updated_data(self._build_data())
        await self._check_entity_health()

    async def async_reset_flow_profile(self, station_id: str) -> None:
        await self._flow_monitor.async_reset_profile(station_id)

    async def async_discard_flow_run(self, station_id: str, run_id: str) -> None:
        await self._flow_monitor.async_discard_run(station_id, run_id)

    async def async_discard_flow_runs_before(self, station_id: str, run_id: str) -> None:
        await self._flow_monitor.async_discard_runs_before(station_id, run_id)

    async def async_update_flow_config(self, updates: dict) -> None:
        """Update global flow monitoring configuration."""
        allowed = {
            "flow_sensor_entity", "flow_alert_threshold",
            "flow_min_runs", "flow_sample_interval",
        }
        filtered = {k: v for k, v in updates.items() if k in allowed}
        self._global.update(filtered)
        await self._save()
        self.async_set_updated_data(self._build_data())

    async def async_reorder_stations(self, station_ids: list[str]) -> None:
        mapping = {s["id"]: s for s in self._stations}
        reordered = [mapping[sid] for sid in station_ids if sid in mapping]
        # Append any that weren't in the list
        listed_ids = set(station_ids)
        reordered += [s for s in self._stations if s["id"] not in listed_ids]
        self._stations = reordered
        self._regenerate_schedules()
        await self._save()
        self.async_set_updated_data(self._build_data())

    async def async_move_station(self, station_id: str, direction: str) -> None:
        """Shift a station one position up or down in the run order."""
        ids = [s["id"] for s in self._stations]
        if station_id not in ids:
            return
        idx = ids.index(station_id)
        if direction == "up" and idx > 0:
            self._stations[idx], self._stations[idx - 1] = (
                self._stations[idx - 1],
                self._stations[idx],
            )
        elif direction == "down" and idx < len(self._stations) - 1:
            self._stations[idx], self._stations[idx + 1] = (
                self._stations[idx + 1],
                self._stations[idx],
            )
        else:
            return
        self._regenerate_schedules()
        await self._save()
        self.async_set_updated_data(self._build_data())

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
        self._manual_station_id = station_id
        if station.get("flow_monitoring"):
            self._flow_monitor._start_monitoring(station["id"])

    async def async_stop_station_manual(self) -> None:
        """Stop whatever station is currently running. No-op if nothing is running."""
        current_sid = self._runtime.get("current_station_id")
        if current_sid:
            # Queue is running — signal cancellation then stop OS
            station = self._get_station(current_sid)
            if station:
                self._manual_stop_requested = True
                await self.hass.services.async_call(
                    OPENSPRINKLER_DOMAIN,
                    OS_SERVICE_STOP,
                    {},
                    target={"entity_id": f"switch.{station['base_name']}_station_enabled"},
                    blocking=True,
                )
        else:
            # Manual run — prefer the tracked station (covers the window before the binary
            # sensor fires ON), fall back to scanning binary sensors
            target = None
            if self._manual_station_id:
                target = self._get_station(self._manual_station_id)
            if target is None:
                for s in self._stations:
                    bs_id = f"binary_sensor.{s['base_name']}_station_running"
                    bs_state = self.hass.states.get(bs_id)
                    if bs_state and bs_state.state == "on":
                        target = s
                        break
            if target:
                self._manual_station_id = None
                await self.hass.services.async_call(
                    OPENSPRINKLER_DOMAIN,
                    OS_SERVICE_STOP,
                    {},
                    target={"entity_id": f"switch.{target['base_name']}_station_enabled"},
                    blocking=True,
                )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_station(self, station_id: str) -> dict | None:
        return next((s for s in self._stations if s["id"] == station_id), None)

    def _build_data(self) -> dict:
        return {
            "global": self._global,
            "stations": self._stations,
            "runtime": self._runtime,
            "day_schedules": self._day_schedules,
        }

    async def _async_update_data(self) -> dict:
        return self._build_data()

    # ------------------------------------------------------------------
    # Public read-only properties (for entities that don't need full data)
    # ------------------------------------------------------------------

    @property
    def global_config(self) -> dict:
        return self._global

    @property
    def stations(self) -> list[dict]:
        return self._stations

    @property
    def runtime(self) -> dict:
        return self._runtime

    @property
    def day_schedules(self) -> list[dict]:
        return self._day_schedules

    @property
    def flow_monitor(self) -> FlowMonitor:
        return self._flow_monitor

    def cleanup(self) -> None:
        for unsub in self._time_unsubs:
            unsub()
        self._time_unsubs.clear()
        for unsub in self._os_unsubs:
            unsub()
        self._os_unsubs.clear()
        for unsub in self._running_unsubs:
            unsub()
        self._running_unsubs.clear()
        for unsub in self._moisture_unsubs:
            unsub()
        self._moisture_unsubs.clear()
        self._flow_monitor.cleanup()


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _parse_time(time_str: str) -> tuple[int, int]:
    """Parse 'HH:MM' and return (hour, minute)."""
    try:
        parts = time_str.split(":")
        return int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        return 6, 0
