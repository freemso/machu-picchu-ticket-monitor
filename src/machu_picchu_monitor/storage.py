from __future__ import annotations

import sqlite3
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .models import AvailabilityChange, AvailabilityRecord, ThresholdAlert, utcnow


def _dt(value: datetime) -> str:
    return value.isoformat()


class SQLiteStorage:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def init(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(
                """
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS availability_current (
                    visit_date TEXT NOT NULL,
                    route TEXT NOT NULL,
                    route_name TEXT NOT NULL,
                    availability INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    PRIMARY KEY (visit_date, route)
                );

                CREATE TABLE IF NOT EXISTS availability_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    visit_date TEXT NOT NULL,
                    route TEXT NOT NULL,
                    route_name TEXT NOT NULL,
                    old_availability INTEGER,
                    new_availability INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    seen_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_history_seen_at
                    ON availability_history (seen_at DESC);

                CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    visit_date TEXT NOT NULL,
                    route TEXT NOT NULL,
                    route_name TEXT NOT NULL,
                    availability INTEGER NOT NULL,
                    channel TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    sent_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS monitor_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    provider TEXT,
                    error TEXT
                );

                CREATE TABLE IF NOT EXISTS slot_current (
                    visit_date TEXT NOT NULL,
                    route TEXT NOT NULL,
                    slot TEXT NOT NULL,
                    route_name TEXT NOT NULL,
                    available INTEGER NOT NULL,
                    capacity INTEGER,
                    last_seen_at TEXT NOT NULL,
                    PRIMARY KEY (visit_date, route, slot)
                );

                CREATE TABLE IF NOT EXISTS slot_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    visit_date TEXT NOT NULL,
                    route TEXT NOT NULL,
                    slot TEXT NOT NULL,
                    old_available INTEGER,
                    new_available INTEGER NOT NULL,
                    capacity INTEGER,
                    seen_at TEXT NOT NULL
                );
                """
            )

    def record_availability(
        self,
        records: list[AvailabilityRecord],
        *,
        alert_on_first_seen: bool = False,
    ) -> list[AvailabilityChange]:
        changes: list[AvailabilityChange] = []
        with self._lock, self._conn:
            for record in records:
                visit_date = record.visit_date.isoformat()
                row = self._conn.execute(
                    """
                    SELECT availability
                    FROM availability_current
                    WHERE visit_date = ? AND route = ?
                    """,
                    (visit_date, record.route),
                ).fetchone()
                old_quantity = None if row is None else int(row["availability"])
                is_changed = old_quantity != record.quantity

                if is_changed:
                    change = AvailabilityChange(
                        visit_date=record.visit_date,
                        route=record.route,
                        route_name=record.route_name,
                        old_quantity=old_quantity,
                        new_quantity=record.quantity,
                        source=record.source,
                        seen_at=record.checked_at,
                    )
                    self._conn.execute(
                        """
                        INSERT INTO availability_history (
                            visit_date, route, route_name, old_availability,
                            new_availability, source, seen_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            visit_date,
                            record.route,
                            record.route_name,
                            old_quantity,
                            record.quantity,
                            record.source,
                            _dt(record.checked_at),
                        ),
                    )
                    if change.is_alertable or (
                        alert_on_first_seen and old_quantity is None and record.quantity > 0
                    ):
                        changes.append(change)

                self._conn.execute(
                    """
                    INSERT INTO availability_current (
                        visit_date, route, route_name, availability, source, last_seen_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(visit_date, route) DO UPDATE SET
                        route_name = excluded.route_name,
                        availability = excluded.availability,
                        source = excluded.source,
                        last_seen_at = excluded.last_seen_at
                    """,
                    (
                        visit_date,
                        record.route,
                        record.route_name,
                        record.quantity,
                        record.source,
                        _dt(record.checked_at),
                    ),
                )
        return changes

    def _insert_notification(
        self,
        *,
        visit_date: str,
        route: str,
        route_name: str,
        availability: int,
        channel: str,
        reason: str,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO notifications (
                    visit_date, route, route_name, availability, channel, reason, sent_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (visit_date, route, route_name, availability, channel, reason, _dt(utcnow())),
            )

    def record_notification(
        self,
        change: AvailabilityChange,
        *,
        channel: str,
        reason: str,
    ) -> None:
        self._insert_notification(
            visit_date=change.visit_date.isoformat(),
            route=change.route,
            route_name=change.route_name,
            availability=change.new_quantity,
            channel=channel,
            reason=reason,
        )

    def record_threshold_notification(
        self,
        alert: ThresholdAlert,
        *,
        channel: str,
        reason: str,
    ) -> None:
        route_label = f"{alert.route_name} {alert.slot}" if alert.slot else alert.route_name
        self._insert_notification(
            visit_date=alert.visit_date.isoformat(),
            route=alert.route,
            route_name=route_label,
            availability=alert.available,
            channel=channel,
            reason=reason,
        )

    def record_slot(
        self,
        *,
        visit_date: date,
        route: str,
        route_name: str,
        slot: str,
        available: int,
        capacity: int | None,
    ) -> int | None:
        """Upsert a watched slot's availability; return the previously stored value
        (None if first seen). Appends to slot_history when the value changes."""
        key_date = visit_date.isoformat()
        seen_at = _dt(utcnow())
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT available FROM slot_current WHERE visit_date=? AND route=? AND slot=?",
                (key_date, route, slot),
            ).fetchone()
            previous = None if row is None else int(row["available"])

            if previous != available:
                self._conn.execute(
                    """
                    INSERT INTO slot_history (
                        visit_date, route, slot, old_available, new_available, capacity, seen_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (key_date, route, slot, previous, available, capacity, seen_at),
                )

            self._conn.execute(
                """
                INSERT INTO slot_current (
                    visit_date, route, slot, route_name, available, capacity, last_seen_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(visit_date, route, slot) DO UPDATE SET
                    route_name = excluded.route_name,
                    available = excluded.available,
                    capacity = excluded.capacity,
                    last_seen_at = excluded.last_seen_at
                """,
                (key_date, route, slot, route_name, available, capacity, seen_at),
            )
        return previous

    def record_monitor_run(
        self,
        *,
        started_at: datetime,
        finished_at: datetime,
        status: str,
        provider: str | None,
        error: str | None,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO monitor_runs (started_at, finished_at, status, provider, error)
                VALUES (?, ?, ?, ?, ?)
                """,
                (_dt(started_at), _dt(finished_at), status, provider, error),
            )

    def list_current(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT visit_date, route, route_name, availability, source, last_seen_at
                FROM availability_current
                ORDER BY visit_date ASC, route ASC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def list_history(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT visit_date, route, route_name, old_availability,
                       new_availability, source, seen_at
                FROM availability_history
                ORDER BY seen_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_slot_current(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT visit_date, route, route_name, slot, available, capacity, last_seen_at
                FROM slot_current
                ORDER BY visit_date ASC, route ASC, slot ASC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def latest_seen_at(self) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(last_seen_at) AS last_seen_at FROM availability_current"
            ).fetchone()
        return None if row is None else row["last_seen_at"]

    def database_ok(self) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT 1 AS ok").fetchone()
        return bool(row and row["ok"] == 1)
