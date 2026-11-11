"""City catalog & free-text resolver for the Travel Pathfinder bot.

- CURATED: the verified `data/destinations_meta.json` cities (FlightEngine.destinations).
- FALLBACK: Korean-traveller favourite cities with conservative region-level defaults only.
  FALLBACK metadata never claims seasons, visa rules or hotel facts (DATA-01): every such field
  says "확인 필요" and the formatter suppresses savings/experience hooks for them.
- Unknown 3-letter codes are always rejected (no arbitrary IATA passthrough).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Optional

from travel_brief.models import DestinationMeta

FALLBACK_CAUTION_NOTE = "계절 정보 확인 필요"
FALLBACK_VISA_INFO = "입국 조건 확인 필요"
FALLBACK_HOTEL_NAME = "시내 3성급 호텔 기준"
FALLBACK_HOTEL_FEATURES = "실시간 요금·재고 확인 필요"

# One-line experience hooks for CURATED cities only (shown as motivation, never for FALLBACK).
CURATED_EXPERIENCE_HOOKS: dict[str, str] = {
    "FUK": "하카타 야타이 포장마차 & 유후인 온천 당일치기",
    "NRT": "시부야 스카이 야경 & 츠키지 아침 스시",
    "KIX": "도톤보리 먹방 & 교토 당일치기",
    "DAD": "미케비치 산책 & 바나힐 골든브릿지",
    "CXR": "빈원더스 & 머드 스파 휴양",
    "BKK": "루프탑 바 야경 & 짜뚜짝 주말시장",
    "TPE": "스린 야시장 & 지우펀 홍등 골목",
    "GUM": "투몬비치 스노클링 & 면세 쇼핑",
    "DPS": "우붓 라이스테라스 & 짱구 선셋",
    "SIN": "마리나베이 야경 & 호커센터 미식",
}

# Conservative (non-zero) region profiles for FALLBACK cities.
_REGION_PROFILES: dict[str, dict] = {
    "JP": {"region": "동아시아", "baseline": 380000, "hotel": 120000, "meal": 65000,
           "airlines": ["제주항공", "진에어", "티웨이항공", "대한항공"]},
    "CN": {"region": "동아시아", "baseline": 360000, "hotel": 110000, "meal": 55000,
           "airlines": ["대한항공", "아시아나항공", "제주항공"]},
    "HKMO": {"region": "동아시아", "baseline": 420000, "hotel": 160000, "meal": 70000,
             "airlines": ["대한항공", "아시아나항공", "제주항공", "티웨이항공"]},
    "TW": {"region": "동아시아", "baseline": 380000, "hotel": 100000, "meal": 50000,
           "airlines": ["대한항공", "티웨이항공", "제주항공"]},
    "MN": {"region": "동아시아", "baseline": 550000, "hotel": 110000, "meal": 45000,
           "airlines": ["대한항공", "제주항공"]},
    "SEA": {"region": "동남아", "baseline": 480000, "hotel": 90000, "meal": 45000,
            "airlines": ["제주항공", "진에어", "티웨이항공", "대한항공"]},
    "PAC": {"region": "태평양", "baseline": 560000, "hotel": 220000, "meal": 90000,
            "airlines": ["제주항공", "티웨이항공", "아시아나항공"]},
    "HAWAII": {"region": "태평양", "baseline": 1200000, "hotel": 300000, "meal": 120000,
               "airlines": ["대한항공", "아시아나항공", "하와이안항공"]},
    "EU": {"region": "유럽", "baseline": 1500000, "hotel": 230000, "meal": 110000,
           "airlines": ["대한항공", "아시아나항공"]},
    "OCE": {"region": "오세아니아", "baseline": 1200000, "hotel": 210000, "meal": 100000,
            "airlines": ["대한항공", "제주항공"]},
}

# code: (name, country, flag, profile, flight_hours, currency, aliases)
_FALLBACK_CITIES: dict[str, tuple[str, str, str, str, float, str, tuple[str, ...]]] = {
    "CTS": ("삿포로", "일본", "🇯🇵", "JP", 2.8, "JPY", ("신치토세", "홋카이도", "sapporo", "chitose")),
    "OKA": ("오키나와", "일본", "🇯🇵", "JP", 2.3, "JPY", ("나하", "okinawa", "naha")),
    "NGO": ("나고야", "일본", "🇯🇵", "JP", 1.9, "JPY", ("주부", "nagoya")),
    "MYJ": ("마쓰야마", "일본", "🇯🇵", "JP", 1.6, "JPY", ("마츠야마", "matsuyama")),
    "KMJ": ("구마모토", "일본", "🇯🇵", "JP", 1.4, "JPY", ("kumamoto",)),
    "KOJ": ("가고시마", "일본", "🇯🇵", "JP", 1.6, "JPY", ("kagoshima",)),
    "CEB": ("세부", "필리핀", "🇵🇭", "SEA", 4.5, "PHP", ("막탄", "cebu")),
    "KLO": ("보라카이", "필리핀", "🇵🇭", "SEA", 4.3, "PHP", ("칼리보", "boracay", "kalibo")),
    "MNL": ("마닐라", "필리핀", "🇵🇭", "SEA", 4.0, "PHP", ("manila",)),
    "HAN": ("하노이", "베트남", "🇻🇳", "SEA", 4.5, "VND", ("hanoi",)),
    "SGN": ("호치민", "베트남", "🇻🇳", "SEA", 5.3, "VND", ("호찌민", "사이공", "hochiminh", "saigon")),
    "PQC": ("푸꾸옥", "베트남", "🇻🇳", "SEA", 5.5, "VND", ("푸쿠옥", "phuquoc")),
    "BKI": ("코타키나발루", "말레이시아", "🇲🇾", "SEA", 5.0, "MYR", ("코타", "kotakinabalu")),
    "KUL": ("쿠알라룸푸르", "말레이시아", "🇲🇾", "SEA", 6.5, "MYR", ("kualalumpur",)),
    "CNX": ("치앙마이", "태국", "🇹🇭", "SEA", 5.8, "THB", ("chiangmai",)),
    "HKT": ("푸껫", "태국", "🇹🇭", "SEA", 6.3, "THB", ("푸켓", "phuket")),
    "HNL": ("하와이", "미국", "🇺🇸", "HAWAII", 8.5, "USD", ("호놀룰루", "오아후", "hawaii", "honolulu")),
    "SPN": ("사이판", "미국", "🇺🇸", "PAC", 4.3, "USD", ("saipan",)),
    "HKG": ("홍콩", "홍콩", "🇭🇰", "HKMO", 3.8, "HKD", ("hongkong",)),
    "MFM": ("마카오", "마카오", "🇲🇴", "HKMO", 3.7, "MOP", ("macau", "macao")),
    "KHH": ("가오슝", "대만", "🇹🇼", "TW", 2.8, "TWD", ("까오슝", "kaohsiung")),
    "TAO": ("칭다오", "중국", "🇨🇳", "CN", 1.6, "CNY", ("청도", "qingdao")),
    "PVG": ("상하이", "중국", "🇨🇳", "CN", 2.0, "CNY", ("상해", "푸둥", "shanghai")),
    "PEK": ("베이징", "중국", "🇨🇳", "CN", 2.3, "CNY", ("북경", "beijing")),
    "ULN": ("울란바토르", "몽골", "🇲🇳", "MN", 3.5, "MNT", ("몽골", "ulaanbaatar")),
    "CDG": ("파리", "프랑스", "🇫🇷", "EU", 14.0, "EUR", ("paris",)),
    "LHR": ("런던", "영국", "🇬🇧", "EU", 14.5, "GBP", ("london",)),
    "FCO": ("로마", "이탈리아", "🇮🇹", "EU", 13.5, "EUR", ("rome", "roma")),
    "SYD": ("시드니", "호주", "🇦🇺", "OCE", 10.5, "AUD", ("sydney",)),
}

# Extra aliases for CURATED cities (names from the JSON are indexed automatically).
_CURATED_ALIASES: dict[str, tuple[str, ...]] = {
    "NRT": ("TYO", "나리타", "하네다", "동경", "tokyo"),
    "KIX": ("OSA", "간사이", "오오사카", "osaka"),
    "FUK": ("하카타", "fukuoka"),
    "DAD": ("danang",),
    "CXR": ("나뜨랑", "냐짱", "캄란", "nhatrang"),
    "BKK": ("수완나품", "bangkok"),
    "TPE": ("타이페이", "대만", "taipei"),
    "GUM": ("guam",),
    "DPS": ("덴파사르", "bali"),
    "SIN": ("싱가폴", "singapore"),
}

_COUNTRY_FLAGS: dict[str, str] = {
    "일본": "🇯🇵", "베트남": "🇻🇳", "태국": "🇹🇭", "대만": "🇹🇼", "미국": "🇺🇸",
    "인도네시아": "🇮🇩", "싱가포르": "🇸🇬",
}

_ZERO_WIDTH_RE = re.compile(r"[​-‏⁠﻿­]")
_WS_RE = re.compile(r"\s+")
_STRIP_PUNCT_RE = re.compile(r"[-()._·,/!?~]")
_QUERY_SUFFIXES = ("여행지", "여행", "특가", "항공권", "비행기", "항공", "가고싶어요", "가고싶어", "가자")


@dataclass(frozen=True)
class CityResolution:
    code: str
    display_name: str
    meta: DestinationMeta
    metadata_quality: str


def normalize_city_query(text: str) -> str:
    """NFKC → zero-width 제거 → 공백 축약 → `-`/`()`/`.` 등 제거 → casefold."""
    s = unicodedata.normalize("NFKC", text or "")
    s = _ZERO_WIDTH_RE.sub("", s)
    s = _WS_RE.sub(" ", s).strip()
    s = _STRIP_PUNCT_RE.sub("", s)
    s = _WS_RE.sub(" ", s).strip()
    return s.casefold()


def _lookup_key(text: str) -> str:
    return normalize_city_query(text).replace(" ", "")


def build_fallback_meta(code: str) -> Optional[DestinationMeta]:
    spec = _FALLBACK_CITIES.get(code)
    if spec is None:
        return None
    name, country, _flag, profile_key, hours, currency, _aliases = spec
    profile = _REGION_PROFILES[profile_key]
    baseline = int(profile["baseline"])
    return DestinationMeta(
        code=code,
        name=name,
        country=country,
        region=profile["region"],
        flight_time_hours=float(hours),
        visa_info=FALLBACK_VISA_INFO,
        best_months=[],
        caution_months=[],
        caution_note=FALLBACK_CAUTION_NOTE,
        flight_baseline_krw=baseline,
        flight_deal_threshold_krw=round(int(baseline * 0.85), -3),
        hotel_night_avg_krw=int(profile["hotel"]),
        daily_meal_transit_krw=int(profile["meal"]),
        currency=currency,
        tips="",
        recommended_hotel_name=FALLBACK_HOTEL_NAME,
        recommended_hotel_grade="",
        recommended_hotel_features=FALLBACK_HOTEL_FEATURES,
        flight_min_floor_krw=0,
        airlines=list(profile["airlines"]),
        metadata_quality="FALLBACK",
        experience_hook="",
    )


class CityCatalog:
    """Resolves free text / codes to CURATED or FALLBACK destinations without mutating the engine."""

    def __init__(self, curated_destinations: dict[str, DestinationMeta]):
        self._curated: dict[str, DestinationMeta] = dict(curated_destinations or {})
        self._fallback: dict[str, DestinationMeta] = {}
        for code in _FALLBACK_CITIES:
            if code in self._curated:
                continue  # a curated JSON entry always wins over the fallback profile
            meta = build_fallback_meta(code)
            if meta is not None:
                self._fallback[code] = meta

        self._index: dict[str, str] = {}
        for code, meta in {**self._fallback, **self._curated}.items():
            self._register(code, code)
            self._register(meta.name, code)
        for code, spec in _FALLBACK_CITIES.items():
            for alias in spec[6]:
                self._register(alias, code)
        for code, aliases in _CURATED_ALIASES.items():
            if code not in self._curated:
                continue
            for alias in aliases:
                self._register(alias, code)

    def _register(self, alias: str, code: str) -> None:
        key = _lookup_key(alias)
        if key:
            self._index.setdefault(key, code)

    # ------------------------------------------------------------------
    @property
    def curated_codes(self) -> set[str]:
        return set(self._curated)

    @property
    def fallback_codes(self) -> set[str]:
        return set(self._fallback)

    def is_curated(self, code: str) -> bool:
        return (code or "").upper() in self._curated

    def is_known_code(self, code: str) -> bool:
        c = (code or "").upper()
        return c in self._curated or c in self._fallback

    def get(self, code: str) -> Optional[CityResolution]:
        c = (code or "").strip().upper()
        meta = self._curated.get(c) or self._fallback.get(c)
        if meta is None:
            return None
        return CityResolution(
            code=c,
            display_name=f"{self.flag_for(c)} {meta.name}".strip(),
            meta=meta,
            metadata_quality=meta.metadata_quality,
        )

    def flag_for(self, code: str) -> str:
        spec = _FALLBACK_CITIES.get(code)
        if spec is not None and code not in self._curated:
            return spec[2]
        meta = self._curated.get(code)
        return _COUNTRY_FLAGS.get(meta.country, "✈️") if meta else "✈️"

    def resolve(self, query: str) -> Optional[CityResolution]:
        """Returns the resolution for a city name/alias/code, or None (unknown 3-letter codes rejected)."""
        key = _lookup_key(query)
        if not key:
            return None
        code = self._index.get(key)
        if code is None:
            for suffix in _QUERY_SUFFIXES:
                if key.endswith(suffix) and len(key) > len(suffix):
                    code = self._index.get(key[: -len(suffix)])
                    if code:
                        break
        if code is None:
            # Arbitrary IATA-like codes never pass through: only catalog codes resolve.
            return None
        return self.get(code)

    def custom_destinations_for(self, code: str) -> Optional[dict[str, DestinationMeta]]:
        """Request-scoped mapping for FALLBACK cities (None for curated / unknown codes)."""
        c = (code or "").upper()
        meta = self._fallback.get(c)
        return {c: meta} if meta is not None else None

    def suggestion_names(self, limit: int = 12) -> list[str]:
        picks = ["CTS", "OKA", "NGO", "CEB", "HAN", "SGN", "BKI", "PQC", "HKG", "SPN", "HNL", "PVG"]
        return [self._fallback[c].name for c in picks if c in self._fallback][:limit]
