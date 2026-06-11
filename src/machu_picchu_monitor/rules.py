from __future__ import annotations

import json
import logging
import re
from datetime import date
from pathlib import Path
from typing import Any

from .models import AlertRule
from .route_matching import normalize_route_code

logger = logging.getLogger(__name__)

VALID_TYPES = {"available", "below_threshold"}
# "increase" is the old name for "available"; accept it so existing rules keep working.
TYPE_ALIASES = {"increase": "available"}
_SLOT_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")


class RuleError(ValueError):
    pass


def _normalize_slot(value: Any) -> str | None:
    if value is None or value == "":
        return None
    match = _SLOT_RE.match(str(value).strip())
    if not match:
        raise RuleError(f"Invalid slot time {value!r}; expected HH:MM (e.g. 08:00)")
    hour, minute, second = match.group(1), match.group(2), match.group(3) or "00"
    return f"{int(hour):02d}:{minute}:{second}"


def _parse_rule(raw: dict[str, Any], index: int) -> AlertRule:
    try:
        rule_type = str(raw["type"]).strip().lower()
        rule_type = TYPE_ALIASES.get(rule_type, rule_type)
        visit_date = date.fromisoformat(str(raw["date"]).strip())
        route = normalize_route_code(str(raw["route"]))
    except KeyError as exc:
        raise RuleError(f"Rule #{index} is missing required field {exc}") from exc
    except ValueError as exc:
        raise RuleError(f"Rule #{index} has an invalid value: {exc}") from exc

    if rule_type not in VALID_TYPES:
        raise RuleError(
            f"Rule #{index} has unknown type {rule_type!r}; valid: {sorted(VALID_TYPES)}"
        )

    slot = _normalize_slot(raw.get("slot"))
    threshold = raw.get("threshold")
    if rule_type == "below_threshold":
        if threshold is None:
            raise RuleError(f"Rule #{index} (below_threshold) requires a 'threshold'")
        threshold = int(threshold)

    name = str(raw.get("name") or f"{rule_type}:{route}:{visit_date}{f':{slot}' if slot else ''}")
    return AlertRule(
        name=name,
        type=rule_type,
        visit_date=visit_date,
        route=route,
        slot=slot,
        threshold=threshold,
    )


def parse_rules(payload: list[dict[str, Any]]) -> list[AlertRule]:
    if not isinstance(payload, list):
        raise RuleError("Rules payload must be a JSON list of rule objects")
    return [_parse_rule(raw, i) for i, raw in enumerate(payload)]


def load_rules(*, inline_json: str | None, rules_file: str | Path) -> list[AlertRule]:
    """Load alert rules from inline JSON (takes precedence) or a rules file."""
    raw_text: str | None = None
    source = ""
    if inline_json and inline_json.strip():
        raw_text, source = inline_json, "ALERT_RULES env"
    else:
        path = Path(rules_file)
        if path.is_file():
            raw_text, source = path.read_text(encoding="utf-8"), str(path)

    if not raw_text:
        logger.warning("no_alert_rules_found", extra={"rules_file": str(rules_file)})
        return []

    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise RuleError(f"Could not parse alert rules from {source}: {exc}") from exc

    rules = parse_rules(payload)
    logger.info("alert_rules_loaded", extra={"count": len(rules), "source": source})
    return rules
