from datetime import date

import pytest

from machu_picchu_monitor.config import Settings
from machu_picchu_monitor.models import AlertRule, AvailabilityRecord
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


class SlotProvider:
    name = "fake"

    def __init__(self, slot_values: list[int], slot: str = "08:00:00", capacity: int = 30):
        self.slot_values = slot_values
        self.slot = slot
        self.capacity = capacity
        self.calls = 0

    async def fetch_availability(self, visit_dates, routes):
        available = self.slot_values[self.calls]
        self.calls += 1
        rows = [
            {"dhora_ini": "07:00:00", "ncupo_actual": 5, "ncupo": 40, "activa": 1},
            {
                "dhora_ini": self.slot,
                "ncupo_actual": available,
                "ncupo": self.capacity,
                "activa": 1,
            },
        ]
        return [
            AvailabilityRecord(
                visit_date=visit_dates[0],
                route=routes[0],
                route_name="Ruta 1-C",
                quantity=5 + available,
                source="fake",
                raw={"horarios": rows},
            )
        ]


class FakeNotifications:
    def __init__(self):
        self.sent = []
        self.threshold_sent = []

    async def send(self, change):
        self.sent.append(change)
        return ["fake"]

    async def send_threshold(self, alert):
        self.threshold_sent.append(alert)
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
    rules = [
        AlertRule(
            name="2A increase",
            type="increase",
            visit_date=date(2026, 8, 24),
            route="2A",
        )
    ]
    monitor = MonitorService(
        settings,
        storage,
        provider=FakeProvider([0, 3]),
        notifications=notifications,
        rules=rules,
    )

    assert await monitor.run_once() == 0
    assert notifications.sent == []

    assert await monitor.run_once() == 1
    assert len(notifications.sent) == 1
    assert notifications.sent[0].new_quantity == 3
    assert storage.list_current()[0]["availability"] == 3
    storage.close()


@pytest.mark.asyncio
async def test_below_threshold_alerts_once_on_crossing(tmp_path) -> None:
    settings = Settings(preferred_notification="fake", sqlite_path=tmp_path / "a.sqlite3")
    storage = SQLiteStorage(settings.sqlite_path)
    storage.init()
    notifications = FakeNotifications()
    rules = [
        AlertRule(
            name="1C 08:00 low stock",
            type="below_threshold",
            visit_date=date(2026, 8, 19),
            route="1C",
            slot="08:00:00",
            threshold=20,
        )
    ]
    # 16 (below 20 -> alert), 16 (still low -> no repeat), 25 (recovered), 9 (re-cross -> alert)
    monitor = MonitorService(
        settings,
        storage,
        provider=SlotProvider([16, 16, 25, 9]),
        notifications=notifications,
        rules=rules,
    )

    assert await monitor.run_once() == 1
    assert len(notifications.threshold_sent) == 1
    assert notifications.threshold_sent[0].available == 16
    assert notifications.threshold_sent[0].capacity == 30

    assert await monitor.run_once() == 0  # still below, no repeat
    assert len(notifications.threshold_sent) == 1

    assert await monitor.run_once() == 0  # recovered above threshold
    assert await monitor.run_once() == 1  # crossed below again
    assert len(notifications.threshold_sent) == 2
    assert notifications.threshold_sent[1].available == 9
    storage.close()
