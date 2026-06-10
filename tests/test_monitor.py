from datetime import date

import pytest

from machu_picchu_monitor.config import Settings
from machu_picchu_monitor.models import AvailabilityRecord
from machu_picchu_monitor.monitor import MonitorService
from machu_picchu_monitor.storage import SQLiteStorage


class FakeProvider:
    name = "fake"

    def __init__(self, quantities: list[int]):
        self.quantities = quantities
        self.calls = 0

    async def fetch_availability(self, visit_dates, routes):
        quantity = self.quantities[self.calls]
        self.calls += 1
        return [
            AvailabilityRecord(
                visit_date=visit_dates[0],
                route=routes[0],
                route_name="Ruta 2-A",
                quantity=quantity,
                source="fake",
            )
        ]


class FakeNotifications:
    def __init__(self):
        self.sent = []

    async def send(self, change):
        self.sent.append(change)
        return ["fake"]


@pytest.mark.asyncio
async def test_monitor_notifies_only_after_stored_increase(tmp_path) -> None:
    settings = Settings(
        target_dates="2026-08-24",
        target_routes="2A",
        preferred_notification="fake",
        sqlite_path=tmp_path / "availability.sqlite3",
    )
    storage = SQLiteStorage(settings.sqlite_path)
    storage.init()
    notifications = FakeNotifications()
    monitor = MonitorService(
        settings,
        storage,
        provider=FakeProvider([0, 3]),
        notifications=notifications,
    )

    assert await monitor.run_once(visit_dates=[date(2026, 8, 24)], routes=["2A"]) == 0
    assert notifications.sent == []

    assert await monitor.run_once(visit_dates=[date(2026, 8, 24)], routes=["2A"]) == 1
    assert len(notifications.sent) == 1
    assert notifications.sent[0].new_quantity == 3
    assert storage.list_current()[0]["availability"] == 3
    storage.close()
