"""Seed catalogue + weekend recommendation for Kids Compass.

Seed policy (fact safety):
- Links are Naver search URLs, so they never 404 even when an official site moves.
- Fees / booking-open rules are typical patterns; every card tells users to confirm on the official notice.
- Hospital seeds carry NO invented phone numbers or opening hours. Until an operator imports the official
  E-gen (응급의료포털) list, tel is empty (cards fall back to 119 안내) and last_verified_at = UNVERIFIED.
"""
from __future__ import annotations
import calendar
from datetime import date, datetime, timedelta
from typing import Optional
from urllib.parse import quote

from kids_compass.models import AgeGroup, BookingEvent, ExperienceCategory, ExperienceItem, MoonlightHospital

UNVERIFIED = "미검증(운영 전 E-gen 확인 필요)"
EGEN_URL = "https://www.e-gen.or.kr/"
NIGHT_PHARMACY_LABEL = "인근 휴일지킴이·공공심야약국"


def naver_search(query: str) -> str:
    return "https://search.naver.com/search.naver?query=" + quote(query)


def naver_map(query: str) -> str:
    return "https://map.naver.com/p/search/" + quote(query)


def _exp(exp_id: str, title: str, age: AgeGroup, cat: ExperienceCategory, sido: str, sigungu: str, venue: str,
         fee_type: str, fee_detail: str, indoor: bool, stroller: bool, nursing: bool, booking: bool,
         open_rule: str, key_points: str) -> ExperienceItem:
    return ExperienceItem(
        exp_id=exp_id, title=title, age_group=age, category=cat, region_sido=sido, region_sigungu=sigungu,
        venue_name=venue, fee_type=fee_type, fee_detail=fee_detail, is_indoor=indoor, stroller_accessible=stroller,
        nursing_room=nursing, booking_required=booking, booking_url=naver_search(f"{venue} 예약"),
        booking_open_rule=open_rule, key_points=key_points,
    )


A, C = AgeGroup, ExperienceCategory
SEED_EXPERIENCES: list[ExperienceItem] = [
    # ── INFANT (0~3세) ──
    _exp("nmk_kids", "국립중앙박물관 어린이박물관 영유아존", A.INFANT, C.PLAY, "서울", "용산구", "국립중앙박물관 어린이박물관",
         "무료", "무료 (회차별 사전예약)", True, True, True, True, "회차제 사전예약", "오감 놀이형 전시 · 유아차 이동 편함"),
    _exp("kidscafe_ttuk", "서울형 키즈카페 뚝섬자벌레점", A.INFANT, C.PLAY, "서울", "광진구", "서울형 키즈카페 뚝섬자벌레점",
         "유료", "저렴한 회차 이용료 (공식 공지 확인)", True, True, True, True, "회차제 사전예약", "한강 뷰 실내 놀이터 · 2시간 회차"),
    _exp("kidscafe_mokdong", "서울형 키즈카페 시립목동점", A.INFANT, C.PLAY, "서울", "양천구", "서울형 키즈카페 시립목동점",
         "유료", "저렴한 회차 이용료 (공식 공지 확인)", True, True, True, True, "회차제 사전예약", "대형 실내 놀이공간 · 보호자 동반"),
    _exp("botanic_park", "서울식물원 온실 & 잔디마당", A.INFANT, C.NATURE, "서울", "강서구", "서울식물원",
         "유료", "온실 유료 · 공원 무료", True, True, True, False, "현장 발권", "비 와도 온실 산책 · 잔디마당 피크닉"),
    _exp("gg_kids_nest", "경기도어린이박물관 아기둥지", A.INFANT, C.PLAY, "경기", "용인시", "경기도어린이박물관",
         "유료", "어린이·보호자 소액 입장료", True, True, True, True, "회차제 사전예약", "36개월 이하 전용 놀이공간"),
    # ── PRESCHOOL (4~7세) ──
    _exp("gwangnaru_119", "광나루 119 안전체험관 소방체험", A.PRESCHOOL, C.SAFETY, "서울", "광진구", "광나루안전체험관",
         "무료", "무료 (사전예약 필수)", True, True, False, True, "월 단위 예약 오픈", "소방관 체험 · 지진/화재 대피 실습"),
    _exp("boramae_safety", "보라매 안전체험관", A.PRESCHOOL, C.SAFETY, "서울", "동작구", "보라매안전체험관",
         "무료", "무료 (사전예약 필수)", True, True, False, True, "월 단위 예약 오픈", "어린이 안전교실 · 119 신고 연습"),
    _exp("seoulforest_insect", "서울숲 곤충식물원 & 나비정원", A.PRESCHOOL, C.NATURE, "서울", "성동구", "서울숲 곤충식물원",
         "무료", "무료", False, True, False, False, "현장 입장", "나비·곤충 관찰 · 넓은 숲 산책로"),
    _exp("ncsm_kids", "국립어린이과학관 (혜화)", A.PRESCHOOL, C.SCIENCE, "서울", "종로구", "국립어린이과학관",
         "유료", "소액 관람료 (회차제)", True, True, True, True, "주 단위 회차 예약", "손으로 만지는 과학 놀이 · 천체투영관"),
    _exp("forest_namsan", "남산 유아숲체험원", A.PRESCHOOL, C.NATURE, "서울", "중구", "남산 유아숲체험원",
         "무료", "무료 (단체·개인 프로그램 예약)", False, False, False, True, "프로그램별 예약", "숲 놀이터 · 숲해설 프로그램"),
    _exp("forest_dream", "북서울꿈의숲 유아숲체험원", A.PRESCHOOL, C.NATURE, "서울", "강북구", "북서울꿈의숲 유아숲체험원",
         "무료", "무료 (프로그램 예약)", False, False, False, True, "프로그램별 예약", "흙·나무 자연놀이 · 생태 관찰"),
    # ── ELEMENTARY (8~13세) ──
    _exp("bok_money", "한국은행 화폐박물관 어린이 경제교실", A.ELEMENTARY, C.ECONOMY, "서울", "중구", "한국은행 화폐박물관",
         "무료", "무료 (교실 사전예약)", True, True, False, True, "월 단위 예약 오픈", "화폐의 역사 · 위조지폐 구별 체험"),
    _exp("krx_finance", "한국거래소(KRX) 금융홍보관", A.ELEMENTARY, C.ECONOMY, "서울", "영등포구", "한국거래소 홍보관",
         "무료", "무료 (견학 사전신청)", True, True, False, True, "견학 사전신청", "주식시장 원리 · 금융 교육"),
    _exp("gwacheon_planet", "국립과천과학관 천체투영관", A.ELEMENTARY, C.SCIENCE, "경기", "과천시", "국립과천과학관",
         "유료", "상설관 + 천체투영관 별도 요금", True, True, True, True, "회차제 사전예약", "돔 스크린 별자리 · 대형 과학 전시"),
    _exp("robot_ai_dobong", "서울로봇인공지능과학관 (도봉)", A.ELEMENTARY, C.SCIENCE, "서울", "도봉구", "서울로봇인공지능과학관",
         "유료", "관람료 (공식 공지 확인)", True, True, True, True, "회차제 사전예약", "로봇·AI 체험 전시"),
    # ── ALL (전 연령) ──
    _exp("childrens_grand_park", "서울어린이대공원", A.ALL, C.NATURE, "서울", "광진구", "서울어린이대공원",
         "무료", "입장 무료 (일부 시설 유료)", False, True, True, False, "현장 입장", "동물원 · 넓은 잔디 · 유아차 산책"),
    _exp("worldcup_park", "월드컵공원 하늘공원 생태 산책", A.ALL, C.NATURE, "서울", "마포구", "월드컵공원",
         "무료", "무료", False, False, False, False, "현장 입장", "억새·생태 탐방 · 노을 피크닉"),
]

# Booking-open rules → concrete D-Day events. ("monthly", day, hh:mm) or ("weekly", weekday 0=Mon, hh:mm)
SEED_BOOKING_RULES: list[dict] = [
    {"booking_id": 1, "exp_id": "gwangnaru_119", "title": "광나루 119 안전체험관 주말 소방체험 예약 오픈",
     "rule": ("monthly", 1, "10:00"), "target": "익월 주말 회차", "memo": "인기 회차 수 분 내 마감"},
    {"booking_id": 2, "exp_id": "ncsm_kids", "title": "국립어린이과학관 상설전시·천체투영관 예약 오픈",
     "rule": ("weekly", 0, "10:00"), "target": "차주 회차", "memo": "천체투영관 회차 별도 예약"},
    {"booking_id": 3, "exp_id": "bok_money", "title": "한국은행 화폐박물관 주말 경제교실 예약 오픈",
     "rule": ("monthly", 15, "10:00"), "target": "익월 주말 교실", "memo": "초등 대상 · 보호자 동반"},
    {"booking_id": 4, "exp_id": "kidscafe_ttuk", "title": "서울형 키즈카페 주말 회차 예약 오픈",
     "rule": ("weekly", 2, "10:00"), "target": "주말 회차", "memo": "지점별 오픈 시각 상이"},
    {"booking_id": 5, "exp_id": "gwacheon_planet", "title": "국립과천과학관 천체투영관 예약 오픈",
     "rule": ("monthly", 25, "10:00"), "target": "익월 회차", "memo": "주말 오후 회차 조기 마감"},
    {"booking_id": 6, "exp_id": "boramae_safety", "title": "보라매 안전체험관 어린이 체험 예약 오픈",
     "rule": ("monthly", 1, "09:00"), "target": "익월 회차", "memo": "유아 단체·가족 회차 구분"},
]


def _next_open(rule: tuple, today: date) -> datetime:
    kind, n, hhmm = rule
    hh, mm = (int(x) for x in hhmm.split(":"))
    if kind == "weekly":
        d = today + timedelta(days=(n - today.weekday()) % 7)
    else:
        y, m = today.year, today.month
        if today.day > n:
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        d = date(y, m, min(n, calendar.monthrange(y, m)[1]))
    return datetime(d.year, d.month, d.day, hh, mm)


def build_booking_events(today: Optional[date] = None) -> list[BookingEvent]:
    """Next open datetime (>= today) for each rule. Dates are computed, never hard-coded, so seeds don't go stale."""
    today = today or date.today()
    out = []
    for r in SEED_BOOKING_RULES:
        exp = next(e for e in SEED_EXPERIENCES if e.exp_id == r["exp_id"])
        out.append(BookingEvent(
            booking_id=r["booking_id"], exp_id=r["exp_id"], title=r["title"],
            open_datetime=_next_open(r["rule"], today).strftime("%Y-%m-%d %H:%M"),
            target_date_range=r["target"], booking_url=exp.booking_url, memo=r["memo"],
        ))
    return out


SEED_BOOKING_EVENTS: list[BookingEvent] = build_booking_events()


def _hosp(hid: str, name: str, sido: str, sigungu: str) -> MoonlightHospital:
    return MoonlightHospital(
        hospital_id=hid, name=name, region_sido=sido, region_sigungu=sigungu, address=f"{sido} {sigungu}",
        tel="", weekday_hours="야간 (기관별 상이)", weekend_hours="기관별 상이",
        holiday_hours="기관별 상이", paired_pharmacy=NIGHT_PHARMACY_LABEL, paired_pharmacy_tel="",
        map_url=naver_map(name), last_verified_at=UNVERIFIED,
    )


SEED_HOSPITALS: list[MoonlightHospital] = [
    _hosp("sel_seongbuk_woori", "우리들소아과의원", "서울", "성북구"),
    _hosp("sel_seocho_gomdori", "연세곰돌이소아청소년과", "서울", "서초구"),
    _hosp("sel_nowon_savekids", "세이브키즈소아과", "서울", "노원구"),
    _hosp("sel_yongsan_sowha", "소화아동병원", "서울", "용산구"),
    _hosp("ggi_bundang_cha", "분당차병원 소아응급", "경기", "성남시"),
    _hosp("ggi_suwon_ajou", "아주맑은소아과", "경기", "수원시"),
    # Region-level map searches (not specific facilities) until the official list is imported
    _hosp("ggi_goyang_search", "고양시 달빛어린이병원 찾기", "경기", "고양시"),
    _hosp("ggi_bucheon_search", "부천시 달빛어린이병원 찾기", "경기", "부천시"),
    _hosp("icn_search", "인천 달빛어린이병원 찾기", "인천", "전체"),
]


def recommend_weekend_picks(db, age_group: AgeGroup, region_sido: Optional[str], weather_rain: bool = False,
                            fine_dust_bad: bool = False, limit: int = 3) -> list[ExperienceItem]:
    """Weather-aware 3-Pick.
    - rain / bad dust: indoor ONLY (returns fewer than `limit` rather than suggest an outdoor spot)
    - sunny: alternate outdoor (nature first) and indoor
    Region matches come first; other regions back-fill when the region alone is too thin.
    """
    indoor_only = weather_rain or fine_dust_bad
    pool = db.list_experiences(age_group=age_group, region_sido=region_sido, indoor_only=indoor_only)
    if region_sido and len(pool) < limit:
        seen = {e.exp_id for e in pool}
        pool += [e for e in db.list_experiences(age_group=age_group, indoor_only=indoor_only) if e.exp_id not in seen]
    # Exact age-group items before generic ALL items
    pool.sort(key=lambda e: e.age_group == AgeGroup.ALL)
    if indoor_only:
        return pool[:limit]
    outdoor = sorted((e for e in pool if not e.is_indoor), key=lambda e: e.category != ExperienceCategory.NATURE)
    indoor = [e for e in pool if e.is_indoor]
    picks: list[ExperienceItem] = []
    while len(picks) < limit and (outdoor or indoor):
        for bucket in (outdoor, indoor):
            if bucket and len(picks) < limit:
                picks.append(bucket.pop(0))
    return picks


def recommend_by_category(db, category: ExperienceCategory, region_sido: Optional[str] = None,
                          limit: int = 5) -> list[ExperienceItem]:
    return db.list_experiences(category=category, region_sido=region_sido)[:limit]


def seed_all(db, today: Optional[date] = None) -> dict[str, int]:
    from kids_compass.fever_engine import DEFAULT_MEDICINES

    return {
        "medicines": db.upsert_medicines(DEFAULT_MEDICINES.values()),
        "experiences": db.upsert_experiences(SEED_EXPERIENCES),
        "bookings": db.upsert_booking_events(build_booking_events(today)),
        "hospitals": db.upsert_hospitals(SEED_HOSPITALS),
    }
