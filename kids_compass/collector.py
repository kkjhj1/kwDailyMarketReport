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

from kids_compass.models import (
    AgeGroup,
    BookingEvent,
    ExperienceCategory,
    ExperienceItem,
    MilestoneDefinition,
    MoonlightHospital,
    PregnancyBenefit,
)
from kids_compass.food_safety import build_food_catalog_seed_rows
from kids_compass.home_play import build_catalog_seed_rows
from kids_compass.storage.db import milestone_window as _child_milestone_window

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
        booking_open_rule=open_rule, key_points=key_points, operating_hours=OPERATING_HOURS.get(exp_id, ""),
    )


# 운영시간·휴관일 (🕘 필수 표출). Seasonal/holiday changes happen — the card footer says to check the official notice.
OPERATING_HOURS: dict[str, str] = {
    "nmk_kids": "10:00~17:50 (1일 5회차 예약제 · 매주 월 휴관)",
    "kidscafe_ttuk": "09:30~17:30 (3회차 운영 · 매주 월 휴관)",
    "kidscafe_mokdong": "09:30~17:30 (3회차 운영 · 매주 월 휴관)",
    "botanic_park": "09:30~18:00 (동절기 17:00 · 매주 월 휴관 · 열린숲 상시)",
    "gg_kids_nest": "10:00~17:30 (회차제 · 매주 월 휴관)",
    "gwangnaru_119": "09:00~17:00 (회차제 3회 운영 · 매주 월 휴관)",
    "boramae_safety": "09:00~17:00 (회차제 운영 · 매주 월 휴관)",
    "seoulforest_insect": "10:00~17:00 (입장마감 16:30 · 매주 월 휴관)",
    "ncsm_kids": "09:30~17:30 (입장마감 16:30 · 매주 월 휴관)",
    "forest_namsan": "상시 개방 (연중무휴 · 일몰 시까지 권장)",
    "forest_dream": "상시 개방 (공원 24시간 · 유아숲 일몰 시까지)",
    "forest_dream_free": "상시 개방 (숲해설은 평일 사전예약제)",
    "grandpark_zoo": "05:00~22:00 (동물원 10:00~17:00 · 연중무휴)",
    "bok_money": "10:00~17:00 (입장마감 16:30 · 매주 월 휴관)",
    "krx_finance": "09:30~17:00 (주말 프로그램 사전예약 · 공휴일 휴관)",
    "gwacheon_planet": "09:30~17:30 (입장마감 16:30 · 매주 월 휴관)",
    "robot_ai_dobong": "09:30~17:30 (입장마감 16:30 · 매주 월 휴관)",
    "childrens_grand_park": "05:00~22:00 (동물원 10:00~17:00 · 연중무휴)",
    "worldcup_park": "상시 개방 (하늘공원은 일몰 무렵 폐장 · 월별 상이)",
    "nmk_main": "10:00~18:00 (수·토 21:00까지 · 1/1, 설·추석 휴관)",
    "preg_botanic": "09:30~18:00 (열린숲 잔디마당 24시간 무료)",
    "preg_kna": "09:00~18:00 (동절기 17:00 · 매주 일·월 휴관 · 사전예약제)",
    "preg_namsan": "상시 개방 (연중무휴 · 야외 산책로)",
    # ── 서울 추가 ──
    "seoul_sangsang": "10:00~18:00 (회차제 · 매주 월 휴관)",
    "air_museum": "10:00~18:00 (입장마감 17:00 · 매주 월 휴관)",
    "seoul_science": "09:30~17:30 (입장마감 17:00 · 매주 월 휴관)",
    "nfm_kids": "10:00~17:00 (회차제 · 매주 월 휴관)",
    "gildong_eco": "10:00~17:00 (탐방 사전예약 · 매주 월 휴관)",
    # ── 경기 ──
    "gg_gwanggyo_lake": "상시 개방 (연중무휴 · 일몰 후 산책 시 조명 구간 이용)",
    "gg_agri_museum": "10:00~18:00 (입장마감 17:00 · 매주 월 휴관)",
    "gg_yuldong_park": "상시 개방 (연중무휴)",
    "gg_shingu_garden": "09:00~18:00 (동절기 단축 · 휴원일 공식 공지 확인)",
    "gg_provincial_museum": "10:00~18:00 (입장마감 17:00 · 매주 월 휴관)",
    "gg_goyang_kids": "10:00~18:00 (회차제 · 매주 월 휴관)",
    "gg_ilsan_lake": "상시 개방 (연중무휴)",
    "gg_comics_museum": "10:00~18:00 (입장마감 17:00 · 매주 월 휴관)",
    "gg_bucheon_eco": "09:30~18:00 (동절기 17:00 · 매주 월 휴관)",
    "gg_seoul_grandpark": "09:00~18:00 (동절기 17:00 · 연중무휴)",
    "gg_gwacheon_kids": "09:30~17:30 (회차제 · 매주 월 휴관)",
    "gg_siheung_gaetgol": "상시 개방 (연중무휴 · 일부 시설 운영시간 별도)",
    # ── 인천 ──
    "icn_central_park": "상시 개방 (연중무휴 · 수상택시 등 시설 별도)",
    "icn_letters_museum": "10:00~18:00 (입장마감 17:30 · 매주 월 휴관)",
    "icn_city_museum": "09:00~18:00 (입장마감 17:00 · 매주 월 휴관)",
    "icn_kids_science": "09:30~17:30 (회차제 · 매주 월 휴관)",
    "icn_butterfly_park": "09:00~18:00 (동절기 단축 · 휴관일 공식 공지 확인)",
    "icn_grand_park": "05:00~23:00 (어린이동물원 09:00~18:00 · 연중무휴)",
    "icn_sorae_wetland": "상시 개방 (전시관 10:00~18:00 · 매주 월 휴관)",
    "icn_seaside_park": "상시 개방 (레일바이크 등 유료시설 별도)",
    "icn_kids_museum": "10:00~17:00 (회차제 · 매주 월 휴관)",
    "icn_sudoguksan": "09:00~18:00 (입장마감 17:30 · 매주 월 휴관)",
}

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
    # 현장직행(booking_required=False) — Thursday dispatch for 4~7세 only uses walk-in spots
    _exp("forest_dream_free", "북서울꿈의숲 유아숲 놀이터 & 잔디광장", A.PRESCHOOL, C.NATURE, "서울", "강북구",
         "북서울꿈의숲", "무료", "무료 (숲해설 프로그램만 별도 예약)", False, True, False, False, "현장 입장",
         "밧줄·통나무 자연놀이 · 대형 잔디광장"),
    _exp("grandpark_zoo", "어린이대공원 동물원 & 잔디마당", A.PRESCHOOL, C.NATURE, "서울", "광진구", "서울어린이대공원",
         "무료", "입장 무료 (일부 시설 유료)", False, True, True, False, "현장 입장", "동물 관찰 · 넓은 잔디마당 뛰놀기"),
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
    _exp("nmk_main", "국립중앙박물관 상설전시관", A.ALL, C.PLAY, "서울", "용산구", "국립중앙박물관",
         "무료", "상설전시 무료 (특별전 별도)", True, True, True, False, "현장 입장", "비 와도 OK · 넓은 실내 동선"),
    # ── PREGNANCY (임신기 태교·산책: 평지 위주) ──
    _exp("preg_botanic", "서울식물원 주제정원 & 온실", A.PREGNANCY, C.NATURE, "서울", "강서구", "서울식물원 주제정원",
         "유료", "온실·주제정원 유료 · 열린숲 무료", True, True, True, False, "현장 발권",
         "평지 산책 · 온실 피톤치드 · 수유·휴게시설"),
    _exp("preg_kna", "국립수목원 전나무숲길", A.PREGNANCY, C.NATURE, "경기", "포천시", "국립수목원",
         "유료", "소액 입장료 (사전예약제)", False, True, True, True, "방문일 사전예약", "피톤치드 힐링 · 평지 숲 산책로"),
    _exp("preg_namsan", "남산 야외식물원 태교 숲길", A.PREGNANCY, C.NATURE, "서울", "용산구", "남산 야외식물원",
         "무료", "무료", False, False, False, False, "현장 입장", "완만한 숲길 · 연못·약용식물원 산책"),
    # ── 서울 추가 (기존 서울 추천 순서를 바꾸지 않도록 뒤에 덧붙임) ──
    _exp("seoul_sangsang", "서울상상나라 (어린이대공원 내)", A.INFANT, C.PLAY, "서울", "광진구", "서울상상나라",
         "유료", "소액 입장료 (공식 공지 확인)", True, True, True, True, "회차제 사전예약", "오감·역할놀이 전시 · 영유아 놀이존"),
    _exp("air_museum", "국립항공박물관", A.ELEMENTARY, C.SCIENCE, "서울", "강서구", "국립항공박물관",
         "무료", "상설전시 무료 (체험관 별도 예약·유료)", True, True, True, False, "현장 입장 (체험만 예약)",
         "실물 항공기 전시 · 비행 원리 체험"),
    _exp("seoul_science", "서울시립과학관 (노원)", A.ELEMENTARY, C.SCIENCE, "서울", "노원구", "서울시립과학관",
         "유료", "소액 관람료 (공식 공지 확인)", True, True, True, True, "회차제 사전예약", "생활 속 과학 전시 · 탐구형 체험"),
    _exp("nfm_kids", "국립민속박물관 어린이박물관", A.PRESCHOOL, C.PLAY, "서울", "종로구", "국립민속박물관 어린이박물관",
         "무료", "무료 (회차별 사전예약)", True, True, True, True, "회차제 사전예약", "옛 생활문화 놀이형 전시"),
    _exp("gildong_eco", "길동생태공원 생태 탐방", A.PRESCHOOL, C.NATURE, "서울", "강동구", "길동생태공원",
         "무료", "무료 (탐방 사전예약)", False, False, False, True, "탐방 사전예약", "습지·곤충 관찰 · 생태 해설"),
    # ── 경기 ──
    _exp("gg_gwanggyo_lake", "광교호수공원 가족 산책", A.ALL, C.NATURE, "경기", "수원시", "광교호수공원",
         "무료", "무료", False, True, False, False, "현장 입장", "호수 둘레 평지 산책로 · 어린이 놀이터"),
    _exp("gg_agri_museum", "국립농업박물관", A.PRESCHOOL, C.PLAY, "경기", "수원시", "국립농업박물관",
         "무료", "무료 (일부 체험 별도 예약)", True, True, True, False, "현장 입장", "농업·식물 체험 전시 · 야외 식물원"),
    _exp("gg_yuldong_park", "분당 율동공원", A.ALL, C.NATURE, "경기", "성남시", "율동공원",
         "무료", "무료", False, True, False, False, "현장 입장", "호수 산책 · 잔디밭 피크닉"),
    _exp("gg_shingu_garden", "신구대학교식물원", A.PRESCHOOL, C.NATURE, "경기", "성남시", "신구대학교식물원",
         "유료", "소액 입장료 (공식 공지 확인)", False, True, False, False, "현장 입장", "테마 정원 · 곤충·식물 관찰"),
    _exp("gg_provincial_museum", "경기도박물관", A.ELEMENTARY, C.PLAY, "경기", "용인시", "경기도박물관",
         "무료", "상설전시 무료", True, True, True, False, "현장 입장", "경기 역사·민속 전시 · 교과 연계"),
    _exp("gg_goyang_kids", "고양어린이박물관", A.INFANT, C.PLAY, "경기", "고양시", "고양어린이박물관",
         "유료", "어린이·보호자 입장료 (공식 공지 확인)", True, True, True, True, "회차제 사전예약", "영유아 놀이 전시 · 회차제"),
    _exp("gg_ilsan_lake", "일산호수공원", A.ALL, C.NATURE, "경기", "고양시", "일산호수공원",
         "무료", "무료", False, True, False, False, "현장 입장", "넓은 호숫가 산책로 · 자전거·피크닉"),
    _exp("gg_comics_museum", "한국만화박물관", A.ELEMENTARY, C.PLAY, "경기", "부천시", "한국만화박물관",
         "유료", "관람료 (공식 공지 확인)", True, True, True, False, "현장 입장", "만화 역사 전시 · 만화 도서관"),
    _exp("gg_bucheon_eco", "부천자연생태공원 (식물원·자연생태박물관)", A.PRESCHOOL, C.NATURE, "경기", "부천시",
         "부천자연생태공원", "유료", "소액 입장료 (공식 공지 확인)", False, True, True, False, "현장 입장",
         "식물원 온실 · 생태 전시 · 야외 정원"),
    _exp("gg_seoul_grandpark", "서울대공원 동물원 (과천)", A.ALL, C.NATURE, "경기", "과천시", "서울대공원",
         "유료", "동물원 소액 입장료", False, True, True, False, "현장 입장", "대형 동물원 · 유아차 대여 · 넓은 산책로"),
    _exp("gg_gwacheon_kids", "국립과천과학관 어린이탐구체험관", A.PRESCHOOL, C.SCIENCE, "경기", "과천시",
         "국립과천과학관 어린이탐구체험관", "유료", "소액 관람료 (회차제)", True, True, True, True, "회차제 사전예약",
         "유아 눈높이 과학 놀이 · 회차제"),
    _exp("gg_siheung_gaetgol", "시흥갯골생태공원", A.ALL, C.NATURE, "경기", "시흥시", "시흥갯골생태공원",
         "무료", "무료", False, True, False, False, "현장 입장", "갯골 습지 산책 · 염전 체험 공간"),
    # ── 인천 ──
    _exp("icn_central_park", "송도 센트럴파크 산책", A.ALL, C.NATURE, "인천", "연수구", "송도 센트럴파크",
         "무료", "무료 (수상시설 별도)", False, True, False, False, "현장 입장", "수로 따라 평지 산책 · 사슴농장"),
    _exp("icn_letters_museum", "국립세계문자박물관", A.ELEMENTARY, C.PLAY, "인천", "연수구", "국립세계문자박물관",
         "무료", "상설전시 무료", True, True, True, False, "현장 입장", "세계 문자 전시 · 어린이 체험 공간"),
    _exp("icn_city_museum", "인천시립박물관", A.ELEMENTARY, C.PLAY, "인천", "연수구", "인천시립박물관",
         "무료", "무료", True, True, False, False, "현장 입장", "인천 역사 전시 · 교과 연계"),
    _exp("icn_kids_science", "인천어린이과학관", A.PRESCHOOL, C.SCIENCE, "인천", "계양구", "인천어린이과학관",
         "유료", "소액 관람료 (회차제)", True, True, True, True, "회차제 사전예약", "몸으로 배우는 과학 놀이"),
    _exp("icn_butterfly_park", "인천나비공원", A.PRESCHOOL, C.NATURE, "인천", "부평구", "인천나비공원",
         "무료", "무료", False, True, False, False, "현장 입장", "나비·곤충 관찰 · 숲 산책"),
    _exp("icn_grand_park", "인천대공원 어린이동물원", A.ALL, C.NATURE, "인천", "남동구", "인천대공원",
         "무료", "입장 무료 (일부 시설 유료)", False, True, True, False, "현장 입장", "어린이동물원 · 넓은 잔디·수목원"),
    _exp("icn_sorae_wetland", "소래습지생태공원", A.PRESCHOOL, C.NATURE, "인천", "남동구", "소래습지생태공원",
         "무료", "무료", False, True, False, False, "현장 입장", "갯벌·염전 관찰 · 풍차 산책길"),
    _exp("icn_seaside_park", "영종 씨사이드파크", A.ALL, C.NATURE, "인천", "중구", "영종 씨사이드파크",
         "무료", "공원 무료 (레일바이크 등 유료)", False, True, False, False, "현장 입장", "바다 보며 산책 · 놀이터"),
    _exp("icn_kids_museum", "인천어린이박물관", A.INFANT, C.PLAY, "인천", "미추홀구", "인천어린이박물관",
         "유료", "소액 입장료 (공식 공지 확인)", True, True, True, True, "회차제 사전예약", "영유아 놀이형 전시 · 회차제"),
    _exp("icn_sudoguksan", "수도국산달동네박물관", A.ELEMENTARY, C.PLAY, "인천", "동구", "수도국산달동네박물관",
         "유료", "소액 관람료", True, False, False, False, "현장 입장", "옛 생활상 재현 전시 · 교과 연계"),
]

# Walk-in tips (오픈런·주차) shown on the 현장직행 3-Pick card. Generic fallback in the formatter.
WALK_IN_TIPS: dict[str, str] = {
    "seoulforest_insect": "🏃 오전 10시 전 도착 · 🚇 수인분당선 서울숲역 권장",
    "forest_dream_free": "🏃 오전 일찍 잔디광장 그늘 선점 · 🚗 주말 주차 혼잡 → 대중교통 권장",
    "grandpark_zoo": "🏃 개장 직후 동물원부터 · 🚇 7호선 어린이대공원역 권장",
    "childrens_grand_park": "🏃 오전 입장 추천 · 🚇 7호선 어린이대공원역 권장",
    "nmk_main": "🏃 개장 직후 입장 · 🚇 4호선·경의중앙선 이촌역 권장",
    "botanic_park": "🏃 오전 입장 추천 · 🚇 9호선·공항철도 마곡나루역 권장",
    "worldcup_park": "🏃 오전·노을 시간 추천 · 🚇 6호선 월드컵경기장역 권장",
}

# 임신기 복지·바우처 (fact safety: amounts vary by year/지자체 — the card always says to check the official notice)
PREGNANCY_BENEFITS: list[PregnancyBenefit] = [
    PregnancyBenefit("first_meet", "첫만남이용권", "첫째 200만원 · 둘째 이상 300만원",
                     "출생 후 국민행복카드 바우처 지급", naver_search("첫만남이용권 신청")),
    PregnancyBenefit("transport", "임산부 교통비 바우처", "서울 70만원 (지자체별 상이)",
                     "거주 지자체에 임신 중 신청", naver_search("임산부 교통비 지원 신청")),
    PregnancyBenefit("folic_iron", "보건소 엽산제·철분제 무료", "임신 초기 엽산 · 16주 이후 철분제",
                     "관할 보건소 등록 후 방문·택배 수령", naver_search("보건소 임산부 엽산제 철분제")),
    PregnancyBenefit("eco_food", "임산부 친환경 농산물 꾸러미", "시행 여부 지자체별 상이",
                     "거주지 시행 여부를 먼저 확인", naver_search("임산부 친환경농산물 꾸러미 지원")),
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

# Weekly occurrences need booking-id level anti-spam in the Monday push. Pull
# views still render every upcoming event from the database unchanged.
WEEKLY_BOOKING_IDS: set[int] = {
    int(rule["booking_id"])
    for rule in SEED_BOOKING_RULES
    if rule.get("rule", (None,))[0] == "weekly"
}


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
        map_url=naver_map(name), last_verified_at=UNVERIFIED, is_placeholder=True, source="SEED",
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


MILESTONE_SCHEDULE_VERSION = "KR-2026.1"
_NIP_URL = naver_search("예방접종도우미 표준예방접종일정표")
_CHECKUP_URL = naver_search("영유아 건강검진 시기 국민건강보험공단")
_PREG_URL = naver_search("임신 주차별 산전검사 일정")


_DENTAL_URL = naver_search("영유아 구강검진 시기 국민건강보험공단")
_BENEFIT_URL = naver_search("복지로 첫만남이용권 부모급여 아동수당 신청")
_PRESCHOOL_URL = naver_search("만3세 유아학비 보육료 전환 아이사랑")
# 공식 신청 창구 (금액은 해마다 바뀌므로 카드에 하드코딩하지 않는다)
BOKJIRO_URL = "https://www.bokjiro.go.kr/"
GOV24_URL = "https://www.gov.kr/"
CHILDCARE_PORTAL_URL = "https://www.childcare.go.kr/"


def _ms(code: str, kind: str, title: str, description: str, source_url: str,
        months: Optional[tuple[int, int]] = None, week: Optional[int] = None,
        window_type: str = "MONTHS", days: tuple[int, int] = (0, 0)) -> MilestoneDefinition:
    return MilestoneDefinition(
        definition_id=f"{code}@{MILESTONE_SCHEDULE_VERSION}", kind=kind, code=code,
        schedule_version=MILESTONE_SCHEDULE_VERSION,
        age_min_months=months[0] if months else None, age_max_months=months[1] if months else None,
        pregnancy_week=week, title=title, description=description, source_url=source_url,
        window_type=window_type, age_min_days=days[0], age_max_days=days[1],
    )


# 건강검진·예방접종·임신 주차 체크포인트. Fact safety: schedules can be revised — every card must say
# "정확한 일정은 소아청소년과·보건소/예방접종도우미에서 확인". Months are inclusive (4~6 = 4개월 ~ 7개월 전날).
SEED_MILESTONES: list[MilestoneDefinition] = [
    # ── 영유아 건강검진 1~8차 ──
    # 1차는 일(day) 단위 — months=(0, 1)은 하위호환 표시용, 실제 창은 생후 14~35일
    _ms("CHECKUP_1", "CHECKUP", "영유아 건강검진 1차", "생후 14~35일 · 성장·발달 확인", _CHECKUP_URL, months=(0, 1),
        window_type="DAYS", days=(14, 35)),
    _ms("CHECKUP_2", "CHECKUP", "영유아 건강검진 2차", "생후 4~6개월", _CHECKUP_URL, months=(4, 6)),
    _ms("CHECKUP_3", "CHECKUP", "영유아 건강검진 3차", "생후 9~12개월", _CHECKUP_URL, months=(9, 12)),
    _ms("CHECKUP_4", "CHECKUP", "영유아 건강검진 4차", "생후 18~24개월 (구강검진 병행 시기)", _CHECKUP_URL, months=(18, 24)),
    _ms("CHECKUP_5", "CHECKUP", "영유아 건강검진 5차", "생후 30~36개월", _CHECKUP_URL, months=(30, 36)),
    _ms("CHECKUP_6", "CHECKUP", "영유아 건강검진 6차", "생후 42~48개월 (구강검진 병행 시기)", _CHECKUP_URL, months=(42, 48)),
    _ms("CHECKUP_7", "CHECKUP", "영유아 건강검진 7차", "생후 54~60개월 (구강검진 병행 시기)", _CHECKUP_URL, months=(54, 60)),
    _ms("CHECKUP_8", "CHECKUP", "영유아 건강검진 8차", "생후 66~71개월", _CHECKUP_URL, months=(66, 71)),
    # ── 영유아 구강검진 1~4차 ──
    _ms("DENTAL_1", "DENTAL", "영유아 구강검진 1차", "생후 18~29개월 · 치과 검진기관 확인", _DENTAL_URL, months=(18, 29)),
    _ms("DENTAL_2", "DENTAL", "영유아 구강검진 2차", "생후 30~41개월 · 치과 검진기관 확인", _DENTAL_URL, months=(30, 41)),
    _ms("DENTAL_3", "DENTAL", "영유아 구강검진 3차", "생후 42~53개월 · 치과 검진기관 확인", _DENTAL_URL, months=(42, 53)),
    _ms("DENTAL_4", "DENTAL", "영유아 구강검진 4차", "생후 54~65개월 · 치과 검진기관 확인", _DENTAL_URL, months=(54, 65)),
    # ── 지원금·전환 체크 (금액 하드코딩 금지 — 공식 창구 안내만) ──
    _ms("BENEFIT_POSTBIRTH", "BENEFIT", "출생 후 지원금 신청 (첫만남이용권·부모급여·아동수당)",
        f"출생신고 후 신청 · 지원 금액·기준은 공식 공지 확인 · 복지로 {BOKJIRO_URL} / 정부24 {GOV24_URL}",
        _BENEFIT_URL, months=(0, 1)),
    _ms("BENEFIT_AGE3_PRESCHOOL", "BENEFIT", "만 3세 학년도 전환 확인 (보육료 → 유아학비)",
        f"만 3세가 되는 해 3월 1일 학년도 전환 · 어린이집·유치원 지원 변경 확인 · 아이사랑 {CHILDCARE_PORTAL_URL}",
        _PRESCHOOL_URL, months=(36, 47), window_type="SCHOOL_YEAR"),
    # ── 국가필수예방접종(NIP) 주요 시점 ──
    _ms("VAC_BCG", "VACCINE", "BCG(결핵) 접종", "생후 4주 이내 권장", _NIP_URL, months=(0, 1)),
    _ms("VAC_HEPB_2", "VACCINE", "B형간염 2차 접종", "생후 1개월 (1차는 출생 시)", _NIP_URL, months=(1, 1)),
    _ms("VAC_2M", "VACCINE", "2개월 접종 (DTaP·폴리오·로타 1차)", "Hib·폐렴구균 1차 동시 시기 · 로타는 제품별 2~3회",
        _NIP_URL, months=(2, 2)),
    _ms("VAC_4M", "VACCINE", "4개월 접종 (DTaP·폴리오·로타 2차)", "Hib·폐렴구균 2차 동시 시기", _NIP_URL, months=(4, 4)),
    _ms("VAC_6M", "VACCINE", "6개월 접종 (DTaP 3차·폴리오 3차·로타)", "폴리오 3차는 6~18개월 · B형간염 3차 · 로타 3차는 해당 제품만",
        _NIP_URL, months=(6, 6)),
    _ms("VAC_12M", "VACCINE", "12개월 접종 (MMR 1차·수두)", "생후 12~15개월 권장", _NIP_URL, months=(12, 15)),
    _ms("VAC_HEPA_1", "VACCINE", "A형간염 1차 접종", "생후 12~23개월 (2차는 6개월 이후)", _NIP_URL, months=(12, 23)),
    _ms("VAC_4_6Y", "VACCINE", "만 4~6세 추가접종", "DTaP 5차 · 폴리오 4차 · MMR 2차", _NIP_URL, months=(48, 83)),
    # ── 임신 주차별 체크포인트 ──
    _ms("PREG_W8", "PREGNANCY", "임신 확인 후 보건소 임산부 등록 · 국민행복카드 신청", "임신·출산 진료비 바우처 신청 시기",
        _PREG_URL, week=8),
    _ms("PREG_W12", "PREGNANCY", "초기 정밀검사 (1차 기형아검사·목덜미 투명대)", "통상 11~13주 · 병원별 일정 확인",
        _PREG_URL, week=12),
    _ms("PREG_W16", "PREGNANCY", "2차 기형아검사 · 철분제 복용 시작", "통상 15~20주 · 보건소 철분제 지원 확인", _PREG_URL, week=16),
    _ms("PREG_W24", "PREGNANCY", "임신성 당뇨 검사", "통상 24~28주", _PREG_URL, week=24),
    _ms("PREG_W28", "PREGNANCY", "백일해(Tdap) 예방접종", "통상 27~36주 권장 · 주치의 상담", _PREG_URL, week=28),
    _ms("PREG_W32", "PREGNANCY", "출산가방 준비 · 첫만남이용권 사전 확인", "출생 후 신청할 바우처 서류 미리 준비",
        _PREG_URL, week=32),
    _ms("PREG_W36", "PREGNANCY", "막달 검사", "출산 전 혈액·태동 검사 등 · 병원별 상이", _PREG_URL, week=36),
]


def milestone_window(defn: MilestoneDefinition, birth_date: Optional[str] = None,
                     due_date: Optional[str] = None) -> tuple[str, str]:
    """(due_from, due_to) ISO dates. Child items delegate to storage.db.milestone_window
    (DAYS / MONTHS month-end safe / SCHOOL_YEAR). Pregnancy: LMP = due - 280 days; window = week w ~ +13 days."""
    if defn.kind == "PREGNANCY":
        if not due_date or defn.pregnancy_week is None:
            raise ValueError("pregnancy milestone needs due_date")
        lmp = date.fromisoformat(due_date) - timedelta(days=280)
        start = lmp + timedelta(weeks=defn.pregnancy_week)
        return start.isoformat(), (start + timedelta(days=13)).isoformat()
    if not birth_date:
        raise ValueError("child milestone needs birth_date")
    return _child_milestone_window(birth_date, defn)


def sync_user_milestones(db, telegram_user_id) -> int:
    """Materialise milestone rows for every child (CHECKUP/VACCINE) and the pregnancy (PREGNANCY).
    Idempotent — existing statuses (USER_CONFIRMED etc.) are preserved. Returns rows ensured."""
    uid = str(telegram_user_id)
    n = 0
    for defn in db.list_milestone_definitions(schedule_version=MILESTONE_SCHEDULE_VERSION):
        if defn.kind == "PREGNANCY":
            preg = db.get_pregnancy(uid)
            if preg is None:
                continue
            frm, to = milestone_window(defn, due_date=preg.due_date)
            db.ensure_subject_milestone(uid, "PREGNANCY", uid, defn.definition_id, defn.schedule_version, frm, to)
            n += 1
        else:
            for child in db.list_children(uid):
                frm, to = milestone_window(defn, birth_date=child.birth_date)
                db.ensure_subject_milestone(uid, "CHILD", child.child_id, defn.definition_id,
                                            defn.schedule_version, frm, to)
                n += 1
    return n


def recommend_weekend_picks(db, age_group: AgeGroup, region_sido: Optional[str] = None, weather_rain: bool = False,
                            fine_dust_bad: bool = False, is_dispatch: bool = False,
                            limit: int = 3, allow_backfill: bool = True,
                            excluded_exp_ids: Optional[set[str]] = None) -> list[ExperienceItem]:
    """Weather-aware 3-Pick.
    - rain / bad dust: indoor ONLY (returns fewer than `limit` rather than suggest an outdoor spot)
    - sunny: alternate outdoor (nature first) and indoor
    - is_dispatch (현장직행): booking_required == False ONLY — also enforced on the region back-fill
    - PREGNANCY returns PREGNANCY spots only; other age groups never see PREGNANCY spots
    - excluded_exp_ids: user feedback / recent-send cooldown — never returned (back-fill included)
    Region matches come first; other regions back-fill when the region alone is too thin, unless
    allow_backfill=False (push: an out-of-region place is worse than a shorter list).
    """
    indoor_only = weather_rain or fine_dust_bad
    excluded = set(excluded_exp_ids or ())

    def fetch(region: Optional[str]) -> list[ExperienceItem]:
        items = db.list_experiences(age_group=age_group, region_sido=region, indoor_only=indoor_only)
        if age_group == AgeGroup.PREGNANCY:
            items = [e for e in items if e.age_group == AgeGroup.PREGNANCY]
        else:
            items = [e for e in items if e.age_group != AgeGroup.PREGNANCY]
        if is_dispatch:
            items = [e for e in items if not e.booking_required]
        return [e for e in items if e.exp_id not in excluded]

    pool = fetch(region_sido)
    if allow_backfill and region_sido and len(pool) < limit:
        seen = {e.exp_id for e in pool}
        pool += [e for e in fetch(None) if e.exp_id not in seen]
    # Exact age-group items before generic ALL items; then drop repeat venues (e.g. 어린이대공원 PRESCHOOL + ALL)
    pool.sort(key=lambda e: e.age_group == AgeGroup.ALL)
    venues: set[str] = set()
    pool = [e for e in pool if not (e.venue_name in venues or venues.add(e.venue_name))]
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

    catalog_rows = build_catalog_seed_rows() + build_food_catalog_seed_rows()
    return {
        "medicines": db.upsert_medicines(DEFAULT_MEDICINES.values()),
        "experiences": db.upsert_experiences(SEED_EXPERIENCES),
        "bookings": db.upsert_booking_events(build_booking_events(today)),
        "hospitals": db.upsert_hospitals(SEED_HOSPITALS),
        "milestones": db.upsert_milestone_definitions(SEED_MILESTONES),
        "catalog": db.seed_content_catalog(catalog_rows),
    }
