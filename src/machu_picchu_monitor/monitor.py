from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

from .config import Settings
from .models import AlertRule, AvailabilityRecord, MonitorStatus, ThresholdAlert, utcnow
from .notifications import NotificationManager
from .observability import (
    AVAILABILITY_GAUGE,
    LAST_SUCCESS_TIMESTAMP,
    MONITOR_RUNS,
    SLOT_AVAILABILITY_GAUGE,
    THRESHOLD_ALERTS,
)
from .providers import AutoProvider, AvailabilityProvider
from .storage import SQLiteStorage

logger = logging.getLogger(__name__)


class MonitorService:
    def __init__(
        self,
        settings: Settings,
        storage: SQLiteStorage,
        *,
        provider: AvailabilityProvider | None = None,
        notifications: NotificationManager | None = None,
        rules: list[AlertRule] | None = None,
    ):
        self.settings = settings
        self.storage = storage
        self.provider = provider or AutoProvider(settings)
        self.notifications = notifications or NotificationManager(settings)
        self.rules = rules if rules is not None else settings.load_alert_rules()
        self.status = MonitorStatus()
        self._stop_event = asyncio.Event()

    async def stop(self) -> None:
        self._stop_event.set()
        aclose = getattr(self.provider, "aclose", None)
        if aclose:
            await aclose()

    async def run_once(self, *, rules: list[AlertRule] | None = None) -> int:
        started_at = utcnow()
        self.status.running = True
        self.status.last_started_at = started_at
        self.status.last_error = None

        active_rules = rules if rules is not None else self.rules
        provider_name = getattr(self.provider, "name", "unknown")
        try:
            needed = {rule.key for rule in active_rules}
            dates = sorted({visit_date for visit_date, _ in needed})
            routes = sorted({route for _, route in needed})
            records = await self.provider.fetch_availability(dates, routes)
            actual_provider = (
                getattr(self.provider, "last_provider", provider_name) or provider_name
            )
            by_key = {(record.visit_date, record.route): record for record in records}

            for record in records:
                AVAILABILITY_GAUGE.labels(
                    visit_date=record.visit_date.isoformat(),
                    route=record.route,
                ).set(record.quantity)

            alerts = 0
            alerts += await self._run_increase_rules(active_rules, by_key)
            alerts += await self._run_threshold_rules(active_rules, by_key)

            finished_at = utcnow()
            self.storage.record_monitor_run(
                started_at=started_at,
                finished_at=finished_at,
                status="success",
                provider=actual_provider,
                error=None,
            )
            self.status.last_finished_at = finished_at
            self.status.last_success_at = finished_at
            self.status.last_provider = actual_provider
            self.status.consecutive_failures = 0
            LAST_SUCCESS_TIMESTAMP.set(finished_at.timestamp())
            MONITOR_RUNS.labels(status="success", provider=actual_provider).inc()
            logger.info(
                "monitor_run_success",
                extra={
                    "provider": actual_provider,
                    "records": len(records),
                    "rules": len(active_rules),
                    "alerts": alerts,
                },
            )
            return alerts
        except Exception as exc:
            finished_at = utcnow()
            self.storage.record_monitor_run(
                started_at=started_at,
                finished_at=finished_at,
                status="failure",
                provider=provider_name,
                error=str(exc),
            )
            self.status.last_finished_at = finished_at
            self.status.last_error = str(exc)
            self.status.consecutive_failures += 1
            MONITOR_RUNS.labels(status="failure", provider=provider_name).inc()
            logger.exception(
                "monitor_run_failed",
                extra={"provider": provider_name, "error": str(exc)},
            )
            raise
        finally:
            self.status.running = False

    async def _run_increase_rules(
        self,
        rules: list[AlertRule],
        by_key: dict[tuple, AvailabilityRecord],
    ) -> int:
        keys = {rule.key for rule in rules if rule.type == "increase"}
        records = [by_key[key] for key in keys if key in by_key]
        changes = self.storage.record_availability(
            records,
            alert_on_first_seen=self.settings.alert_on_first_seen,
        )
        alerts = 0
        for change in changes:
            sent_channels = await self.notifications.send(change)
            for channel in sent_channels:
                self.storage.record_notification(
                    change, channel=channel, reason="availability_increase"
                )
            if sent_channels:
                alerts += 1
        return alerts

    async def _run_threshold_rules(
        self,
        rules: list[AlertRule],
        by_key: dict[tuple, AvailabilityRecord],
    ) -> int:
        alerts = 0
        for rule in rules:
            if rule.type != "below_threshold" or rule.threshold is None:
                continue
            record = by_key.get(rule.key)
            if record is None:
                logger.warning(
                    "threshold_rule_no_data",
                    extra={"rule": rule.name, "route": rule.route},
                )
                continue

            available, capacity = self._availability_for_rule(rule, record)
            if available is None:
                logger.warning(
                    "threshold_rule_slot_missing",
                    extra={"rule": rule.name, "route": rule.route, "slot": rule.slot},
                )
                continue

            slot_key = rule.slot or "aggregate"
            previous = self.storage.record_slot(
                visit_date=rule.visit_date,
                route=rule.route,
                route_name=record.route_name,
                slot=slot_key,
                available=available,
                capacity=capacity,
            )
            if rule.slot:
                SLOT_AVAILABILITY_GAUGE.labels(
                    visit_date=rule.visit_date.isoformat(),
                    route=rule.route,
                    slot=rule.slot,
                ).set(available)

            crossed_below = available < rule.threshold and (
                previous is None or previous >= rule.threshold
            )
            if not crossed_below:
                continue

            alert = ThresholdAlert(
                rule_name=rule.name,
                visit_date=rule.visit_date,
                route=rule.route,
                route_name=record.route_name,
                slot=rule.slot,
                available=available,
                capacity=capacity,
                threshold=rule.threshold,
                previous=previous,
                seen_at=utcnow(),
            )
            sent_channels = await self.notifications.send_threshold(alert)
            for channel in sent_channels:
                self.storage.record_threshold_notification(
                    alert, channel=channel, reason="below_threshold"
                )
            if sent_channels:
                alerts += 1
            THRESHOLD_ALERTS.labels(route=rule.route, slot=slot_key).inc()
            logger.info(
                "threshold_alert",
                extra={
                    "rule": rule.name,
                    "route": rule.route,
                    "slot": rule.slot,
                    "available": available,
                    "threshold": rule.threshold,
                    "previous": previous,
                    "channels": sent_channels,
                },
            )
        return alerts

    @staticmethod
    def _availability_for_rule(
        rule: AlertRule,
        record: AvailabilityRecord,
    ) -> tuple[int | None, int | None]:
        if not rule.slot:
            return record.quantity, None
        rows: Any = record.raw.get("horarios") if isinstance(record.raw, dict) else None
        if not isinstance(rows, list):
            return None, None
        for row in rows:
            if str(row.get("dhora_ini")) == rule.slot:
                available = int(row.get("ncupo_actual") or row.get("ncupoActual") or 0)
                capacity = int(row.get("ncupo") or 0) or None
                return available, capacity
        return None, None

    async def run_forever(self) -> None:
        logger.info(
            "monitor_started",
            extra={
                "rules": [rule.name for rule in self.rules],
                "rule_count": len(self.rules),
                "interval_seconds": self.settings.poll_interval_seconds,
            },
        )
        while not self._stop_event.is_set():
            try:
                await self.run_once()
                delay = self._next_poll_delay()
            except Exception:
                delay = self._backoff_delay()
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
            except TimeoutError:
                continue

    def _next_poll_delay(self) -> float:
        jitter = random.uniform(-self.settings.jitter_seconds, self.settings.jitter_seconds)
        return max(60.0, self.settings.poll_interval_seconds + jitter)

    def _backoff_delay(self) -> float:
        failures = max(1, self.status.consecutive_failures)
        return min(
            self.settings.retry_max_seconds,
            self.settings.retry_base_seconds * (2 ** (failures - 1)),
        )
