"""[TG-01/02/03/04] Slim Mobile 1-Line-1-Info Card Renderer for YOLO Bot (욜로봇).
- Enforces visible text <= 1,800 chars and raw HTML <= 3,500 chars ([TG-01]).
- Escapes all dynamic text via `html.escape()` ([TG-02]).
- Enforces 64-byte UTF-8 limit on all `callback_data` ([TG-04]).
"""
from __future__ import annotations

import html
import re
from typing import Any, Optional, Sequence

from yolo_brief.matching import TIME_SLOT_RELAXED_NOTICE, MatchResult
from yolo_brief.models import (
    AREA_NAMES,
    AREA_SHORT_CODES,
    ActivityMode,
    SOLO_LEVEL_LABELS,
    SoloArchetype,
    TimeSlot,
    VALID_MBTI_TYPES,
    WeatherSnapshot,
    YoloCuration,
    YoloUserProfile,
)
from yolo_brief.weather_engine import evaluate_weather_gate

MAX_VISIBLE_CHARS = 1800
MAX_HTML_CHARS = 3500
MAX_CALLBACK_BYTES = 64

WEB_MAGAZINE_URL = "http://161.33.196.211:8002/reports/yolo_latest.html"
WEB_MAGAZINE_BUTTON_TEXT = "🌐 감성 사진 & 나홀로 풀코스 매거진 보기"

_TAG_RE = re.compile(r"<[^>]+>")
_NUMBER_BADGES = ("1️⃣", "2️⃣", "3️⃣")


def measure_message(html_text: str) -> tuple[int, int]:
    """Returns `(visible_char_len, raw_html_len)`."""
    stripped = _TAG_RE.sub("", html_text)
    unescaped = html.unescape(stripped)
    return len(unescaped), len(html_text)


def validate_callback_data(callback_data: str) -> str:
    """Enforces [TG-04] 64-byte UTF-8 callback_data limit."""
    raw_bytes = callback_data.encode("utf-8")
    if len(raw_bytes) > MAX_CALLBACK_BYTES:
        raise ValueError(
            f"[TG-04 Violation] callback_data exceeds 64 bytes ({len(raw_bytes)}B): {callback_data!r}"
        )
    return callback_data


def _btn(text: str, callback_data: str) -> dict[str, str]:
    return {"text": text, "callback_data": validate_callback_data(callback_data)}


def build_archetype_keyboard(
    current: Optional[SoloArchetype] = None,
) -> dict[str, Any]:
    """Builds inline keyboard for selecting among the 4 Solo Archetypes."""
    rows: list[list[dict[str, str]]] = []
    for arc in SoloArchetype:
        prefix = "✅ " if current == arc else ""
        label = f"{prefix}{arc.emoji} {arc.label}"
        rows.append([_btn(label, f"yolo:set:{arc.value}")])
    if current is not None:
        rows.append([_btn("↩️ 내 큐레이션 보드로", "yolo:view:weekend")])
    return {"inline_keyboard": rows}


AREA_CODE_TO_SHORT: dict[str, str] = {code: short for short, code in AREA_SHORT_CODES.items()}

_SLOT_BUTTON_LABELS: dict[TimeSlot, str] = {
    TimeSlot.ALL: "전체",
    TimeSlot.QUICK_2H: "⚡2시간",
    TimeSlot.HALF_DAY: "🌤반나절",
    TimeSlot.FULL_OR_TRIP: "🧳하루·1박",
}


def area_label(area_code: str) -> str:
    return AREA_NAMES.get(str(area_code), "서울·수도권")


def build_item_action_rows(match: Optional[MatchResult]) -> list[list[dict[str, str]]]:
    """`📘 가이드` / `⭐ 찜` / `✅ 다녀옴` rows for the items on the card (`<= 64B`)."""
    if match is None or match.blocked_by_weather_no_fallback or not match.items:
        return []
    guide_row: list[dict[str, str]] = []
    save_row: list[dict[str, str]] = []
    done_row: list[dict[str, str]] = []
    for idx, item in enumerate(match.items[:3], start=1):
        guide_cb = f"yolo:guide:{item.id}"
        sav_cb = f"yolo:log:sav:{item.id}"
        done_cb = f"yolo:log:done:{item.id}"
        if (
            len(guide_cb.encode("utf-8")) > MAX_CALLBACK_BYTES
            or len(done_cb.encode("utf-8")) > MAX_CALLBACK_BYTES
        ):
            continue  # [TG-04] never emit an oversized callback
        guide_row.append(_btn(f"📘 {idx}번 가이드", guide_cb))
        save_row.append(_btn(f"⭐ {idx}번 찜", sav_cb))
        done_row.append(_btn(f"✅ {idx}번 다녀옴", done_cb))
    return [row for row in (guide_row, save_row, done_row) if row]


def build_time_slot_row(current: TimeSlot) -> list[dict[str, str]]:
    row = []
    for slot in TimeSlot:
        label = _SLOT_BUTTON_LABELS[slot]
        if slot == current:
            label = f"✔{label}"
        row.append(_btn(label, f"yolo:slot:{slot.callback_code}"))
    return row


def build_main_keyboard(
    user: YoloUserProfile, match: Optional[MatchResult] = None
) -> dict[str, Any]:
    """Builds main interactive inline keyboard under curation cards."""
    fb_next = "0" if user.weather_fallback_enabled else "1"
    fb_label = (
        "☔ 실내대체: ON" if user.weather_fallback_enabled else "☔ 실내대체: OFF"
    )
    mbti_tag = f"·{user.mbti}" if user.mbti else ""
    rows = build_item_action_rows(match)
    rows += [
        build_time_slot_row(TimeSlot(user.preferred_time_slot)),
        [
            _btn("🔀 다른 코스 보기", "yolo:view:next"),
            _btn("⭐ 내 찜·완료 보관함", "yolo:view:saved"),
        ],
        [
            _btn(f"🧩 성향·MBTI({user.solo_level_label}{mbti_tag})", "yolo:profile"),
            _btn(f"📍 지역: {area_label(user.weather_area_code)}", "yolo:area:menu"),
        ],
        [
            _btn("🔄 주말 추천", "yolo:view:weekend"),
            _btn("🌧️ 우천대체 미리보기", "yolo:view:rain"),
        ],
        [
            _btn("🎯 성향 변경", "yolo:menu"),
            _btn(fb_label, f"yolo:cfg:fb:{fb_next}"),
        ],
        [
            _btn("🔕 구독 해지", "yolo:unsub:ask"),
        ],
    ]
    return {"inline_keyboard": rows}


def build_area_keyboard(user: YoloUserProfile) -> dict[str, Any]:
    """7 weather areas (`yolo:area:<SHORT>`), 2 per row, plus a back button."""
    rows: list[list[dict[str, str]]] = []
    row: list[dict[str, str]] = []
    for short, code in AREA_SHORT_CODES.items():
        label = AREA_NAMES[code]
        if code == user.weather_area_code:
            label = f"✔ {label}"
        row.append(_btn(label, f"yolo:area:{short}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([_btn("↩️ 내 큐레이션 보드로", "yolo:view:weekend")])
    return {"inline_keyboard": rows}


def render_area_menu_card(user: YoloUserProfile) -> tuple[str, dict[str, Any]]:
    """Area picker card: the chosen area drives the real-time weather lookup."""
    text = "\n".join(
        [
            "📍 <b>날씨 기준 지역 변경</b>",
            "───────────────────────",
            f"현재 지역: <b>{html.escape(area_label(user.weather_area_code))}</b>",
            "선택한 지역의 실시간 날씨로 야외/실내 코스를 판단해요.",
        ]
    )
    return text, build_area_keyboard(user)


def render_onboarding_card(
    user: Optional[YoloUserProfile] = None,
) -> tuple[str, dict[str, Any]]:
    """Renders the /start or archetype selection card."""
    current_arc = user.archetype if user else None
    lines = [
        "🎯 <b>나홀로 욜로봇 · 취향 성향 선택</b>",
        "",
        "혼자 있을 때 에너지를 얻는 나만의 방식을 골라주세요.",
        "사계절 날씨와 100% 1인 맞춤 코스가 실시간 연결됩니다.",
        "",
        "───────────────────────",
        "🏃 <b>액티브 챌린저</b> ｜ 땀·성취 (마라톤/등산/클라이밍)",
        "🎨 <b>어반 힙스터</b> ｜ 문화·미식 (전시/팝업/1인 LP바)",
        "✈️ <b>로드 노마드</b> ｜ 혼여·방랑 (KTX당일/섬/온천)",
        "🛋️ <b>웰니스 딥다이버</b> ｜ 홈바·휴식 (칵테일/북캉스/OTT)",
        "───────────────────────",
    ]
    if current_arc:
        lines.append(f"현재 선택: <b>{current_arc.emoji} {current_arc.label}</b>")
    else:
        lines.append("👇 <i>아래 버튼을 눌러 성향을 선택하세요 (언제든 1초 변경 가능)</i>")
    text = "\n".join(lines)
    return text, build_archetype_keyboard(current=current_arc)


def render_curation_card(
    user: YoloUserProfile,
    match: MatchResult,
) -> tuple[str, dict[str, Any]]:
    """Renders [TG-01/02/03] 1-line-1-info mobile card with up to 3 solo curations."""
    if user.archetype is None:
        return render_onboarding_card(user)

    arc = user.archetype
    area_label = AREA_NAMES.get(user.weather_area_code, "서울·수도권")
    esc_reason = html.escape(match.weather_reason)
    esc_arc = html.escape(f"{arc.emoji} {arc.label}")
    esc_season = html.escape(match.season.label)
    esc_area = html.escape(area_label)

    if match.fallback_applied:
        title_line = "☔ <b>이번 주 나홀로 욜로 · 실내 스마트 대체</b>"
        weather_line = f"🌧 날씨: <b>{esc_reason}</b> (실내 대체 가동)"
    elif match.is_severe_weather:
        title_line = "⚠️ <b>이번 주 나홀로 욜로 · 기상 주의</b>"
        weather_line = f"🌧 날씨: <b>{esc_reason}</b>"
    else:
        title_line = f"✨ <b>이번 주 나홀로 욜로 · {esc_season} 큐레이션</b>"
        weather_line = f"🌤 날씨: <b>{esc_reason}</b> ({esc_area})"

    slot_suffix = (
        f" ｜ ⏱ <b>{html.escape(match.time_slot.label)}</b>"
        if match.time_slot != TimeSlot.ALL
        else ""
    )
    profile_bits = [html.escape(user.solo_level_label)]
    if user.mbti:
        profile_bits.append(html.escape(user.mbti))
    if user.secondary_archetype and user.secondary_archetype != arc:
        profile_bits.append(f"+{html.escape(user.secondary_archetype.label)}")
    profile_suffix = f" ｜ 🧩 <b>{'·'.join(profile_bits)}</b>"

    lines: list[str] = [
        title_line,
        f"🎯 <b>{esc_arc}</b>{profile_suffix} ｜ {weather_line}{slot_suffix}",
        "───────────────────────",
    ]
    if match.time_slot_relaxed:
        lines.append(f"ℹ️ <i>{html.escape(TIME_SLOT_RELAXED_NOTICE)}</i>")

    if match.blocked_by_weather_no_fallback:
        lines.extend(
            [
                "🚨 <b>악천후 안전 가드 작동</b>",
                "현재 야외 활동이 위험하며, <b>실내 대체(Fallback)</b> 설정이 꺼져 있어 야외 추천을 보류합니다.",
                "",
                "👉 아래 <b>[실내대체: ON]</b> 버튼을 켜시면 비·눈·폭염에도 안전한 1인 실내 코스를 바로 추천해 드립니다.",
            ]
        )
    elif not match.items:
        lines.append("현재 조건에 맞는 1인 큐레이션이 없습니다. 성향을 변경해 보세요.")
    else:
        for idx, item in enumerate(match.items[:3]):
            badge = _NUMBER_BADGES[idx] if idx < len(_NUMBER_BADGES) else "•"
            esc_title = html.escape(item.title)
            esc_loc = html.escape(item.location_name)
            esc_cost = html.escape(item.cost_or_duration)
            tags_str = " · ".join(html.escape(t) for t in item.solo_tags[:3])
            safe_url = html.escape(item.action_url, quote=True)
            diff_badge = f"<code>[Lv.{int(item.solo_difficulty)}]</code>"
            mbti_chip = (
                f"  🧩<i>{'·'.join(html.escape(m) for m in item.mbti_tags[:2])}</i>"
                if item.mbti_tags
                else ""
            )

            lines.append(f"{badge} {item.emoji} <b>{esc_title}</b> {diff_badge}")
            lines.append(f"📍 {esc_loc}  ｜  ⏱ {esc_cost}")
            lines.append(f"🏷️ <b>{tags_str}</b>{mbti_chip}  <a href=\"{safe_url}\">바로가기↗</a>")
            if idx < len(match.items[:3]) - 1:
                lines.append("")

    lines.append("───────────────────────")
    lines.append("💡 <i>초보 실행 요령·준비물은 [📘 가이드], 사진·풀코스는 웹 매거진에서!</i>")

    full_html = "\n".join(lines)
    vis_len, html_len = measure_message(full_html)
    if vis_len > MAX_VISIBLE_CHARS or html_len > MAX_HTML_CHARS:
        # Fixed slim fallback without arbitrary item deletion ([TG-01])
        slim_lines = [
            title_line,
            f"🎯 성향: <b>{esc_arc}</b>",
            weather_line,
            "──────────────────",
        ]
        for idx, item in enumerate(match.items[:3]):
            badge = _NUMBER_BADGES[idx] if idx < len(_NUMBER_BADGES) else "•"
            esc_title = html.escape(item.title[:40])
            esc_loc = html.escape(item.location_name[:20])
            tags_str = " · ".join(html.escape(t) for t in item.solo_tags[:2])
            safe_url = html.escape(item.action_url, quote=True)
            slim_lines.append(f"{badge} {item.emoji} <b>{esc_title}</b>")
            slim_lines.append(f"📍 {esc_loc} | 🧍 <b>{tags_str}</b>")
            slim_lines.append(f'🔗 <a href="{safe_url}">상세 웹 리포트 보기</a>')
        full_html = "\n".join(slim_lines)

    # v2.0: 1행에 웹 매거진 URL 버튼 (URL 버튼은 callback_data 없음)
    keyboard = build_main_keyboard(user, match)
    keyboard["inline_keyboard"].insert(
        0, [{"text": WEB_MAGAZINE_BUTTON_TEXT, "url": WEB_MAGAZINE_URL}]
    )
    return full_html, keyboard


def render_unsub_confirm_card() -> tuple[str, dict[str, Any]]:
    """[TG-04] In-Place 2-step confirmation for unsubscribe action."""
    text = (
        "🔕 <b>나홀로 욜로봇 정기 브리핑 해지 확인</b>\n"
        "──────────────────\n"
        "주간 맞춤형 1인 라이프스타일 큐레이션 알림을 중단하시겠습니까?\n"
        "해지 후에도 언제든 <code>/start</code>로 다시 시작할 수 있습니다."
    )
    keyboard = {
        "inline_keyboard": [
            [
                _btn("✅ 네, 해지합니다", "yolo:unsub:ok"),
                _btn("↩️ 아니오, 유지할게요", "yolo:unsub:no"),
            ]
        ]
    }
    return text, keyboard


def render_unsubscribed_card() -> tuple[str, dict[str, Any]]:
    """Renders completion card after unsubscribing."""
    text = (
        "✅ <b>정기 브리핑 구독이 해지되었습니다.</b>\n"
        "혼자만의 영감과 휴식이 필요할 때 언제든 다시 찾아주세요!"
    )
    keyboard = {
        "inline_keyboard": [
            [_btn("🔄 다시 성향 고르고 시작하기", "yolo:menu")]
        ]
    }
    return text, keyboard


def render_guide_card(
    user: YoloUserProfile,
    curation: YoloCuration,
    snapshot: Optional[WeatherSnapshot] = None,
) -> tuple[str, dict[str, Any]]:
    """Renders the Phase B 1-person execution coach card (`yolo:guide:<id>`).
    Enforces [INV-02] weather gate, [TG-01] 1,800/3,500 length caps, and [TG-02] HTML escape."""
    is_severe, _, reason = evaluate_weather_gate(snapshot)
    esc_reason = html.escape(reason)

    # [INV-02] Hard gate: never expose an OUTDOOR execution guide under severe/UNKNOWN weather
    if is_severe and curation.activity_mode == ActivityMode.OUTDOOR:
        blocked_text = "\n".join(
            [
                "☔ <b>1인 실행 코치 · 기상 안전 가드</b>",
                f"🌧 현재 날씨: <b>{esc_reason}</b>",
                "───────────────────────",
                "선택하신 코스는 <b>야외 활동</b>이므로 현재 기상 조건에서는 안전을 위해 실행 가이드를 보류합니다.",
                "👉 아래 버튼으로 오늘 날씨에 안전한 <b>1인 실내 스마트 대체 코스</b>를 확인해 보세요!",
            ]
        )
        kb = {
            "inline_keyboard": [
                [_btn("☔ 안전한 실내 대체 코스 보기", "yolo:view:weekend")],
                [_btn("⭐ 내 찜·완료 보관함", "yolo:view:saved")],
            ]
        }
        return blocked_text, kb

    esc_title = html.escape(curation.title)
    esc_loc = html.escape(curation.location_name)
    esc_cost = html.escape(curation.cost_or_duration)
    esc_diff = html.escape(curation.difficulty_label)
    mbti_str = " · ".join(f"#{html.escape(m)}" for m in curation.mbti_tags) if curation.mbti_tags else "전 성향"
    pace_label = "예약·계획형(J)" if curation.pace_fit == "J" else ("즉흥출발형(P)" if curation.pace_fit == "P" else "자유형")
    social_label = "완전독립·무대화(I)" if curation.social_fit == "I" else ("현장감·클래스(E)" if curation.social_fit == "E" else "1인 자유")

    lines: list[str] = [
        f"📘 <b>1인 실행 코치 · {curation.emoji} {esc_title}</b>",
        f"🎚 난이도: <b>{esc_diff}</b> ｜ 🧩 <b>{mbti_str}</b> ({social_label}·{pace_label})",
        f"📍 {esc_loc} ｜ ⏱ {esc_cost}",
        "───────────────────────",
    ]

    if curation.how_to_steps:
        lines.append("👣 <b>혼자 처음 할 때 3단계 행동 요령</b>")
        for idx, step in enumerate(curation.how_to_steps[:3]):
            badge = _NUMBER_BADGES[idx] if idx < len(_NUMBER_BADGES) else f"{idx + 1}."
            lines.append(f"{badge} {html.escape(step)}")
        lines.append("")

    if curation.checklist:
        lines.append("🎒 <b>출발·시작 전 준비물 체크리스트</b>")
        lines.append(" · ".join(f"☑️ {html.escape(c)}" for c in curation.checklist[:4]))
        lines.append("")

    if curation.timeline:
        lines.append("🕒 <b>나홀로 동선 요약</b>")
        for t in curation.timeline[:4]:
            lines.append(
                f"• <code>{html.escape(t.time)}</code> <b>{html.escape(t.spot)}</b> — {html.escape(t.desc)}"
            )
        lines.append("")

    if curation.total_budget_krw > 0:
        lines.append(f"💸 <b>1인 예상 총경비</b>: <b>₩{curation.total_budget_krw:,}</b>")

    if curation.solo_secrets:
        lines.append(f"🤫 <b>1인 시크릿 팁</b>: {html.escape(curation.solo_secrets)}")

    full_html = "\n".join(lines)
    vis_len, html_len = measure_message(full_html)
    if vis_len > MAX_VISIBLE_CHARS or html_len > MAX_HTML_CHARS:
        slim = [
            f"📘 <b>1인 실행 코치 · {curation.emoji} {html.escape(curation.title[:50])}</b>",
            f"🎚 <b>{esc_diff}</b> ｜ 📍 {html.escape(curation.location_name[:30])}",
            "──────────────────",
        ]
        for idx, step in enumerate(curation.how_to_steps[:3]):
            badge = _NUMBER_BADGES[idx] if idx < len(_NUMBER_BADGES) else f"{idx + 1}."
            slim.append(f"{badge} {html.escape(step[:120])}")
        if curation.checklist:
            slim.append("🎒 " + " · ".join(html.escape(c[:30]) for c in curation.checklist[:4]))
        if curation.solo_secrets:
            slim.append(f"🤫 {html.escape(curation.solo_secrets[:140])}")
        full_html = "\n".join(slim)

    rows: list[list[dict[str, str]]] = [
        [{"text": "🔗 상세·1인 예약 안내 바로가기", "url": curation.action_url}],
    ]
    sav_cb = f"yolo:log:sav:{curation.id}"
    done_cb = f"yolo:log:done:{curation.id}"
    if len(done_cb.encode("utf-8")) <= MAX_CALLBACK_BYTES:
        rows.append(
            [
                _btn("⭐ 이 코스 찜", sav_cb),
                _btn("✅ 다녀옴 완료", done_cb),
            ]
        )
    rows.append(
        [
            _btn("⭐ 내 보관함", "yolo:view:saved"),
            _btn("↩️ 추천 보드로", "yolo:view:weekend"),
        ]
    )
    return full_html, {"inline_keyboard": rows}


def render_saved_locker_card(
    user: YoloUserProfile,
    saved_curations: Sequence[YoloCuration],
    done_curations: Sequence[YoloCuration],
    snapshot: Optional[WeatherSnapshot] = None,
) -> tuple[str, dict[str, Any]]:
    """Renders the user's `⭐ 찜` & `✅ 다녀옴 완료` locker (`yolo:view:saved`).
    Enforces [INV-02] weather gate: outdoor items are hidden under severe/UNKNOWN weather."""
    is_severe, _, reason = evaluate_weather_gate(snapshot)
    hidden_outdoor = 0
    if is_severe:
        safe_saved = []
        for c in saved_curations:
            if c.activity_mode == ActivityMode.OUTDOOR:
                hidden_outdoor += 1
            else:
                safe_saved.append(c)
        safe_done = [c for c in done_curations if c.activity_mode != ActivityMode.OUTDOOR]
    else:
        safe_saved = list(saved_curations)
        safe_done = list(done_curations)

    lines: list[str] = [
        "⭐ <b>나홀로 욜로 · 내 찜 &amp; 완료 보관함</b>",
        f"🧩 내 레벨: <b>{html.escape(user.solo_level_label)}</b>"
        + (f" · MBTI: <b>{html.escape(user.mbti)}</b>" if user.mbti else "")
        + f" ｜ 누적 완료: <b>{len(done_curations)}건</b>",
        "───────────────────────",
    ]
    if is_severe and hidden_outdoor > 0:
        lines.append(
            f"☔ <i>현재 기상({html.escape(reason)}) 안전 가드로 야외 찜 코스 {hidden_outdoor}건은 일시 숨김 처리되었습니다.</i>"
        )

    guide_buttons: list[list[dict[str, str]]] = []
    lines.append(f"📌 <b>내가 찜한 코스 ({len(safe_saved)}건)</b>")
    if not safe_saved:
        lines.append("• 아직 찜한 코스가 없습니다. 추천 카드에서 [⭐ 찜]을 눌러보세요!")
    else:
        for idx, c in enumerate(safe_saved[:5], start=1):
            lines.append(
                f"{idx}. {c.emoji} <b>{html.escape(c.title)}</b> <code>[Lv.{c.solo_difficulty}]</code>"
            )
            g_cb = f"yolo:guide:{c.id}"
            if len(g_cb.encode("utf-8")) <= MAX_CALLBACK_BYTES:
                short_title = c.title[:14] + ("…" if len(c.title) > 14 else "")
                guide_buttons.append([_btn(f"📘 가이드 · {short_title}", g_cb)])

    lines.append("")
    lines.append(f"✅ <b>내가 다녀온 완료 코스 ({len(safe_done)}건)</b>")
    if not safe_done:
        lines.append("• 완료한 코스가 없습니다. 다녀온 뒤 [✅ 다녀옴]을 눌러 기록을 쌓아보세요!")
    else:
        for idx, c in enumerate(safe_done[:5], start=1):
            lines.append(f"✔ {c.emoji} {html.escape(c.title)} <code>[Lv.{c.solo_difficulty}]</code>")

    full_html = "\n".join(lines)
    rows = guide_buttons[:4]
    rows.append([_btn("↩️ 내 큐레이션 보드로", "yolo:view:weekend")])
    return full_html, {"inline_keyboard": rows}


def build_profile_keyboard(user: YoloUserProfile) -> dict[str, Any]:
    """Builds inline keyboard for Solo Level (1..5) and 16 MBTI types (`<= 64B`)."""
    lvl_row: list[dict[str, str]] = []
    for lvl in range(1, 6):
        prefix = "✔" if user.solo_level == lvl else ""
        lvl_row.append(_btn(f"{prefix}Lv.{lvl}", f"yolo:lvl:{lvl}"))

    mbti_rows: list[list[dict[str, str]]] = []
    for i in range(0, len(VALID_MBTI_TYPES), 4):
        chunk = VALID_MBTI_TYPES[i : i + 4]
        row: list[dict[str, str]] = []
        for code in chunk:
            prefix = "✔" if user.mbti == code else ""
            row.append(_btn(f"{prefix}{code}", f"yolo:mbti:{code}"))
        mbti_rows.append(row)

    rows: list[list[dict[str, str]]] = [
        lvl_row,
        *mbti_rows,
        [
            _btn("🎯 주 성향 변경", "yolo:menu"),
            _btn("↩️ 내 큐레이션 보드로", "yolo:view:weekend"),
        ],
    ]
    return {"inline_keyboard": rows}


def render_profile_card(user: YoloUserProfile) -> tuple[str, dict[str, Any]]:
    """Renders the Phase B personality & solo-level tuning card (`yolo:profile`)."""
    arc_txt = f"{user.archetype.emoji} {user.archetype.label}" if user.archetype else "미선택"
    sec_txt = (
        f"{user.secondary_archetype.emoji} {user.secondary_archetype.label}"
        if user.secondary_archetype
        else "자동/미설정"
    )
    mbti_txt = user.mbti or "미설정 (아래에서 1초 선택)"
    energy_txt = (
        "I (완전 독립·무대화 선호)"
        if user.social_energy == "I"
        else ("E (현장감·원데이클래스 선호)" if user.social_energy == "E" else "전체")
    )
    pace_txt = (
        "J (사전 예약·체계적 풀코스)"
        if user.pace_style == "J"
        else ("P (예약 없는 즉흥 출발)" if user.pace_style == "P" else "전체")
    )

    lines = [
        "🧩 <b>나홀로 욜로 · 내 성향 &amp; 혼자 레벨 설정</b>",
        "───────────────────────",
        f"• 주 성향: <b>{html.escape(arc_txt)}</b>",
        f"• 보조 성향: <b>{html.escape(sec_txt)}</b>",
        f"• 혼자 레벨: <b>{html.escape(user.solo_level_label)}</b>",
        f"• 나의 MBTI: <b>{html.escape(mbti_txt)}</b>",
        f"• 몰입 스타일: <b>{html.escape(energy_txt)} / {html.escape(pace_txt)}</b>",
        "───────────────────────",
        "👇 <i>혼자 레벨(Lv.1~5)과 MBTI를 선택하면 내 성향에 딱 맞는 난이도와 코스가 우선 추천됩니다.</i>",
    ]
    return "\n".join(lines), build_profile_keyboard(user)
