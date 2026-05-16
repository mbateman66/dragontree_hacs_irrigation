"""SQLite storage for station flow profiles and run history."""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Keep this many valid runs per station in the rolling baseline window.
PROFILE_WINDOW = 20


class FlowDatabase:
    def __init__(self, db_path: str) -> None:
        self._path = db_path
        self._lock = asyncio.Lock()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn

    def initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS flow_runs (
                    run_id TEXT PRIMARY KEY,
                    station_id TEXT NOT NULL,
                    start_time TEXT NOT NULL,
                    end_time TEXT,
                    sample_count INTEGER DEFAULT 0,
                    steady_count INTEGER DEFAULT 0,
                    steady_median REAL,
                    steady_q1 REAL,
                    steady_q3 REAL,
                    anomaly_detected INTEGER DEFAULT 0,
                    anomaly_score REAL,
                    baseline_median REAL,
                    baseline_stddev REAL,
                    discarded INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS flow_readings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    station_id TEXT NOT NULL,
                    elapsed_seconds REAL NOT NULL,
                    flow_rate REAL NOT NULL,
                    timestamp TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_readings_run
                    ON flow_readings(run_id);
                CREATE INDEX IF NOT EXISTS idx_runs_station
                    ON flow_runs(station_id, start_time);
            """)

    async def save_run(self, run: dict) -> None:
        async with self._lock:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._save_run_sync, run)

    def _save_run_sync(self, run: dict) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO flow_runs
                    (run_id, station_id, start_time, end_time, sample_count,
                     steady_count, steady_median, steady_q1, steady_q3,
                     anomaly_detected, anomaly_score, baseline_median,
                     baseline_stddev, discarded)
                VALUES
                    (:run_id, :station_id, :start_time, :end_time, :sample_count,
                     :steady_count, :steady_median, :steady_q1, :steady_q3,
                     :anomaly_detected, :anomaly_score, :baseline_median,
                     :baseline_stddev, :discarded)
                """,
                run,
            )

    async def save_readings(self, readings: list[dict]) -> None:
        async with self._lock:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._save_readings_sync, readings)

    def _save_readings_sync(self, readings: list[dict]) -> None:
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO flow_readings
                    (run_id, station_id, elapsed_seconds, flow_rate, timestamp)
                VALUES
                    (:run_id, :station_id, :elapsed_seconds, :flow_rate, :timestamp)
                """,
                readings,
            )

    async def get_baseline_runs(self, station_id: str) -> list[float]:
        """Return the last PROFILE_WINDOW steady_median values for a station."""
        async with self._lock:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None, self._get_baseline_runs_sync, station_id
            )

    def _get_baseline_runs_sync(self, station_id: str) -> list[float]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT steady_median FROM flow_runs
                WHERE station_id = ? AND discarded = 0 AND steady_median IS NOT NULL
                ORDER BY start_time DESC
                LIMIT ?
                """,
                (station_id, PROFILE_WINDOW),
            ).fetchall()
        return [r["steady_median"] for r in rows]

    async def get_station_startup_data(
        self, station_id: str
    ) -> tuple[list[float], list[dict]]:
        """Return baseline medians and recent run details in a single query."""
        async with self._lock:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None, self._get_station_startup_data_sync, station_id
            )

    def _get_station_startup_data_sync(
        self, station_id: str
    ) -> tuple[list[float], list[dict]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT run_id, start_time, end_time, steady_median,
                       steady_q1, steady_q3, steady_count,
                       anomaly_score, baseline_median
                FROM flow_runs
                WHERE station_id = ? AND discarded = 0 AND steady_median IS NOT NULL
                ORDER BY start_time DESC
                LIMIT ?
                """,
                (station_id, PROFILE_WINDOW),
            ).fetchall()
        baseline_medians = [r["steady_median"] for r in rows]
        recent_details = [
            {
                "run_id": r["run_id"],
                "start_time": r["start_time"],
                "end_time": r["end_time"],
                "median": round(r["steady_median"], 3),
                "q1": round(r["steady_q1"], 3) if r["steady_q1"] is not None else None,
                "q3": round(r["steady_q3"], 3) if r["steady_q3"] is not None else None,
                "steady_count": r["steady_count"],
                "anomaly_score": round(r["anomaly_score"], 4) if r["anomaly_score"] is not None else None,
                "baseline_median": round(r["baseline_median"], 3) if r["baseline_median"] is not None else None,
            }
            for r in rows[:10]
        ]
        return baseline_medians, recent_details

    async def get_run_count(self, station_id: str) -> int:
        """Count non-discarded runs with valid steady-state data."""
        async with self._lock:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None, self._get_run_count_sync, station_id
            )

    def _get_run_count_sync(self, station_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) as cnt FROM flow_runs
                WHERE station_id = ? AND discarded = 0 AND steady_median IS NOT NULL
                """,
                (station_id,),
            ).fetchone()
        return row["cnt"] if row else 0

    async def reset_profile(self, station_id: str) -> None:
        """Soft-delete all runs for a station — history is kept but excluded from baseline."""
        async with self._lock:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None, self._reset_profile_sync, station_id
            )

    def _reset_profile_sync(self, station_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE flow_runs SET discarded = 1 WHERE station_id = ?",
                (station_id,),
            )

    async def discard_run(self, run_id: str) -> None:
        """Mark a single run as discarded so it is excluded from the baseline."""
        async with self._lock:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._discard_run_sync, run_id)

    def _discard_run_sync(self, run_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE flow_runs SET discarded = 1 WHERE run_id = ?",
                (run_id,),
            )

    async def discard_runs_before(self, station_id: str, run_id: str) -> None:
        """Discard all runs for a station that are older than the given run."""
        async with self._lock:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._discard_runs_before_sync, station_id, run_id)

    def _discard_runs_before_sync(self, station_id: str, run_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE flow_runs SET discarded = 1
                WHERE station_id = ?
                  AND start_time < (SELECT start_time FROM flow_runs WHERE run_id = ?)
                """,
                (station_id, run_id),
            )

    async def get_recent_run_details(self, station_id: str, limit: int = 10) -> list[dict]:
        """Return run_id, start_time, and steady_median for the last N valid runs."""
        async with self._lock:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None, self._get_recent_run_details_sync, station_id, limit
            )

    def _get_recent_run_details_sync(self, station_id: str, limit: int) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT run_id, start_time, end_time, steady_median,
                       steady_q1, steady_q3, steady_count,
                       anomaly_score, baseline_median
                FROM flow_runs
                WHERE station_id = ? AND discarded = 0 AND steady_median IS NOT NULL
                ORDER BY start_time DESC
                LIMIT ?
                """,
                (station_id, limit),
            ).fetchall()
        return [
            {
                "run_id": r["run_id"],
                "start_time": r["start_time"],
                "end_time": r["end_time"],
                "median": round(r["steady_median"], 3),
                "q1": round(r["steady_q1"], 3) if r["steady_q1"] is not None else None,
                "q3": round(r["steady_q3"], 3) if r["steady_q3"] is not None else None,
                "steady_count": r["steady_count"],
                "anomaly_score": round(r["anomaly_score"], 4) if r["anomaly_score"] is not None else None,
                "baseline_median": round(r["baseline_median"], 3) if r["baseline_median"] is not None else None,
            }
            for r in rows
        ]

    async def get_recent_runs(self, station_id: str, limit: int = 10) -> list[dict]:
        async with self._lock:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None, self._get_recent_runs_sync, station_id, limit
            )

    def _get_recent_runs_sync(self, station_id: str, limit: int) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM flow_runs
                WHERE station_id = ?
                ORDER BY start_time DESC
                LIMIT ?
                """,
                (station_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]
