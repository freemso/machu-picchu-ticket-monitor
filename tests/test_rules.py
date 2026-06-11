from datetime import date

import pytest

from machu_picchu_monitor.rules import RuleError, load_rules, parse_rules


def test_parse_available_and_threshold_rules() -> None:
    rules = parse_rules(
        [
            {"name": "agg", "type": "available", "date": "2026-08-19", "route": "Circuit 2A"},
            {
                "name": "slot watch",
                "type": "below_threshold",
                "date": "2026-08-19",
                "route": "1C",
                "slot": "8:00",
                "threshold": 20,
            },
        ]
    )
    assert rules[0].type == "available"
    assert rules[0].route == "2A"  # normalized
    assert rules[0].visit_date == date(2026, 8, 19)

    assert rules[1].type == "below_threshold"
    assert rules[1].slot == "08:00:00"  # normalized to HH:MM:SS
    assert rules[1].threshold == 20


def test_increase_is_accepted_as_alias_for_available() -> None:
    rules = parse_rules([{"type": "increase", "date": "2026-08-19", "route": "2A"}])
    assert rules[0].type == "available"


def test_below_threshold_requires_threshold() -> None:
    with pytest.raises(RuleError):
        parse_rules([{"type": "below_threshold", "date": "2026-08-19", "route": "1C"}])


def test_invalid_type_and_slot_rejected() -> None:
    with pytest.raises(RuleError):
        parse_rules([{"type": "nonsense", "date": "2026-08-19", "route": "1C"}])
    with pytest.raises(RuleError):
        parse_rules(
            [
                {
                    "type": "below_threshold",
                    "date": "2026-08-19",
                    "route": "1C",
                    "slot": "8pm",
                    "threshold": 5,
                }
            ]
        )


def test_inline_json_takes_precedence_over_file(tmp_path) -> None:
    rules_file = tmp_path / "rules.json"
    rules_file.write_text('[{"type":"increase","date":"2026-08-19","route":"2A"}]')
    inline = '[{"type":"increase","date":"2026-08-20","route":"3B"}]'
    rules = load_rules(inline_json=inline, rules_file=rules_file)
    assert len(rules) == 1
    assert rules[0].route == "3B"
    assert rules[0].visit_date == date(2026, 8, 20)


def test_missing_file_returns_empty(tmp_path) -> None:
    assert load_rules(inline_json="", rules_file=tmp_path / "absent.json") == []
