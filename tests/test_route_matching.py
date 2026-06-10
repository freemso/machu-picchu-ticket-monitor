from machu_picchu_monitor.route_matching import normalize_route_code, route_code_from_text


def test_normalize_route_code_variants() -> None:
    assert normalize_route_code("2A") == "2A"
    assert normalize_route_code("Circuit 2B") == "2B"
    assert normalize_route_code("Circuito 3 - b") == "3B"
    assert normalize_route_code("Ruta 1-C: Portada Intipunku") == "1C"


def test_route_code_from_official_text() -> None:
    assert route_code_from_text("Ruta 2-A: Clásico Diseñada") == "2A"
    assert route_code_from_text("Ruta 3-B: Realeza diseñada") == "3B"
    assert route_code_from_text("No matching route") is None
