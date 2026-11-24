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


# ----------------------------------------------------------------------
# 8 regions / ~115 ICN direct cities / long-haul trip profile
# ----------------------------------------------------------------------
def test_catalog_8_regions_115_cities_and_sea_seattle_disjoint():
    from travel_brief.city_catalog import (
        CITY_THEMES,
        REGION_CODES,
        REGION_LABELS,
        CityCatalog,
        city_region_theme,
        is_region_code,
    )

    engine = FlightEngine()
    cat = CityCatalog(engine.destinations)
    codes = cat.all_codes()
    assert 110 <= len(codes) <= 120 and len(codes) == len(set(codes))
    assert REGION_CODES == ("JP", "TWHK", "CNMN", "SEASIA", "PAC", "OCE", "NA", "EU")
    assert REGION_LABELS["SEASIA"] == "🌴 동남아" and REGION_LABELS["NA"] == "🗽 북미"
    assert not set(REGION_CODES) & set(codes)  # region codes never collide with city codes
    assert not is_region_code("SEA") and is_region_code("SEASIA") and not is_region_code("ALL")
    assert cat.region_of("SEA") == "NA" and cat.resolve("시애틀").code == "SEA"
    assert all(n >= 1 for n in cat.region_city_counts().values())
    for code in codes:
        region, theme = city_region_theme(code)
        assert region in REGION_CODES and theme in CITY_THEMES, code
    required = {"YVR", "SEA", "LAX", "SFO", "LAS", "JFK", "EWR", "BOS", "IAD", "ORD", "ATL", "DFW", "IAH",
                "DTW", "MSP", "DEN", "YYZ", "MEX", "PRG", "ZAG", "MEL", "BNE", "AKL", "ROR", "RMQ"}
    assert required <= set(codes)
    assert set(engine.destinations) <= set(codes)
    assert all(m.region_code in REGION_CODES and m.theme for m in engine.destinations.values())
    # sparse (region, theme) has no city -> Track A falls back to the region ALL theme
    assert cat.cities_in_region("EU", "RESORT") == [] and cat.cities_in_region("EU")
    assert set(cat.cities_in_region("SEASIA", "RESORT")) >= {"CXR", "DPS", "CEB"}
    assert "DAD" in cat.cities_in_region("SEASIA", "RESORT") and "DAD" in cat.cities_in_region("SEASIA", "CITY")
    assert cat.cities_in_region("JP")[:3] == ["NRT", "KIX", "FUK"]


def test_long_haul_profile_slots_and_copy():
    from travel_brief import city_catalog as cc
    from travel_brief.city_catalog import CityCatalog, build_fallback_meta
    from travel_brief.models import format_trip_length

    for code in ("CDG", "JFK", "SYD", "HNL", "CGK", "YVR"):
        meta = build_fallback_meta(code)
        assert meta.is_long_haul and meta.trip_profile == "LONG", code
    for code in ("CTS", "CEB", "HKG", "SPN", "ULN"):
        assert not build_fallback_meta(code).is_long_haul, code
    engine = FlightEngine()
    assert engine.destinations["DPS"].is_long_haul  # 7.0h curated -> long-haul copy
    assert not engine.destinations["FUK"].is_long_haul
    ref = date(2026, 11, 16)
    extra = CityCatalog(engine.destinations).destinations_for(["CDG", "CTS"])
    nights: dict = {}
    for d in engine.search_deals(extra_destinations=extra, ref_date=ref):
        nights.setdefault(d.destination_code, set()).add(d.nights)
    assert nights == {"CDG": {5}, "CTS": {3}}  # extra_destinations replace the curated set
    # curated JSON cities keep the legacy 3N weekend contract (incl. the 7.0h DPS)
    assert {d.nights for d in engine.search_deals(ref_date=ref)} == {3}
    long_slots = engine.generate_long_haul_slots(ref_date=ref, months_ahead=2)
    assert long_slots and all(n == 5 and dow in ("목", "금") for _d, _r, dow, _rd, n in long_slots)
    assert format_trip_length(3) == "3박 4일"
    assert format_trip_length(5, True) == "5박 6일 · 장거리 5박 이상 권장"
    # legacy budget profile keys stay intact; NA is added
    assert {"HAWAII", "PAC", "OCE", "EU", "NA", "SEA", "JP", "CN", "HKMO", "TW", "MN"} <= set(cc._REGION_PROFILES)


def test_kr_holidays_leave_calculator_and_office_slots():
    from datetime import date
    from travel_brief.city_catalog import CityCatalog
    from travel_brief.flight_engine import (
        KR_PUBLIC_HOLIDAYS_2026_2028,
        calc_required_leave_days,
        generate_watch_slots,
    )

    # Normal Fri->Sun (2N): 18:00+ = 0.0, 14:30..17:59 = 0.5, <14:30 or missing = 1.0
    fri, sun = date(2026, 12, 4), date(2026, 12, 6)
    assert calc_required_leave_days(fri, sun, "18:00") == 0.0
    assert calc_required_leave_days(fri, sun, "17:59") == 0.5
    assert calc_required_leave_days(fri, sun, "14:30") == 0.5
    assert calc_required_leave_days(fri, sun, "14:29") == 1.0
    assert calc_required_leave_days(fri, sun, "") == 1.0
    # Holiday-anchored 4N (Thu->Mon where Mon 2027-03-01 is Samiljeol): Thu 18:30 + Fri work = 1.0 leave
    assert date(2027, 3, 1) in KR_PUBLIC_HOLIDAYS_2026_2028
    assert date(2026, 7, 17) in KR_PUBLIC_HOLIDAYS_2026_2028
    assert date(2027, 5, 3) in KR_PUBLIC_HOLIDAYS_2026_2028
    assert date(2027, 7, 19) in KR_PUBLIC_HOLIDAYS_2026_2028
    assert calc_required_leave_days(date(2027, 2, 25), date(2027, 3, 1), "18:30") == 1.0
    # 2029 fallback (outside 2026-2028 holiday table) works without error using weekends only
    assert calc_required_leave_days(date(2029, 1, 5), date(2029, 1, 7), "19:00") == 0.0

    office_slots = generate_watch_slots(ref_date=date(2027, 2, 18), horizon_months=1, weekday_option="OFFICE")
    assert any((d, r, n) == (date(2027, 2, 25), date(2027, 3, 1), 4) for d, r, _, _, n in office_slots)
    assert any((d, r, n) == (date(2027, 2, 26), date(2027, 2, 28), 2) for d, r, _, _, n in office_slots)

    # Long-haul cities are excluded from OFFICE watch slots
    engine = FlightEngine(live_naver=False)
    cdg_meta = CityCatalog(engine.destinations).get("CDG").meta
    assert engine.search_watch_deals("CDG", horizon_months=3, weekday_option="OFFICE", custom_destinations={"CDG": cdg_meta}) == []
