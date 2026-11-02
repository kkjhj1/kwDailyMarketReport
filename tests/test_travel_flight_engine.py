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


def test_departure_time_slot_realism():
    engine = FlightEngine()
    deals = engine.search_deals(months_ahead=6)
    assert len(deals) > 0

    thu_deals = [d for d in deals if d.departure_day_of_week == "목"]
    fri_deals = [d for d in deals if d.departure_day_of_week == "금"]

    # All Thursday flights must recommend 2-day vacation
    for d in thu_deals:
        assert d.departure_time_slot == "오전 08:30~10:30 (목·금 연차 2일 추천)"

    # Friday flights must be categorized realistically
    valid_fri_slots = {
        "오전 08:30~10:30 (금요일 전일 연차 · 알찬 3박4일)",
        "오후 14:00~16:00 (금요일 오후 반차 추천)",
        "야간 19:30~21:00 (퇴근 후 출발 · 연차 절약)",
    }
    fri_slots_found = set()
    for d in fri_deals:
        assert d.departure_time_slot in valid_fri_slots
        fri_slots_found.add(d.departure_time_slot)

    # Across 6 months of destinations, all 3 Friday slot types must be generated
    assert len(fri_slots_found) == 3, f"Expected all 3 Friday slots, got {fri_slots_found}"


def test_peak_holidays_never_super_deal_and_surge_applied():
    engine = FlightEngine()
    ref = date(2026, 11, 2)
    deals = engine.search_deals(months_ahead=6, ref_date=ref)
    by_key = {(d.destination_code, d.departure_date): d for d in deals}

    # 1. Christmas Eve (2026-12-24 ~ 12-27) must surge & NEVER be super deal
    cxr_xmas = by_key[("CXR", "2026-12-24")]
    fuk_xmas = by_key[("FUK", "2026-12-24")]
    assert cxr_xmas.is_peak_holiday is True
    assert cxr_xmas.is_super_deal is False
    assert cxr_xmas.price_krw >= 600000
    assert cxr_xmas.price_diff_pct > 40.0

    assert fuk_xmas.is_peak_holiday is True
    assert fuk_xmas.is_super_deal is False
    assert fuk_xmas.price_krw >= 390000

    # 2. Return-date overlap with 2027 Seollal (2027-02-04 Thu ~ 02-07 Sun)
    dad_seollal = by_key[("DAD", "2027-02-04")]
    assert dad_seollal.is_peak_holiday is True
    assert dad_seollal.is_super_deal is False
    assert "설날" in dad_seollal.holiday_name


def test_statutory_tax_fuel_floor_and_carrier_mapping():
    engine = FlightEngine()
    deals = engine.search_deals(months_ahead=6, ref_date=date(2026, 11, 2))
    assert len(deals) > 0
    for d in deals:
        dest = engine.destinations[d.destination_code]
        assert d.price_krw >= dest.flight_min_floor_krw
        assert d.price_krw >= 210000
        assert d.price_max_krw >= d.price_min_krw + 30000
        assert "만원대" in d.price_band_str
        # Carrier mapping must never fall back to broken generic placeholders
        assert d.airline not in ("국적항공사", "LCC특가")
