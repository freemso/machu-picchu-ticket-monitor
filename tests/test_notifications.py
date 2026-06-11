from datetime import date

import pytest

from machu_picchu_monitor.config import Settings
from machu_picchu_monitor.models import RuleAlert, utcnow
from machu_picchu_monitor.notifications import NotificationManager


class FakeNotifier:
    def __init__(self, channel: str, *, enabled: bool = True, fail: bool = False):
        self.channel = channel
        self._enabled = enabled
        self.fail = fail
        self.sent = 0

    def enabled(self) -> bool:
        return self._enabled

    async def send(self, subject: str, message: str) -> None:
        self.sent += 1
        if self.fail:
            raise RuntimeError(f"{self.channel} failed")


def alert() -> RuleAlert:
    return RuleAlert(
        rule_name="2A available",
        rule_type="available",
        visit_date=date(2026, 8, 19),
        route="2A",
        route_name="Ruta 2-A",
        slot=None,
        available=12,
        capacity=None,
        threshold=None,
        seen_at=utcnow(),
    )


@pytest.mark.asyncio
async def test_backup_notifier_is_not_used_when_primary_succeeds() -> None:
    settings = Settings(preferred_notification="telegram", backup_notifications="email")
    telegram = FakeNotifier("telegram")
    email = FakeNotifier("email")
    manager = NotificationManager(settings)
    manager.notifiers = [telegram, email]

    assert await manager.send_alert(alert()) == ["telegram"]
    assert telegram.sent == 1
    assert email.sent == 0


@pytest.mark.asyncio
async def test_backup_notifier_is_used_when_primary_fails() -> None:
    settings = Settings(preferred_notification="telegram", backup_notifications="email")
    telegram = FakeNotifier("telegram", fail=True)
    email = FakeNotifier("email")
    manager = NotificationManager(settings)
    manager.notifiers = [telegram, email]

    assert await manager.send_alert(alert()) == ["email"]
    assert telegram.sent == 1
    assert email.sent == 1
