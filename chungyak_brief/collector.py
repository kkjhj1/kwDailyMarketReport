"""청약홈 (한국부동산원 ApplyhomeInfoDetailSvc) OpenAPI collector.

- Notice kinds: APT_GENERAL / PUBLIC (APT 분양정보), MOOSUNWI / CANCEL_RE (무순위·잔여세대), IMUI (임의공급).
- 오피스텔·도시형생활주택 endpoint is fetched only with explicit opt-in (`include_officetel=True`).
- Each notice gets a deterministic `content_hash` (canonical JSON, sorted keys) so 정정공고 are detected as AMENDED.
- Network access goes through an injectable `fetch_json` callable; tests / `--demo` never touch the network.
- The service key is read from env (`CHUNGYAK_API_KEY` -> `DATA_GO_KR_SERVICE_KEY`) and never logged.
"""
from __future__ import annotations

from datetime import date
import json
import logging
import os
from typing import Any, Callable, Iterable, Optional
import urllib.parse
import urllib.request

from chungyak_brief.models import (
    SPECIAL_SUPPLY_TYPES,
    ChungyakNotice,
    HouseType,
    NoticeType,
    normalize_sido,
    parse_date,
    to_decimal,
)

logger = logging.getLogger(__name__)

API_BASE = "https://api.odcloud.kr/api/ApplyhomeInfoDetailSvc/v1"
ENDPOINTS: dict[str, str] = {
    "APT": "getAPTLttotPblancDetail",
    "REMNDR": "getRemndrLttotPblancDetail",
    "OPT": "getOPTLttotPblancDetail",
    "URBTY_OFCTL": "getUrbtyOfctlLttotPblancDetail",
}

FetchJson = Callable[[str, dict[str, Any]], dict[str, Any]]


def get_service_key() -> str:
    return (os.environ.get("CHUNGYAK_API_KEY") or os.environ.get("DATA_GO_KR_SERVICE_KEY") or "").strip()


def default_fetch_json(endpoint: str, params: dict[str, Any], timeout: float = 15.0) -> dict[str, Any]:
    key = get_service_key()
    if not key:
        raise RuntimeError("청약홈 API 키(CHUNGYAK_API_KEY)가 설정되지 않았습니다.")
    query = urllib.parse.urlencode({**params, "serviceKey": key})
    req = urllib.request.Request(f"{API_BASE}/{endpoint}?{query}", headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https host
        return json.loads(resp.read().decode("utf-8"))


def _s(rec: dict[str, Any], *keys: str) -> str:
    for k in keys:
        v = rec.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""


def _flag(rec: dict[str, Any], *keys: str) -> Optional[bool]:
    seen = False
    for k in keys:
        v = rec.get(k)
        if v is None or str(v).strip() == "":
            continue
        seen = True
        if str(v).strip().upper() in ("Y", "1", "TRUE"):
            return True
    return False if seen else None


def _int(value: Any) -> Optional[int]:
    dec = to_decimal(value)
    return int(dec) if dec is not None else None


def classify_notice_type(source: str, rec: dict[str, Any]) -> Optional[NoticeType]:
    secd = _s(rec, "HOUSE_SECD")
    secd_nm = _s(rec, "HOUSE_SECD_NM")
    dtl = _s(rec, "HOUSE_DTL_SECD", "HOUSE_DTL_SECD_NM")
    if source == "APT":
        if dtl in ("03",) or "국민" in dtl or "공공" in secd_nm:
            return NoticeType.PUBLIC
        return NoticeType.APT_GENERAL
    if source == "REMNDR":
        if secd == "06" or "취소" in secd_nm or "재공급" in secd_nm:
            return NoticeType.CANCEL_RE
        return NoticeType.MOOSUNWI
    if source == "OPT":
        return NoticeType.IMUI
    if source == "URBTY_OFCTL":
        return NoticeType.APT_GENERAL
    return None


def classify_house_type(source: str, rec: dict[str, Any]) -> HouseType:
    if source != "URBTY_OFCTL":
        return HouseType.APT
    name = _s(rec, "HOUSE_SECD_NM", "HOUSE_DTL_SECD_NM")
    if "도시형" in name:
        return HouseType.URBAN_LIFE
    return HouseType.OFFICETEL


def parse_special_types(rec: dict[str, Any]) -> list[str]:
    """Reads explicit `SPSPLY_TYPES` (comma list) or per-type household counts (`*_HSHLDCO` > 0)."""
    explicit = rec.get("SPSPLY_TYPES")
    found: set[str] = set()
    if isinstance(explicit, (list, tuple)):
        found |= {str(x).strip().upper() for x in explicit}
    elif isinstance(explicit, str) and explicit.strip():
        found |= {x.strip().upper() for x in explicit.split(",")}
    count_fields = {
        "NEWLYWED": "NWWDS_HSHLDCO",
        "FIRST_LIFE": "LFE_FRST_HSHLDCO",
        "MULTI_CHILD": "MNYCH_HSHLDCO",
        "ELDERLY_PARENT": "OLD_PARNTS_SUPORT_HSHLDCO",
        "NEWBORN": "NWBB_HSHLDCO",
        "YOUTH": "YGMN_HSHLDCO",
    }
    for code, fld in count_fields.items():
        n = _int(rec.get(fld))
        if n is not None and n > 0:
            found.add(code)
    return sorted(c for c in found if c in SPECIAL_SUPPLY_TYPES)


def parse_record(source: str, rec: dict[str, Any], *, is_sample: bool = False) -> Optional[ChungyakNotice]:
    notice_type = classify_notice_type(source, rec)
    manage_no = _s(rec, "HOUSE_MANAGE_NO")
    pblanc_no = _s(rec, "PBLANC_NO") or manage_no
    name = _s(rec, "HOUSE_NM")
    if notice_type is None or not manage_no or not name:
        return None
    house_type = classify_house_type(source, rec)
    prefix = notice_type.value if house_type == HouseType.APT else house_type.value
    address = _s(rec, "HSSPLY_ADRES")
    sido = normalize_sido(_s(rec, "SUBSCRPT_AREA_CODE_NM")) or normalize_sido(address)
    sigungu = None
    parts = address.split()
    if len(parts) >= 2:
        sigungu = parts[1]
    regulated = _flag(rec, "SPECLT_RDN_EARTH_AT", "MDAT_TRGET_AREA_SECD")
    notice = ChungyakNotice(
        notice_key=f"{prefix}:{manage_no}:{pblanc_no}",
        notice_type=notice_type,
        house_type=house_type,
        house_manage_no=manage_no,
        pblanc_no=pblanc_no,
        house_name=name,
        region_sido=sido,
        region_sigungu=sigungu,
        address=address,
        supply_households=_int(rec.get("TOT_SUPLY_HSHLDCO")),
        announce_date=parse_date(rec.get("RCRIT_PBLANC_DE")),
        receipt_start=parse_date(rec.get("RCEPT_BGNDE") or rec.get("SUBSCRPT_RCEPT_BGNDE")),
        receipt_end=parse_date(rec.get("RCEPT_ENDDE") or rec.get("SUBSCRPT_RCEPT_ENDDE")),
        winner_date=parse_date(rec.get("PRZWNER_PRESNATN_DE")),
        homepage_url=_s(rec, "PBLANC_URL", "HMPG_ADRES"),
        min_area_m2=to_decimal(rec.get("MIN_SUPLY_AR")),
        max_area_m2=to_decimal(rec.get("MAX_SUPLY_AR")),
        min_price_eok=to_decimal(rec.get("MIN_PRICE_EOK")),
        is_regulated=regulated,
        special_supply_types=parse_special_types(rec),
        is_sample=is_sample,
    )
    return notice.ensure_hash()


class ChungyakCollector:
    def __init__(self, fetch_json: Optional[FetchJson] = None, per_page: int = 100) -> None:
        self.fetch_json = fetch_json or default_fetch_json
        self.per_page = per_page

    def sources(self, include_officetel: bool = False) -> list[str]:
        base = ["APT", "REMNDR", "OPT"]
        return base + (["URBTY_OFCTL"] if include_officetel else [])

    def fetch(self, since: Optional[date] = None, include_officetel: bool = False) -> list[ChungyakNotice]:
        notices: list[ChungyakNotice] = []
        for source in self.sources(include_officetel):
            params: dict[str, Any] = {"page": 1, "perPage": self.per_page, "returnType": "JSON"}
            if since is not None:
                params["cond[RCRIT_PBLANC_DE::GTE]"] = since.isoformat()
            try:
                payload = self.fetch_json(ENDPOINTS[source], params)
            except Exception as exc:  # network / quota: skip this source, never crash the cron tick
                logger.warning("청약홈 %s 수집 실패: %s", source, type(exc).__name__)
                continue
            for rec in payload.get("data") or []:
                if not isinstance(rec, dict):
                    continue
                notice = parse_record(source, rec)
                if notice is not None:
                    notices.append(notice)
        return notices

    @staticmethod
    def store(db: Any, notices: Iterable[ChungyakNotice]) -> dict[str, list[ChungyakNotice]]:
        result: dict[str, list[ChungyakNotice]] = {"NEW": [], "AMENDED": [], "UNCHANGED": []}
        for n in notices:
            result[db.upsert_notice(n)].append(n)
        return result


# ---------------------------------------------------------------------------------------------------------
# Sample fixtures (is_sample=1): used by `--demo` / `--seed-sample` / tests only. Live dispatch excludes them.
# ---------------------------------------------------------------------------------------------------------
SAMPLE_RECORDS: list[tuple[str, dict[str, Any]]] = [
    ("APT", {
        "HOUSE_MANAGE_NO": "2026910001", "PBLANC_NO": "2026910001", "HOUSE_NM": "[샘플] 한강 리버뷰 자이",
        "HOUSE_SECD": "01", "HOUSE_SECD_NM": "APT", "HOUSE_DTL_SECD": "01", "SUBSCRPT_AREA_CODE_NM": "서울",
        "HSSPLY_ADRES": "서울특별시 마포구 상암동 1-1", "TOT_SUPLY_HSHLDCO": "412",
        "RCRIT_PBLANC_DE": "2026-11-20", "RCEPT_BGNDE": "2026-12-01", "RCEPT_ENDDE": "2026-12-03",
        "PRZWNER_PRESNATN_DE": "2026-12-10", "PBLANC_URL": "https://www.applyhome.co.kr",
        "SPECLT_RDN_EARTH_AT": "Y", "MIN_SUPLY_AR": "59.9", "MAX_SUPLY_AR": "84.9", "MIN_PRICE_EOK": "9.8",
        "NWWDS_HSHLDCO": "40", "LFE_FRST_HSHLDCO": "20", "MNYCH_HSHLDCO": "10",
    }),
    ("APT", {
        "HOUSE_MANAGE_NO": "2026910002", "PBLANC_NO": "2026910002", "HOUSE_NM": "[샘플] 동탄 센트럴 푸르지오",
        "HOUSE_SECD": "01", "HOUSE_SECD_NM": "APT", "HOUSE_DTL_SECD": "01", "SUBSCRPT_AREA_CODE_NM": "경기",
        "HSSPLY_ADRES": "경기도 화성시 오산동 100", "TOT_SUPLY_HSHLDCO": "850",
        "RCRIT_PBLANC_DE": "2026-11-21", "RCEPT_BGNDE": "2026-12-02", "RCEPT_ENDDE": "2026-12-04",
        "PRZWNER_PRESNATN_DE": "2026-12-11", "PBLANC_URL": "https://www.applyhome.co.kr",
        "SPECLT_RDN_EARTH_AT": "N", "MDAT_TRGET_AREA_SECD": "N", "MIN_SUPLY_AR": "74.5", "MAX_SUPLY_AR": "101.8",
        "MIN_PRICE_EOK": "5.2", "NWWDS_HSHLDCO": "120", "NWBB_HSHLDCO": "30",
    }),
    ("APT", {
        "HOUSE_MANAGE_NO": "2026910003", "PBLANC_NO": "2026910003", "HOUSE_NM": "[샘플] 3기 신도시 공공분양",
        "HOUSE_SECD": "01", "HOUSE_SECD_NM": "APT", "HOUSE_DTL_SECD": "03", "SUBSCRPT_AREA_CODE_NM": "인천",
        "HSSPLY_ADRES": "인천광역시 계양구 박촌동 50", "TOT_SUPLY_HSHLDCO": "600",
        "RCRIT_PBLANC_DE": "2026-11-22", "RCEPT_BGNDE": "2026-12-05", "RCEPT_ENDDE": "2026-12-09",
        "PRZWNER_PRESNATN_DE": "2026-12-18", "PBLANC_URL": "https://www.applyhome.co.kr",
        "SPECLT_RDN_EARTH_AT": "N", "MIN_SUPLY_AR": "55", "MAX_SUPLY_AR": "84", "MIN_PRICE_EOK": "4.1",
        "SPSPLY_TYPES": "YOUTH,NEWBORN,NEWLYWED",
    }),
    ("REMNDR", {
        "HOUSE_MANAGE_NO": "2026910004", "PBLANC_NO": "2026910004", "HOUSE_NM": "[샘플] 해운대 오션 무순위",
        "HOUSE_SECD": "04", "HOUSE_SECD_NM": "무순위", "SUBSCRPT_AREA_CODE_NM": "부산",
        "HSSPLY_ADRES": "부산광역시 해운대구 우동 77", "TOT_SUPLY_HSHLDCO": "3",
        "RCRIT_PBLANC_DE": "2026-11-23", "RCEPT_BGNDE": "2026-11-30", "RCEPT_ENDDE": "2026-11-30",
        "PRZWNER_PRESNATN_DE": "2026-12-03", "PBLANC_URL": "https://www.applyhome.co.kr",
        "MIN_SUPLY_AR": "84.9", "MAX_SUPLY_AR": "84.9", "MIN_PRICE_EOK": "7.4",
    }),
    ("URBTY_OFCTL", {
        "HOUSE_MANAGE_NO": "2026910005", "PBLANC_NO": "2026910005", "HOUSE_NM": "[샘플] 강남 오피스텔 레지던스",
        "HOUSE_SECD": "02", "HOUSE_SECD_NM": "오피스텔", "SUBSCRPT_AREA_CODE_NM": "서울",
        "HSSPLY_ADRES": "서울특별시 강남구 역삼동 9", "TOT_SUPLY_HSHLDCO": "120",
        "RCRIT_PBLANC_DE": "2026-11-23", "RCEPT_BGNDE": "2026-12-01", "RCEPT_ENDDE": "2026-12-02",
        "PBLANC_URL": "https://www.applyhome.co.kr", "MIN_PRICE_EOK": "4.5",
    }),
]


def sample_notices() -> list[ChungyakNotice]:
    out: list[ChungyakNotice] = []
    for source, rec in SAMPLE_RECORDS:
        n = parse_record(source, dict(rec), is_sample=True)
        if n is not None:
            out.append(n)
    return out
