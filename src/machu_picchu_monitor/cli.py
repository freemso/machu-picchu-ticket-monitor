from __future__ import annotations

import argparse
import asyncio
from datetime import date

import uvicorn

from .app import create_app
from .config import get_settings
from .models import AvailabilityChange, utcnow
from .monitor import MonitorService
from .notifications import NotificationManager
from .observability import setup_logging
from .storage import SQLiteStorage


async def _run_check() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    storage = SQLiteStorage(settings.sqlite_path)
    storage.init()
    monitor = MonitorService(settings, storage)
    try:
        await monitor.run_once()
    finally:
        await monitor.stop()
        storage.close()


async def _run_monitor() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    storage = SQLiteStorage(settings.sqlite_path)
    storage.init()
    monitor = MonitorService(settings, storage)
    try:
        await monitor.run_forever()
    finally:
        await monitor.stop()
        storage.close()


async def _test_notification(channel: str | None = None) -> None:
    settings = get_settings()
    if channel:
        settings = settings.model_copy(
            update={"preferred_notification": channel, "backup_notifications": ""}
        )
    setup_logging(settings.log_level)
    manager = NotificationManager(settings)
    if settings.target_date_values:
        visit_date = settings.target_date_values[0]
    else:
        visit_date = date(2026, 8, 19)
    change = AvailabilityChange(
        visit_date=visit_date,
        route=settings.target_route_values[0] if settings.target_route_values else "2A",
        route_name="Test route notification",
        old_quantity=0,
        new_quantity=1,
        source="test_notification",
        seen_at=utcnow(),
    )
    sent = await manager.send(change)
    if not sent:
        raise SystemExit("No notification sent. Check credentials for the selected channel.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Machu Picchu ticket availability monitor")
    parser.add_argument(
        "command",
        choices=["serve", "monitor", "check", "init-db", "test-notification"],
        nargs="?",
        default="serve",
    )
    parser.add_argument(
        "--channel",
        choices=["telegram", "email", "slack", "all"],
        help="Notification channel to use with test-notification.",
    )
    args = parser.parse_args()

    settings = get_settings()
    if args.command == "serve":
        uvicorn.run(create_app(), host=settings.app_host, port=settings.app_port)
    elif args.command == "monitor":
        asyncio.run(_run_monitor())
    elif args.command == "check":
        asyncio.run(_run_check())
    elif args.command == "test-notification":
        asyncio.run(_test_notification(args.channel))
    elif args.command == "init-db":
        setup_logging(settings.log_level)
        storage = SQLiteStorage(settings.sqlite_path)
        storage.init()
        storage.close()


if __name__ == "__main__":
    main()
