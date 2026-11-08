"""Link Confirmation Gate (2단계 링크 안내 및 확인 시스템).
Enforces GPT architecture audit:
- [INV-01] Strict 64-byte callback_data: `gate:g:<token>` and `gate:c:<token>` (<= 24 bytes)
- [INV-02] Pre-emptive callback ACK (< 0.1s)
- [INV-03] Deterministic restore via `restore_route`
- [INV-04] Strict HTML escape & TG-01 character boundaries
- [TG-06] In-memory registry with zero persistent DB pollution
"""
from __future__ import annotations

import hashlib
import html
import logging
import threading
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LinkTarget:
    """External destination link metadata with confirmation prompt."""
    token: str
    target_name: str
    dest_url: str
    description: str = ""
    restore_route: str = "brief:latest"


class LinkGateRegistry:
    """Thread-safe registry for static preset tokens and dynamic short-lived URL tokens."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._static_targets: dict[str, LinkTarget] = {}
        self._dynamic_targets: dict[str, LinkTarget] = {}
        self._seed_presets()

    def _seed_presets(self) -> None:
        presets = [
            LinkTarget(
                token="bf_rep",
                target_name="일일 시황 브리핑 상세페이지",
                dest_url="http://161.33.196.211:8002/reports/daily_latest.html",
                description="실시간 차트와 거시 지표, 종목별 상승/하락률 상세 분석 웹 리포트로 이동합니다.",
                restore_route="brief:latest",
            ),
            LinkTarget(
                token="trv_rep",
                target_name="6개월 최저가 여행 리포트",
                dest_url="http://161.33.196.211:8002/reports/travel_latest.html",
                description="월별 최저가 항공권 히트맵 및 AI 가성비 추천지 전체 리포트로 이동합니다.",
                restore_route="travel:home",
            ),
            LinkTarget(
                token="trv_nv",
                target_name="네이버 항공권 실시간 조회",
                dest_url="https://m-flight.naver.com/",
                description="선택하신 노선의 네이버 항공권 실시간 예약 페이지로 이동합니다.",
                restore_route="travel:home",
            ),
            LinkTarget(
                token="trv_gpt",
                target_name="ChatGPT 3박4일 추천 일정",
                dest_url="https://chatgpt.com/",
                description="AI가 맞춤 생성한 여행 동선 및 일정표로 이동합니다.",
                restore_route="travel:home",
            ),
            LinkTarget(
                token="adm_rep",
                target_name="주간 입시 나침반 리포트",
                dest_url="http://161.33.196.211:8002/reports/admission_latest.html",
                description="전국 주요 대학 입시 일정표와 대입 전략 큐레이션 리포트로 이동합니다.",
                restore_route="admission:home",
            ),
            LinkTarget(
                token="adm_bk",
                target_name="입시 설명회 공식 예약 신청",
                dest_url="http://161.33.196.211:8002/reports/admission_latest.html",
                description="선착순 티켓팅 및 설명회 공식 접수 사이트로 이동합니다.",
                restore_route="admission:home",
            ),
            LinkTarget(
                token="yolo_mag",
                target_name="욜로 라이프 1인 매거진",
                dest_url="http://161.33.196.211:8002/reports/yolo_magazine.html",
                description="풀코스 타임라인과 예산서, 포토 갤러리 웹 매거진으로 이동합니다.",
                restore_route="yolo:home",
            ),
            LinkTarget(
                token="yolo_act",
                target_name="액티비티/전시 티켓 예매",
                dest_url="http://161.33.196.211:8002/reports/yolo_magazine.html",
                description="공식 예매 및 세부 정보 페이지로 이동합니다.",
                restore_route="yolo:home",
            ),
            LinkTarget(
                token="sil_rep",
                target_name="부모님 복지 상세 리포트",
                dest_url="http://161.33.196.211:8002/reports/silver_latest.html",
                description="부모님 맞춤 복지 혜택과 지자체 지원금 상세 안내 웹 리포트로 이동합니다.",
                restore_route="silver:home",
            ),
            LinkTarget(
                token="bot_menu",
                target_name="🧭 통합 메뉴봇 대화방",
                dest_url="https://t.me/KwMenu_bot?start=from_sub",
                description="전체 정보봇 구독 관리 및 통합 대시보드 봇으로 이동합니다.",
                restore_route="portal:menu",
            ),
            # Bot Deep-links (Telegram app-to-app routing)
            LinkTarget(
                token="bot_brief",
                target_name="📈 시황 브리핑 봇 대화방",
                dest_url="https://t.me/KwDailyMarketInfo_bot",
                description="06:30 모닝시황, 15:40 마감시황 및 긴급 변동성 브리핑 봇으로 이동합니다.",
                restore_route="portal:menu",
            ),
            LinkTarget(
                token="bot_travel",
                target_name="✈️ 여행 특가 봇 대화방",
                dest_url="https://t.me/KwTravelLowPrice_bot",
                description="실시간 특가 항공권 및 AI 시즌별 추천지 가이드 봇으로 이동합니다.",
                restore_route="portal:menu",
            ),
            LinkTarget(
                token="bot_admission",
                target_name="🎓 입시 나침반 봇 대화방",
                dest_url="https://t.me/KwEduInfo_bot",
                description="학년별 주간 입시 큐레이션 및 설명회 티켓팅 알림 봇으로 이동합니다.",
                restore_route="portal:menu",
            ),
            LinkTarget(
                token="bot_silver",
                target_name="👵 실버케어 봇 대화방",
                dest_url="https://t.me/KwSilver_bot",
                description="부모님 맞춤 동사무소·보건소 복지 및 효도 브리핑 봇으로 이동합니다.",
                restore_route="portal:menu",
            ),
            LinkTarget(
                token="bot_kids",
                target_name="👶 자녀 나침반 봇 대화방",
                dest_url="https://t.me/KwKids_bot",
                description="주말 맞춤 나들이 추천 및 해열제·달빛병원 응급 가이드 봇으로 이동합니다.",
                restore_route="portal:menu",
            ),
            LinkTarget(
                token="bot_yolo",
                target_name="🏄 욜로 라이프 봇 대화방",
                dest_url="https://t.me/KwYolo_bot",
                description="1인 라이프·취미·혼여 큐레이션 및 실내 플랜 B 봇으로 이동합니다.",
                restore_route="portal:menu",
            ),
        ]
        for p in presets:
            self._static_targets[p.token] = p

    def get_target(self, token: str) -> LinkTarget | None:
        tok = str(token).strip()
        with self._lock:
            if tok in self._static_targets:
                return self._static_targets[tok]
            return self._dynamic_targets.get(tok)

    def register_dynamic(
        self,
        dest_url: str,
        target_name: str,
        description: str = "",
        restore_route: str = "brief:latest",
    ) -> str:
        """Registers a dynamic URL target and returns a short <=12 char token."""
        url_clean = str(dest_url).strip()
        raw_hash = hashlib.sha256(f"{url_clean}:{target_name}".encode("utf-8")).hexdigest()[:10]
        token = f"d_{raw_hash}"
        target = LinkTarget(
            token=token,
            target_name=target_name,
            dest_url=url_clean,
            description=description,
            restore_route=restore_route,
        )
        with self._lock:
            self._dynamic_targets[token] = target
            # Bound dynamic cache size to avoid unbounded memory growth
            if len(self._dynamic_targets) > 500:
                oldest_keys = list(self._dynamic_targets.keys())[:100]
                for k in oldest_keys:
                    self._dynamic_targets.pop(k, None)
        return token

    def update_preset_url(self, token: str, new_url: str) -> None:
        with self._lock:
            existing = self._static_targets.get(token)
            if existing:
                self._static_targets[token] = LinkTarget(
                    token=existing.token,
                    target_name=existing.target_name,
                    dest_url=str(new_url).strip(),
                    description=existing.description,
                    restore_route=existing.restore_route,
                )


# Global singleton instance
GATE_REGISTRY = LinkGateRegistry()


def build_gate_button(token: str, button_text: str) -> dict[str, str]:
    """Generates an inline keyboard button bound to Link Confirmation Gate callback.
    Enforces [TG-04] <= 64 byte limit.
    """
    cb = f"gate:g:{token}"
    cb_bytes = len(cb.encode("utf-8"))
    if cb_bytes > 64:
        raise ValueError(f"callback_data exceeds 64 bytes: {cb} ({cb_bytes} bytes)")
    return {"text": button_text, "callback_data": cb}


def render_confirmation_card(target: LinkTarget) -> tuple[str, list[list[dict[str, str]]]]:
    """Renders the standard 2-step link confirmation card with OK and Cancel buttons."""
    name_esc = html.escape(target.target_name)
    desc_esc = html.escape(target.description) if target.description else "상세 정보 페이지로 연결됩니다."
    lines = [
        "🌐 <b>[페이지 이동 안내]</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"<b>'{name_esc}'</b>(으)로 이동하시겠습니까?",
        "",
        f"💡 <i>{desc_esc}</i>",
        "외부 브라우저(또는 지정된 대화방)로 연결됩니다.",
        "",
        "계속 진행하시려면 아래 <b>[확인 (OK)]</b> 버튼을 눌러주세요.",
    ]
    card_html = "\n".join(lines)
    keyboard = [
        [{"text": "🌐 이동하기 (OK)", "url": target.dest_url}],
        [{"text": "❌ 취소 (Cancel)", "callback_data": f"gate:c:{target.token}"}],
    ]
    return card_html, keyboard


class LinkGateRouter:
    """Dispatches `gate:g:*` and `gate:c:*` callbacks, editing message in-place."""

    def __init__(self, registry: LinkGateRegistry | None = None) -> None:
        self.registry = registry or GATE_REGISTRY

    def handle_callback(
        self,
        cb_data: str,
        bot_id: str,
        chat_id: str,
        message_id: int,
    ) -> dict[str, Any]:
        """Processes link gate callbacks. Returns result dict for transport rendering."""
        cb = str(cb_data).strip()
        parts = cb.split(":")
        if len(parts) < 3 or parts[0] != "gate":
            return {"handled": False, "reason": "not_gate_callback"}

        action = parts[1]
        token = parts[2]

        if action == "g":
            target = self.registry.get_target(token)
            if not target:
                return {
                    "handled": True,
                    "action": "EXPIRED",
                    "toast": "⚠️ 링크 안내가 만료되었습니다.",
                    "html": "⚠️ <b>[안내 만료]</b>\n\n선택하신 링크 안내 정보가 만료되었습니다.\n최신 메뉴 또는 브리핑을 다시 확인해주세요.",
                    "inline_keyboard": [[{"text": "🔄 최신 브리핑 확인", "callback_data": f"info:preview:{bot_id}"}]],
                }

            card_html, kb = render_confirmation_card(target)
            return {
                "handled": True,
                "action": "CONFIRM_CARD",
                "toast": "🌐 페이지 이동 안내입니다.",
                "html": card_html,
                "inline_keyboard": kb,
                "target": target,
            }

        elif action == "c":
            target = self.registry.get_target(token)
            restore_route = target.restore_route if target else "brief:latest"
            return {
                "handled": True,
                "action": "CANCEL",
                "toast": "이동을 취소했습니다.",
                "restore_route": restore_route,
                "token": token,
            }

        return {"handled": False, "reason": "unknown_gate_action"}
