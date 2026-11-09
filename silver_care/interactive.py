"""Interactive 2-step explorer for SilverCare (실버케어).

Flow: 메인 메뉴 ➔ [1단계] 부모님 호칭·출생연도·시도·시군구·주거·소득
      ➔ [2단계] 자녀 대리신청 1순위 미션 카드 (최대 5개 순환)
         ➔ 대리신청 키트 / 통화 대본 / 안심 카톡 / 진행 상태
      ➔ [📚 전체 혜택 목록] 후보 리스트 ➔ 혜택 상세 ➔ 통화 대본 / 카톡 전송문 / 찜하기

[TG-04] callback_data = 'SC:<action>[:<args>]' (ASCII, <= 64 bytes). answerCallbackQuery is always
called first, before any DB work. Screen updates go through _safe_edit_or_send. Deleting a parent
profile uses an in-place 2-step confirm.
"""
from __future__ import annotations
import logging
import re
from typing import Any, Optional

from silver_care.channels.telegram_card import (
    LIST_PAGE_SIZE,
    esc,
    format_benefit_card,
    format_bookmarks,
    format_candidate_list,
    format_main_menu,
    format_mission_status_picker,
    format_no_mission,
    format_parent_peace_card,
    format_phone_script,
    format_proxy_action_kit,
    format_single_mission_card,
    format_wizard_prompt,
    page_bounds,
)
from silver_care.matching import MAX_MISSIONS, rank_missions
from silver_care.models import SIDO_NAMES, LivingType, MissionStatus, ParentProfile, normalize_sido_code
from silver_care.storage.db import SilverCareDatabase

logger = logging.getLogger(__name__)

CB_PREFIX = "SC"
MAX_CALLBACK_BYTES = 64

ALIAS_CODES: dict[str, str] = {"M": "엄마", "F": "아빠", "MI": "어머님", "FI": "아버님"}
DECADES: tuple[int, ...] = (1935, 1940, 1945, 1950, 1955, 1960, 1965)  # 5-year bands
INCOME_CODES: dict[str, tuple[str, int, int]] = {
    # code: (label, is_pension_receiver, is_low_income)
    "PEN": ("기초연금 받으심", 1, -1),
    "LOW": ("기초수급·차상위", 1, 1),
    "NO": ("둘 다 아님", 0, 0),
    "UNK": ("잘 모름", -1, -1),
}
_TAG_RE = re.compile(r"<[^>]*>")
_SIGUNGU_RE = re.compile(r"[^0-9A-Za-z가-힣 ]")


def cb(*parts: object) -> str:
    data = ":".join([CB_PREFIX, *(str(p) for p in parts)])
    if len(data.encode("utf-8")) > MAX_CALLBACK_BYTES:
        raise ValueError(f"callback_data exceeds {MAX_CALLBACK_BYTES} bytes: {data!r}")
    return data


def _btn(text: str, data: str) -> dict[str, str]:
    return {"text": text, "callback_data": data}


def _kb(rows: list[list[dict[str, str]]]) -> dict[str, Any]:
    return {"inline_keyboard": rows}


def _chunks(items: list, size: int) -> list[list]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def _short(title: str, limit: int = 18) -> str:
    return title if len(title) <= limit else title[: limit - 1] + "…"


# ── Keyboards ─────────────────────────────────────────────────
def build_main_menu_keyboard(parents: list[ParentProfile], weekly_on: bool) -> dict[str, Any]:
    rows = [[_btn(f"👵 {p.alias} 복지 미션", cb("L", p.id, 0))] for p in parents]
    rows.append([_btn("➕ 부모님 등록", cb("new")), _btn("⭐ 찜한 혜택", cb("bm"))])
    rows.append([_btn(f"🔔 일요일 효도 브리핑 {'ON' if weekly_on else 'OFF'}", cb("wk"))])
    return _kb(rows)


def build_wizard_keyboard(step: str, draft: dict[str, Any]) -> Optional[dict[str, Any]]:
    cancel = [_btn("✖ 취소", cb("m"))]
    if step == "alias":
        rows = _chunks([_btn(v, cb("a", k)) for k, v in ALIAS_CODES.items()], 2)
    elif step == "decade":
        rows = _chunks([_btn(f"{d}~{d + 4}년생", cb("dc", d)) for d in DECADES], 2)
    elif step == "year":
        start = int(draft.get("decade", DECADES[0]))
        rows = _chunks([_btn(f"{y}년", cb("y", y)) for y in range(start, start + 5)], 3)
    elif step == "region":
        rows = _chunks([_btn(name, cb("r", code)) for code, name in SIDO_NAMES.items()], 3)
    elif step == "sigungu":
        rows = [[_btn("⏭ 건너뛰기", cb("sk"))]]
    elif step == "living":
        rows = [[_btn(lt.label, cb("l", lt.value))] for lt in LivingType]
    elif step == "income":
        rows = _chunks([_btn(label, cb("i", code)) for code, (label, _, _) in INCOME_CODES.items()], 2)
    else:
        return None
    return _kb(rows + [cancel])


def build_list_keyboard(parent: ParentProfile, matches: list, page: int) -> dict[str, Any]:
    page, start, last_page = page_bounds(len(matches), page)
    rows = [
        [_btn(f"{m.grade.badge} {_short(m.benefit.title)}", cb("d", parent.id, m.benefit.id, page))]
        for m in matches[start:start + LIST_PAGE_SIZE]
    ]
    pager = []
    if page > 0:
        pager.append(_btn("◀ 이전", cb("all", parent.id, page - 1)))
    if page < last_page:
        pager.append(_btn("다음 ▶", cb("all", parent.id, page + 1)))
    if pager:
        rows.append(pager)
    rows.append([_btn("🎯 1순위 미션", cb("L", parent.id, 0))])
    rows.append([_btn("🗑 정보 삭제", cb("x", parent.id)), _btn("🏠 메뉴", cb("m"))])
    return _kb(rows)


def build_detail_keyboard(
    pid: int, bid: int, page: int, bookmarked: bool, report_url: str = ""
) -> dict[str, Any]:
    url = report_url or "http://161.33.196.211:8002/reports/silver_latest.html"
    return _kb([
        [_btn("📞 통화 대본", cb("t", pid, bid, page)), _btn("💌 카톡 전송문", cb("k", pid, bid, page))],
        [{"text": "📊 부모님 복지 상세 리포트 ↗", "url": url}],
        [_btn("⭐ 찜 해제" if bookmarked else "⭐ 찜하기", cb("b", pid, bid, page))],
        [_btn("◀ 목록", cb("all", pid, page)), _btn("🏠 메뉴", cb("m"))],
    ])


def build_mission_keyboard(pid: int, bid: int, index: int, total: int) -> dict[str, Any]:
    rows = [
        [_btn("🎒 대리신청 준비물", cb("prx", pid, bid))],
        [_btn("📞 통화 대본", cb("t", pid, bid)), _btn("💌 안심 카톡", cb("k", pid, bid))],
        [_btn("📌 진행 상태 변경", cb("mst", pid, bid))],
    ]
    if total > 1:
        nxt = (index + 1) % total
        rows.append([_btn(f"▶ 다음 추천 ({nxt + 1}/{total})", cb("nxt", pid, bid, nxt))])
    rows.append([_btn("📚 전체 혜택 목록", cb("all", pid, 0))])
    rows.append([_btn("🗑 정보 삭제", cb("x", pid)), _btn("🏠 메뉴", cb("m"))])
    return _kb(rows)


def build_mission_status_keyboard(pid: int, bid: int, current: str) -> dict[str, Any]:
    rows = [
        [_btn(f"{'✔ ' if s.value == current else ''}{s.badge} {s.label}", cb("mss", pid, bid, s.value))]
        for s in MissionStatus
    ]
    rows.append([_btn("◀ 미션 카드", cb("mc", pid, bid))])
    return _kb(rows)


def build_back_to_mission_keyboard(pid: int, bid: int) -> dict[str, Any]:
    return _kb([[_btn("◀ 미션 카드", cb("mc", pid, bid)), _btn("🏠 메뉴", cb("m"))]])


def build_back_to_detail_keyboard(pid: int, bid: int, page: int) -> dict[str, Any]:
    return _kb([[_btn("◀ 혜택 상세", cb("d", pid, bid, page)), _btn("🏠 메뉴", cb("m"))]])


def build_delete_confirm_keyboard(pid: int) -> dict[str, Any]:
    return _kb([[_btn("🗑 네, 삭제", cb("xy", pid)), _btn("취소", cb("L", pid, 0))]])


# ── Bot ───────────────────────────────────────────────────────
class SilverCareInteractiveBot:
    def __init__(self, channel: Any, db: SilverCareDatabase, ref_year: Optional[int] = None):
        self.channel = channel
        self.db = db
        self.ref_year = ref_year

    # ---------- entry ----------
    def handle_update(self, update: dict[str, Any]) -> None:
        try:
            if "callback_query" in update:
                self._handle_callback(update["callback_query"])
            elif "message" in update:
                self._handle_message(update["message"])
        except Exception:  # noqa: BLE001 - one bad update must not kill the poll loop
            logger.exception("SilverCare update handling failed")

    def poll_once(self, offset: int = 0) -> int:
        for upd in self.channel.get_updates(offset=offset):
            offset = max(offset, int(upd["update_id"]) + 1)
            self.handle_update(upd)
        return offset

    # ---------- messages ----------
    def _handle_message(self, msg: dict[str, Any]) -> None:
        chat_id = int((msg.get("chat") or {}).get("id") or 0)
        text = (msg.get("text") or "").strip()
        if not chat_id or not text:
            return
        cmd = text.split()[0].split("@")[0].lower()
        if cmd in ("/start", "/menu", "/parents"):
            self.db.clear_draft(chat_id)
            self._show_menu(chat_id, None)
            return
        if cmd == "/bookmarks":
            self._show_bookmarks(chat_id, None)
            return
        draft = self.db.get_draft(chat_id)
        if draft.get("step") == "sigungu" and not text.startswith("/"):
            draft["sigungu_name"] = _SIGUNGU_RE.sub("", _TAG_RE.sub("", text))[:20].strip()
            self._advance(chat_id, None, draft, "living")
            return
        self._show_menu(chat_id, None)

    # ---------- callbacks ----------
    @staticmethod
    def _toast(action: str) -> str:
        return {
            "b": "⭐ 찜 목록을 업데이트했어요", "xy": "🗑 삭제했어요", "wk": "🔔 알림 설정을 바꿨어요",
            "mss": "📌 진행 상태를 저장했어요",
        }.get(action, "")

    def _handle_callback(self, cq: dict[str, Any]) -> None:
        data = str(cq.get("data") or "")
        parts = data.split(":")
        action = parts[1] if len(parts) > 1 and parts[0] == CB_PREFIX else ""
        # [TG-04] Acknowledge first — before any DB work — to clear the loading spinner
        self.channel.answer_callback_query(cq.get("id", ""), text=self._toast(action))

        msg = cq.get("message") or {}
        chat_id = int((msg.get("chat") or {}).get("id") or (cq.get("from") or {}).get("id") or 0)
        message_id = msg.get("message_id")
        if not chat_id or not action:
            return
        args = parts[2:]
        try:
            self._route(chat_id, message_id, action, args)
        except (ValueError, IndexError):
            logger.info("Malformed SilverCare callback %r", data)
            self._show_menu(chat_id, message_id)

    def _route(self, chat_id: int, mid: Optional[int], action: str, args: list[str]) -> None:
        if action == "m":
            self.db.clear_draft(chat_id)
            self._show_menu(chat_id, mid)
        elif action == "new":
            self._advance(chat_id, mid, {}, "alias")
        elif action in ("a", "dc", "y", "r", "sk", "l", "i"):
            self._wizard_step(chat_id, mid, action, args)
        elif action == "L":
            self._show_mission(chat_id, mid, int(args[0]), 0)
        elif action == "all":
            self._show_list(chat_id, mid, int(args[0]), int(args[1]) if len(args) > 1 else 0)
        elif action == "nxt":
            self._show_mission(chat_id, mid, int(args[0]), int(args[2]))
        elif action == "mc":
            self._show_mission(chat_id, mid, int(args[0]), bid=int(args[1]))
        elif action == "mst":
            self._show_mission_status(chat_id, mid, int(args[0]), int(args[1]))
        elif action == "mss":
            pid, bid, status = int(args[0]), int(args[1]), MissionStatus(args[2]).value
            parent = self.db.get_parent(pid, chat_id)
            if parent and self.db.evaluate(parent, bid, self.ref_year):
                self.db.set_mission_status(chat_id, pid, bid, status)
            self._show_mission(chat_id, mid, pid, bid=bid)
        elif action == "prx" or (action in ("t", "k") and len(args) == 2):
            # 2-arg t/k come from the mission card → back button returns to the mission card
            self._show_mission_tool(chat_id, mid, action, int(args[0]), int(args[1]))
        elif action in ("d", "t", "k", "b"):
            pid, bid = int(args[0]), int(args[1])
            page = int(args[2]) if len(args) > 2 else 0
            if action == "b":
                if self.db.get_parent(pid, chat_id):
                    self.db.toggle_bookmark(chat_id, pid, bid)
                action = "d"
            self._show_benefit(chat_id, mid, action, pid, bid, page)
        elif action == "bm":
            self._show_bookmarks(chat_id, mid)
        elif action == "x":
            self._confirm_delete(chat_id, mid, int(args[0]))
        elif action == "xy":
            self.db.delete_parent(int(args[0]), chat_id)
            self._show_menu(chat_id, mid)
        elif action == "wk":
            self.db.set_weekly_opt_in(chat_id, not self.db.is_weekly_opt_in(chat_id))
            self._show_menu(chat_id, mid)
        else:
            self._show_menu(chat_id, mid)

    # ---------- wizard ----------
    def _advance(self, chat_id: int, mid: Optional[int], draft: dict[str, Any], step: str) -> None:
        draft["step"] = step
        self.db.set_draft(chat_id, draft)
        self._safe_edit_or_send(chat_id, mid, format_wizard_prompt(step, draft), build_wizard_keyboard(step, draft))

    def _wizard_step(self, chat_id: int, mid: Optional[int], action: str, args: list[str]) -> None:
        draft = self.db.get_draft(chat_id)
        if not draft.get("step"):  # stale button after menu/cancel
            self._show_menu(chat_id, mid)
            return
        if action == "a":
            draft["alias"] = ALIAS_CODES[args[0]]
            self._advance(chat_id, mid, draft, "decade")
        elif action == "dc":
            decade = int(args[0])
            if decade not in DECADES:
                raise ValueError(decade)
            draft["decade"] = decade
            self._advance(chat_id, mid, draft, "year")
        elif action == "y":
            year = int(args[0])
            if not (DECADES[0] <= year < DECADES[-1] + 5):
                raise ValueError(year)
            draft["birth_year"] = year
            self._advance(chat_id, mid, draft, "region")
        elif action == "r":
            code = normalize_sido_code(args[0])  # 구 강원 42 / 전북 45 → 51 / 52
            if code not in SIDO_NAMES:
                raise ValueError(code)
            draft["sido_code"] = code
            self._advance(chat_id, mid, draft, "sigungu")
        elif action == "sk":
            draft["sigungu_name"] = ""
            self._advance(chat_id, mid, draft, "living")
        elif action == "l":
            draft["living_type"] = LivingType(args[0]).value
            self._advance(chat_id, mid, draft, "income")
        elif action == "i":
            _, pension, low = INCOME_CODES[args[0]]
            required = ("alias", "birth_year", "sido_code", "living_type")
            if any(k not in draft for k in required):
                self._show_menu(chat_id, mid)
                return
            parent = ParentProfile(
                id=0, chat_id=chat_id, alias=draft["alias"], birth_year=int(draft["birth_year"]),
                sido_code=draft["sido_code"], sigungu_name=draft.get("sigungu_name", ""),
                living_type=draft["living_type"], is_pension_receiver=pension, is_low_income=low,
            )
            pid = self.db.save_parent(parent)
            self.db.clear_draft(chat_id)
            self._show_mission(chat_id, mid, pid, 0)

    # ---------- screens ----------
    def _show_menu(self, chat_id: int, mid: Optional[int]) -> None:
        parents = self.db.list_parents(chat_id)
        self._safe_edit_or_send(
            chat_id, mid, format_main_menu(parents, self.ref_year),
            build_main_menu_keyboard(parents, self.db.is_weekly_opt_in(chat_id)),
        )

    def _show_list(self, chat_id: int, mid: Optional[int], pid: int, page: int) -> None:
        parent = self.db.get_parent(pid, chat_id)
        if parent is None:
            self._show_menu(chat_id, mid)
            return
        matches = self.db.match_for_parent(parent, self.ref_year)
        page, _, _ = page_bounds(len(matches), page)  # clamp stale pager buttons
        self._safe_edit_or_send(
            chat_id, mid, format_candidate_list(parent, matches, page, self.ref_year),
            build_list_keyboard(parent, matches, page),
        )

    def _show_mission(
        self, chat_id: int, mid: Optional[int], pid: int, index: int = 0, bid: Optional[int] = None
    ) -> None:
        """Renders one proxy-application mission card. bid (when ranked) wins over index."""
        parent = self.db.get_parent(pid, chat_id)
        if parent is None:
            self._show_menu(chat_id, mid)
            return
        matches = self.db.match_for_parent(parent, self.ref_year)
        missions = rank_missions(matches, parent, MAX_MISSIONS)
        if not missions:
            kb = _kb([[_btn("📚 전체 혜택 목록", cb("all", pid, 0))],
                      [_btn("🗑 정보 삭제", cb("x", pid)), _btn("🏠 메뉴", cb("m"))]])
            self._safe_edit_or_send(chat_id, mid, format_no_mission(parent, self.ref_year), kb)
            return
        ids = [m.benefit.id for m in missions]
        index = ids.index(bid) if bid in ids else min(max(0, index), len(missions) - 1)
        match = missions[index]
        mission = self.db.get_mission(chat_id, pid, match.benefit.id)
        text = format_single_mission_card(
            parent, match, mission, index + 1, len(missions), len(matches), self.ref_year
        )
        self._safe_edit_or_send(chat_id, mid, text, build_mission_keyboard(pid, match.benefit.id, index, len(missions)))

    def _show_mission_status(self, chat_id: int, mid: Optional[int], pid: int, bid: int) -> None:
        parent = self.db.get_parent(pid, chat_id)
        match = self.db.evaluate(parent, bid, self.ref_year) if parent else None
        if parent is None or match is None:
            self._show_menu(chat_id, mid)
            return
        mission = self.db.get_mission(chat_id, pid, bid)
        current = mission.status if mission else MissionStatus.NOT_STARTED.value
        self._safe_edit_or_send(
            chat_id, mid, format_mission_status_picker(match.benefit, mission),
            build_mission_status_keyboard(pid, bid, current),
        )

    def _show_mission_tool(self, chat_id: int, mid: Optional[int], view: str, pid: int, bid: int) -> None:
        """대리신청 키트(prx) / 통화 대본(t) / 안심 카톡(k) opened from the mission card."""
        parent = self.db.get_parent(pid, chat_id)
        match = self.db.evaluate(parent, bid, self.ref_year) if parent else None
        if parent is None or match is None:
            self._show_menu(chat_id, mid)
            return
        if view == "prx":
            text = format_proxy_action_kit(parent, match.benefit)
        elif view == "t":
            text = format_phone_script(match.benefit, parent)
        else:
            text = format_parent_peace_card(parent, match.benefit)
        self._safe_edit_or_send(chat_id, mid, text, build_back_to_mission_keyboard(pid, bid))

    def _show_benefit(self, chat_id: int, mid: Optional[int], view: str, pid: int, bid: int, page: int) -> None:
        parent = self.db.get_parent(pid, chat_id)
        match = self.db.evaluate(parent, bid, self.ref_year) if parent else None
        if parent is None or match is None:
            self._show_menu(chat_id, mid)
            return
        if view == "t":
            text, kb = format_phone_script(match.benefit, parent), build_back_to_detail_keyboard(pid, bid, page)
        elif view == "k":
            text, kb = format_parent_peace_card(parent, match.benefit), build_back_to_detail_keyboard(pid, bid, page)
        else:
            marked = self.db.is_bookmarked(chat_id, pid, bid)
            text = format_benefit_card(parent, match, marked, self.ref_year)
            kb = build_detail_keyboard(pid, bid, page, marked)
        self._safe_edit_or_send(chat_id, mid, text, kb)

    def _show_bookmarks(self, chat_id: int, mid: Optional[int]) -> None:
        items, rows = [], []
        for pid, bid in self.db.list_bookmarks(chat_id):
            parent, benefit = self.db.get_parent(pid, chat_id), self.db.get_benefit(bid)
            if parent and benefit:
                items.append((parent, benefit))
                rows.append([_btn(f"⭐ {_short(benefit.title)} · {parent.alias}", cb("d", pid, bid, 0))])
        rows.append([_btn("🏠 메뉴", cb("m"))])
        self._safe_edit_or_send(chat_id, mid, format_bookmarks(items), _kb(rows))

    def _confirm_delete(self, chat_id: int, mid: Optional[int], pid: int) -> None:
        parent = self.db.get_parent(pid, chat_id)
        if parent is None:
            self._show_menu(chat_id, mid)
            return
        text = f"🗑 <b>{esc(parent.alias)}</b> 정보와 찜·진행 상태를 삭제할까요?\n되돌릴 수 없어요."
        self._safe_edit_or_send(chat_id, mid, text, build_delete_confirm_keyboard(pid))

    # ---------- transport ----------
    def _safe_edit_or_send(
        self, chat_id: int, message_id: Optional[int], text: str, reply_markup: Optional[dict[str, Any]] = None
    ) -> None:
        """Edits in place ('message is not modified' = success); falls back to a new message."""
        if message_id and self.channel.edit_message_text(chat_id, message_id, text, reply_markup=reply_markup):
            return
        self.channel.send_message(chat_id, text, reply_markup=reply_markup)
