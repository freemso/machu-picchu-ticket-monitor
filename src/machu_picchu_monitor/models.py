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


@dataclass(frozen=True)
class AlertRule:
    """A declarative monitoring rule. Add one to rules.json to monitor more.

    Both types alert on EVERY run while the condition currently holds (a recurring
    reminder), not just on a transition.

    type="available"       -> alert while availability is greater than 0.
    type="below_threshold" -> alert while availability is below `threshold`.

    If `slot` is set (e.g. "08:00:00") the rule watches that time slot; otherwise it
    watches the per-route total.
    """

    name: str
    type: str
    visit_date: date
    route: str
    slot: str | None = None
    threshold: int | None = None

    @property
    def key(self) -> tuple[date, str]:
        return (self.visit_date, self.route)


@dataclass(frozen=True)
class RuleAlert:
    rule_name: str
    rule_type: str
    visit_date: date
    route: str
    route_name: str
    slot: str | None
    available: int
    capacity: int | None
    threshold: int | None
    seen_at: datetime


@dataclass
class MonitorStatus:
    running: bool = False
    last_started_at: datetime | None = None
    last_finished_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error: str | None = None
    last_provider: str | None = None
    consecutive_failures: int = 0
