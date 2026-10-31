from datetime import date
from travel_brief.flight_engine import FlightEngine


def test_flight_engine_load_destinations():
    engine = FlightEngine()
    assert len(engine.destinations) == 10
    assert "FUK" in engine.destinations
    assert "DAD" in engine.destinations
    assert "BKK" in engine.destinations


def test_weekend_slots_generation():
    engine = FlightEngine()
    ref = date(2026, 11, 1)
    slots = engine.generate_weekend_slots(ref_date=ref, months_ahead=2)
    assert len(slots) > 0
    for dep, ret, dep_dow, ret_dow, nights in slots:
        assert nights == 3
        assert dep_dow in ["목", "금"]
        assert ret_dow in ["일", "월"]
        assert (ret - dep).days == 3


def test_search_deals():
    engine = FlightEngine()
    deals = engine.search_deals(dest_code="FUK", months_ahead=3)
    assert len(deals) > 0
    for d in deals:
        assert d.destination_code == "FUK"
        assert d.price_krw > 0
        assert d.airline is not None


def test_6month_matrix():
    engine = FlightEngine()
    matrix = engine.get_6month_matrix()
    assert len(matrix) == 10
    for code, months in matrix.items():
        assert len(months) >= 5
        for month_str, deal in months.items():
            assert deal.destination_code == code
            assert deal.departure_date.startswith(month_str)


def test_search_deals_invalid_dest_code():
    engine = FlightEngine()
    deals = engine.search_deals(dest_code="INVALID_CODE")
    assert deals == []
