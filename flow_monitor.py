"""Flow monitoring for irrigation stations using a whole-house flow sensor.

Watches OpenSprinkler station running binary sensors. When a station turns on,
samples the configured flow sensor at a fixed interval. The first N seconds
(per-station fill_time) are discarded so that long pipe-fill transients don't
contaminate the steady-state measurement.

After each run, IQR filtering removes brief spikes (toilet flushes, etc.), then
the median of the remaining samples is compared against a rolling baseline of
the last PROFILE_WINDOW valid runs. A deviation beyond alert_threshold (default
25%) fires a persistent HA notification and sets the station's anomaly flag.

Limitation: sustained concurrent household water use (e.g. a shower running the
entire irrigation cycle) will inflate the measured flow and slowly shift the
baseline. The system is designed for early-morning irrigation when household use
is minimal.
"""
from __future__ import annotations

import asyncio
import logging
import statistics
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event

from .flow_database import FlowDatabase

if TYPE_CHECKING:
    from .coordinator import IrrigationCoordinator

_LOGGER = logging.getLogger(__name__)

# Minimum steady-state samples required to record a valid run.
MIN_STEADY_SAMPLES = 3

# IQR fence multiplier for outlier rejection.
IQR_FENCE = 1.5


class FlowMonitor:
    """Samples the Droplet sensor during station runs and detects flow anomalies."""

    def __init__(
        self,
        hass: HomeAssistant,
        db: FlowDatabase,
        coordinator: IrrigationCoordinator,
    ) -> None:
        self.hass = hass
        self._db = db
        self._coordinator = coordinator
        self._unsubs: list = []
        self._active_tasks: dict[str, asyncio.Task] = {}
        # station_id → runtime state dict exposed to HA entities
        self._station_state: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Setup / teardown
    # ------------------------------------------------------------------

    def setup(self, stations: list[dict]) -> None:
        """Register state listeners for all flow-monitored stations."""
        self._teardown_listeners()

        monitored = [
            s for s in stations
            if s.get("flow_monitoring", False)
        ]
        if not monitored:
            return

        self._base_name_to_id: dict[str, str] = {
            s["base_name"]: s["id"] for s in monitored
        }
        entity_ids = [
            f"binary_sensor.{s['base_name']}_station_running"
            for s in monitored
        ]

        @callback
        def _state_changed(event: Any) -> None:
            entity_id: str = event.data.get("entity_id", "")
            new_state = event.data.get("new_state")
            old_state = event.data.get("old_state")
            if not new_state:
                return

            base_name = (
                entity_id
                .removeprefix("binary_sensor.")
                .removesuffix("_station_running")
            )
            station_id = self._base_name_to_id.get(base_name)
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

    def _teardown_listeners(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()

    def cleanup(self) -> None:
        self._teardown_listeners()
        for task in self._active_tasks.values():
            task.cancel()
        self._active_tasks.clear()

    # ------------------------------------------------------------------
    # Monitoring lifecycle
    # ------------------------------------------------------------------

    def _is_monitoring_active(self, station_id: str) -> bool:
        """Return True if a live monitoring task exists for this station."""
        task = self._active_tasks.get(station_id)
        return task is not None and not task.done()

    def _start_monitoring(self, station_id: str) -> None:
        if station_id in self._active_tasks:
            self._active_tasks[station_id].cancel()

        station = self._coordinator._get_station(station_id)
        if not station:
            return

        flow_sensor = self._coordinator.global_config.get("flow_sensor_entity")
        if not flow_sensor:
            _LOGGER.debug(
                "Flow monitoring: no sensor configured, skipping %s", station_id
            )
            return

        fill_time = int(station.get("flow_fill_time", 120))
        sample_interval = int(
            self._coordinator.global_config.get("flow_sample_interval", 10)
        )

        _LOGGER.info(
            "Flow monitoring started for %s (fill_time=%ds, interval=%ds)",
            station_id, fill_time, sample_interval,
        )
        self._update_station_state(station_id, {"status": "monitoring"})

        task = self.hass.async_create_task(
            self._sample_loop(station_id, flow_sensor, fill_time, sample_interval)
        )
        self._active_tasks[station_id] = task

    def _stop_monitoring(self, station_id: str) -> None:
        task = self._active_tasks.pop(station_id, None)
        if task and not task.done():
            task.cancel()

    # ------------------------------------------------------------------
    # Public accessors for HA entities
    # ------------------------------------------------------------------

    def get_station_flow_state(self, station_id: str) -> dict:
        return self._station_state.get(
            station_id,
            {
                "status": "unknown",
                "baseline_median": None,
                "last_anomaly": False,
                "run_count": 0,
                "recent_run_details": [],
            },
        )

    async def async_reset_profile(self, station_id: str) -> None:
        await self._db.reset_profile(station_id)
        self._update_station_state(
            station_id,
            {
                "status": "learning",
                "baseline_median": None,
                "last_anomaly": False,
                "run_count": 0,
                "recent_runs": [],
                "recent_run_details": [],
            },
        )
        _LOGGER.info("Flow profile reset for station %s", station_id)

    async def async_discard_run(self, station_id: str, run_id: str) -> None:
        """Mark a single run as discarded and refresh in-memory state from the DB."""
        await self._db.discard_run(run_id)
        await self.async_load_station_state(station_id)
        self._coordinator.async_set_updated_data(self._coordinator._build_data())
        _LOGGER.info("Flow run %s discarded for station %s", run_id, station_id)

    async def async_discard_runs_before(self, station_id: str, run_id: str) -> None:
        """Discard all runs older than run_id and refresh in-memory state."""
        await self._db.discard_runs_before(station_id, run_id)
        await self.async_load_station_state(station_id)
        self._coordinator.async_set_updated_data(self._coordinator._build_data())
        _LOGGER.info("Flow runs before %s discarded for station %s", run_id, station_id)

    async def async_load_station_state(self, station_id: str) -> None:
        """Populate in-memory state from the database on startup."""
        prior, run_details = await self._db.get_station_startup_data(station_id)
        run_count = len(prior)
        min_runs = int(
            self._coordinator.global_config.get("flow_min_runs", 5)
        )
        if run_count == 0:
            status = "learning"
            baseline_median = None
        elif run_count < min_runs:
            status = "learning"
            baseline_median = round(statistics.median(prior), 3)
        else:
            status = "normal"
            baseline_median = round(statistics.median(prior), 3)
        self._station_state[station_id] = {
            "status": status,
            "baseline_median": baseline_median,
            "last_anomaly": False,
            "run_count": run_count,
            "recent_runs": [round(v, 3) for v in reversed(prior[:10])],
            "recent_run_details": list(reversed(run_details)),
        }

    # ------------------------------------------------------------------
    # Sampling loop (runs as an asyncio task per active station)
    # ------------------------------------------------------------------

    async def _sample_loop(
        self,
        station_id: str,
        flow_sensor: str,
        fill_time: int,
        sample_interval: int,
    ) -> None:
        run_id = str(uuid.uuid4())
        start_time = datetime.now()
        readings: list[dict] = []
        elapsed = 0.0

        try:
            while True:
                state = self.hass.states.get(flow_sensor)
                if state is not None:
                    try:
                        rate = float(state.state)
                        readings.append(
                            {
                                "run_id": run_id,
                                "station_id": station_id,
                                "elapsed_seconds": elapsed,
                                "flow_rate": rate,
                                "timestamp": datetime.now().isoformat(),
                            }
                        )
                    except (ValueError, TypeError):
                        pass

                await asyncio.sleep(sample_interval)
                elapsed += sample_interval

        except asyncio.CancelledError:
            pass
        finally:
            end_time = datetime.now()
            try:
                await self._analyze_run(
                    run_id, station_id, start_time, end_time, readings, fill_time
                )
            except Exception:
                _LOGGER.exception(
                    "Flow analysis failed for %s; marking idle", station_id
                )
                if not self._is_monitoring_active(station_id):
                    self._update_station_state(station_id, {"status": "idle"})

    # ------------------------------------------------------------------
    # Run analysis
    # ------------------------------------------------------------------

    async def _analyze_run(
        self,
        run_id: str,
        station_id: str,
        start_time: datetime,
        end_time: datetime,
        readings: list[dict],
        fill_time: int,
    ) -> None:
        if readings:
            await self._db.save_readings(readings)

        steady_readings = [
            r["flow_rate"] for r in readings
            if r["elapsed_seconds"] >= fill_time
        ]

        alert_threshold = float(
            self._coordinator.global_config.get("flow_alert_threshold", 0.25)
        )
        min_runs = int(self._coordinator.global_config.get("flow_min_runs", 5))

        run_record: dict = {
            "run_id": run_id,
            "station_id": station_id,
            "start_time": start_time.isoformat(),
            "end_time": end_time.isoformat(),
            "sample_count": len(readings),
            "steady_count": len(steady_readings),
            "steady_median": None,
            "steady_q1": None,
            "steady_q3": None,
            "anomaly_detected": 0,
            "anomaly_score": None,
            "baseline_median": None,
            "baseline_stddev": None,
            "discarded": 0,
        }

        if len(steady_readings) < MIN_STEADY_SAMPLES:
            _LOGGER.debug(
                "Flow: %s run too short (%d steady samples < %d required), discarding",
                station_id, len(steady_readings), MIN_STEADY_SAMPLES,
            )
            run_record["discarded"] = 1
            await self._db.save_run(run_record)
            if not self._is_monitoring_active(station_id):
                self._update_station_state(station_id, {"status": "idle"})
            return

        filtered = _iqr_filter(steady_readings)
        if len(filtered) < MIN_STEADY_SAMPLES:
            _LOGGER.debug(
                "Flow: %s — too few samples after outlier filtering, discarding",
                station_id,
            )
            run_record["discarded"] = 1
            await self._db.save_run(run_record)
            if not self._is_monitoring_active(station_id):
                self._update_station_state(station_id, {"status": "idle"})
            return

        run_median = statistics.median(filtered)
        q1, q3 = _quartiles(filtered)
        run_record["steady_median"] = run_median
        run_record["steady_q1"] = q1
        run_record["steady_q3"] = q3

        # Load prior baseline runs (excludes the current run — not yet saved)
        prior_medians = await self._db.get_baseline_runs(station_id)
        run_count = len(prior_medians)

        if run_count + 1 < min_runs:
            run_record["discarded"] = 0
            await self._db.save_run(run_record)
            new_count = run_count + 1
            all_medians = list(reversed(prior_medians)) + [run_median]
            prior_details = self._station_state.get(station_id, {}).get("recent_run_details", [])
            new_detail = {
                "run_id": run_id,
                "start_time": start_time.isoformat(),
                "end_time": end_time.isoformat(),
                "median": round(run_median, 3),
                "q1": round(q1, 3) if q1 is not None else None,
                "q3": round(q3, 3) if q3 is not None else None,
                "steady_count": len(filtered),
                "anomaly_score": None,
                "baseline_median": None,
            }
            new_details = (prior_details + [new_detail])[-10:]
            self._update_station_state(
                station_id,
                {
                    "status": "learning",
                    "run_count": new_count,
                    "baseline_median": round(statistics.median(all_medians), 3),
                    "last_anomaly": False,
                    "recent_runs": [round(v, 3) for v in all_medians[-10:]],
                    "recent_run_details": new_details,
                },
            )
            _LOGGER.info(
                "Flow: %s learning (%d/%d runs), median=%.3f GPM",
                station_id, new_count, min_runs, run_median,
            )
            return

        # Enough history — compare against baseline
        baseline_median = statistics.median(prior_medians)
        baseline_stddev = (
            statistics.stdev(prior_medians) if len(prior_medians) > 1 else 0.0
        )

        run_record["baseline_median"] = baseline_median
        run_record["baseline_stddev"] = baseline_stddev

        if baseline_median > 0:
            deviation = abs(run_median - baseline_median) / baseline_median
            anomaly = deviation > alert_threshold
        else:
            deviation = 0.0
            anomaly = False

        run_record["anomaly_score"] = round(deviation, 4)
        run_record["anomaly_detected"] = 1 if anomaly else 0
        await self._db.save_run(run_record)

        if anomaly:
            direction = "high" if run_median > baseline_median else "low"
            status = direction
        else:
            status = "normal"

        all_medians = list(reversed(prior_medians)) + [run_median]
        prior_details = self._station_state.get(station_id, {}).get("recent_run_details", [])
        new_detail = {
            "run_id": run_id,
            "start_time": start_time.isoformat(),
            "end_time": end_time.isoformat(),
            "median": round(run_median, 3),
            "q1": round(q1, 3) if q1 is not None else None,
            "q3": round(q3, 3) if q3 is not None else None,
            "steady_count": len(filtered),
            "anomaly_score": round(deviation, 4),
            "baseline_median": round(baseline_median, 3),
        }
        new_details = (prior_details + [new_detail])[-10:]
        self._update_station_state(
            station_id,
            {
                "status": status,
                "run_count": run_count + 1,
                "baseline_median": round(baseline_median, 3),
                "last_anomaly": anomaly,
                "recent_runs": [round(v, 3) for v in all_medians[-10:]],
                "recent_run_details": new_details,
            },
        )

        _LOGGER.info(
            "Flow: %s run complete — median=%.3f GPM, baseline=%.3f GPM, "
            "deviation=%.1f%%, anomaly=%s",
            station_id, run_median, baseline_median,
            deviation * 100, anomaly,
        )

        if anomaly:
            station = self._coordinator._get_station(station_id)
            friendly = (
                station.get("friendly_name", station_id) if station else station_id
            )
            pct = round(deviation * 100, 1)
            message = (
                f"Measured {run_median:.2f} GPM vs baseline {baseline_median:.2f} GPM "
                f"({pct}% deviation). Check for broken or leaking pipes."
            )
            self.hass.async_create_task(
                self._fire_notification(station_id, friendly, message)
            )

    async def _fire_notification(
        self, station_id: str, friendly: str, message: str
    ) -> None:
        await self.hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": f"Irrigation Flow Anomaly: {friendly}",
                "message": message,
                "notification_id": f"dragontree_flow_anomaly_{station_id}",
            },
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _update_station_state(self, station_id: str, updates: dict) -> None:
        existing = self._station_state.setdefault(
            station_id,
            {
                "status": "unknown",
                "baseline_median": None,
                "last_anomaly": False,
                "run_count": 0,
            },
        )
        existing.update(updates)
        self._coordinator.async_set_updated_data(self._coordinator._build_data())


# ------------------------------------------------------------------
# Statistics helpers
# ------------------------------------------------------------------

def _iqr_filter(values: list[float]) -> list[float]:
    """Remove values outside the 1.5×IQR fence."""
    if len(values) < 4:
        return values
    q1, q3 = _quartiles(values)
    iqr = q3 - q1
    lo = q1 - IQR_FENCE * iqr
    hi = q3 + IQR_FENCE * iqr
    return [v for v in values if lo <= v <= hi]


def _quartiles(values: list[float]) -> tuple[float, float]:
    sorted_v = sorted(values)
    n = len(sorted_v)
    q1 = statistics.median(sorted_v[: n // 2])
    q3 = statistics.median(sorted_v[(n + 1) // 2 :])
    return q1, q3
