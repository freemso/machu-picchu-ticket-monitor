from datetime import date

from machu_picchu_monitor.models import AvailabilityRecord
from machu_picchu_monitor.storage import SQLiteStorage


def test_record_availability_alerts_only_on_increase(tmp_path) -> None:
    storage = SQLiteStorage(tmp_path / "availability.sqlite3")
    storage.init()

    first = [
        AvailabilityRecord(
            visit_date=date(2026, 8, 24),
            route="2A",
            route_name="Ruta 2-A",
            quantity=0,
            source="test",
        )
    ]
    assert storage.record_availability(first) == []

    same = [
        AvailabilityRecord(
            visit_date=date(2026, 8, 24),
            route="2A",
            route_name="Ruta 2-A",
            quantity=0,
            source="test",
        )
    ]
    assert storage.record_availability(same) == []

    increase = [
        AvailabilityRecord(
            visit_date=date(2026, 8, 24),
            route="2A",
            route_name="Ruta 2-A",
            quantity=4,
            source="test",
        )
    ]
    changes = storage.record_availability(increase)
    assert len(changes) == 1
    assert changes[0].old_quantity == 0
    assert changes[0].new_quantity == 4

    decrease = [
        AvailabilityRecord(
            visit_date=date(2026, 8, 24),
            route="2A",
            route_name="Ruta 2-A",
            quantity=2,
            source="test",
        )
    ]
    assert storage.record_availability(decrease) == []
    assert storage.list_current()[0]["availability"] == 2
    assert len(storage.list_history()) == 3
    storage.close()


def test_record_slot_tracks_previous_and_history(tmp_path) -> None:
    storage = SQLiteStorage(tmp_path / "availability.sqlite3")
    storage.init()

    common = dict(
        visit_date=date(2026, 8, 19),
        route="1C",
        route_name="Ruta 1-C",
        slot="08:00:00",
        capacity=30,
    )
    assert storage.record_slot(available=16, **common) is None  # first time
    assert storage.record_slot(available=16, **common) == 16  # unchanged
    assert storage.record_slot(available=9, **common) == 16  # changed

    current = storage.list_slot_current()
    assert len(current) == 1
    assert current[0]["available"] == 9
    assert current[0]["capacity"] == 30
    storage.close()
