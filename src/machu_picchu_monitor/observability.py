from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

from prometheus_client import Counter, Gauge

MONITOR_RUNS = Counter(
    "machu_picchu_monitor_runs_total",
    "Monitor runs by status and provider.",
    ["status", "provider"],
)
PROVIDER_FAILURES = Counter(
    "machu_picchu_provider_failures_total",
    "Availability provider failures.",
    ["provider"],
)
NOTIFICATIONS_SENT = Counter(
    "machu_picchu_notifications_sent_total",
    "Notifications sent by channel.",
    ["channel"],
)
AVAILABILITY_GAUGE = Gauge(
    "machu_picchu_ticket_availability",
    "Current available tickets by visit date and route.",
    ["visit_date", "route"],
)
SLOT_AVAILABILITY_GAUGE = Gauge(
    "machu_picchu_slot_availability",
    "Current available tickets for a watched time slot.",
    ["visit_date", "route", "slot"],
)
THRESHOLD_ALERTS = Counter(
    "machu_picchu_threshold_alerts_total",
    "Below-threshold (low-stock) alerts fired by rule.",
    ["route", "slot"],
)
LAST_SUCCESS_TIMESTAMP = Gauge(
    "machu_picchu_last_success_timestamp_seconds",
    "Unix timestamp of the last successful monitor run.",
)

TELEGRAM_TOKEN_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]+")


def redact(value: Any) -> Any:
    if isinstance(value, str):
        return TELEGRAM_TOKEN_RE.sub("bot<redacted>", value)
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        for key, value in record.__dict__.items():
            if key.startswith("_") or key in {
                "args",
                "asctime",
                "created",
                "exc_info",
                "exc_text",
                "filename",
                "funcName",
                "levelname",
                "levelno",
                "lineno",
                "module",
                "msecs",
                "message",
                "msg",
                "name",
                "pathname",
                "process",
                "processName",
                "relativeCreated",
                "stack_info",
                "thread",
                "threadName",
            }:
                continue
            try:
                safe_value = redact(value)
                json.dumps(safe_value)
                payload[key] = safe_value
            except TypeError:
                payload[key] = redact(str(value))
        return json.dumps(payload, separators=(",", ":"))


def setup_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
