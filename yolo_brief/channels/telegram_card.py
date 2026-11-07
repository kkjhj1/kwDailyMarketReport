"""[TG-01/02/03/04] Slim Mobile 1-Line-1-Info Card Renderer for YOLO Bot (욜로봇).
- Enforces visible text <= 1,800 chars and raw HTML <= 3,500 chars ([TG-01]).
- Escapes all dynamic text via `html.escape()` ([TG-02]).
- Enforces 64-byte UTF-8 limit on all `callback_data` ([TG-04]).
"""
from __future__ import annotations

import html
import re
from typing import Any, Optional

from yolo_brief.matching import MatchResult
from yolo_brief.models import (
    AREA_NAMES,
    SoloArchetype,
    YoloUserProfile,
)

MAX_VISIBLE_CHARS = 1800
MAX_HTML_CHARS = 3500
MAX_CALLBACK_BYTES = 64

WEB_MAGAZINE_URL = "https://kkjhj1.github.io/kwDailyMarketReport/reports/yolo_latest.html"
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


def build_main_keyboard(user: YoloUserProfile) -> dict[str, Any]:
    """Builds main interactive inline keyboard under curation cards."""
    fb_next = "0" if user.weather_fallback_enabled else "1"
    fb_label = (
        "☔ 실내대체: ON" if user.weather_fallback_enabled else "☔ 실내대체: OFF"
    )
    rows = [
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

    lines: list[str] = [
        title_line,
        f"🎯 <b>{esc_arc}</b> ｜ {weather_line}",
        "───────────────────────",
    ]

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

            lines.append(f"{badge} {item.emoji} <b>{esc_title}</b>")
            lines.append(f"📍 {esc_loc}  ｜  ⏱ {esc_cost}")
            lines.append(f"🏷️ <b>{tags_str}</b>  <a href=\"{safe_url}\">바로가기↗</a>")
            if idx < len(match.items[:3]) - 1:
                lines.append("")

    lines.append("───────────────────────")
    lines.append("💡 <i>상세 사진·타임테이블·예산은 아래 웹 매거진에서 확인하세요!</i>")

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
    keyboard = build_main_keyboard(user)
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
