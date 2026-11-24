"""Telegram transport, [TG-05] dispatcher and HTML card formatters for Kids Compass.

[TG-01] every card stays within 1,800 visible / 3,500 HTML chars (see measure_message).
[TG-02] every dynamic value goes through esc(); only <b>, <i>, <code>, <a href> are emitted.
[TG-03] links are rendered as anchor labels — raw URLs are never shown.
"""
from __future__ import annotations
import html
import json
import logging
import re
import socket
import time
import urllib.error
import urllib.request
from datetime import date, datetime
from typing import Any, Callable, Iterable, Optional

from market_brief.channels.http_keepalive import open_request
from kids_compass.collector import WALK_IN_TIPS
from kids_compass.food_safety import (
    FOOD_DISCLAIMER,
    FoodItem,
    evaluate_food_for_child,
)
from kids_compass.fever_engine import (
    CROSS_ALLOWED_WITH_CAUTION,
    CROSS_BLOCKED_SAME_NSAID,
    CROSS_DOSING_GUIDE,
    CROSS_SAME_INGREDIENT,
    CROSS_TIMER_NOTICE,
    DISCLAIMER,
    STATUS_BLOCKED_INFANT,
    WARN_INFANT_UNDER_3M,
    cross_dosing_candidates,
)
from kids_compass.hospitals_importer import is_open_now
from kids_compass.home_play import DEV_DISCLAIMER, DevGuideItem, HomePlayItem
from kids_compass.models import (
    AgeGroup,
    BookingEvent,
    ChildProfile,
    DoseTimer,
    ExperienceCategory,
    ExperienceItem,
    FeverCalculationResult,
    IngredientType,
    MoonlightHospital,
    PregnancyBenefit,
    PregnancyProfile,
    PushMessage,
    WeatherSnapshot,
)

logger = logging.getLogger(__name__)

# Message-level idempotency row in kids_sent_history: at most ONE push per (user, stream, window),
# independent of which entities the (re)built message happens to contain.
PUSH_MESSAGE_SENTINEL = "__msg__"
MILESTONE_DISCLAIMER = "참고용 표준 시기 안내이며 실제 접종·검진 여부는 예방접종도우미·소아과와 확인하세요"
MILESTONE_KIND_ICONS = {"CHECKUP": "🩺", "VACCINE": "💉", "PREGNANCY": "🤰"}
MILESTONE_PUSH_MAX_ITEMS = 6

MAX_VISIBLE_CHARS = 1800
MAX_HTML_CHARS = 3500
HOSPITAL_PHONE_WARNING = "⚠️ 출발 전 반드시 유선 전화로 당일 진료 여부를 확인하세요"
EMERGENCY_FALLBACK = "📞 위급하면 <b>119</b> · 병원·약국 안내도 119에서 받을 수 있어요"

KIDS_COMMANDS = [
    {"command": "start", "description": "키즈 나침반 홈"},
    {"command": "fever", "description": "해열제 용량 계산기"},
    {"command": "hospital", "description": "달빛어린이병원 찾기"},
    {"command": "weekend", "description": "주말 나들이 3-Pick"},
    {"command": "preg", "description": "임신기 태교·바우처"},
    {"command": "food", "description": "이유식·음식 안전 사전"},
]

WALK_IN_HEADER = "⚡ 예약 없이 바로 출발 3-Pick"
DEFAULT_WALK_IN_TIP = "🏃 오전 일찍 도착 추천 · 🚗 주말 주차 혼잡 → 대중교통 권장"
PREG_REGISTER_HINT = "🤰 태명 등록: <code>/preg 복덩이 2027-03-15</code> (태명, 출산예정일)"

_TAG_RE = re.compile(r"<[^>]+>")
WEEKDAYS_KO = "월화수목금토일"


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def link(label: str, url: str) -> str:
    if not url:
        return esc(label)
    return f'<a href="{esc(url)}">{esc(label)}</a>'


def measure_message(html_text: str) -> tuple[int, int]:
    """Returns (html_len, visible_len) — same order as the other bots' measure_message."""
    return len(html_text), len(html.unescape(_TAG_RE.sub("", html_text)))


def fits_limits(html_text: str) -> bool:
    html_len, visible_len = measure_message(html_text)
    return html_len <= MAX_HTML_CHARS and visible_len <= MAX_VISIBLE_CHARS


def format_food_hub_html(subject_label: str, age_months: Optional[int],
                         is_pregnancy: bool = False) -> str:
    """Compact Pull-only entry card for the food-safety catalogue."""
    if is_pregnancy:
        summary = "🤰 임신 중에는 날것·생선회·카페인을 주의하고 식재료를 충분히 익혀 드세요."
    elif age_months is None:
        summary = "👶 월령을 등록하면 꿀·생우유·질식 위험 음식의 시작 시기를 맞춰 안내해요."
    else:
        cautions = []
        if age_months < 12:
            cautions.append("꿀·생우유 금지")
        if age_months < 48:
            cautions.append("통견과류·방울토마토·포도 질식 주의")
        summary = "⚠️ 지금 주의: " + (" · ".join(cautions) if cautions else "질식·알레르기 주의")
    return "\n".join([
        "🥣 <b>먹거리·이유식·음식 안전 사전</b>",
        f"👧 기준: <b>{esc(subject_label)}</b>",
        summary,
        "",
        "카테고리를 고르거나 음식 이름으로 검색해 보세요 👇",
    ])


def format_food_list_html(title: str, items: list[FoodItem], age_months: Optional[int],
                         is_pregnancy: bool = False) -> str:
    """Render a short list with an evaluated status for every food."""
    lines = [f"🥣 <b>{esc(title)}</b>", ""]
    if not items:
        lines.append("조건에 맞는 음식이 없어요. 다른 이름으로 검색해 보세요.")
    for item in items[:8]:
        status, guidance = evaluate_food_for_child(item, age_months, is_pregnancy)
        icon = {"OK": "✅", "BLOCKED": "🚫", "INFO": "ℹ️"}.get(status, "ℹ️")
        lines.append(f"{icon} <b>{esc(item.name)}</b> · {esc(guidance)}")
        if item.risk_level != "SAFE":
            lines.append(f"   ⚠️ {esc(item.safety_warning)}")
    return "\n".join(lines)


def format_food_detail_html(food: FoodItem, subject_label: str, age_months: Optional[int],
                            is_pregnancy: bool = False) -> str:
    """Render one food card, including preparation and safety notes."""
    status, guidance = evaluate_food_for_child(food, age_months, is_pregnancy)
    banner = {"OK": "✅", "BLOCKED": "🚫", "INFO": "ℹ️"}.get(status, "ℹ️")
    age_label = "임신기" if is_pregnancy else (f"생후 {age_months}개월" if age_months is not None else "월령 미등록")
    lines = [
        f"🥣 <b>{esc(food.name)}</b>",
        f"👧 {esc(subject_label)} · {esc(age_label)}",
        f"{banner} <b>{esc(guidance)}</b>",
        "",
        f"🍳 <b>조리·손질</b>\n{esc(food.prep_tip)}",
        f"🧡 <b>알레르기</b>\n{esc(food.allergy_note)}",
        f"⚠️ <b>안전 경고</b>\n{esc(food.safety_warning)}",
        "",
        f"<i>{esc(FOOD_DISCLAIMER)}</i>",
    ]
    return "\n".join(lines)


# ── Cards ─────────────────────────────────────────────────────
def kg_text(weight: Optional[float]) -> str:
    """'11.2kg' or '체중 미입력' — never raises on a missing (None / 0) weight."""
    return f"{weight:g}kg" if weight else "체중 미입력"


def _on_off(profile: Optional[dict], column: str) -> str:
    return "ON" if (profile or {}).get(column) else "OFF"


def format_home_html(children: list[ChildProfile], profile: Optional[dict] = None,
                     pregnancy: Optional[PregnancyProfile] = None, today: Optional[date] = None) -> str:
    lines = ["🍼 <b>키즈 나침반</b>", "평일엔 응급육아 · 주말엔 체험 나들이", ""]
    if not children and pregnancy is None:
        lines += [
            "👋 <b>5초 간편 등록</b>으로 우리 아이 맞춤 비서를 시작하세요",
            "아래 <b>[➕ 아이 등록]</b> 또는 <b>[🤰 임신 중이에요]</b>를 눌러 주세요.",
            "",
            "🚨 급하면 등록 없이 바로 <b>해열제 계산</b> · <b>달빛병원</b>을 누르세요.",
        ]
    else:
        if pregnancy is not None:
            lines.append(f"🤰 {esc(pregnancy.nickname)} · 출산예정 <b>{esc(pregnancy.dday(today))}</b>"
                         f" · 임신 {pregnancy.gestational_week(today)}주")
        if children:
            lines.append("👧 <b>우리 아이</b>")
            for c in children[:4]:
                star = "⭐ " if c.is_primary else "• "
                lines.append(f"{star}{esc(c.nickname)} · 생후 <b>{c.age_months(today)}개월</b> · {esc(kg_text(c.weight_kg))}")
        region = (profile or {}).get("default_region") or "서울"
        lines += ["", f"📍 기본 지역 <b>{esc(region)}</b> · ⭐ 선택된 아이 기준으로 추천해요"]
    if profile:
        lines += [
            "",
            f"🔔 검진·접종 <b>{_on_off(profile, 'alert_health_dday')}</b> · "
            f"주말추천 <b>{_on_off(profile, 'alert_weekend_brief')}</b> · "
            f"예약 D-Day <b>{_on_off(profile, 'alert_dday_reserve')}</b>",
        ]
    lines += ["", "아래 버튼을 눌러 주세요 👇"]
    return "\n".join(lines)


def format_home_play_html(subject_label: str, stage_label: str, items: list[HomePlayItem]) -> str:
    """Compact Pull card for two home activities; all catalogue values are escaped.

    The slice is intentional: callers can safely pass a catalogue or a stage
    list without creating a Telegram over-limit message.  The bot's normal
    picker already supplies two items.
    """
    lines = [f"🏠 <b>집콕놀이·발달</b>", f"👧 {esc(subject_label)} · {esc(stage_label)}", ""]
    shown_items = items[:2]
    if not shown_items:
        lines.append("이 시기에 맞춘 놀이를 준비 중이에요.")
    for number, item in enumerate(shown_items, 1):
        lines.extend([
            f"<b>{number}. {esc(item.title)}</b> · ⏱ {int(item.duration_min)}분",
            f"🧺 준비물: {esc(item.materials)}",
            f"▶ {esc(item.how_to)}",
            f"🌱 {esc(item.dev_benefit)}",
            f"⚠️ {esc(item.safety_note)}",
            "",
        ])
    return "\n".join(lines).rstrip()


def format_dev_guide_html(subject_label: str, stage_label: str,
                          guides: list[DevGuideItem]) -> str:
    """Render development checkpoints, parent tips, consult signs and disclaimer."""
    lines = ["🌱 <b>이 시기 발달·훈육 가이드</b>",
             f"👧 {esc(subject_label)} · {esc(stage_label)}", ""]
    if not guides:
        lines.append("이 시기 가이드를 준비 중이에요.")
    for guide in guides:
        lines.append(f"<b>{esc(guide.title)}</b>")
        lines.append("✅ 체크포인트")
        lines.extend(f"• {esc(point)}" for point in guide.checkpoints)
        lines.append("🧡 부모 대처 팁")
        lines.extend(f"• {esc(tip)}" for tip in guide.parent_tips)
        lines.append("🩺 소아과 상담 권장 신호")
        lines.extend(f"• {esc(sign)}" for sign in guide.consult_signs)
        lines.append("")
    lines.append(f"<i>{esc(DEV_DISCLAIMER)}</i>")
    return "\n".join(lines).rstrip()


def _exp_line(n: int, e: ExperienceItem, walk_in: bool = False) -> list[str]:
    tags = ["🏠실내" if e.is_indoor else "🌳야외"]
    if e.stroller_accessible:
        tags.append("👶유아차")
    if e.nursing_room:
        tags.append("🍼수유실")
    if e.booking_required:
        tags.append("📅예약")
    lines = [
        f"<b>{n}. {esc(e.title)}</b>",
        f"📍 {esc(e.region_sido)} {esc(e.region_sigungu)} · 💳 {esc(e.fee_detail)}",
    ]
    if e.operating_hours:
        lines.append(f"🕘 {esc(e.operating_hours)}")
    lines.append(f"✨ {esc(e.key_points)}")
    if walk_in:
        lines.append(esc(WALK_IN_TIPS.get(e.exp_id, DEFAULT_WALK_IN_TIP)))
    lines.append(f"{' '.join(tags)} · {link('안내' if walk_in else '예약·안내', e.booking_url)}")
    return lines


def format_weekend_briefing_html(picks: list[ExperienceItem], age_group: AgeGroup, weather_summary: str,
                                 walk_in: bool = False) -> str:
    """walk_in=True: 현장직행 card — caller must pass picks from recommend_weekend_picks(..., is_dispatch=True)."""
    title = f"[{WALK_IN_HEADER}]" if walk_in else "이번 주말 나들이 3-Pick"
    lines = [f"🎈 <b>{title}</b>", f"{esc(age_group.label)}", f"🌤 {esc(weather_summary)}", ""]
    if not picks:
        lines.append("조건에 맞는 장소가 아직 없어요. 다른 연령·테마를 눌러 보세요.")
    for i, e in enumerate(picks, 1):
        lines += _exp_line(i, e, walk_in) + [""]
    lines.append("ℹ️ 요금·운영시간은 방문 전 공식 공지를 확인하세요.")
    return "\n".join(lines)


def format_pregnancy_html(preg_profile: Optional[PregnancyProfile], benefits: list[PregnancyBenefit],
                          walks: list[ExperienceItem], today=None) -> str:
    """임신기 대시보드: 태명 D-Day + 보건소·바우처 혜택 TOP + 힐링 태교 산책길."""
    lines = ["🤰 <b>임신기 태교·바우처</b>"]
    if preg_profile:
        lines.append(f"👶 {esc(preg_profile.nickname)} · 출산예정 <b>{esc(preg_profile.dday(today))}</b>"
                     f" ({esc(preg_profile.due_date)})")
    else:
        lines.append(PREG_REGISTER_HINT)
    lines += ["", "🎁 <b>보건소·바우처 혜택 TOP</b>"]
    for b in benefits[:4]:
        lines += [f"• <b>{esc(b.title)}</b> · {esc(b.amount)}", f"   {esc(b.detail)} · {link('신청 안내', b.url)}"]
    lines += ["", "🌿 <b>힐링 태교 산책길</b>"]
    if not walks:
        lines.append("조건에 맞는 산책길이 아직 없어요.")
    for i, e in enumerate(walks[:3], 1):
        lines += _exp_line(i, e) + [""]
    lines.append("ℹ️ 지원 금액·대상은 지자체·연도별로 달라요. 신청 전 공식 공지를 확인하세요.")
    return "\n".join(lines)


def format_category_html(picks: list[ExperienceItem], category: ExperienceCategory) -> str:
    lines = [f"<b>{esc(category.label)}</b> 테마 추천", ""]
    if not picks:
        lines.append("등록된 장소가 아직 없어요.")
    for i, e in enumerate(picks, 1):
        lines += _exp_line(i, e) + [""]
    lines.append("ℹ️ 요금·운영시간은 방문 전 공식 공지를 확인하세요.")
    return "\n".join(lines)


def format_hub_picks_html(title: str, subject_label: str, region: str, picks: list[ExperienceItem]) -> str:
    """Pull hub card: selected child (or 형제 함께) + default region applied automatically."""
    lines = [f"<b>{esc(title)}</b>", f"👧 {esc(subject_label)} · 📍 {esc(region)}", ""]
    if not picks:
        lines.append("이 지역·연령에 맞는 장소가 아직 없어요. 설정에서 지역을 바꿔 보세요.")
    for i, e in enumerate(picks, 1):
        lines += _exp_line(i, e) + [""]
    lines.append("ℹ️ 요금·운영시간은 방문 전 공식 공지를 확인하세요.")
    return "\n".join(lines)


def format_weekend_push_html(picks: list[ExperienceItem], subject_label: str,
                             weather: Optional[WeatherSnapshot], region: str) -> str:
    """Thursday 1:1 push: 1~2 hand-picked places. Weather wording appears ONLY for a real (non-failed) snapshot —
    a missing/failed forecast is never presented as '날씨 반영' (and never as sunny)."""
    lines = ["🎈 <b>이번 주말, 여기 어때요?</b>", f"👧 {esc(subject_label)} · 📍 {esc(region)}"]
    if weather is not None and not weather.is_failed:
        lines.append(f"🌤 {esc(weather.summary_text)}")
        lines.append("☔ 날씨 반영 · 실내 위주로 골랐어요" if weather.is_rain_or_snow else "🌤 날씨 반영 · 야외+실내 균형")
    lines.append("")
    for i, e in enumerate(picks[:2], 1):
        lines += _exp_line(i, e) + [""]
    lines += [
        "👇 다녀왔거나 관심 없으면 눌러 주세요 — 다음 추천에서 빼 드려요.",
        "ℹ️ 요금·운영시간은 방문 전 공식 공지를 확인하세요.",
    ]
    return "\n".join(lines)


MILESTONE_DDAY_SHOW_DAYS = 60  # long windows (e.g. 만 4~6세) show the end date only, not 'D-731'


def _md(iso: str, today: date) -> str:
    try:
        d = date.fromisoformat(iso)
    except ValueError:
        return esc(iso)
    year = f"{d.year}." if d.year != today.year else ""
    return f"{year}{d.month}/{d.day}({WEEKDAYS_KO[d.weekday()]})"


def format_milestone_push_html(items: list[dict], today: date, subtitle: str = "이번 주 챙길 표준 시기 항목이에요",
                               limit: int = MILESTONE_PUSH_MAX_ITEMS) -> str:
    """검진·예방접종·임신 체크포인트를 사용자당 1통으로 통합.
    items: {"subject", "kind", "title", "description", "due_from", "due_to"} (milestone_id is used by the keyboard)."""
    lines = ["🩺 <b>우리 아이 건강 체크 알림</b>", esc(subtitle), ""]
    if not items:
        lines += ["지금 챙길 검진·접종 항목이 없어요 👍", ""]
    shown = items[:max(0, int(limit))]
    for n, it in enumerate(shown, 1):
        icon = MILESTONE_KIND_ICONS.get(str(it.get("kind")), "📌")
        try:
            left = (date.fromisoformat(str(it.get("due_to"))) - today).days
            until = "" if left > MILESTONE_DDAY_SHOW_DAYS else (f"D-{left}" if left > 0 else "D-DAY")
        except ValueError:
            until = ""
        lines += [
            f"{n}. {icon} <b>{esc(it.get('title', ''))}</b>",
            f"   👧 {esc(it.get('subject', ''))} · {_md(str(it.get('due_from')), today)} ~ "
            f"{_md(str(it.get('due_to')), today)}"
            + (f" (<b>{until}</b>)" if until else ""),
        ]
        if it.get("description"):
            lines.append(f"   {esc(it['description'])}")
        lines.append("")
    if len(items) > len(shown):
        lines += [f"➕ 그 외 {len(items) - len(shown)}건은 홈 ▸ 병원·해열제·검진에서 확인하세요.", ""]
    if shown:
        lines += ["✅ 이미 했다면 아래 버튼으로 표시해 주세요 (다시 알리지 않아요).", ""]
    lines.append(f"<i>{esc(MILESTONE_DISCLAIMER)}</i>")
    return "\n".join(lines)


def format_combined_milestone_push_html(d14_text: str, weekly_text: str) -> str:
    """Merge the two Tuesday health sections into one compact Telegram card."""
    sections = ["🩺 <b>우리 아이 건강 체크 알림</b>"]
    if d14_text:
        sections += ["", "⏳ <b>마감 D-14 리마인드</b>", "\n".join(d14_text.splitlines()[1:])]
    if weekly_text:
        sections += ["", "📅 <b>이번 주 챙길 항목</b>", "\n".join(weekly_text.splitlines()[1:])]
    return "\n".join(sections)


def format_d14_milestone_push_html(items: list[dict], today: date) -> str:
    """검진·접종 마감 14일 전 리마인드 카드."""
    lines = ["🩺 <b>검진·접종 마감 D-14 알림</b>",
             "마감일이 가까운 항목이에요. 이미 했다면 아래 확인 완료를 눌러 주세요.", ""]
    shown = items[:MILESTONE_PUSH_MAX_ITEMS]
    for n, item in enumerate(shown, 1):
        due_to = str(item.get("due_to") or "")
        try:
            days_left = (date.fromisoformat(due_to) - today).days
            dday = "D-DAY" if days_left <= 0 else f"D-{days_left}"
        except ValueError:
            dday = "D-day 확인 필요"
        icon = MILESTONE_KIND_ICONS.get(str(item.get("kind")), "📌")
        lines += [
            f"{n}. {icon} <b>{esc(item.get('title', '검진·접종'))}</b>",
            f"   👧 {esc(item.get('subject', '아이'))} · 마감 <b>{esc(due_to or '확인 필요')}</b> ({esc(dday)})",
        ]
        if item.get("description"):
            lines.append(f"   {esc(item['description'])}")
        lines.append("")
    if len(items) > len(shown):
        lines += [f"➕ 그 외 {len(items) - len(shown)}건이 더 있어요.", ""]
    if shown:
        lines += ["✅ 아래 [확인 완료]를 누르면 이 항목은 다시 알리지 않아요.", ""]
    lines.append(f"<i>{esc(MILESTONE_DISCLAIMER)}</i>")
    return "\n".join(lines)


def dday_label(open_dt: datetime, now: datetime) -> str:
    days = (open_dt.date() - now.date()).days
    return "D-DAY" if days <= 0 else f"D-{days}"


def format_dday_alert_html(events: list[BookingEvent], now: Optional[datetime] = None,
                           subtitle: Optional[str] = None) -> str:
    now = now or datetime.now()
    lines = ["⏰ <b>예약 오픈 D-Day 스나이퍼</b>", esc(subtitle) if subtitle else "놓치면 마감되는 국공립 체험", ""]
    if not events:
        lines.append("2주 안에 열리는 예약이 없어요.")
    for ev in events[:8]:
        try:
            open_dt = datetime.strptime(ev.open_datetime, "%Y-%m-%d %H:%M")
            when = f"<b>{dday_label(open_dt, now)}</b> {open_dt:%m/%d}({WEEKDAYS_KO[open_dt.weekday()]}) {open_dt:%H:%M}"
        except ValueError:
            when = esc(ev.open_datetime)
        lines += [
            f"🎯 {when}",
            f"   {esc(ev.title)}",
            f"   🗓 {esc(ev.target_date_range)} · {esc(ev.memo)} · {link('예약', ev.booking_url)}",
            "",
        ]
    lines.append("ℹ️ 오픈 시각은 기관 사정으로 바뀔 수 있어요. 공식 공지를 확인하세요.")
    return "\n".join(lines)


def format_fever_menu_html(children: list[ChildProfile]) -> str:
    lines = [
        "🌡 <b>해열제 용량 계산기</b>",
        "",
        "아이를 고르거나 체중 버튼을 눌러 주세요.",
        "🚨 <b>생후 3개월 미만</b>은 계산하지 않아요 (즉시 진료)",
        "⚠️ <b>생후 4~5개월</b>은 아세트아미노펜만 안내해요",
    ]
    if not children:
        lines += ["", "👧 홈의 <b>[➕ 아이 등록]</b>으로 등록하면 아이 버튼이 생겨요."]
    elif any(not c.weight_kg for c in children):
        lines += ["", "⚖️ 체중 미입력 아이는 누르면 체중을 고르는 화면이 나와요."]
    return "\n".join(lines)


def format_fever_weight_pick_html(child: ChildProfile, months: int) -> str:
    """Child without a stored weight: ask for the weight first (age bucket comes from the child's birth date)."""
    lines = [
        "🌡 <b>해열제 용량 계산기</b>",
        f"👧 {esc(child.nickname)} · 생후 <b>{months}개월</b> · 체중 미입력",
        "",
        "⚖️ 지금 체중에 가장 가까운 버튼을 눌러 주세요.",
    ]
    if months < 6:
        lines.append("⚠️ <b>생후 4~5개월</b>은 아세트아미노펜만 안내해요")
    return "\n".join(lines)


def format_fever_emergency_html() -> str:
    return "\n".join([
        "🚨 <b>생후 3개월 미만 발열</b>",
        "",
        esc(WARN_INFANT_UNDER_3M),
        "",
        EMERGENCY_FALLBACK,
        "🏥 가까운 응급실·달빛어린이병원은 아래 버튼으로 찾으세요.",
    ])


def format_fever_result_html(
    results_by_ingredient: dict[IngredientType, FeverCalculationResult], child: Optional[ChildProfile] = None,
    age_label: Optional[str] = None,
) -> str:
    """age_label overrides the exact month count for quick-weight buttons (e.g. '6개월 이상')."""
    any_result = next(iter(results_by_ingredient.values()), None)
    weight = any_result.weight_kg if any_result else (child.weight_kg if child else None)
    months = any_result.age_months if any_result else 0
    who = f"{esc(child.nickname)} · " if child else ""
    age_text = esc(age_label) if age_label else f"{months}개월"
    weight_text = f"체중 <b>{weight:g}kg</b>" if weight else "체중 미입력"
    lines = ["🌡 <b>해열제 1회 용량</b>", f"{who}{weight_text} · 생후 <b>{age_text}</b>", ""]

    if any(r.status == STATUS_BLOCKED_INFANT for r in results_by_ingredient.values()):
        lines += [esc(WARN_INFANT_UNDER_3M), "", EMERGENCY_FALLBACK]
    else:
        shown_warnings: set[str] = set()
        for ing, r in results_by_ingredient.items():
            name = esc(r.product.product_name) if r.product else esc(ing.label)
            if r.is_ok:
                lines += [
                    f"💊 <b>{esc(ing.label)}</b> · {name}",
                    f"   1회 <b>{r.recommended_ml:g} mL</b> ({r.recommended_mg:g}mg)",
                    f"   {r.interval_hours}시간 간격 · 하루 최대 {r.max_times_per_day}회 · 총 {r.max_daily_ml:g} mL",
                    "",
                ]
            else:
                lines += [f"⛔ <b>{esc(ing.label)}</b> · {name}", "   계산 차단", ""]
                if r.warning_message:
                    shown_warnings.add(r.warning_message)
        for w in sorted(shown_warnings):
            lines += [esc(w), ""]
        lines.append("⛔ 이부프로펜 ↔ 덱시부프로펜 교차복용 금지")
    lines += ["", f"<i>{esc(DISCLAIMER)}</i>"]
    return "\n".join(lines)


def format_dose_timer_push_html(child_name: str, timer: DoseTimer, given_at_str: str = "") -> str:
    """Render a conservative cross-dosing reminder from a timer snapshot."""
    raw_given_value = getattr(timer, "given_ingredient", "") or ""
    raw_given = raw_given_value.value if isinstance(raw_given_value, IngredientType) else str(raw_given_value)
    try:
        given = IngredientType(raw_given)
        given_label = given.label
        candidates = cross_dosing_candidates(given, 6)
        candidate_text = ", ".join(c.label for c in candidates) if candidates else "없음"
    except (TypeError, ValueError):
        given_label = raw_given or "확인 필요"
        candidate_text = "의료진·약사 확인"
    lines = [
        "⏰ <b>교차복용 확인 알림</b>",
        f"👧 <b>{esc(child_name or '아이')}</b> · 방금 기록한 성분: <b>{esc(given_label)}</b>",
    ]
    if given_at_str:
        lines.append(f"🕘 기록 시각: {esc(given_at_str)}")
    lines += [
        f"🔁 교차 고려 후보: <b>{esc(candidate_text)}</b>",
        "   후보는 생후 6개월 이상에서만 검토하며, 제품 설명서·의료진 지시를 먼저 확인하세요.",
        "⛔ 이부프로펜 ↔ 덱시부프로펜은 같은 NSAID 계열이라 교차복용하지 않습니다.",
        "",
        f"<i>{esc(CROSS_TIMER_NOTICE)}</i>",
    ]
    return "\n".join(lines)


def format_cross_dosing_html() -> str:
    return "\n".join([
        "🔥 <b>열이 안 떨어질 때</b>",
        "",
        f"1️⃣ <b>같은 성분</b>: {esc(CROSS_DOSING_GUIDE[CROSS_SAME_INGREDIENT])}",
        "",
        f"2️⃣ <b>이부프로펜 ↔ 덱시부프로펜</b>: {esc(CROSS_DOSING_GUIDE[CROSS_BLOCKED_SAME_NSAID])}",
        "",
        f"3️⃣ <b>아세트아미노펜 ↔ 이부/덱시부</b>: {esc(CROSS_DOSING_GUIDE[CROSS_ALLOWED_WITH_CAUTION])}",
        "",
        "🚨 아래 증상이면 바로 진료받으세요",
        "• 생후 3개월 미만 발열",
        "• 처지거나 깨우기 어려움 · 경련",
        "• 숨쉬기 힘듦 · 소변량 급감",
        "• 고열이 이틀 이상 지속",
        "",
        EMERGENCY_FALLBACK,
        "",
        f"<i>{esc(DISCLAIMER)}</i>",
    ])


def format_hospital_list_html(hospitals: list[MoonlightHospital], region_label: str, limit: int = 8,
                              hours_by_hospital: Optional[dict[str, list[Any]]] = None,
                              now_dt: Optional[datetime] = None) -> str:
    lines = [f"🌙 <b>{esc(region_label)} 달빛어린이병원</b>", f"<b>{HOSPITAL_PHONE_WARNING}</b>", ""]
    if not hospitals:
        lines.append("등록된 병원이 없어요.")
    for h in hospitals[:limit]:
        tel = f"📞 <code>{esc(h.tel)}</code>" if h.tel else "📞 번호 확인: 119 안내"
        if hours_by_hospital is not None and not getattr(h, "is_placeholder", False):
            state = is_open_now(hours_by_hospital.get(h.hospital_id, []), now_dt or datetime.now())
            open_label = {"OPEN": "🟢 영업 중", "CLOSED": "🔴 영업 종료"}.get(state, "⚪ 전화 확인 필수")
        else:
            open_label = "⚪ 전화 확인 필수"
        lines += [
            f"🏥 <b>{esc(h.name)}</b> · {esc(h.region_sigungu)} · {open_label}",
            f"   🕘 평일 {esc(h.weekday_hours)} · 주말 {esc(h.weekend_hours)}",
            f"   {tel} · {link('지도', h.map_url)}",
            f"   💊 {esc(h.paired_pharmacy)}" + (f" <code>{esc(h.paired_pharmacy_tel)}</code>" if h.paired_pharmacy_tel else ""),
            f"   🔎 확인일: {esc(h.last_verified_at or '미검증')}",
            "",
        ]
    lines += [
        f"🔗 공식 목록: {link('응급의료포털 E-gen', 'https://www.e-gen.or.kr/')}",
        EMERGENCY_FALLBACK,
    ]
    return "\n".join(lines)


# ── Transport ─────────────────────────────────────────────────
class TelegramAPIError(RuntimeError):
    def __init__(self, method: str, code: int, description: str, retry_after: float = 0.0):
        super().__init__(f"Telegram API {method} HTTP {code}: {description}")
        self.code = code
        self.description = description
        self.retry_after = retry_after


class KidsTelegramChannel:
    """Minimal Telegram Bot API client (shared keep-alive helper; stdlib fallback)."""

    def __init__(self, bot_token: str, opener: Optional[Callable[..., Any]] = None):
        self.bot_token = (bot_token or "").strip()
        self._injected_opener = opener  # None -> shared keep-alive helper (patched urlopen still honoured)
        self._opener = opener or urllib.request.urlopen
        if not self.bot_token:
            logger.warning("KidsTelegramChannel initialized with empty bot_token")

    def _api_call(self, method: str, payload: Optional[dict[str, Any]] = None, timeout: int = 15) -> dict[str, Any]:
        url = f"https://api.telegram.org/bot{self.bot_token}/{method}"
        req = urllib.request.Request(
            url, data=json.dumps(payload or {}).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json", "User-Agent": "KidsCompassBot/1.0"},
        )
        try:
            with open_request(req, timeout, opener=self._injected_opener) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            desc, retry_after = str(exc), 0.0
            try:
                err = json.loads(exc.read().decode("utf-8", errors="ignore"))
                desc = err.get("description", desc)
                retry_after = float((err.get("parameters") or {}).get("retry_after", 0) or 0)
            except Exception:
                pass
            raise TelegramAPIError(method, exc.code, desc, retry_after) from exc
        if not data.get("ok"):
            raise TelegramAPIError(method, 0, data.get("description", "unknown"))
        return data

    def set_my_commands(self, commands: Optional[list[dict[str, str]]] = None) -> bool:
        return bool(self._api_call("setMyCommands", {"commands": commands or KIDS_COMMANDS}).get("ok"))

    def send_message(self, chat_id: int | str, text: str, reply_markup: Optional[dict[str, Any]] = None,
                     disable_notification: bool = False) -> int:
        body: dict[str, Any] = {"chat_id": str(chat_id), "text": text, "parse_mode": "HTML",
                                "disable_web_page_preview": True}
        if disable_notification:
            body["disable_notification"] = True
        if reply_markup:
            body["reply_markup"] = reply_markup
        return int(self._api_call("sendMessage", body)["result"]["message_id"])

    def edit_message_text(self, chat_id: int | str, message_id: int, text: str,
                          reply_markup: Optional[dict[str, Any]] = None) -> bool:
        body: dict[str, Any] = {"chat_id": str(chat_id), "message_id": message_id, "text": text,
                                "parse_mode": "HTML", "disable_web_page_preview": True}
        if reply_markup is not None:
            body["reply_markup"] = reply_markup
        try:
            return bool(self._api_call("editMessageText", body).get("ok"))
        except TelegramAPIError as exc:
            if "message is not modified" in exc.description:
                return True
            logger.warning("edit_message_text failed: %s", exc)
            return False

    def answer_callback_query(self, callback_query_id: str, text: str = "") -> bool:
        try:
            body = {"callback_query_id": callback_query_id, "text": text, "show_alert": False}
            return bool(self._api_call("answerCallbackQuery", body, timeout=5).get("ok"))
        except Exception as exc:  # noqa: BLE001 - spinner ack must never break the flow
            logger.debug("answerCallbackQuery failed: %s", exc)
            return False

    def get_updates(self, offset: int = 0, timeout: int = 25) -> list[dict[str, Any]]:
        res = self._api_call(
            "getUpdates", {"offset": offset, "timeout": timeout, "allowed_updates": ["message", "callback_query"]},
            timeout=timeout + 10,
        )
        return res.get("result", [])


class TelegramRateLimiter:
    """Global <= 25 msg/s and >= 1.0s per chat ([TG-05])."""

    def __init__(self, max_global_rate: float = 25.0, min_chat_interval: float = 1.0,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic):
        self.min_global_interval = 1.0 / max_global_rate if max_global_rate > 0 else 0.04
        self.min_chat_interval = min_chat_interval
        self._sleep = sleep
        self._clock = clock
        self._last_global = float("-inf")
        self._last_chat: dict[str, float] = {}

    def wait_before_send(self, chat_id: int | str) -> None:
        key = str(chat_id)
        now = self._clock()
        wait = max(
            self.min_global_interval - (now - self._last_global),
            self.min_chat_interval - (now - self._last_chat.get(key, float("-inf"))),
            0.0,
        )
        if wait > 0:
            self._sleep(wait)
        stamp = self._clock()
        self._last_global = stamp
        self._last_chat[key] = stamp


class SafeTelegramDispatcher:
    """One message per chat with idempotency log + 429 exponential back-off.

    Statuses: SENT | UNKNOWN (network timeout — may have been delivered, never resent) |
              BLOCKED (400/403) | FAILED (retryable on the next run).
    """

    def __init__(self, rate_limiter: Optional[TelegramRateLimiter] = None, max_retries: int = 3,
                 sleep: Callable[[float], None] = time.sleep):
        self.limiter = rate_limiter or TelegramRateLimiter(sleep=sleep)
        self.max_retries = max_retries
        self._sleep = sleep

    def dispatch(self, channel: Any, db: Any, dispatch_key: str,
                 messages: Iterable[tuple[int | str, str]]) -> dict[str, Any]:
        stats: dict[str, Any] = {"total": 0, "sent": 0, "skipped": 0, "unknown": 0, "blocked": 0, "failed": 0,
                                 "errors": {}}
        for chat_id, text in messages:
            stats["total"] += 1
            if db.has_dispatched(dispatch_key, chat_id):
                stats["skipped"] += 1
                continue
            status, err = self._send_with_retry(channel, chat_id, text)
            db.log_dispatch(dispatch_key, chat_id, status)
            stats[status.lower()] += 1
            if err:
                stats["errors"][str(chat_id)] = err
        return stats

    def dispatch_push_messages(self, channel: Any, db: Any, push_messages: Iterable[PushMessage],
                               dispatch_window: str) -> dict[str, Any]:
        """1:1 push with inline buttons, idempotent on kids_sent_history.

        Per message: [TG-01] pre-check -> atomic IN_FLIGHT claim of the message sentinel row
        (user, stream, window) -> per-entity claims (cooldown / already-sent bookkeeping) -> send ->
        finalize every claimed row with the same status. Existing IN_FLIGHT / SENT / UNKNOWN / BLOCKED rows are
        skipped (UNKNOWN = possibly delivered → never resent); only FAILED is claimed again.
        """
        stats: dict[str, Any] = {"total": 0, "sent": 0, "skipped": 0, "unknown": 0, "blocked": 0, "failed": 0,
                                 "invalid": 0, "errors": {}}
        for msg in push_messages:
            stats["total"] += 1
            chat_id, stream, etype = str(msg.chat_id), msg.stream or "push", msg.entity_type
            if not fits_limits(msg.text):
                stats["invalid"] += 1
                stats["errors"][chat_id] = "TG-01 limit exceeded (%d html / %d visible)" % measure_message(msg.text)
                continue
            try:
                claimed = db.claim_sent_history(chat_id, stream, etype, PUSH_MESSAGE_SENTINEL, dispatch_window,
                                                content_hash=msg.content_hash)
            except ValueError as exc:  # unknown entity_type
                stats["invalid"] += 1
                stats["errors"][chat_id] = str(exc)
                continue
            if not claimed:
                stats["skipped"] += 1
                continue
            entity_rows = [eid for eid in dict.fromkeys(str(e) for e in msg.entity_ids)
                           if eid != PUSH_MESSAGE_SENTINEL
                           and db.claim_sent_history(chat_id, stream, etype, eid, dispatch_window,
                                                     content_hash=msg.content_hash)]
            status, err = self._send_with_retry(channel, chat_id, msg.text, msg.reply_markup)
            for eid in [PUSH_MESSAGE_SENTINEL, *entity_rows]:
                db.finalize_sent_history(chat_id, stream, etype, eid, dispatch_window, status)
            stats[status.lower()] += 1
            if err:
                stats["errors"][chat_id] = err
        return stats

    def dispatch_combined_push_messages(self, channel: Any, db: Any,
                                        push_messages: Iterable[PushMessage]) -> dict[str, Any]:
        """Deliver one rendered message while claiming/finalizing multiple stream aliases."""
        stats: dict[str, Any] = {"total": 0, "sent": 0, "skipped": 0, "unknown": 0, "blocked": 0,
                                 "failed": 0, "invalid": 0, "errors": {}}
        for msg in push_messages:
            stats["total"] += 1
            if not fits_limits(msg.text):
                stats["invalid"] += 1
                stats["errors"][str(msg.chat_id)] = "TG-01 limit exceeded (%d html / %d visible)" % measure_message(msg.text)
                continue
            aliases = list(getattr(msg, "delivery_aliases", ()) or ())
            if not aliases:
                aliases = [(msg.stream, msg.entity_type, getattr(msg, "dispatch_window", ""), msg.entity_ids)]
            claimed: list[tuple[str, str, str, list[str]]] = []
            blocked = False
            for stream, etype, window, entity_ids in aliases:
                if db.get_sent_status(str(msg.chat_id), stream, etype, PUSH_MESSAGE_SENTINEL, window) in {
                    "IN_FLIGHT", "SENT", "UNKNOWN", "BLOCKED"
                }:
                    blocked = True
                    break
            if blocked:
                stats["skipped"] += 1
                continue
            try:
                for stream, etype, window, entity_ids in aliases:
                    if not db.claim_sent_history(str(msg.chat_id), stream, etype, PUSH_MESSAGE_SENTINEL, window,
                                                  content_hash=msg.content_hash):
                        blocked = True
                        break
                    entity_claimed = [str(eid) for eid in dict.fromkeys(entity_ids)
                                      if str(eid) != PUSH_MESSAGE_SENTINEL and
                                      db.claim_sent_history(str(msg.chat_id), stream, etype, str(eid), window,
                                                             content_hash=msg.content_hash)]
                    claimed.append((stream, etype, window, [PUSH_MESSAGE_SENTINEL, *entity_claimed]))
            except ValueError as exc:
                stats["invalid"] += 1
                stats["errors"][str(msg.chat_id)] = str(exc)
                blocked = True
            if blocked:
                for stream, etype, window, ids in claimed:
                    for eid in ids:
                        db.finalize_sent_history(str(msg.chat_id), stream, etype, eid, window, "FAILED")
                if not claimed:
                    stats["skipped"] += 1
                continue
            status, err = self._send_with_retry(channel, str(msg.chat_id), msg.text, msg.reply_markup)
            for stream, etype, window, ids in claimed:
                for eid in ids:
                    db.finalize_sent_history(str(msg.chat_id), stream, etype, eid, window, status)
            stats[status.lower()] += 1
            if err:
                stats["errors"][str(msg.chat_id)] = err
        return stats

    def _send_with_retry(self, channel: Any, chat_id: int | str, text: str,
                         reply_markup: Optional[dict[str, Any]] = None) -> tuple[str, Optional[str]]:
        for attempt in range(self.max_retries + 1):
            self.limiter.wait_before_send(chat_id)
            try:
                if reply_markup is None:
                    channel.send_message(chat_id, text)
                else:
                    channel.send_message(chat_id, text, reply_markup=reply_markup)
                return "SENT", None
            except TelegramAPIError as exc:
                if exc.code == 429 and attempt < self.max_retries:
                    self._sleep(max(exc.retry_after, 2.0 ** attempt))
                    continue
                if exc.code in (400, 403):
                    return "BLOCKED", str(exc)
                return "FAILED", str(exc)
            except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
                # Delivery is ambiguous — never resend (no duplicate push)
                logger.warning("send to %s network error (not retried): %s", chat_id, exc)
                return "UNKNOWN", str(exc)
            except Exception as exc:  # noqa: BLE001
                logger.warning("send to %s failed: %s", chat_id, exc)
                return "FAILED", str(exc)
        return "FAILED", "429 retries exhausted"
