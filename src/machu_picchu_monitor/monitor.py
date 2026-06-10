from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Sequence
from datetime import date

from .config import Settings
from .models import MonitorStatus, utcnow
from .notifications import NotificationManager
from .observability import AVAILABILITY_GAUGE, LAST_SUCCESS_TIMESTAMP, MONITOR_RUNS
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
    ):
        self.settings = settings
        self.storage = storage
        self.provider = provider or AutoProvider(settings)
        self.notifications = notifications or NotificationManager(settings)
        self.status = MonitorStatus()
        self._stop_event = asyncio.Event()

    async def stop(self) -> None:
        self._stop_event.set()
        aclose = getattr(self.provider, "aclose", None)
        if aclose:
            await aclose()

    async def run_once(
        self,
        *,
        visit_dates: Sequence[date] | None = None,
        routes: Sequence[str] | None = None,
    ) -> int:
        started_at = utcnow()
        self.status.running = True
        self.status.last_started_at = started_at
        self.status.last_error = None

        provider_name = getattr(self.provider, "name", "unknown")
        try:
            records = await self.provider.fetch_availability(
                visit_dates or self.settings.target_date_values,
                routes or self.settings.target_route_values,
            )
            actual_provider = (
                getattr(self.provider, "last_provider", provider_name) or provider_name
            )
            changes = self.storage.record_availability(
                records,
                alert_on_first_seen=self.settings.alert_on_first_seen,
            )
            for record in records:
                AVAILABILITY_GAUGE.labels(
                    visit_date=record.visit_date.isoformat(),
                    route=record.route,
                ).set(record.quantity)

            for change in changes:
                sent_channels = await self.notifications.send(change)
                for channel in sent_channels:
                    self.storage.record_notification(
                        change,
                        channel=channel,
                        reason="availability_increase",
                    )

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
                    "changes": len(changes),
                },
            )
            return len(changes)
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

    async def run_forever(self) -> None:
        logger.info(
            "monitor_started",
            extra={
                "target_dates": [item.isoformat() for item in self.settings.target_date_values],
                "target_routes": self.settings.target_route_values,
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
