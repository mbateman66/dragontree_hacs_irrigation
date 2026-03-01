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
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    DAYS_OF_WEEK,
    DEFAULT_AM_START_TIME,
    DEFAULT_LOOKAHEAD_DAYS,
    DEFAULT_PM_START_TIME,
    DEFAULT_RAIN_MODE,
    DEFAULT_WEEK_INTERVAL,
    DOMAIN,
    OPENSPRINKLER_DOMAIN,
    OS_SERVICE_RUN_STATION,
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
    STATUS_RUNNING,
    STATUS_SCHEDULED,
    STORAGE_KEY,
    STORAGE_VERSION,
)

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
    "schedule_mode": SCHEDULE_MODE_NORMAL,
    "sensitive": False,
    "tracked": True,
    "normal_schedule": None,
    "hot_schedule": None,
    "last_run": None,
}


def _make_station(base_name: str, friendly_name: str) -> dict:
    s = deepcopy(DEFAULT_STATION_TEMPLATE)
    s["id"] = base_name
    s["base_name"] = base_name
    s["friendly_name"] = friendly_name
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
        self._queue_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    async def async_initialize(self) -> None:
        """Load persisted data, merge-discover OS stations, setup triggers."""
        stored = await self._store.async_load()
        if stored:
            self._global = stored.get("global", deepcopy(DEFAULT_GLOBAL))
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

    async def _merge_discover_stations(self) -> None:
        """Scan OpenSprinkler entity registry and add any stations not yet tracked.

        Default-named stations (s1, s2, …) are added with ignored=True so they
        appear in the management view but don't affect scheduling until the user
        explicitly enables them.  Custom-named stations are added with ignored=False.
        """
        registry = er.async_get(self.hass)
        default_re = re.compile(r"^s\d+$")
        existing_ids = {s["id"] for s in self._stations}

        added = 0
        for entry in registry.entities.values():
            if entry.platform != OPENSPRINKLER_DOMAIN:
                continue
            if not entry.entity_id.startswith("switch."):
                continue
            if not entry.entity_id.endswith("_station_enabled"):
                continue
            base_name = (
                entry.entity_id.removeprefix("switch.").removesuffix("_station_enabled")
            )
            if base_name in existing_ids:
                continue
            is_default_name = bool(default_re.match(base_name))
            friendly = base_name.replace("_", " ").title()
            station = _make_station(base_name, friendly)
            station["tracked"] = not is_default_name
            self._stations.append(station)
            existing_ids.add(base_name)
            added += 1

        if added:
            _LOGGER.info(
                "Dragontree Irrigation: added %d new station(s) from OpenSprinkler",
                added,
            )
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

        entity_ids = [
            f"switch.{s['base_name']}_station_enabled"
            for s in self._stations
        ]
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

        entity_ids = [
            f"binary_sensor.{s['base_name']}_station_running"
            for s in self._stations
        ]
        if not entity_ids:
            return

        @callback
        def _station_running_changed(_event: Any) -> None:
            self.async_set_updated_data(self._build_data())

        self._running_unsubs.append(
            async_track_state_change_event(self.hass, entity_ids, _station_running_changed)
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
                if station_entry["status"] in (STATUS_CANCELLED, STATUS_COMPLETE):
                    continue
                if not self._global.get("master_enable"):
                    break

                station = self._get_station(station_entry["station_id"])
                if not station:
                    station_entry["status"] = STATUS_CANCELLED
                    continue

                station_entry["status"] = STATUS_RUNNING
                self._runtime["current_station_id"] = station["id"]
                self.async_set_updated_data(self._build_data())

                duration = station_entry["duration"]
                entity_id = f"switch.{station['base_name']}_station_enabled"
                try:
                    await self.hass.services.async_call(
                        OPENSPRINKLER_DOMAIN,
                        OS_SERVICE_RUN_STATION,
                        {"run_seconds": duration},
                        target={"entity_id": entity_id},
                        blocking=False,
                    )
                except Exception as err:
                    _LOGGER.error("Failed to start %s: %s", station["base_name"], err)
                    station_entry["status"] = STATUS_CANCELLED
                    self._runtime["current_station_id"] = None
                    continue

                await self._wait_for_station(station["base_name"], duration + 60)

                if station_entry["status"] == STATUS_RUNNING:
                    station_entry["status"] = STATUS_COMPLETE
                    station["last_run"] = date.today().isoformat()

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

    async def _wait_for_station(self, base_name: str, timeout_seconds: int) -> None:
        """Wait for a station to start and then finish running.

        Sets up the state-change listener BEFORE sampling current state to
        avoid a race condition where the binary sensor turns on between the
        sample and the listener being registered.
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
                    return

            # Station is on — now wait for it to finish.
            try:
                await asyncio.wait_for(done.wait(), timeout=float(timeout_seconds))
            except asyncio.TimeoutError:
                _LOGGER.warning("Timeout waiting for station %s to finish", base_name)
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
        await self._save()
        async_dispatcher_send(self.hass, SIGNAL_STATIONS_UPDATED)
        self.async_set_updated_data(self._build_data())

    async def async_update_station(self, station_id: str, data: dict) -> None:
        station = self._get_station(station_id)
        if not station:
            raise ValueError(f"Station '{station_id}' not found")
        station.update(data)
        self._regenerate_schedules()
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
        await self._save()
        async_dispatcher_send(self.hass, SIGNAL_STATIONS_UPDATED)
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
