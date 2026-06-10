from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class RouteMetadata:
    code: str
    name: str
    circuit_id: int
    route_id: int


@dataclass(frozen=True)
class AvailabilityRecord:
    visit_date: date
    route: str
    route_name: str
    quantity: int
    source: str
    checked_at: datetime = field(default_factory=utcnow)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AvailabilityChange:
    visit_date: date
    route: str
    route_name: str
    old_quantity: int | None
    new_quantity: int
    source: str
    seen_at: datetime

    @property
    def is_alertable(self) -> bool:
        return (
            self.old_quantity is not None
            and self.new_quantity > self.old_quantity
            and self.new_quantity > 0
        )


@dataclass
class MonitorStatus:
    running: bool = False
    last_started_at: datetime | None = None
    last_finished_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error: str | None = None
    last_provider: str | None = None
    consecutive_failures: int = 0
