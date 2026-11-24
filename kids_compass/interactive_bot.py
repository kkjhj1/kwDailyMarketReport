"""Interactive 2-Tap bot for Kids Compass (키즈 나침반) — 1:1 assistant (Phase 2-B).

[TG-04] callback_data = 'k:<domain>[:<action>[:<args>]]' (ASCII, <= 64 bytes). answerCallbackQuery is
always called first — before parsing, DB reads or rendering. Screens update in place via _safe_edit_or_send.
Deleting a child or pregnancy profile is an in-place 2-step confirm.
Weekend picks: 'k:exp:a:<age>:<weather>[:<walk|all>]' — walk = 현장직행 (booking_required == False only).

1:1 assistant domains:
- ob   대화형 온보딩 (kids_onboarding_state): c=아이 등록, p=임신 등록, x=취소, r:<SEL|GGI|ICN>=지역 선택
- ch   p:<id> 대표 아이 전환 / x·xy:<id> 삭제 2단계
- hub  목적별 Pull 허브: pl 놀이·체험 / ln 학습·과학·경제 / out 야외·나들이 / med 병원·해열제·검진 ([:t] = 형제 함께)
- fb   v|x:<opaque token> 장소 피드백 (DB 소유권·만료 검증, 위조/만료는 무해한 no-op)
- ms   ok:<milestone_id> 검진·접종 확인 완료 (소유권 검증) / l 일정 보기
- set  h|w|d 알림 토글, m 주말 모드(TOGETHER/PRIMARY), r:<code> 지역

Push builders (Silence Rule: 미등록자·후보 0건·이미 발송 → 미발송, 사용자당 1통):
build_milestone_push_messages / build_weekend_push_messages / build_dday_push_messages -> list[PushMessage].
"""
from __future__ import annotations
import hashlib
import ipaddress
import logging
import math
import os
import re
import secrets
import socket
import urllib.error
from urllib.parse import urlparse
from dataclasses import replace
from datetime import date, datetime, timedelta
from typing import Any, Callable, Iterable, Optional

from market_brief.channels.http_keepalive import answer_callback_once, callback_ack_scope

from kids_compass.channels.telegram import (
    PUSH_MESSAGE_SENTINEL,
    esc,
    format_category_html,
    format_cross_dosing_html,
    format_d14_milestone_push_html,
    format_dose_timer_push_html,
    format_dday_alert_html,
    format_fever_emergency_html,
    format_fever_menu_html,
    format_fever_result_html,
    format_fever_weight_pick_html,
    format_home_html,
    format_home_play_html,
    format_dev_guide_html,
    format_food_detail_html,
    format_food_hub_html,
    format_food_list_html,
    format_hospital_list_html,
    format_hub_picks_html,
    format_milestone_push_html,
    format_pregnancy_html,
    format_weekend_briefing_html,
    format_weekend_push_html,
    kg_text,
    TelegramAPIError,
)
from kids_compass.collector import (
    MILESTONE_SCHEDULE_VERSION,
    PREGNANCY_BENEFITS,
    WEEKLY_BOOKING_IDS,
    milestone_window,
    recommend_by_category,
    recommend_weekend_picks,
)
from kids_compass.fever_engine import (
    DEFAULT_MEDICINES,
    INFANT_BLOCK_MONTHS,
    NSAID_MIN_MONTHS,
    calculate_all,
    check_24h_dose_limit,
    cross_dosing_candidates,
)
from kids_compass.food_safety import (
    FoodItem,
    get_food_item,
    list_foods_by_category,
    search_foods,
)
from kids_compass.home_play import (
    DEV_STAGE_LABELS,
    PLAY_STAGE_LABELS,
    DevStage,
    PlayStage,
    dev_stage_for_months,
    get_dev_guides,
    pick_home_plays,
    play_stage_for_months,
)
from kids_compass.models import AgeGroup, ExperienceCategory, ExperienceItem, IngredientType, PushMessage, WeatherSnapshot
from kids_compass.storage.db import (
    SENT_BLOCKING_STATES,
    KidsCompassDatabase,
    _platform_active_subscriber_ids,
)
from kids_compass.web_guide import CONTENT_ID_RE
from kids_compass.weather import fetch_weekend_weather, weekend_outlook

logger = logging.getLogger(__name__)

CB_PREFIX = "k"
MAX_CALLBACK_BYTES = 64
KIDS_WEB_DEFAULT = "http://161.33.196.211:8002"
REGION_CODES: dict[str, str] = {"SEL": "서울", "GGI": "경기", "ICN": "인천"}
WEATHER_CODES: dict[str, tuple[bool, str]] = {
    "sunny": (False, "☀️ 맑음 기준 · 야외+실내 균형 추천"),
    "rain": (True, "☔ 비·미세먼지 기준 · 실내만 추천"),
}
QUICK_WEIGHTS_6M: tuple[int, ...] = (5, 7, 9, 11, 13, 15, 18, 20, 25, 30)
QUICK_WEIGHTS_4_5M: tuple[int, ...] = (6, 7, 8, 9)
AGE_FOR_6M_PLUS = 6   # quick-weight path assumes the youngest age allowed for all three products
AGE_FOR_4_5M = 5      # NSAID blocked, acetaminophen calculated
DDAY_WINDOW_DAYS = 14
DISPATCH_WEATHER_SUMMARY = "날씨 미연동 · 비 오면 봇에서 ☔ 실내 추천을 눌러 주세요"
EXP_MODES = ("walk", "all")  # walk = 현장직행 (no booking needed), all = 예약 포함 전체
PREG_MAX_DAYS_AHEAD = 300    # ~42주
PREG_MAX_DAYS_OVERDUE = 14
CHILD_MAX_AGE_YEARS = 14

# Onboarding state machine (kids_onboarding_state)
ONBOARDING_TTL = timedelta(minutes=30)
ST_CHILD_NICKNAME, ST_CHILD_BIRTH, ST_CHILD_REGION = "CHILD_NICKNAME", "CHILD_BIRTH_DATE", "CHILD_REGION"
ST_PREG_NICKNAME, ST_PREG_DUE, ST_PREG_REGION = "PREGNANCY_NICKNAME", "PREGNANCY_DUE_DATE", "PREGNANCY_REGION"
ST_FOOD_SEARCH = "FOOD_SEARCH"
ST_FEEDBACK_INPUT = "FEEDBACK_INPUT"
REGION_STEPS = (ST_CHILD_REGION, ST_PREG_REGION)

# Push policy
PUSH_PICK_LIMIT = 2
DDAY_PUSH_LIMIT = 2
MILESTONE_LOOKAHEAD_DAYS = 7       # weekly push: windows overlapping the next 7 days
MILESTONE_PULL_LOOKAHEAD_DAYS = 30
REC_TOKEN_TTL = timedelta(days=14)
FEEDBACK_ACTIONS = {"v": "VISITED", "x": "NOT_INTERESTED"}
STREAM_WEEKEND, STREAM_DDAY, STREAM_MILESTONE = "weekend", "dday", "milestone"
STREAM_MILESTONE_D14 = "milestone_d14"

# Pull hub: code -> (title, categories | None for 야외·나들이)
HUB_MENUS: dict[str, tuple[str, Optional[tuple[ExperienceCategory, ...]]]] = {
    "pl": ("🎨 놀이·체험", (ExperienceCategory.PLAY, ExperienceCategory.SAFETY)),
    "ln": ("🔬 학습·과학·경제", (ExperienceCategory.SCIENCE, ExperienceCategory.ECONOMY)),
    "out": ("🌳 야외·나들이", None),
}
HUB_PICK_LIMIT = 4

_NICK_RE = re.compile(r"^[0-9A-Za-z가-힣]{1,10}$")
_DATE_COMPACT_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
_DATE_ISO_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_CHILD_CMD_RE = re.compile(r"^/child(?:@\w+)?\s+([0-9A-Za-z가-힣]{1,10})\s+(\d{4}-\d{2}-\d{2})\s+(\d{1,3}(?:\.\d)?)\s*$")
_PREG_CMD_RE = re.compile(r"^/preg(?:@\w+)?\s+([0-9A-Za-z가-힣]{1,10})\s+(\d{4}-\d{2}-\d{2})\s*$")

WeatherFetcher = Callable[[str, date], list[WeatherSnapshot]]


def default_exp_mode(age: AgeGroup) -> str:
    """4~7세 defaults to 현장직행; PREGNANCY has no walk-in mode (its spots are curated separately)."""
    return "walk" if age == AgeGroup.PRESCHOOL else "all"


def cb(*parts: object) -> str:
    data = ":".join([CB_PREFIX, *(str(p) for p in parts)])
    if not data.isascii() or len(data.encode("utf-8")) > MAX_CALLBACK_BYTES:
        raise ValueError(f"invalid callback_data: {data!r}")
    return data


def _btn(text: str, data: str) -> dict[str, str]:
    return {"text": text, "callback_data": data}


class _UrlButton(dict[str, str]):
    """URL button that keeps legacy callback-only test helpers harmless.

    The compatibility fallback is virtual: it is not present in the mapping
    sent to Telegram, so the button still has exactly one action (its URL).
    """

    def __getitem__(self, key: str) -> str:
        if key == "callback_data" and key not in self:
            return ""
        return super().__getitem__(key)

    def get(self, key: str, default: Any = None) -> Any:
        if key == "callback_data" and key not in self:
            return ""
        return super().get(key, default)


def _url_btn(text: str, url: str) -> dict[str, str]:
    return _UrlButton(text=text, url=url)


def _kb(rows: list[list[dict[str, str]]]) -> dict[str, Any]:
    return {"inline_keyboard": rows}


def _chunks(items: list, size: int) -> list[list]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def parse_user_date(text: str) -> Optional[date]:
    """'YYYYMMDD' or 'YYYY-MM-DD' -> date; anything else (or an impossible date) -> None."""
    m = _DATE_COMPACT_RE.match(text.strip()) or _DATE_ISO_RE.match(text.strip())
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _age_short(age: AgeGroup) -> str:
    return age.label.split(" (")[0]


def _round_robin(lists: list[list[ExperienceItem]], limit: int) -> list[ExperienceItem]:
    """Coverage merge: 1st pick of each subject, then 2nd, ... (dedupe by place and venue)."""
    out: list[ExperienceItem] = []
    seen_ids: set[str] = set()
    seen_venues: set[str] = set()
    depth = max((len(lst) for lst in lists), default=0)
    for i in range(depth):
        for lst in lists:
            if len(out) >= limit:
                return out
            if i < len(lst):
                e = lst[i]
                if e.exp_id in seen_ids or e.venue_name in seen_venues:
                    continue
                seen_ids.add(e.exp_id)
                seen_venues.add(e.venue_name)
                out.append(e)
    return out


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def push_window(day: date) -> str:
    """Dispatch window shared by the push builders and the dispatcher (ISO week, e.g. '2026-W47')."""
    y, w, _ = day.isocalendar()
    return f"{y}-W{w:02d}"


HOME_ROW = [_btn("🏠 홈", cb("home"))]
CANCEL_ROW = [_btn("❌ 취소", cb("ob", "x"))]


# ── Keyboards ─────────────────────────────────────────────────
def build_home_keyboard(children: Optional[list] = None, has_profile: bool = False) -> dict[str, Any]:
    """미등록자: 등록 버튼 최상단 + 응급(해열제·달빛병원). 등록자: 아이 스위처 + 목적별 Pull 허브."""
    children = children or []
    if not has_profile and not children:
        return _kb([
            [_btn("➕ 아이 등록", cb("ob", "c")), _btn("🤰 임신 중이에요", cb("ob", "p"))],
            [_btn("🏠 집콕놀이·발달", cb("hp"))],
            [_btn("🥣 먹거리·이유식", cb("fd"))],
            [_btn("🌡 해열제 계산", cb("fvr")), _btn("🌙 달빛병원", cb("hsp"))],
            [_btn("🎈 주말 나들이", cb("exp")), _btn("⏰ 예약 D-Day", cb("dday"))],
            [_btn("🤰 임신기 태교·바우처", cb("preg")), _btn("🔔 알림 설정", cb("set"))],
            [_btn("💬 의견·오류 제보", cb("inq"))],
        ])
    rows: list[list[dict[str, str]]] = []
    if len(children) > 1:
        rows += _chunks([_btn(("⭐ " if c.is_primary else "") + c.nickname, cb("ch", "p", c.child_id))
                         for c in children[:4]], 3)
        rows.append([_btn("👨‍👩‍👧 형제 함께 보기", cb("hub", "out", "t"))])
    rows += [
        [_btn("🏠 집콕놀이·발달", cb("hp")), _btn("🎈 나들이·예약", cb("exp"))],
        [_btn("🥣 먹거리·이유식", cb("fd"))],
        [_btn("🎨 놀이·체험", cb("hub", "pl")), _btn("🔬 학습·과학·경제", cb("hub", "ln"))],
        [_btn("🌳 야외·나들이", cb("hub", "out")), _btn("🏥 병원·해열제·검진", cb("hub", "med"))],
        [_btn("🤰 지원금·임신", cb("preg")), _btn("⚙️ 아이·알림 설정", cb("set"))],
        [_btn("⏰ 예약 D-Day", cb("dday")), _btn("🎈 연령별 나들이", cb("exp"))],
        [_btn("💬 의견·오류 제보", cb("inq"))],
    ]
    return _kb(rows)


FOOD_CATEGORY_BUTTONS: tuple[tuple[str, str, str], ...] = (
    ("🚫 금지·질식 주의", "FORBIDDEN", "금지·질식 주의"),
    ("🥩 단백질", "PROTEIN", "단백질"),
    ("🥕 과일·채소", "PRODUCE", "과일·채소"),
    ("🥣 유제품·곡물", "DAIRY_GRAIN", "유제품·곡물"),
)


def build_food_hub_keyboard() -> dict[str, Any]:
    rows = [[_btn(label, cb("fd", "c", category)) for label, category, _ in FOOD_CATEGORY_BUTTONS[:2]],
            [_btn(label, cb("fd", "c", category)) for label, category, _ in FOOD_CATEGORY_BUTTONS[2:]],
            [_btn("🔍 음식 이름 검색", cb("fd", "q"))],
            HOME_ROW]
    return _kb(rows)


def build_food_list_keyboard(items: list[FoodItem]) -> dict[str, Any]:
    rows = [[_btn(f"🥣 {item.name}", cb("fd", "i", item.food_id))] for item in items[:8]]
    rows += [[_btn("🔍 음식 이름 검색", cb("fd", "q"))], [_btn("◀ 먹거리 사전", cb("fd")), *HOME_ROW]]
    return _kb(rows)


def get_kids_guide_url(content_id: str) -> Optional[str]:
    """Return a safe public guide URL, or None when the configured base is unsafe."""
    if not isinstance(content_id, str) or not CONTENT_ID_RE.fullmatch(content_id):
        return None
    base = (
        os.environ.get("KIDS_WEB_BASE_URL", "").strip()
        or os.environ.get("REPORT_BASE_URL", "").strip()
        or KIDS_WEB_DEFAULT
    )
    try:
        parsed = urlparse(base)
        scheme = parsed.scheme.lower()
        host = (parsed.hostname or "").lower().rstrip(".")
        if scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
            return None
        if host == "localhost" or host.endswith(".localhost"):
            return None
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and (
            address.is_private or address.is_loopback or address.is_unspecified
            or address.is_link_local or address.is_reserved
        ):
            return None
        _ = parsed.port
    except (TypeError, ValueError):
        return None
    return f"{base.rstrip('/')}/kids/guide/{content_id}"


def build_food_detail_keyboard(content_id: Optional[str] = None) -> dict[str, Any]:
    rows = [[_btn("◀ 먹거리 사전", cb("fd")), _btn("🔍 다시 검색", cb("fd", "q"))]]
    if content_id and (url := get_kids_guide_url(content_id)):
        rows.append([_url_btn("📖 상세 가이드 보기", url)])
    rows.append(HOME_ROW)
    return _kb(rows)


def build_home_play_stage_keyboard() -> dict[str, Any]:
    """All manual play buckets, including pregnancy, for unregistered users."""
    rows = [[_btn(PLAY_STAGE_LABELS[stage], cb("hp", "p", stage))]
            for stage in (PlayStage.M0_11.value, PlayStage.M12_23.value, PlayStage.M24_47.value,
                          PlayStage.Y4_7.value, PlayStage.Y8_13.value, PlayStage.PREG.value)]
    rows.append(HOME_ROW)
    return _kb(rows)


def build_home_play_keyboard(stage: str, offset: int = 0, db: Optional[KidsCompassDatabase] = None) -> dict[str, Any]:
    """Navigation for a home-play card.  Callback payloads stay ASCII and short."""
    items = pick_home_plays(stage, offset=offset, limit=2, db=db)
    next_offset = (int(offset) + len(items)) % 3
    rows = [
        [_btn("🎲 다른 놀이", cb("hp", "n", stage, next_offset)),
         _btn("🌱 이 시기 발달·훈육 가이드", cb("hp", "d", stage))],
        [_btn("🔄 다른 연령 보기", cb("hp", "s"))],
    ]
    if items and (url := get_kids_guide_url(items[0].play_id)):
        rows.append([_url_btn("📖 상세 가이드 보기", url)])
    rows.append(HOME_ROW)
    return _kb(rows)


def build_dev_guide_keyboard(stage: str, db: Optional[KidsCompassDatabase] = None,
                             content_id: Optional[str] = None) -> dict[str, Any]:
    guide_id = content_id
    if not guide_id:
        dev_stage = stage if stage in DEV_STAGE_LABELS else {
            PlayStage.M0_11.value: DevStage.D0_3.value,
            PlayStage.M12_23.value: DevStage.D13_24.value,
            PlayStage.M24_47.value: DevStage.D25_36.value,
            PlayStage.Y4_7.value: DevStage.DY4_7.value,
            PlayStage.Y8_13.value: DevStage.DY8_13.value,
            PlayStage.PREG.value: DevStage.DPREG.value,
        }.get(stage, stage)
        guides = get_dev_guides(dev_stage, db=db)
        guide_id = guides[0].guide_id if guides else None
    rows = [
        [_btn("🏠 놀이 보기", cb("hp", "p", stage)), _btn("🔄 다른 연령 보기", cb("hp", "s"))],
    ]
    if guide_id and (url := get_kids_guide_url(guide_id)):
        rows.append([_url_btn("📖 상세 가이드 보기", url)])
    rows.append(HOME_ROW)
    return _kb(rows)


def build_fever_keyboard(children: list) -> dict[str, Any]:
    rows = [[_btn(f"👧 {c.nickname} ({c.weight_kg:g}kg)" if c.weight_kg else f"👧 {c.nickname} (체중 선택)",
                  cb("fvr", "c", c.child_id))] for c in children[:4]]
    rows.append([_btn("🚨 생후 3개월 미만", cb("fvr", "m3"))])
    rows += _chunks([_btn(f"4~5개월 {kg}kg", cb("fvr", "m5", kg)) for kg in QUICK_WEIGHTS_4_5M], 2)
    rows += _chunks([_btn(f"{kg}kg", cb("fvr", "w", kg)) for kg in QUICK_WEIGHTS_6M], 5)
    rows.append([_btn("🔥 열이 안 떨어져요", cb("fvr", "x"))])
    rows.append([_btn("🌙 달빛병원", cb("hsp")), *HOME_ROW])
    return _kb(rows)


def build_child_weight_keyboard(child_id: int, months: int) -> dict[str, Any]:
    weights = QUICK_WEIGHTS_4_5M if months < NSAID_MIN_MONTHS else QUICK_WEIGHTS_6M
    rows = _chunks([_btn(f"{kg}kg", cb("fvr", "cw", child_id, kg)) for kg in weights], 5)
    rows.append([_btn("◀ 계산기", cb("fvr")), *HOME_ROW])
    return _kb(rows)


def build_fever_result_keyboard() -> dict[str, Any]:
    return _kb([
        [_btn("🔥 열이 안 떨어져요", cb("fvr", "x")), _btn("🌙 달빛병원", cb("hsp"))],
        [_btn("◀ 계산기", cb("fvr")), *HOME_ROW],
    ])


def build_hospital_region_keyboard() -> dict[str, Any]:
    return _kb([[_btn(name, cb("hsp", "r", code)) for code, name in REGION_CODES.items()], HOME_ROW])


def build_hospital_list_keyboard() -> dict[str, Any]:
    return _kb([[_btn("◀ 지역 선택", cb("hsp")), _btn("🌡 해열제", cb("fvr"))], HOME_ROW])


def build_exp_age_keyboard() -> dict[str, Any]:
    rows = []
    for ag in (AgeGroup.PREGNANCY, AgeGroup.INFANT, AgeGroup.PRESCHOOL, AgeGroup.ELEMENTARY):
        short = ag.label.split(" (")[0]
        mode = default_exp_mode(ag)
        rows.append([_btn(f"{short} ☀️", cb("exp", "a", ag.value, "sunny", mode)),
                     _btn(f"{short} ☔", cb("exp", "a", ag.value, "rain", mode))])
    rows += _chunks([_btn(c.label, cb("exp", "c", c.value)) for c in ExperienceCategory], 2)
    rows.append([_btn("⏰ 예약 D-Day", cb("dday")), *HOME_ROW])
    return _kb(rows)


def build_exp_result_keyboard(age: Optional[AgeGroup] = None, weather: Optional[str] = None,
                              mode: Optional[str] = None) -> dict[str, Any]:
    rows = []
    if age is not None:
        mode = mode or default_exp_mode(age)
        other = "rain" if weather == "sunny" else "sunny"
        row = [_btn("☔ 실내만 보기" if other == "rain" else "☀️ 맑음 추천", cb("exp", "a", age.value, other, mode))]
        if age != AgeGroup.PREGNANCY:
            flip = "all" if mode == "walk" else "walk"
            row.append(_btn("📅 예약 포함 전체" if flip == "all" else "⚡ 현장직행만",
                            cb("exp", "a", age.value, weather or "sunny", flip)))
        rows.append(row)
    rows.append([_btn("◀ 연령·테마", cb("exp")), _btn("⏰ 예약 D-Day", cb("dday"))])
    rows.append(HOME_ROW)
    return _kb(rows)


def build_hub_result_keyboard(code: str, together: bool, multi_child: bool) -> dict[str, Any]:
    rows = []
    if multi_child:
        rows.append([_btn("⭐ 대표 아이만" if together else "👨‍👩‍👧 형제 함께",
                          cb("hub", code) if together else cb("hub", code, "t"))])
    rows.append([_btn("🎨 놀이", cb("hub", "pl")), _btn("🔬 학습", cb("hub", "ln")), _btn("🌳 야외", cb("hub", "out"))])
    rows.append(HOME_ROW)
    return _kb(rows)


def build_med_hub_keyboard() -> dict[str, Any]:
    return _kb([
        [_btn("🌡 해열제 계산", cb("fvr")), _btn("🌙 달빛병원", cb("hsp"))],
        [_btn("🩺 검진·접종 일정 보기", cb("ms", "l"))],
        HOME_ROW,
    ])


def build_milestone_keyboard(items: list[dict], limit: int = 6) -> dict[str, Any]:
    buttons = [_btn(f"✅ {n} 확인 완료", cb("ms", "ok", it["milestone_id"])) for n, it in enumerate(items[:limit], 1)]
    return _kb(_chunks(buttons, 2) + [HOME_ROW])


def build_settings_keyboard(profile: dict, children: list, pregnancy=None) -> dict[str, Any]:
    hl = "ON" if profile.get("alert_health_dday") else "OFF"
    wk = "ON" if profile.get("alert_weekend_brief") else "OFF"
    dd = "ON" if profile.get("alert_dday_reserve") else "OFF"
    together = (profile.get("alert_weekend_mode") or "TOGETHER") == "TOGETHER"
    rows = [
        [_btn("💬 의견·오류 제보", cb("inq"))],
        [_btn(f"🩺 검진·접종 알림 {hl}", cb("set", "h"))],
        [_btn(f"🎈 목 17시 주말추천 {wk}", cb("set", "w"))],
        [_btn(f"⏰ 월 9시 예약 D-Day {dd}", cb("set", "d"))],
        [_btn("👨‍👩‍👧 주말추천: 형제 함께" if together else "⭐ 주말추천: 대표 아이만", cb("set", "m"))],
        [_btn(("✅ " if profile.get("default_region") == name else "") + name, cb("set", "r", code))
         for code, name in REGION_CODES.items()],
    ]
    for c in children[:4]:
        row = [] if c.is_primary else [_btn(f"⭐ {c.nickname} 대표로", cb("ch", "p", c.child_id))]
        rows.append(row + [_btn(f"🗑 {c.nickname} 삭제", cb("ch", "x", c.child_id))])
    if pregnancy is not None:
        rows.append([_btn(f"🗑 {pregnancy.nickname} 임신정보 삭제", cb("preg", "x"))])
    add_row = [_btn("➕ 아이 등록", cb("ob", "c"))]
    if pregnancy is None:
        add_row.append(_btn("🤰 임신 등록", cb("ob", "p")))
    rows.append(add_row)
    rows.append(HOME_ROW)
    return _kb(rows)


def build_delete_confirm_keyboard(child_id: int) -> dict[str, Any]:
    return _kb([[_btn("🗑 네, 삭제", cb("ch", "xy", child_id)), _btn("취소", cb("set"))]])


def build_pregnancy_keyboard(has_profile: bool) -> dict[str, Any]:
    rows = [[_btn("🌿 태교 산책 ☀️", cb("exp", "a", AgeGroup.PREGNANCY.value, "sunny", "all")),
             _btn("🌿 실내 산책 ☔", cb("exp", "a", AgeGroup.PREGNANCY.value, "rain", "all"))]]
    if has_profile:
        rows.append([_btn("🗑 임신 정보 삭제", cb("preg", "x"))])
    else:
        rows.append([_btn("🤰 임신 정보 등록", cb("ob", "p"))])
    rows.append(HOME_ROW)
    return _kb(rows)


def build_pregnancy_delete_confirm_keyboard() -> dict[str, Any]:
    return _kb([[_btn("🗑 네, 삭제", cb("preg", "xy")), _btn("취소", cb("preg"))]])


def build_onboarding_region_keyboard() -> dict[str, Any]:
    return _kb([[_btn(name, cb("ob", "r", code)) for code, name in REGION_CODES.items()], CANCEL_ROW])


def build_weekend_feedback_keyboard(pairs: list[tuple[ExperienceItem, str]]) -> dict[str, Any]:
    rows = [[_btn(f"{n} 가봄", cb("fb", "v", token)), _btn(f"{n} 관심없음", cb("fb", "x", token))]
            for n, (_, token) in enumerate(pairs, 1)]
    rows.append(HOME_ROW)
    return _kb(rows)


def build_dday_push_keyboard(events: list) -> dict[str, Any]:
    rows = [[_btn("🚫 이 예약 안 보기", cb("dd", "m", ev.booking_id))] for ev in events]
    rows += [[_btn("⏰ 전체 D-Day 보기", cb("dday"))], HOME_ROW]
    return _kb(rows)


# ── Shared subject / push helpers ─────────────────────────────
def _subjects(db: KidsCompassDatabase, uid: str, today: date, together: bool) -> tuple[list[AgeGroup], str]:
    """(age groups, label). Children win over pregnancy; TOGETHER covers every child, else the ⭐ primary."""
    children = db.list_children(uid)
    if children:
        if together and len(children) > 1:
            ages = list(dict.fromkeys(c.age_group(today) for c in children))
            return ages, "·".join(c.nickname for c in children[:3]) + " 함께"
        primary = children[0]  # list_children orders is_primary first
        age = primary.age_group(today)
        return [age], f"{primary.nickname} · {_age_short(age)}"
    preg = db.get_pregnancy(uid)
    if preg is not None:
        return [AgeGroup.PREGNANCY], f"{preg.nickname} 태교"
    return [AgeGroup.ALL], "전 연령"


def _push_targets(db: KidsCompassDatabase, kind: str) -> list[dict]:
    """Alert ON (+ platform SSOT inside list_alert_subscribers) AND a registered child/pregnancy."""
    return [p for p in db.list_alert_subscribers(kind) if db.has_active_profile(p["telegram_user_id"])]


def _push_already_claimed(db: KidsCompassDatabase, uid: str, stream: str, entity_type: str, window: str) -> bool:
    return db.get_sent_status(uid, stream, entity_type, PUSH_MESSAGE_SENTINEL, window) in SENT_BLOCKING_STATES


def _sent_entity_ids(db: KidsCompassDatabase, uid: str, stream: str, entity_type: str) -> set[str]:
    """Entities already pushed in ANY window (IN_FLIGHT / SENT / UNKNOWN / BLOCKED) — never re-pushed."""
    marks = ",".join("?" * len(SENT_BLOCKING_STATES))
    rows = db.conn.execute(
        f"SELECT entity_id FROM kids_sent_history WHERE telegram_user_id = ? AND stream = ? AND entity_type = ? "
        f"AND status IN ({marks})",
        (str(uid), stream, entity_type, *SENT_BLOCKING_STATES),
    ).fetchall()
    return {r[0] for r in rows} - {PUSH_MESSAGE_SENTINEL}


def sync_relevant_milestones(db: KidsCompassDatabase, uid: str, today: date, lookahead_days: int) -> int:
    """ensure_subject_milestone only for items whose window (from the child's months / the pregnancy week)
    overlaps [today, today + lookahead]. Idempotent — user answers are never reset."""
    end = today + timedelta(days=lookahead_days)
    children = db.list_children(uid)
    preg = db.get_pregnancy(uid)
    n = 0
    for defn in db.list_milestone_definitions(schedule_version=MILESTONE_SCHEDULE_VERSION):
        if defn.kind == "PREGNANCY":
            subjects = [("PREGNANCY", uid, {"due_date": preg.due_date})] if preg is not None else []
        else:
            subjects = [("CHILD", c.child_id, {"birth_date": c.birth_date}) for c in children]
        for kind, sid, kw in subjects:
            frm, to = milestone_window(defn, **kw)
            if date.fromisoformat(frm) <= end and date.fromisoformat(to) >= today:
                db.ensure_subject_milestone(uid, kind, sid, defn.definition_id, defn.schedule_version, frm, to)
                n += 1
    return n


def collect_due_milestone_items(db: KidsCompassDatabase, uid: str, today: date, lookahead_days: int,
                                exclude_ids: Optional[set[str]] = None) -> list[dict]:
    """DUE rows only, for subjects that still exist (a deleted child's rows are ignored)."""
    sync_relevant_milestones(db, uid, today, lookahead_days)
    children = {str(c.child_id): c.nickname for c in db.list_children(uid)}
    preg = db.get_pregnancy(uid)
    defs = {d.definition_id: d for d in db.list_milestone_definitions()}
    exclude_ids = exclude_ids or set()
    items = []
    for m in db.list_due_milestones(uid, today, lookahead_days):
        if str(m.milestone_id) in exclude_ids or m.definition_id not in defs:
            continue
        if m.subject_kind == "CHILD":
            subject = children.get(m.subject_id)
        else:
            subject = f"{preg.nickname} (임신 {preg.gestational_week(today)}주)" if preg is not None else None
        if subject is None:
            continue
        d = defs[m.definition_id]
        items.append({"milestone_id": m.milestone_id, "subject": subject, "kind": d.kind, "title": d.title,
                      "description": d.description, "due_from": m.due_from, "due_to": m.due_to})
    items.sort(key=lambda it: (it["due_to"], it["milestone_id"]))
    return items


def offline_weather(region: str, day: date) -> list[WeatherSnapshot]:
    """No network: an empty outlook = 'unknown' → indoor-only picks and no weather wording."""
    return []


# ── Push builders (Silence Rule) ──────────────────────────────
def build_milestone_push_messages(db: KidsCompassDatabase, today: date) -> list[PushMessage]:
    window = push_window(today)
    out: list[PushMessage] = []
    for prof in _push_targets(db, "health"):
        uid = str(prof["telegram_user_id"])
        if _push_already_claimed(db, uid, STREAM_MILESTONE, "MILESTONE", window):
            continue
        sent = _sent_entity_ids(db, uid, STREAM_MILESTONE, "MILESTONE")
        items = collect_due_milestone_items(db, uid, today, MILESTONE_LOOKAHEAD_DAYS, exclude_ids=sent)
        if not items:
            continue  # Silence: nothing due
        shown = items[:3]
        text = format_milestone_push_html(items, today, limit=3)
        out.append(PushMessage(chat_id=uid, text=text, reply_markup=build_milestone_keyboard(shown),
                               stream=STREAM_MILESTONE, entity_type="MILESTONE",
                               entity_ids=[str(it["milestone_id"]) for it in shown],
                               content_hash=_content_hash(text)))
    return out


def build_d14_milestone_push_messages(db: KidsCompassDatabase, today: date) -> list[PushMessage]:
    """Build one D-14 reminder per opted-in user, with a week-scoped D14 window."""
    targets = _push_targets(db, "health")
    for prof in targets:
        sync_relevant_milestones(db, str(prof["telegram_user_id"]), today, DDAY_WINDOW_DAYS)
    rows = db.list_d14_milestones(today)
    if not rows:
        return []
    children_by_user = {}
    pregnancy_by_user = {}
    definitions = {d.definition_id: d for d in db.list_milestone_definitions()}
    out: list[PushMessage] = []
    for prof in targets:
        uid = str(prof["telegram_user_id"])
        dispatch_window = f"D14:{push_window(today)}"
        if _push_already_claimed(db, uid, STREAM_MILESTONE_D14, "MILESTONE", dispatch_window):
            continue
        children_by_user[uid] = {str(c.child_id): c.nickname for c in db.list_children(uid)}
        pregnancy_by_user[uid] = db.get_pregnancy(uid)
        sent = _sent_entity_ids(db, uid, STREAM_MILESTONE_D14, "MILESTONE")
        items = []
        for row in rows:
            if str(row.get("telegram_user_id")) != uid or str(row.get("milestone_id")) in sent:
                continue
            status = str(row.get("status") or "")
            if status in ("USER_CONFIRMED", "NOT_APPLICABLE"):
                continue
            if row.get("subject_kind") == "CHILD":
                subject = children_by_user[uid].get(str(row.get("subject_id")))
            else:
                preg = pregnancy_by_user[uid]
                subject = f"{preg.nickname} (임신)" if preg is not None else None
            if not subject or str(row.get("definition_id")) not in definitions:
                continue
            item = dict(row)
            item["subject"] = subject
            item["kind"] = item.get("kind") or definitions[str(row["definition_id"])].kind
            item["title"] = item.get("title") or definitions[str(row["definition_id"])].title
            item["description"] = item.get("description") or definitions[str(row["definition_id"])].description
            items.append(item)
        items.sort(key=lambda it: (str(it.get("due_to") or ""), int(it.get("milestone_id") or 0)))
        if not items:
            continue
        shown = items[:6]
        text = format_d14_milestone_push_html(items, today)
        message = PushMessage(
            chat_id=uid,
            text=text,
            reply_markup=build_milestone_keyboard(shown),
            stream=STREAM_MILESTONE_D14,
            entity_type="MILESTONE",
            entity_ids=[str(it["milestone_id"]) for it in shown],
            content_hash=_content_hash(text),
        )
        # PushMessage is intentionally kept backward-compatible; runners can use this
        # additive marker to select the D14 dispatch window.
        message.dispatch_window = dispatch_window
        out.append(message)
    return out


def dispatch_due_dose_timers(
    db: KidsCompassDatabase,
    channel: Any,
    now_dt: Optional[datetime] = None,
    *,
    now: Optional[datetime] = None,
    startup_cutoff: Optional[str] = None,
) -> dict[str, Any]:
    """Claim and deliver due dose timers exactly once.

    A timeout is deliberately finalized as UNKNOWN: Telegram may have accepted the
    request, so the timer must never be returned to PENDING or sent again.
    """
    if now_dt is not None and now is not None:
        raise TypeError("pass either now_dt or now, not both")
    dispatch_now = now or now_dt or datetime.now()
    claim_token = secrets.token_hex(16)
    active_ids = _platform_active_subscriber_ids(
        db.db_path, ("kids_bot",), conn=db.conn
    )
    timers = db.claim_due_dose_timers(
        dispatch_now,
        claim_token,
        cutoff_iso=startup_cutoff,
        eligible_user_ids=active_ids,
    )
    stats: dict[str, Any] = {
        "total": len(timers), "claimed": len(timers), "sent": 0,
        "unknown": 0, "blocked": 0, "failed": 0, "errors": {},
    }
    for timer in timers:
        child = db.get_child(timer.child_id, timer.telegram_user_id)
        child_name = child.nickname if child is not None else "아이"
        text = format_dose_timer_push_html(child_name, timer, timer.created_at or "")
        markup = _kb([[
            _btn("🌙 지금 문 연 달빛병원", cb("hsp")),
            _btn("✅ 열 내렸어요(종료)", cb("fvr", "tc", timer.child_id)),
        ]])
        status, error = "SENT", None
        try:
            channel.send_message(timer.telegram_user_id, text, reply_markup=markup)
        except TelegramAPIError as exc:
            status = "BLOCKED" if exc.code in (400, 403) else "FAILED"
            error = str(exc)
        except urllib.error.HTTPError as exc:
            status = "BLOCKED" if exc.code in (400, 403) else "FAILED"
            error = str(exc)
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
            status, error = "UNKNOWN", str(exc)
        except Exception as exc:  # noqa: BLE001 - one timer must not stop the sweep
            status, error = "FAILED", str(exc)
        try:
            db.finalize_dose_timer(timer.timer_id, claim_token, status, error or "", now=dispatch_now)
        except Exception:  # noqa: BLE001 - leave the row terminally guarded by its lease
            logger.exception("Failed to finalize dose timer %s", timer.timer_id)
        stats[status.lower()] += 1
        if error:
            stats["errors"][str(timer.timer_id)] = error
    return stats


def build_weekend_push_messages(db: KidsCompassDatabase, today: date, weather_fetcher: Optional[WeatherFetcher] = None,
                                now: Optional[datetime] = None, issue_tokens: bool = True) -> list[PushMessage]:
    """1~2 places per registered user, own region only (no back-fill), feedback/cooldown exclusions applied.
    Rain → indoor only. Failed/unknown forecast → indoor only AND no weather wording. 0 candidates → Silence.
    issue_tokens=False (dry-run) renders placeholder tokens without writing recommendation rows."""
    fetch = weather_fetcher or (lambda region, day: fetch_weekend_weather(region, day, db=db))
    now = now or datetime.now()
    window = push_window(today)
    weather_cache: dict[str, list[WeatherSnapshot]] = {}
    out: list[PushMessage] = []
    for prof in _push_targets(db, "weekend"):
        uid = str(prof["telegram_user_id"])
        if _push_already_claimed(db, uid, STREAM_WEEKEND, "EXPERIENCE", window):
            continue
        region = prof.get("default_region") or "서울"
        if region not in weather_cache:
            try:
                weather_cache[region] = list(fetch(region, today) or [])
            except Exception:  # noqa: BLE001 - a weather outage must never block (or fake) the briefing
                logger.warning("weather fetch failed for %s", region, exc_info=True)
                weather_cache[region] = []
        snaps = weather_cache[region]
        rain, failed = weekend_outlook(snaps)
        indoor_only = rain or failed
        card_weather = None if failed else replace(
            next((s for s in snaps if s.is_rain_or_snow), snaps[0]),
            summary_text=" / ".join(s.summary_text for s in snaps), is_rain_or_snow=rain)

        together = (prof.get("alert_weekend_mode") or "TOGETHER") == "TOGETHER"
        ages, label = _subjects(db, uid, today, together)
        excluded = db.get_excluded_exp_ids(uid, today)
        lists = [recommend_weekend_picks(db, age, region, weather_rain=indoor_only,
                                         is_dispatch=(age == AgeGroup.PRESCHOOL), limit=PUSH_PICK_LIMIT,
                                         allow_backfill=False, excluded_exp_ids=excluded) for age in ages]
        picks = _round_robin(lists, PUSH_PICK_LIMIT)
        if not picks:
            continue  # Silence: no in-region candidate
        if issue_tokens:
            pairs = db.create_rec_items(uid, STREAM_WEEKEND, picks, now + REC_TOKEN_TTL,
                                        coverage={"ages": [a.value for a in ages]}, now=now)
        else:
            pairs = [(p, f"dryrun{n:04d}") for n, p in enumerate(picks, 1)]
        text = format_weekend_push_html(picks, label, card_weather, region)
        out.append(PushMessage(chat_id=uid, text=text, reply_markup=build_weekend_feedback_keyboard(pairs),
                               stream=STREAM_WEEKEND, entity_type="EXPERIENCE",
                               entity_ids=[p.exp_id for p in picks], content_hash=_content_hash(text)))
    return out


def booking_entity_id(ev) -> str:
    """A booking rule re-opens every week/month — the occurrence (id + open time) is the dedupe unit."""
    return f"{ev.booking_id}@{ev.open_datetime}"


def build_dday_push_messages(db: KidsCompassDatabase, now: datetime) -> list[PushMessage]:
    """Only bookings matching my children's age groups AND my region (max 2), never an occurrence already sent."""
    window = push_window(now.date())
    events = db.list_upcoming_bookings(now.strftime("%Y-%m-%d %H:%M"),
                                       (now + timedelta(days=DDAY_WINDOW_DAYS)).strftime("%Y-%m-%d %H:%M"))
    if not events:
        return []
    exp_cache: dict[str, Optional[ExperienceItem]] = {}
    out: list[PushMessage] = []
    for prof in _push_targets(db, "dday"):
        uid = str(prof["telegram_user_id"])
        if _push_already_claimed(db, uid, STREAM_DDAY, "BOOKING", window):
            continue
        region = prof.get("default_region") or "서울"
        children = db.list_children(uid)
        ages = {c.age_group(now.date()) for c in children}
        if db.get_pregnancy(uid) is not None:
            ages.add(AgeGroup.PREGNANCY)
        if children:
            ages.add(AgeGroup.ALL)
        sent = _sent_entity_ids(db, uid, STREAM_DDAY, "BOOKING")
        muted = db.list_muted_booking_ids(uid)
        recent_weekly = db.list_sent_booking_ids_since(
            uid, STREAM_DDAY, (now - timedelta(days=28)).strftime("%Y-%m-%d %H:%M:%S")
        )
        matches = []
        for ev in events:
            if ev.booking_id in muted:
                continue
            if ev.exp_id not in exp_cache:
                exp_cache[ev.exp_id] = db.get_experience(ev.exp_id)
            exp = exp_cache[ev.exp_id]
            if exp is None or exp.region_sido != region or exp.age_group not in ages:
                continue
            if booking_entity_id(ev) in sent:
                continue
            if ev.booking_id in WEEKLY_BOOKING_IDS and ev.booking_id in recent_weekly:
                continue
            matches.append(ev)
            if len(matches) >= DDAY_PUSH_LIMIT:
                break
        if not matches:
            continue  # Silence
        _, label = _subjects(db, uid, now.date(), together=True)
        text = format_dday_alert_html(matches, now, subtitle=f"{label} · {region} 맞춤 예약 오픈")
        out.append(PushMessage(chat_id=uid, text=text,
                               reply_markup=build_dday_push_keyboard(matches),
                               stream=STREAM_DDAY, entity_type="BOOKING",
                               entity_ids=[booking_entity_id(ev) for ev in matches],
                               content_hash=_content_hash(text)))
    return out


# ── Legacy dispatch wrappers (tests / compat) ─────────────────
def iso_week_key(prefix: str, day: date) -> str:
    y, w, _ = day.isocalendar()
    return f"{prefix}:{y}-W{w:02d}"


def build_weekend_messages(db: KidsCompassDatabase, today: date,
                           weather_fetcher: Optional[WeatherFetcher] = None) -> tuple[str, list[tuple[str, str]]]:
    """Compat wrapper over build_weekend_push_messages (new policy: registered users only, own region, Silence).
    Network-free by default (unknown weather → indoor-only, no weather wording)."""
    key = iso_week_key("kids_weekend", today)
    msgs = build_weekend_push_messages(db, today, weather_fetcher=weather_fetcher or offline_weather)
    return key, [(m.chat_id, m.text) for m in msgs]


def build_dday_messages(db: KidsCompassDatabase, now: datetime) -> tuple[str, list[tuple[str, str]]]:
    """Compat wrapper over build_dday_push_messages (personalised; empty when nothing matches)."""
    key = iso_week_key("kids_dday", now.date())
    return key, [(m.chat_id, m.text) for m in build_dday_push_messages(db, now)]


# ── Bot ───────────────────────────────────────────────────────
class KidsCompassInteractiveBot:
    def __init__(self, channel: Any, db: KidsCompassDatabase, clock=datetime.now):
        self.channel = channel
        self.db = db
        self._clock = clock

    def handle_update(self, update: dict[str, Any]) -> None:
        try:
            if "callback_query" in update:
                self._handle_callback(update["callback_query"])
            elif "message" in update:
                self._handle_message(update["message"])
        except Exception:  # noqa: BLE001 - one bad update must not kill the poll loop
            logger.exception("KidsCompass update handling failed")

    def poll_once(self, offset: int = 0) -> int:
        for upd in self.channel.get_updates(offset=offset):
            offset = max(offset, int(upd["update_id"]) + 1)
            self.handle_update(upd)
        return offset

    # ---------- messages ----------
    def _handle_message(self, msg: dict[str, Any]) -> None:
        chat_id = int((msg.get("chat") or {}).get("id") or 0)
        text = str(msg.get("text") or "").strip()
        if not chat_id:
            return
        if not text:
            state = self.db.get_onboarding_state(chat_id, self._clock())
            if state is not None and state.get("step") == ST_FEEDBACK_INPUT:
                self._onboarding_input(chat_id, state, text)
            return
        cmd = text.split()[0].split("@")[0].lower()
        if cmd == "/cancel":
            self.db.clear_onboarding_state(chat_id)
            self.db.ensure_user(chat_id)
            self.channel.send_message(chat_id, "❌ 등록을 취소했어요.")
            self._show_home(chat_id, None)
            return
        if cmd.startswith("/"):
            self.db.clear_onboarding_state(chat_id)  # any other command abandons an open flow
        else:
            state = self.db.get_onboarding_state(chat_id, self._clock())
            if state is not None:
                self._onboarding_input(chat_id, state, text)
                return
        if cmd == "/child":
            self._add_child(chat_id, text)
            return
        if cmd == "/preg" and len(text.split()) > 1:
            self._set_pregnancy(chat_id, text)
            return
        if cmd == "/food":
            query = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""
            if query:
                self._show_food_search(chat_id, None, query)
            else:
                self._show_food(chat_id, None)
            return
        self.db.ensure_user(chat_id)
        screens = {"/fever": self._show_fever_menu, "/hospital": self._show_hospital_regions,
                   "/weekend": self._show_exp_menu, "/dday": self._show_dday, "/preg": self._show_pregnancy,
                   "/food": self._show_food}
        screens.get(cmd, self._show_home)(chat_id, None)

    def _add_child(self, chat_id: int, text: str) -> None:
        m = _CHILD_CMD_RE.match(text)
        error = "형식: <code>/child 별명 2023-05-01 11.2</code> (별명 한글·영문·숫자 10자 이내, 생년월일, 체중kg)"
        if m:
            nickname, birth, weight = m.group(1), m.group(2), float(m.group(3))
            try:
                born = date.fromisoformat(birth)
            except ValueError:
                born = None
            today = self._clock().date()
            if born is None or born > today or born < today.replace(year=today.year - CHILD_MAX_AGE_YEARS):
                error = "생년월일을 확인해 주세요 (최근 14년 이내, 미래 날짜 불가)."
            elif not (1.0 <= weight <= 100.0):
                error = "체중을 확인해 주세요 (1~100kg)."
            else:
                self.db.add_child(chat_id, nickname, birth, weight)
                self.channel.send_message(chat_id, f"✅ <b>{esc(nickname)}</b> 등록 완료!")
                self._show_fever_menu(chat_id, None)
                return
        self.channel.send_message(chat_id, f"⚠️ {error}")

    def _set_pregnancy(self, chat_id: int, text: str) -> None:
        m = _PREG_CMD_RE.match(text)
        error = "형식: <code>/preg 복덩이 2027-03-15</code> (태명 한글·영문·숫자 10자 이내, 출산예정일)"
        if m:
            nickname, due = m.group(1), m.group(2)
            try:
                due_date = date.fromisoformat(due)
            except ValueError:
                due_date = None
            if due_date is None or not self._due_date_ok(due_date):
                error = "출산예정일을 확인해 주세요 (오늘부터 약 10개월 이내)."
            else:
                self.db.set_pregnancy(chat_id, nickname, due)
                self.channel.send_message(chat_id, f"✅ <b>{esc(nickname)}</b> 임신 정보 등록 완료!")
                self._show_pregnancy(chat_id, None)
                return
        self.channel.send_message(chat_id, f"⚠️ {error}")

    def _birth_date_ok(self, born: date) -> bool:
        today = self._clock().date()
        return today.replace(year=today.year - CHILD_MAX_AGE_YEARS) <= born <= today

    def _due_date_ok(self, due: date) -> bool:
        today = self._clock().date()
        return today - timedelta(days=PREG_MAX_DAYS_OVERDUE) <= due <= today + timedelta(days=PREG_MAX_DAYS_AHEAD)

    # ---------- onboarding state machine ----------
    _PROMPTS = {
        ST_CHILD_NICKNAME: "➕ <b>아이 등록</b> (1/3)\n아이 <b>별명</b>을 입력해 주세요.\n예) 콩이 · 한글·영문·숫자 10자 이내",
        ST_CHILD_BIRTH: "➕ <b>아이 등록</b> (2/3)\n<b>생년월일</b>을 입력해 주세요.\n예) 20230501 또는 2023-05-01",
        ST_CHILD_REGION: "➕ <b>아이 등록</b> (3/3)\n주로 다니는 <b>지역</b>을 골라 주세요 👇",
        ST_PREG_NICKNAME: "🤰 <b>임신 등록</b> (1/3)\n<b>태명</b>을 입력해 주세요.\n예) 복덩이 · 한글·영문·숫자 10자 이내",
        ST_PREG_DUE: "🤰 <b>임신 등록</b> (2/3)\n<b>출산예정일</b>을 입력해 주세요.\n예) 20270315 또는 2027-03-15",
        ST_PREG_REGION: "🤰 <b>임신 등록</b> (3/3)\n주로 다니는 <b>지역</b>을 골라 주세요 👇",
    }

    def _start_onboarding(self, chat_id: int, mid: Optional[int], kind: str) -> None:
        step = ST_CHILD_NICKNAME if kind == "CHILD" else ST_PREG_NICKNAME
        self.db.set_onboarding_state(chat_id, secrets.token_hex(4), kind, step, {},
                                     self._clock() + ONBOARDING_TTL)
        self._prompt_step(chat_id, mid, step)

    def _prompt_step(self, chat_id: int, mid: Optional[int], step: str, error: str = "") -> None:
        text = (f"⚠️ {error}\n\n" if error else "") + self._PROMPTS[step]
        kb = build_onboarding_region_keyboard() if step in REGION_STEPS else _kb([CANCEL_ROW])
        self._safe_edit_or_send(chat_id, mid, text, kb)

    def _advance(self, chat_id: int, state: dict, step: str, payload: dict) -> None:
        self.db.set_onboarding_state(chat_id, state.get("flow_id", ""), state.get("kind", ""), step, payload,
                                     self._clock() + ONBOARDING_TTL)
        self._prompt_step(chat_id, None, step)

    def _onboarding_input(self, chat_id: int, state: dict, text: str) -> None:
        step, payload = state.get("step"), dict(state.get("payload") or {})
        if step == ST_FEEDBACK_INPUT:
            if not text or len(text) > 500:
                self.channel.send_message(
                    chat_id,
                    "⚠️ 의견은 1~500자로 입력해 주세요. 다시 입력해 주세요!",
                    _kb([[_btn("❌ 취소", cb("inq", "x"))]]),
                )
                return
            self.db.add_feedback_note(chat_id, text)
            self.db.clear_onboarding_state(chat_id)
            self.channel.send_message(
                chat_id,
                "🙏 의견을 보내 주셔서 고마워요! 더 편하고 정확한 봇을 만드는 데 반영할게요.",
                build_home_keyboard(self.db.list_children(chat_id), self.db.get_pregnancy(chat_id) is not None),
            )
        elif step == ST_FOOD_SEARCH:
            self._show_food_search(chat_id, None, text)
        elif step in (ST_CHILD_NICKNAME, ST_PREG_NICKNAME):
            if not _NICK_RE.match(text):
                self._prompt_step(chat_id, None, step, "한글·영문·숫자 10자 이내로 입력해 주세요 (기호·띄어쓰기 불가).")
                return
            payload["nickname"] = text
            self._advance(chat_id, state, ST_CHILD_BIRTH if step == ST_CHILD_NICKNAME else ST_PREG_DUE, payload)
        elif step == ST_CHILD_BIRTH:
            born = parse_user_date(text)
            if born is None or not self._birth_date_ok(born):
                self._prompt_step(chat_id, None, step, "생년월일을 확인해 주세요 (최근 14년 이내, 미래 날짜 불가).")
                return
            payload["birth_date"] = born.isoformat()
            self._advance(chat_id, state, ST_CHILD_REGION, payload)
        elif step == ST_PREG_DUE:
            due = parse_user_date(text)
            if due is None or not self._due_date_ok(due):
                self._prompt_step(chat_id, None, step, "출산예정일을 확인해 주세요 (오늘부터 약 10개월 이내).")
                return
            payload["due_date"] = due.isoformat()
            self._advance(chat_id, state, ST_PREG_REGION, payload)
        elif step in REGION_STEPS:
            region = text if text in REGION_CODES.values() else None
            if region is None:
                self._prompt_step(chat_id, None, step, "아래 버튼에서 지역을 골라 주세요.")
                return
            self._finish_onboarding(chat_id, None, state, region)
        else:  # unknown / corrupted step → reset
            self.db.clear_onboarding_state(chat_id)
            self._show_home(chat_id, None)

    def _finish_onboarding(self, chat_id: int, mid: Optional[int], state: dict, region: str) -> None:
        payload = dict(state.get("payload") or {})
        nickname = payload.get("nickname") or ""
        if state.get("step") == ST_CHILD_REGION and nickname and payload.get("birth_date"):
            self.db.add_child(chat_id, nickname, payload["birth_date"], None)
            done = f"✅ <b>{esc(nickname)}</b> 등록 완료! 체중은 해열제 계산 때 골라요."
        elif state.get("step") == ST_PREG_REGION and nickname and payload.get("due_date"):
            self.db.set_pregnancy(chat_id, nickname, payload["due_date"])
            done = f"✅ <b>{esc(nickname)}</b> 임신 정보 등록 완료!"
        else:
            self.db.clear_onboarding_state(chat_id)
            self._show_home(chat_id, mid)
            return
        self.db.set_default_region(chat_id, region)
        self.db.clear_onboarding_state(chat_id)
        self.channel.send_message(chat_id, f"{done}\n📍 지역: <b>{esc(region)}</b>")
        self._show_home(chat_id, None)

    # ---------- callbacks ----------
    def _handle_callback(self, cq: dict[str, Any]) -> None:
        # Single-ack scope: joins the manager's scope when dispatched by `DynamicBotManager`.
        with callback_ack_scope(cq.get("id", "")):
            self._handle_callback_scoped(cq)

    def _handle_callback_scoped(self, cq: dict[str, Any]) -> None:
        # [TG-04] Acknowledge first — before parsing, DB work or rendering — to clear the spinner (exactly once)
        cq_id = cq.get("id", "")
        answer_callback_once(cq_id, lambda: self.channel.answer_callback_query(cq_id))

        data = str(cq.get("data") or "")
        msg = cq.get("message") or {}
        chat_id = int((msg.get("chat") or {}).get("id") or (cq.get("from") or {}).get("id") or 0)
        message_id = msg.get("message_id")
        parts = data.split(":")
        if not chat_id or len(parts) < 2 or parts[0] != CB_PREFIX:
            return
        self.db.ensure_user(chat_id)
        try:
            self._route(chat_id, message_id, parts[1], parts[2:])
        except (ValueError, KeyError, IndexError):
            logger.info("Malformed KidsCompass callback %r", data)
            self._show_home(chat_id, message_id)

    def _route(self, chat_id: int, mid: Optional[int], domain: str, args: list[str]) -> None:
        action = args[0] if args else ""
        if domain == "fvr":
            self._route_fever(chat_id, mid, action, args)
        elif domain == "hsp":
            if action == "r":
                region = REGION_CODES[args[1]]
                self._show_hospital_list(chat_id, mid, region)
            else:
                self._show_hospital_regions(chat_id, mid)
        elif domain == "exp":
            if action == "a":
                age, weather = AgeGroup(args[1]), args[2]
                rain, summary = WEATHER_CODES[weather]
                mode = args[3] if len(args) > 3 else default_exp_mode(age)
                if mode not in EXP_MODES:
                    raise ValueError(mode)
                walk_in = mode == "walk" and age != AgeGroup.PREGNANCY
                region = (self.db.get_user_profile(chat_id) or {}).get("default_region") or None
                picks = recommend_weekend_picks(self.db, age, region, weather_rain=rain, is_dispatch=walk_in)
                self._safe_edit_or_send(chat_id, mid, format_weekend_briefing_html(picks, age, summary, walk_in=walk_in),
                                        build_exp_result_keyboard(age, weather, mode))
            elif action == "c":
                cat = ExperienceCategory(args[1])
                self._safe_edit_or_send(chat_id, mid, format_category_html(recommend_by_category(self.db, cat), cat),
                                        build_exp_result_keyboard())
            else:
                self._show_exp_menu(chat_id, mid)
        elif domain in ("dday", "dd"):
            if action == "m":
                if len(args) < 2:
                    raise ValueError(action)
                self.db.mute_booking(chat_id, int(args[1]))
                self.channel.send_message(
                    chat_id,
                    "🚫 이 예약은 앞으로 월요일 알림에서 제외할게요. 예약 D-Day 화면에서는 계속 확인할 수 있어요.",
                    _kb([[_btn("⏰ 전체 D-Day 보기", cb("dday"))], HOME_ROW]),
                )
            else:
                self._show_dday(chat_id, mid)
        elif domain == "preg":
            preg = self.db.get_pregnancy(chat_id)
            if action == "x" and preg is not None:
                text = f"🗑 <b>{esc(preg.nickname)}</b> 임신 정보를 삭제할까요?\n되돌릴 수 없어요."
                self._safe_edit_or_send(chat_id, mid, text, build_pregnancy_delete_confirm_keyboard())
            elif action == "xy":
                self.db.delete_pregnancy(chat_id)
                self._show_settings(chat_id, mid)
            elif action in ("", "x"):
                self._show_pregnancy(chat_id, mid)
            else:
                raise ValueError(action)
        elif domain == "set":
            profile = self.db.get_user_profile(chat_id) or {}
            if action == "w":
                self.db.set_alert(chat_id, "weekend", not profile.get("alert_weekend_brief"))
            elif action == "d":
                self.db.set_alert(chat_id, "dday", not profile.get("alert_dday_reserve"))
            elif action == "h":
                self.db.set_alert(chat_id, "health", not profile.get("alert_health_dday"))
            elif action == "m":
                together = (profile.get("alert_weekend_mode") or "TOGETHER") == "TOGETHER"
                self.db.set_weekend_mode(chat_id, "PRIMARY" if together else "TOGETHER")
            elif action == "r":
                self.db.set_default_region(chat_id, REGION_CODES[args[1]])
            self._show_settings(chat_id, mid)
        elif domain == "ch":
            child = self.db.get_child(int(args[1]), chat_id)
            if child is None:
                self._show_settings(chat_id, mid)
            elif action == "p":
                self.db.set_primary_child(chat_id, child.child_id)
                self._show_home(chat_id, mid)
            elif action == "x":
                text = f"🗑 <b>{esc(child.nickname)}</b> 정보를 삭제할까요?\n되돌릴 수 없어요."
                self._safe_edit_or_send(chat_id, mid, text, build_delete_confirm_keyboard(child.child_id))
            elif action == "xy":
                self.db.delete_child(child.child_id, chat_id)
                self._show_settings(chat_id, mid)
            else:
                raise ValueError(action)
        elif domain == "ob":
            self._route_onboarding(chat_id, mid, action, args)
        elif domain == "hub":
            self._route_hub(chat_id, mid, action, args)
        elif domain == "hp":
            self._route_home_play(chat_id, mid, action, args)
        elif domain == "fd":
            self._route_food(chat_id, mid, action, args)
        elif domain == "fb":
            self._route_feedback(chat_id, action, args)
        elif domain == "inq":
            self._route_inquiry(chat_id, mid, action)
        elif domain == "ms":
            self._route_milestone(chat_id, mid, action, args)
        else:
            self._show_home(chat_id, mid)

    # ---------- food catalogue (Pull only) ----------
    def _food_context(self, chat_id: int) -> tuple[str, Optional[int], bool]:
        children = self.db.list_children(chat_id)
        if children:
            child = children[0]  # DB order keeps the primary child first.
            return child.nickname, child.age_months(self._clock().date()), False
        pregnancy = self.db.get_pregnancy(chat_id)
        if pregnancy is not None:
            return f"{pregnancy.nickname} 태교", None, True
        return "우리 아이", None, False

    def _route_food(self, chat_id: int, mid: Optional[int], action: str, args: list[str]) -> None:
        subject_label, age_months, is_pregnancy = self._food_context(chat_id)
        if action == "":
            self.db.clear_onboarding_state(chat_id)
            self._show_food(chat_id, mid)
        elif action == "c":
            category = args[1] if len(args) > 1 else ""
            items = list_foods_by_category(category, db=self.db)
            title = next((label for _, code, label in FOOD_CATEGORY_BUTTONS if code == category), category)
            self._safe_edit_or_send(chat_id, mid, format_food_list_html(title, items, age_months, is_pregnancy),
                                    build_food_list_keyboard(items))
        elif action == "i":
            item = get_food_item(args[1], db=self.db) if len(args) > 1 else None
            if item is None:
                self._show_food(chat_id, mid)
                return
            self._safe_edit_or_send(chat_id, mid,
                                    format_food_detail_html(item, subject_label, age_months, is_pregnancy),
                                    build_food_detail_keyboard(item.food_id))
        elif action == "q":
            self.db.set_onboarding_state(chat_id, secrets.token_hex(4), "FOOD", ST_FOOD_SEARCH, {},
                                         self._clock() + ONBOARDING_TTL)
            self._safe_edit_or_send(chat_id, mid,
                                    "🔍 <b>음식 이름 검색</b>\n궁금한 음식 이름을 입력해 주세요\n(예: 꿀, 복숭아, 소고기)",
                                    _kb([[_btn("❌ 취소", cb("fd"))]]))
        else:
            raise ValueError(action)

    def _show_food(self, chat_id: int, mid: Optional[int]) -> None:
        subject_label, age_months, is_pregnancy = self._food_context(chat_id)
        self._safe_edit_or_send(chat_id, mid, format_food_hub_html(subject_label, age_months, is_pregnancy),
                                build_food_hub_keyboard())

    def _show_food_search(self, chat_id: int, mid: Optional[int], query: str) -> None:
        subject_label, age_months, is_pregnancy = self._food_context(chat_id)
        items = search_foods(query, db=self.db)
        # Keep the state open so a miss or a second plain-text query remains useful;
        # /cancel or the hub button explicitly exits the search mode.
        self.db.set_onboarding_state(chat_id, secrets.token_hex(4), "FOOD", ST_FOOD_SEARCH, {},
                                     self._clock() + ONBOARDING_TTL)
        title = f"'{query}' 검색 결과"
        self._safe_edit_or_send(chat_id, mid, format_food_list_html(title, items, age_months, is_pregnancy),
                                build_food_list_keyboard(items))

    def _route_fever(self, chat_id: int, mid: Optional[int], action: str, args: list[str]) -> None:
        if action == "":
            self._show_fever_menu(chat_id, mid)
        elif action == "m3":
            self._safe_edit_or_send(chat_id, mid, format_fever_emergency_html(), build_fever_result_keyboard())
        elif action == "m5":
            self._show_fever_result(chat_id, mid, float(args[1]), AGE_FOR_4_5M, age_label="4~5개월")
        elif action == "w":
            self._show_fever_result(chat_id, mid, float(args[1]), AGE_FOR_6M_PLUS, age_label="6개월 이상")
        elif action in ("c", "cw"):
            child = self.db.get_child(int(args[1]), chat_id)
            if child is None:
                self._show_fever_menu(chat_id, mid)
                return
            months = child.age_months(self._clock().date())
            if action == "cw":
                weight = float(args[2])
                if not (1.0 <= weight <= 100.0):
                    raise ValueError(weight)
            else:
                weight = child.weight_kg or 0.0
            if not weight:
                # 체중 미입력 아이: 3개월 미만은 계산 없이 즉시 진료 안내, 그 외에는 월령에 맞는 체중 버튼
                if months < INFANT_BLOCK_MONTHS:
                    self._safe_edit_or_send(chat_id, mid, format_fever_emergency_html(), build_fever_result_keyboard())
                else:
                    self._safe_edit_or_send(chat_id, mid, format_fever_weight_pick_html(child, months),
                                            build_child_weight_keyboard(child.child_id, months))
                return
            self._show_fever_result(chat_id, mid, weight, months, child)
        elif action == "x":
            self._safe_edit_or_send(chat_id, mid, format_cross_dosing_html(), build_fever_result_keyboard())
        elif action == "log":
            if len(args) < 2:
                return
            now = self._clock()
            token_row = self.db.consume_fever_token(chat_id, args[1], now)
            if token_row is None:
                self.channel.send_message(chat_id, "⚠️ 만료되었거나 이미 처리된 투약 기록 버튼이에요.")
                return
            try:
                ingredient = IngredientType(str(token_row["ingredient"]))
                product = (self.db.list_medicines() or DEFAULT_MEDICINES).get(str(token_row["product_id"]))
                if product is None:
                    raise ValueError("medicine snapshot is missing")
                recent = self.db.list_recent_dose_events(
                    int(token_row["child_id"]), now - timedelta(hours=24)
                )
                allowed, reason = check_24h_dose_limit(
                    recent, ingredient, float(token_row["weight_kg"]), product, now
                )
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                logger.warning("Invalid fever token snapshot: %s", exc)
                self.channel.send_message(chat_id, "⚠️ 투약 정보를 확인할 수 없어 기록하지 않았어요.")
                return
            if not allowed:
                self.channel.send_message(chat_id, esc(reason))
                return
            age_months = int(token_row["age_months"])
            candidates = cross_dosing_candidates(ingredient, age_months)
            suggested = candidates[0].value if candidates else ""
            try:
                result = self.db.record_dose_and_start_timer(
                    chat_id, int(token_row["child_id"]), str(token_row["product_id"]), ingredient.value,
                    float(token_row["dose_ml"]), float(token_row["dose_mg"]),
                    float(token_row["concentration_mg_ml"]), float(token_row["weight_kg"]), age_months,
                    idempotency_key=f"fvr:{chat_id}:{args[1]}", suggested_ingredient=suggested,
                    now=now, given_at=now,
                )
            except (PermissionError, KeyError, TypeError, ValueError) as exc:
                logger.info("Dose record rejected: %s", exc)
                self.channel.send_message(chat_id, "⚠️ 아이 정보를 확인할 수 없어 투약을 기록하지 않았어요.")
                return
            child_id = int(token_row["child_id"])
            timer_note = "2시간 30분 뒤 교차복용 확인 타이머를 설정했어요." if result.get("timer_id") else "교차복용 후보가 없어 복용 기록만 남겼어요."
            markup = _kb([[
                _btn("🔕 타이머 취소", cb("fvr", "tc", child_id)),
                _btn("🌙 달빛병원", cb("hsp")),
            ]])
            self.channel.send_message(chat_id, f"✅ 투약 기록을 저장했어요.\n{timer_note}", reply_markup=markup)
        elif action == "tc":
            if len(args) < 2:
                return
            child_id = int(args[1])
            count = self.db.cancel_child_dose_timers(chat_id, child_id, self._clock())
            self.channel.send_message(chat_id, f"🔕 교차복용 타이머를 취소했어요 ({count}건).")
        else:
            raise ValueError(action)

    def _route_onboarding(self, chat_id: int, mid: Optional[int], action: str, args: list[str]) -> None:
        if action == "c":
            self._start_onboarding(chat_id, mid, "CHILD")
        elif action == "p":
            self._start_onboarding(chat_id, mid, "PREGNANCY")
        elif action == "x":
            self.db.clear_onboarding_state(chat_id)
            self._show_home(chat_id, mid)
        elif action == "r":
            region = REGION_CODES[args[1]]
            state = self.db.get_onboarding_state(chat_id, self._clock())
            if state is None or state.get("step") not in REGION_STEPS:
                self._show_home(chat_id, mid)  # stale button: harmless
                return
            self._finish_onboarding(chat_id, mid, state, region)
        else:
            raise ValueError(action)

    def _route_hub(self, chat_id: int, mid: Optional[int], code: str, args: list[str]) -> None:
        if code == "med":
            text = ("🏥 <b>병원·해열제·검진</b>\n밤에 열이 날 때 · 야간 소아과 · 표준 검진/접종 시기\n\n"
                    "📞 위급하면 <b>119</b>")
            self._safe_edit_or_send(chat_id, mid, text, build_med_hub_keyboard())
            return
        title, cats = HUB_MENUS[code]
        together = len(args) > 1 and args[1] == "t"
        today = self._clock().date()
        region = (self.db.get_user_profile(chat_id) or {}).get("default_region") or "서울"
        ages, label = _subjects(self.db, str(chat_id), today, together)
        lists = []
        for age in ages:
            if cats is None:  # 야외·나들이: outdoor first, nature first
                items = [e for e in recommend_weekend_picks(self.db, age, region, limit=HUB_PICK_LIMIT * 2,
                                                            allow_backfill=False) if not e.is_indoor]
            else:
                items = [e for e in self.db.list_experiences(age_group=age, region_sido=region)
                         if e.category in cats]
            if age == AgeGroup.PREGNANCY:
                items = [e for e in items if e.age_group == AgeGroup.PREGNANCY]
            else:
                items = [e for e in items if e.age_group != AgeGroup.PREGNANCY]
            lists.append(items)
        picks = _round_robin(lists, HUB_PICK_LIMIT)
        multi = len(self.db.list_children(chat_id)) > 1
        self._safe_edit_or_send(chat_id, mid, format_hub_picks_html(title, label, region, picks),
                                build_hub_result_keyboard(code, together, multi))

    def _route_home_play(self, chat_id: int, mid: Optional[int], action: str, args: list[str]) -> None:
        """Pull-only home-play router; it never touches push settings or sent history."""
        if action == "":
            self._show_home_play_auto(chat_id, mid)
        elif action == "n":
            stage = args[1]
            offset = int(args[2])
            self._show_home_play(chat_id, mid, stage, offset, self._home_play_subject_label(chat_id, stage))
        elif action == "d":
            stage = args[1]
            self._show_dev_guide(chat_id, mid, stage, self._home_play_subject_label(chat_id, stage))
        elif action == "s":
            self._safe_edit_or_send(chat_id, mid, "🏠 <b>다른 연령의 집콕놀이</b>\n연령대를 골라 주세요 👇",
                                    build_home_play_stage_keyboard())
        elif action == "p":
            stage = args[1]
            self._show_home_play(chat_id, mid, stage, 0, "선택한 연령")
        else:
            raise ValueError(action)

    def _home_play_subject_label(self, chat_id: int, stage: str) -> str:
        children = self.db.list_children(chat_id)
        if children:
            return children[0].nickname
        pregnancy = self.db.get_pregnancy(chat_id)
        if pregnancy is not None and stage == PlayStage.PREG.value:
            return pregnancy.nickname
        return "선택한 연령"

    def _show_home_play_auto(self, chat_id: int, mid: Optional[int]) -> None:
        children = self.db.list_children(chat_id)
        if children:
            child = children[0]
            stage = play_stage_for_months(child.age_months(self._clock().date()))
            self._show_home_play(chat_id, mid, stage, 0, child.nickname)
            return
        pregnancy = self.db.get_pregnancy(chat_id)
        if pregnancy is not None:
            self._show_home_play(chat_id, mid, PlayStage.PREG.value, 0, pregnancy.nickname)
            return
        self._safe_edit_or_send(chat_id, mid, "🏠 <b>집콕놀이·발달</b>\n연령대를 골라 바로 확인해 보세요 👇",
                                build_home_play_stage_keyboard())

    def _show_home_play(self, chat_id: int, mid: Optional[int], stage: str, offset: int,
                        subject_label: str) -> None:
        # Validate before rendering stale/forged callbacks; malformed callbacks are harmless.
        if stage not in PLAY_STAGE_LABELS:
            raise ValueError(stage)
        items = pick_home_plays(stage, offset=offset, limit=2, db=self.db)
        text = format_home_play_html(subject_label, PLAY_STAGE_LABELS[stage], items)
        self._safe_edit_or_send(chat_id, mid, text, build_home_play_keyboard(stage, offset, db=self.db))

    def _show_dev_guide(self, chat_id: int, mid: Optional[int], play_stage: str,
                        subject_label: str) -> None:
        if play_stage not in PLAY_STAGE_LABELS and play_stage not in DEV_STAGE_LABELS:
            raise ValueError(play_stage)
        if play_stage in DEV_STAGE_LABELS:
            dev_stage = play_stage
            back_stage = {
                DevStage.D0_3.value: PlayStage.M0_11.value,
                DevStage.D4_6.value: PlayStage.M0_11.value,
                DevStage.D7_12.value: PlayStage.M0_11.value,
                DevStage.D13_24.value: PlayStage.M12_23.value,
                DevStage.D25_36.value: PlayStage.M24_47.value,
                DevStage.DY4_7.value: PlayStage.Y4_7.value,
                DevStage.DY8_13.value: PlayStage.Y8_13.value,
                DevStage.DPREG.value: PlayStage.PREG.value,
            }[dev_stage]
        else:
            back_stage = play_stage
            children = self.db.list_children(chat_id)
            if children and play_stage != PlayStage.PREG.value:
                dev_stage = dev_stage_for_months(children[0].age_months(self._clock().date()))
            elif self.db.get_pregnancy(chat_id) is not None and play_stage == PlayStage.PREG.value:
                dev_stage = DevStage.DPREG.value
            else:
                dev_stage = DevStage.DPREG.value if play_stage == PlayStage.PREG.value else {
                    PlayStage.M0_11.value: DevStage.D0_3.value,
                    PlayStage.M12_23.value: DevStage.D13_24.value,
                    PlayStage.M24_47.value: DevStage.D25_36.value,
                    PlayStage.Y4_7.value: DevStage.DY4_7.value,
                    PlayStage.Y8_13.value: DevStage.DY8_13.value,
                }[play_stage]
        guides = get_dev_guides(dev_stage, db=self.db)
        text = format_dev_guide_html(subject_label, DEV_STAGE_LABELS[dev_stage], guides)
        guide_id = guides[0].guide_id if guides else None
        self._safe_edit_or_send(chat_id, mid, text,
                                build_dev_guide_keyboard(back_stage, db=self.db, content_id=guide_id))

    def _route_feedback(self, chat_id: int, action: str, args: list[str]) -> None:
        """Forged / expired / other user's token → harmless no-op (nothing recorded, nothing rendered)."""
        if action not in FEEDBACK_ACTIONS or len(args) < 2:
            return
        now = self._clock()
        exp = self.db.resolve_rec_token(chat_id, args[1], now)
        if exp is None:
            return
        feedback = FEEDBACK_ACTIONS[action]
        self.db.record_place_feedback(chat_id, exp.exp_id, feedback, now)
        note = "다녀오셨군요! 180일간 추천에서 빼 둘게요." if feedback == "VISITED" else "관심 없음 — 다시 추천하지 않을게요."
        self.channel.send_message(chat_id, f"👍 <b>{esc(exp.title)}</b>\n{note}")

    def _route_inquiry(self, chat_id: int, mid: Optional[int], action: str) -> None:
        if action == "x":
            self.db.clear_onboarding_state(chat_id)
            self._show_home(chat_id, mid)
            return
        if action not in ("", "start"):
            raise ValueError(action)
        self.db.set_onboarding_state(
            chat_id, secrets.token_hex(4), "FEEDBACK", ST_FEEDBACK_INPUT, {}, self._clock() + ONBOARDING_TTL
        )
        self._safe_edit_or_send(
            chat_id,
            mid,
            "💬 <b>의견·오류 제보</b>\n봇 이용 중 불편한 점, 잘못된 정보, 추가 희망 기능을 편하게 입력해 주세요!\n(취소: /cancel)",
            _kb([[_btn("❌ 취소", cb("inq", "x"))]]),
        )

    def _route_milestone(self, chat_id: int, mid: Optional[int], action: str, args: list[str]) -> None:
        if action == "ok":
            m = self.db.get_subject_milestone(chat_id, int(args[1]))  # owner-scoped
            if m is None:
                return  # other user's / unknown milestone → harmless no-op
            if m.status != "USER_CONFIRMED":
                self.db.set_milestone_status(chat_id, m.milestone_id, "USER_CONFIRMED", "USER", self._clock())
            defn = next((d for d in self.db.list_milestone_definitions() if d.definition_id == m.definition_id), None)
            title = defn.title if defn else "검진·접종"
            self.channel.send_message(chat_id, f"✅ <b>{esc(title)}</b> 확인 완료로 기록했어요. 다시 알리지 않아요.")
        elif action == "l":
            today = self._clock().date()
            items = collect_due_milestone_items(self.db, str(chat_id), today, MILESTONE_PULL_LOOKAHEAD_DAYS)
            text = format_milestone_push_html(items, today, subtitle="앞으로 30일 안에 챙길 표준 시기 항목이에요")
            self._safe_edit_or_send(chat_id, mid, text, build_milestone_keyboard(items))
        else:
            raise ValueError(action)

    # ---------- screens ----------
    def _show_home(self, chat_id: int, mid: Optional[int]) -> None:
        children = self.db.list_children(chat_id)
        pregnancy = self.db.get_pregnancy(chat_id)
        text = format_home_html(children, self.db.get_user_profile(chat_id), pregnancy, self._clock().date())
        self._safe_edit_or_send(chat_id, mid, text, build_home_keyboard(children, pregnancy is not None))

    def _show_fever_menu(self, chat_id: int, mid: Optional[int]) -> None:
        children = self.db.list_children(chat_id)
        self._safe_edit_or_send(chat_id, mid, format_fever_menu_html(children), build_fever_keyboard(children))

    def _show_fever_result(self, chat_id: int, mid: Optional[int], weight: float, months: int, child=None,
                           age_label: Optional[str] = None) -> None:
        products = self.db.list_medicines() or DEFAULT_MEDICINES
        results = calculate_all(weight, months, products)
        rows = []
        valid_weight = False
        try:
            valid_weight = float(weight) > 0 and math.isfinite(float(weight))
        except (TypeError, ValueError, OverflowError):
            pass
        if child is not None and valid_weight:
            now = self._clock()
            for ingredient, result in results.items():
                if result.status != "OK" or result.product is None:
                    continue
                token = self.db.create_fever_token(
                    chat_id, child.child_id, result.product.product_id, ingredient.value,
                    result.recommended_ml, result.recommended_mg, float(weight), months,
                    result.product.concentration_mg_ml, now=now,
                )
                rows.append([_btn(f"💊 {ingredient.label} 방금 먹였어요", cb("fvr", "log", token))])
        keyboard = _kb(rows + build_fever_result_keyboard()["inline_keyboard"])
        self._safe_edit_or_send(chat_id, mid, format_fever_result_html(results, child, age_label), keyboard)

    def _show_hospital_regions(self, chat_id: int, mid: Optional[int]) -> None:
        text = ("🌙 <b>달빛어린이병원 찾기</b>\n밤·주말·공휴일 소아 진료 병원\n\n"
                "지역을 골라 주세요 👇\n\n📞 위급하면 <b>119</b>")
        self._safe_edit_or_send(chat_id, mid, text, build_hospital_region_keyboard())

    def _show_hospital_list(self, chat_id: int, mid: Optional[int], region: str) -> None:
        hospitals = self.db.list_hospitals(region)
        hours = {h.hospital_id: self.db.list_hospital_hours(h.hospital_id) for h in hospitals}
        text = format_hospital_list_html(
            hospitals, region, hours_by_hospital=hours, now_dt=self._clock()
        )
        self._safe_edit_or_send(chat_id, mid, text, build_hospital_list_keyboard())

    def _show_exp_menu(self, chat_id: int, mid: Optional[int]) -> None:
        text = "🎈 <b>주말 나들이</b>\n연령과 날씨(☀️/☔)를 고르거나 테마를 눌러 주세요 👇"
        self._safe_edit_or_send(chat_id, mid, text, build_exp_age_keyboard())

    def _show_pregnancy(self, chat_id: int, mid: Optional[int]) -> None:
        preg = self.db.get_pregnancy(chat_id)
        region = (self.db.get_user_profile(chat_id) or {}).get("default_region") or None
        walks = recommend_weekend_picks(self.db, AgeGroup.PREGNANCY, region)
        text = format_pregnancy_html(preg, PREGNANCY_BENEFITS, walks, self._clock().date())
        self._safe_edit_or_send(chat_id, mid, text, build_pregnancy_keyboard(preg is not None))

    def _show_dday(self, chat_id: int, mid: Optional[int]) -> None:
        now = self._clock()
        events = self.db.list_upcoming_bookings(now.strftime("%Y-%m-%d %H:%M"),
                                                (now + timedelta(days=DDAY_WINDOW_DAYS)).strftime("%Y-%m-%d %H:%M"))
        self._safe_edit_or_send(chat_id, mid, format_dday_alert_html(events, now), build_exp_result_keyboard())

    def _show_settings(self, chat_id: int, mid: Optional[int]) -> None:
        profile = self.db.get_user_profile(chat_id) or {}
        children = self.db.list_children(chat_id)
        preg = self.db.get_pregnancy(chat_id)
        lines = ["⚙️ <b>아이·알림 설정</b>", f"📍 기본 지역: <b>{esc(profile.get('default_region', '서울'))}</b>"]
        lines += [f"{'⭐' if c.is_primary else '👧'} {esc(c.nickname)} · {esc(c.birth_date)} · {esc(kg_text(c.weight_kg))}"
                  for c in children]
        if not children:
            lines.append("👧 아래 <b>[➕ 아이 등록]</b>으로 5초 만에 등록하세요.")
        if preg is not None:
            lines.append(f"🤰 {esc(preg.nickname)} · 출산예정 {esc(preg.due_date)}")
        lines += ["", "🩺 검진·접종: 이번 주 챙길 항목만 1통 · 🎈 목 17시: 내 지역 1~2곳 · ⏰ 월 9시: 내 아이 맞춤 예약",
                  "추천할 게 없으면 보내지 않아요."]
        self._safe_edit_or_send(chat_id, mid, "\n".join(lines), build_settings_keyboard(profile, children, preg))

    # ---------- transport ----------
    def _safe_edit_or_send(self, chat_id: int, message_id: Optional[int], text: str,
                           reply_markup: Optional[dict[str, Any]] = None) -> None:
        """Edits in place ('message is not modified' = success); falls back to a new message."""
        if message_id and self.channel.edit_message_text(chat_id, message_id, text, reply_markup=reply_markup):
            return
        self.channel.send_message(chat_id, text, reply_markup=reply_markup)


def iter_push_preview(messages: Iterable[PushMessage]) -> Iterable[tuple[PushMessage, list[str]]]:
    """(message, button lines) for dry-run / demo previews."""
    for m in messages:
        rows = (m.reply_markup or {}).get("inline_keyboard", [])
        yield m, [" | ".join(f"[{b['text']}]({b.get('callback_data')})" for b in row) for row in rows]
