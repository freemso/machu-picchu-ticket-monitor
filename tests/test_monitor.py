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
        self.alerts = []

    async def send_alert(self, alert):
        self.alerts.append(alert)
        return ["fake"]


@pytest.mark.asyncio
async def test_available_rule_alerts_whenever_above_zero(tmp_path) -> None:
    settings = Settings(preferred_notification="fake", sqlite_path=tmp_path / "a.sqlite3")
    storage = SQLiteStorage(settings.sqlite_path)
    storage.init()
    notifications = FakeNotifications()
    rules = [
        AlertRule(name="2A", type="available", visit_date=date(2026, 8, 24), route="2A")
    ]
    # 0 (none -> no alert), 3 (available -> alert), 5 (still available -> alert again)
    monitor = MonitorService(
        settings,
        storage,
        provider=FakeProvider([0, 3, 5]),
        notifications=notifications,
        rules=rules,
    )

    assert await monitor.run_once() == 0
    assert notifications.alerts == []

    assert await monitor.run_once() == 1
    assert notifications.alerts[-1].available == 3
    assert storage.list_current()[0]["availability"] == 3

    assert await monitor.run_once() == 1  # still available -> alerts every run
    assert len(notifications.alerts) == 2
    storage.close()


@pytest.mark.asyncio
async def test_below_threshold_alerts_every_run_while_below(tmp_path) -> None:
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
    # 16 (<20 -> alert), 16 (<20 -> alert again), 25 (>=20 -> no alert), 9 (<20 -> alert)
    monitor = MonitorService(
        settings,
        storage,
        provider=SlotProvider([16, 16, 25, 9]),
        notifications=notifications,
        rules=rules,
    )

    assert await monitor.run_once() == 1
    assert notifications.alerts[0].available == 16
    assert notifications.alerts[0].capacity == 30

    assert await monitor.run_once() == 1  # still below -> alerts again
    assert len(notifications.alerts) == 2

    assert await monitor.run_once() == 0  # >= threshold -> no alert
    assert await monitor.run_once() == 1  # below again
    assert len(notifications.alerts) == 3
    assert notifications.alerts[-1].available == 9
    storage.close()
