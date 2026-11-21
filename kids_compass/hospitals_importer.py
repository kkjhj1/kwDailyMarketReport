"""Official hospital importer — E-gen(응급의료포털) `HsptlAsembySearchService` XML / JSON / CSV.

Fact-safety invariants:
- Imported rows are official (is_placeholder=False, source="EGEN"); seeds can never overwrite them
  (enforced in KidsCompassDatabase.upsert_hospitals).
- Opening hours are parsed per day into kids_hospital_hours. Anything we cannot parse with certainty
  (empty, '-', bad HHmm, open == close) is stored with parse_status != 'OK' and `is_open_now` answers
  "UNKNOWN" — never a guessed OPEN/CLOSED.
- replace_official_snapshot=True deactivates (is_active=0, never deletes) stale official rows ONLY in the
  regions present in this payload; a partial (one-region) import never touches other regions.
  An import that parses zero hospitals never deactivates anything (fail-closed).
"""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional, Union

try:  # hardened parser when available; stdlib expat (>= 2.4.1) still blocks entity expansion bombs
    from defusedxml import ElementTree as ET  # type: ignore
except ImportError:  # pragma: no cover - depends on environment
    import xml.etree.ElementTree as ET  # noqa: N817

from kids_compass.collector import NIGHT_PHARMACY_LABEL, naver_map
from kids_compass.models import HospitalHourSlot, MoonlightHospital

SOURCE_EGEN = "EGEN"
HOLIDAY_DOW = 7  # E-gen dutyTime8 = 공휴일
DAY_LABELS = ("월", "화", "수", "목", "금", "토", "일", "공휴일")

PARSE_OK = "OK"
PARSE_MISSING = "MISSING"
PARSE_INVALID = "INVALID"
PARSE_AMBIGUOUS = "AMBIGUOUS"

OPEN, CLOSED, UNKNOWN = "OPEN", "CLOSED", "UNKNOWN"

SIDO_SHORT = {
    "서울특별시": "서울", "서울시": "서울", "서울": "서울",
    "경기도": "경기", "경기": "경기",
    "인천광역시": "인천", "인천시": "인천", "인천": "인천",
    "부산광역시": "부산", "부산": "부산", "대구광역시": "대구", "대구": "대구",
    "광주광역시": "광주", "광주": "광주", "대전광역시": "대전", "대전": "대전",
    "울산광역시": "울산", "울산": "울산", "세종특별자치시": "세종", "세종": "세종",
    "강원도": "강원", "강원특별자치도": "강원", "강원": "강원",
    "충청북도": "충북", "충북": "충북", "충청남도": "충남", "충남": "충남",
    "전라북도": "전북", "전북특별자치도": "전북", "전북": "전북",
    "전라남도": "전남", "전남": "전남", "경상북도": "경북", "경북": "경북",
    "경상남도": "경남", "경남": "경남", "제주특별자치도": "제주", "제주도": "제주", "제주": "제주",
}

# CSV / JSON key aliases -> canonical E-gen field names
FIELD_ALIASES = {
    "기관ID": "hpid", "기관아이디": "hpid", "hpid": "hpid",
    "기관명": "dutyName", "dutyName": "dutyName",
    "주소": "dutyAddr", "dutyAddr": "dutyAddr",
    "대표전화1": "dutyTel1", "대표전화": "dutyTel1", "dutyTel1": "dutyTel1",
}
for _i in range(1, 9):
    _label = DAY_LABELS[_i - 1]
    FIELD_ALIASES[f"dutyTime{_i}s"] = f"dutyTime{_i}s"
    FIELD_ALIASES[f"dutyTime{_i}c"] = f"dutyTime{_i}c"
    FIELD_ALIASES[f"진료시간({_label})S"] = f"dutyTime{_i}s"
    FIELD_ALIASES[f"진료시간({_label})C"] = f"dutyTime{_i}c"


# ── time parsing ──────────────────────────────────────────────
def _clean(value) -> str:
    return "" if value is None else str(value).strip()


def parse_hhmm(value, is_close: bool = False) -> Optional[int]:
    """'HHmm' -> minutes. '2400' is valid only as a close time (1440). None when invalid/empty."""
    text = _clean(value).replace(":", "")
    if not text or text == "-" or not text.isdigit():
        return None
    if len(text) == 3:
        text = "0" + text
    if len(text) != 4:
        return None
    hh, mm = int(text[:2]), int(text[2:])
    if mm >= 60:
        return None
    if hh == 24 and mm == 0:
        return 1440 if is_close else None
    if hh >= 24:
        return None
    return hh * 60 + mm


def parse_slot(hospital_id: str, day_of_week: int, raw_open, raw_close) -> HospitalHourSlot:
    """One day's slot. close <= open -> crosses_midnight (야간 익일). '0000' close after a non-midnight open is
    treated as 24:00 the same day. open == close (incl. 0000~0000) is AMBIGUOUS (24h? closed?) -> UNKNOWN."""
    ro, rc = _clean(raw_open), _clean(raw_close)
    base = dict(hospital_id=str(hospital_id), day_of_week=int(day_of_week),
                is_holiday=int(day_of_week) == HOLIDAY_DOW, raw_open=ro, raw_close=rc)
    if (not ro or ro == "-") and (not rc or rc == "-"):
        return HospitalHourSlot(open_minute=0, close_minute=0, crosses_midnight=False,
                                parse_status=PARSE_MISSING, **base)
    o, c = parse_hhmm(ro), parse_hhmm(rc, is_close=True)
    if o is None or c is None:
        return HospitalHourSlot(open_minute=o or 0, close_minute=c or 0, crosses_midnight=False,
                                parse_status=PARSE_INVALID, **base)
    if c == 0 and o > 0:
        c = 1440
    if c == o:
        return HospitalHourSlot(open_minute=o, close_minute=c, crosses_midnight=False,
                                parse_status=PARSE_AMBIGUOUS, **base)
    return HospitalHourSlot(open_minute=o, close_minute=c, crosses_midnight=c < o, parse_status=PARSE_OK, **base)


def slots_from_record(record: dict) -> list[HospitalHourSlot]:
    hid = _clean(record.get("hpid"))
    return [parse_slot(hid, i - 1, record.get(f"dutyTime{i}s"), record.get(f"dutyTime{i}c")) for i in range(1, 9)]


def _fmt_minute(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def slot_text(slot: Optional[HospitalHourSlot]) -> str:
    if slot is None or slot.parse_status != PARSE_OK:
        return "확인 필요"
    text = f"{_fmt_minute(slot.open_minute)}~{_fmt_minute(slot.close_minute)}"
    return text + " (익일)" if slot.crosses_midnight else text


def _slot_open_at(slot: HospitalHourSlot, minute: int) -> bool:
    if slot.crosses_midnight:
        return minute >= slot.open_minute  # today's part; the after-midnight part is checked on the next day
    return slot.open_minute <= minute < slot.close_minute


def is_open_now(hours_rows: list[HospitalHourSlot], now_dt_kst: datetime, is_holiday: bool = False) -> str:
    """"OPEN" | "CLOSED" | "UNKNOWN". UNKNOWN whenever today's slot is missing or parse_status != 'OK'.
    A previous-day slot that crosses midnight keeps the hospital OPEN until its close time; an unparseable
    previous-day slot makes early-morning answers (before today's open) UNKNOWN rather than CLOSED."""
    rows = list(hours_rows or [])
    minute = now_dt_kst.hour * 60 + now_dt_kst.minute
    if is_holiday:
        today = next((s for s in rows if s.is_holiday), None)
    else:
        today = next((s for s in rows if not s.is_holiday and s.day_of_week == now_dt_kst.weekday()), None)
    if today is None or today.parse_status != PARSE_OK:
        return UNKNOWN
    if _slot_open_at(today, minute):
        return OPEN
    prev_dow = (now_dt_kst - timedelta(days=1)).weekday()
    prev = next((s for s in rows if not s.is_holiday and s.day_of_week == prev_dow), None)
    if prev is not None:
        if prev.parse_status == PARSE_OK:
            if prev.crosses_midnight and minute < prev.close_minute:
                return OPEN
        elif minute < today.open_minute:
            return UNKNOWN
    return CLOSED


# ── payload parsing ───────────────────────────────────────────
def _canon(record: dict) -> dict:
    out = {}
    for k, v in (record or {}).items():
        key = FIELD_ALIASES.get(_clean(k).lstrip("﻿"), _clean(k).lstrip("﻿"))
        out[key] = v
    return out


def parse_xml(payload: Union[str, bytes]) -> list[dict]:
    root = ET.fromstring(payload if isinstance(payload, (bytes, str)) else str(payload))
    records = []
    for item in root.iter("item"):
        records.append({child.tag: (child.text or "").strip() for child in item})
    return records


def _json_items(node) -> list:
    if isinstance(node, list):
        return node
    if not isinstance(node, dict):
        return []
    if "hpid" in node or "dutyName" in node:
        return [node]
    for key in ("response", "body", "items", "item", "data", "records"):
        if key in node:
            return _json_items(node[key])
    return []


def parse_json(payload: Union[str, bytes]) -> list[dict]:
    text = payload.decode("utf-8-sig") if isinstance(payload, bytes) else payload.lstrip("﻿")
    return [r for r in _json_items(json.loads(text)) if isinstance(r, dict)]


def decode_bytes(raw: bytes) -> str:
    """UTF-8 (BOM optional) first, then CP949 (공공데이터 CSV 기본 인코딩)."""
    for enc in ("utf-8-sig", "cp949"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError("unsupported text encoding (expected UTF-8 or CP949)")


def parse_csv(payload: Union[str, bytes]) -> list[dict]:
    text = decode_bytes(payload) if isinstance(payload, bytes) else payload.lstrip("﻿")
    return [dict(r) for r in csv.DictReader(io.StringIO(text))]


def detect_format(payload: Union[str, bytes]) -> str:
    head = (decode_bytes(payload) if isinstance(payload, bytes) else payload).lstrip("﻿ \t\r\n")[:1]
    if head == "<":
        return "xml"
    if head in ("{", "["):
        return "json"
    return "csv"


def parse_payload(payload: Union[str, bytes], fmt: Optional[str] = None) -> list[dict]:
    fmt = (fmt or detect_format(payload)).lower()
    if fmt == "xml":
        records = parse_xml(payload)
    elif fmt == "json":
        records = parse_json(payload)
    elif fmt == "csv":
        records = parse_csv(payload)
    else:
        raise ValueError(f"unsupported format: {fmt!r}")
    return [_canon(r) for r in records]


def region_from_address(address: str) -> tuple[str, str]:
    parts = _clean(address).split()
    if not parts:
        return "", ""
    sido = SIDO_SHORT.get(parts[0], parts[0])
    return sido, parts[1] if len(parts) > 1 else ""


def hospital_from_record(record: dict, verified_on: str) -> Optional[tuple[MoonlightHospital, list[HospitalHourSlot]]]:
    """None when the record lacks an id, a name or a resolvable region."""
    hid, name, addr = _clean(record.get("hpid")), _clean(record.get("dutyName")), _clean(record.get("dutyAddr"))
    sido, sigungu = region_from_address(addr)
    if not hid or not name or not sido:
        return None
    slots = slots_from_record(record)
    weekday = slots[0]
    weekend = f"토 {slot_text(slots[5])} · 일 {slot_text(slots[6])}"
    hosp = MoonlightHospital(
        hospital_id=hid, name=name, region_sido=sido, region_sigungu=sigungu, address=addr,
        tel=_clean(record.get("dutyTel1")), weekday_hours=f"평일(월) {slot_text(weekday)}",
        weekend_hours=weekend, holiday_hours=f"공휴일 {slot_text(slots[HOLIDAY_DOW])}",
        paired_pharmacy=NIGHT_PHARMACY_LABEL, paired_pharmacy_tel="", map_url=naver_map(name),
        last_verified_at=verified_on, is_placeholder=False, source=SOURCE_EGEN,
    )
    return hosp, slots


def import_hospitals_payload(db, payload: Union[str, bytes], fmt: Optional[str] = None,
                             replace_official_snapshot: bool = False,
                             now: Optional[datetime] = None) -> dict:
    """Parse + upsert official hospitals and their hours. Returns counts:
    {"parsed", "imported", "skipped", "deactivated", "regions"}."""
    verified_on = (now or datetime.now()).strftime("%Y-%m-%d")
    records = parse_payload(payload, fmt)
    hospitals, hours, skipped = [], {}, 0
    seen: set[str] = set()
    for rec in records:
        parsed = hospital_from_record(rec, verified_on)
        if parsed is None or parsed[0].hospital_id in seen:
            skipped += 1
            continue
        hosp, slots = parsed
        seen.add(hosp.hospital_id)
        hospitals.append(hosp)
        hours[hosp.hospital_id] = slots
    imported = db.upsert_hospitals(hospitals) if hospitals else 0
    for hid, slots in hours.items():
        db.replace_hospital_hours(hid, slots)
    regions = sorted({h.region_sido for h in hospitals})
    deactivated = 0
    if replace_official_snapshot and hospitals:
        deactivated = db.deactivate_stale_official_hospitals(seen, regions)
    return {"parsed": len(records), "imported": imported, "skipped": skipped,
            "deactivated": deactivated, "regions": regions}


def import_hospitals_file(db, path: Union[str, Path], fmt: Optional[str] = None,
                          replace_official_snapshot: bool = False, now: Optional[datetime] = None) -> dict:
    p = Path(path)
    raw = p.read_bytes()
    if fmt is None:
        ext = p.suffix.lower().lstrip(".")
        fmt = ext if ext in ("xml", "json", "csv") else None
    payload: Union[str, bytes] = raw if fmt in (None, "xml") else decode_bytes(raw)
    return import_hospitals_payload(db, payload, fmt=fmt, replace_official_snapshot=replace_official_snapshot,
                                    now=now)


def summarize_hours(slots: Iterable[HospitalHourSlot]) -> str:
    """'월 09:00~23:00 · 화 ...' — 확인 필요 for unparsed days."""
    by_day = {s.day_of_week: s for s in slots}
    return " · ".join(f"{DAY_LABELS[d]} {slot_text(by_day.get(d))}" for d in range(8))
