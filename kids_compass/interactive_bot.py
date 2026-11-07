"""Interactive 2-Tap bot for Kids Compass (키즈 나침반).

[TG-04] callback_data = 'k:<domain>[:<action>[:<args>]]' (ASCII, <= 64 bytes). answerCallbackQuery is
always called first — before parsing, DB reads or rendering. Screens update in place via _safe_edit_or_send.
Deleting a child or pregnancy profile is an in-place 2-step confirm.
Weekend picks: 'k:exp:a:<age>:<weather>[:<walk|all>]' — walk = 현장직행 (booking_required == False only).
"""
from __future__ import annotations
import logging
import re
from datetime import date, datetime, timedelta
from typing import Any, Optional

from kids_compass.channels.telegram import (
    esc,
    format_category_html,
    format_cross_dosing_html,
    format_dday_alert_html,
    format_fever_emergency_html,
    format_fever_menu_html,
    format_fever_result_html,
    format_home_html,
    format_hospital_list_html,
    format_pregnancy_html,
    format_weekend_briefing_html,
)
from kids_compass.collector import PREGNANCY_BENEFITS, recommend_by_category, recommend_weekend_picks
from kids_compass.fever_engine import DEFAULT_MEDICINES, calculate_all
from kids_compass.models import AgeGroup, ExperienceCategory
from kids_compass.storage.db import KidsCompassDatabase

logger = logging.getLogger(__name__)

CB_PREFIX = "k"
MAX_CALLBACK_BYTES = 64
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

_CHILD_CMD_RE = re.compile(r"^/child(?:@\w+)?\s+([0-9A-Za-z가-힣]{1,10})\s+(\d{4}-\d{2}-\d{2})\s+(\d{1,3}(?:\.\d)?)\s*$")
_PREG_CMD_RE = re.compile(r"^/preg(?:@\w+)?\s+([0-9A-Za-z가-힣]{1,10})\s+(\d{4}-\d{2}-\d{2})\s*$")


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


def _kb(rows: list[list[dict[str, str]]]) -> dict[str, Any]:
    return {"inline_keyboard": rows}


def _chunks(items: list, size: int) -> list[list]:
    return [items[i:i + size] for i in range(0, len(items), size)]


HOME_ROW = [_btn("🏠 홈", cb("home"))]


# ── Keyboards ─────────────────────────────────────────────────
def build_home_keyboard() -> dict[str, Any]:
    return _kb([
        [_btn("🌡 해열제 계산", cb("fvr")), _btn("🌙 달빛병원", cb("hsp"))],
        [_btn("🎈 주말 나들이", cb("exp")), _btn("⏰ 예약 D-Day", cb("dday"))],
        [_btn("🤰 임신기 태교·바우처", cb("preg"))],
        [_btn("🔔 알림·아이 설정", cb("set"))],
    ])


def build_fever_keyboard(children: list) -> dict[str, Any]:
    rows = [[_btn(f"👧 {c.nickname} ({c.weight_kg:g}kg)", cb("fvr", "c", c.child_id))] for c in children[:4]]
    rows.append([_btn("🚨 생후 3개월 미만", cb("fvr", "m3"))])
    rows += _chunks([_btn(f"4~5개월 {kg}kg", cb("fvr", "m5", kg)) for kg in QUICK_WEIGHTS_4_5M], 2)
    rows += _chunks([_btn(f"{kg}kg", cb("fvr", "w", kg)) for kg in QUICK_WEIGHTS_6M], 5)
    rows.append([_btn("🔥 열이 안 떨어져요", cb("fvr", "x"))])
    rows.append([_btn("🌙 달빛병원", cb("hsp")), *HOME_ROW])
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


def build_settings_keyboard(profile: dict, children: list, pregnancy=None) -> dict[str, Any]:
    wk = "ON" if profile.get("alert_weekend_brief") else "OFF"
    dd = "ON" if profile.get("alert_dday_reserve") else "OFF"
    rows = [
        [_btn(f"🎈 목 17시 주말브리핑 {wk}", cb("set", "w"))],
        [_btn(f"⏰ 월 9시 예약 D-Day {dd}", cb("set", "d"))],
        [_btn(("✅ " if profile.get("default_region") == name else "") + name, cb("set", "r", code))
         for code, name in REGION_CODES.items()],
    ]
    rows += [[_btn(f"🗑 {c.nickname} 삭제", cb("ch", "x", c.child_id))] for c in children[:4]]
    if pregnancy is not None:
        rows.append([_btn(f"🗑 {pregnancy.nickname} 임신정보 삭제", cb("preg", "x"))])
    rows.append(HOME_ROW)
    return _kb(rows)


def build_delete_confirm_keyboard(child_id: int) -> dict[str, Any]:
    return _kb([[_btn("🗑 네, 삭제", cb("ch", "xy", child_id)), _btn("취소", cb("set"))]])


def build_pregnancy_keyboard(has_profile: bool) -> dict[str, Any]:
    rows = [[_btn("🌿 태교 산책 ☀️", cb("exp", "a", AgeGroup.PREGNANCY.value, "sunny", "all")),
             _btn("🌿 실내 산책 ☔", cb("exp", "a", AgeGroup.PREGNANCY.value, "rain", "all"))]]
    if has_profile:
        rows.append([_btn("🗑 임신 정보 삭제", cb("preg", "x"))])
    rows.append(HOME_ROW)
    return _kb(rows)


def build_pregnancy_delete_confirm_keyboard() -> dict[str, Any]:
    return _kb([[_btn("🗑 네, 삭제", cb("preg", "xy")), _btn("취소", cb("preg"))]])


# ── Dispatch message builders (weekend Thu 17:00 / D-Day Mon 09:00) ──
def iso_week_key(prefix: str, day: date) -> str:
    y, w, _ = day.isocalendar()
    return f"{prefix}:{y}-W{w:02d}"


def build_weekend_messages(db: KidsCompassDatabase, today: date) -> tuple[str, list[tuple[str, str]]]:
    """One message per subscriber (idempotency is per chat+week):
    - registered child → primary child's age group; 4~7세 gets the 현장직행 card (booking_required == False only)
    - no child but a pregnancy profile → 임신기 태교·바우처 dashboard
    - neither → all-age picks
    """
    key = iso_week_key("kids_weekend", today)
    messages = []
    for prof in db.list_alert_subscribers("weekend"):
        uid = prof["telegram_user_id"]
        region = prof.get("default_region") or None
        children = db.list_children(uid)
        pregnancy = db.get_pregnancy(uid) if not children else None
        if pregnancy is not None:
            walks = recommend_weekend_picks(db, AgeGroup.PREGNANCY, region)
            messages.append((uid, format_pregnancy_html(pregnancy, PREGNANCY_BENEFITS, walks, today)))
            continue
        age = children[0].age_group(today) if children else AgeGroup.ALL
        walk_in = age == AgeGroup.PRESCHOOL
        picks = recommend_weekend_picks(db, age, region, is_dispatch=walk_in)
        messages.append((uid, format_weekend_briefing_html(picks, age, DISPATCH_WEATHER_SUMMARY, walk_in=walk_in)))
    return key, messages


def build_dday_messages(db: KidsCompassDatabase, now: datetime) -> tuple[str, list[tuple[str, str]]]:
    """Empty list when nothing opens in the window — no 'nothing to report' spam."""
    key = iso_week_key("kids_dday", now.date())
    events = db.list_upcoming_bookings(now.strftime("%Y-%m-%d %H:%M"),
                                       (now + timedelta(days=DDAY_WINDOW_DAYS)).strftime("%Y-%m-%d %H:%M"))
    if not events:
        return key, []
    text = format_dday_alert_html(events, now)
    return key, [(prof["telegram_user_id"], text) for prof in db.list_alert_subscribers("dday")]


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
        text = (msg.get("text") or "").strip()
        if not chat_id or not text:
            return
        cmd = text.split()[0].split("@")[0].lower()
        if cmd == "/child":
            self._add_child(chat_id, text)
            return
        if cmd == "/preg" and len(text.split()) > 1:
            self._set_pregnancy(chat_id, text)
            return
        self.db.ensure_user(chat_id)
        screens = {"/fever": self._show_fever_menu, "/hospital": self._show_hospital_regions,
                   "/weekend": self._show_exp_menu, "/dday": self._show_dday, "/preg": self._show_pregnancy}
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
            if born is None or born > today or born < today.replace(year=today.year - 14):
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
            today = self._clock().date()
            if (due_date is None or due_date < today - timedelta(days=PREG_MAX_DAYS_OVERDUE)
                    or due_date > today + timedelta(days=PREG_MAX_DAYS_AHEAD)):
                error = "출산예정일을 확인해 주세요 (오늘부터 약 10개월 이내)."
            else:
                self.db.set_pregnancy(chat_id, nickname, due)
                self.channel.send_message(chat_id, f"✅ <b>{esc(nickname)}</b> 임신 정보 등록 완료!")
                self._show_pregnancy(chat_id, None)
                return
        self.channel.send_message(chat_id, f"⚠️ {error}")

    # ---------- callbacks ----------
    def _handle_callback(self, cq: dict[str, Any]) -> None:
        # [TG-04] Acknowledge first — before parsing, DB work or rendering — to clear the spinner
        self.channel.answer_callback_query(cq.get("id", ""))

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
            if action == "":
                self._show_fever_menu(chat_id, mid)
            elif action == "m3":
                self._safe_edit_or_send(chat_id, mid, format_fever_emergency_html(), build_fever_result_keyboard())
            elif action == "m5":
                self._show_fever_result(chat_id, mid, float(args[1]), AGE_FOR_4_5M, age_label="4~5개월")
            elif action == "w":
                self._show_fever_result(chat_id, mid, float(args[1]), AGE_FOR_6M_PLUS, age_label="6개월 이상")
            elif action == "c":
                child = self.db.get_child(int(args[1]), chat_id)
                if child is None:
                    self._show_fever_menu(chat_id, mid)
                else:
                    self._show_fever_result(chat_id, mid, child.weight_kg, child.age_months(self._clock().date()), child)
            elif action == "x":
                self._safe_edit_or_send(chat_id, mid, format_cross_dosing_html(), build_fever_result_keyboard())
            else:
                raise ValueError(action)
        elif domain == "hsp":
            if action == "r":
                region = REGION_CODES[args[1]]
                self._safe_edit_or_send(chat_id, mid, format_hospital_list_html(self.db.list_hospitals(region), region),
                                        build_hospital_list_keyboard())
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
        elif domain == "dday":
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
            elif action == "r":
                self.db.set_default_region(chat_id, REGION_CODES[args[1]])
            self._show_settings(chat_id, mid)
        elif domain == "ch":
            child = self.db.get_child(int(args[1]), chat_id)
            if child is None:
                self._show_settings(chat_id, mid)
            elif action == "x":
                text = f"🗑 <b>{esc(child.nickname)}</b> 정보를 삭제할까요?\n되돌릴 수 없어요."
                self._safe_edit_or_send(chat_id, mid, text, build_delete_confirm_keyboard(child.child_id))
            elif action == "xy":
                self.db.delete_child(child.child_id, chat_id)
                self._show_settings(chat_id, mid)
            else:
                raise ValueError(action)
        else:
            self._show_home(chat_id, mid)

    # ---------- screens ----------
    def _show_home(self, chat_id: int, mid: Optional[int]) -> None:
        self._safe_edit_or_send(chat_id, mid, format_home_html(self.db.list_children(chat_id),
                                                               self.db.get_user_profile(chat_id),
                                                               self.db.get_pregnancy(chat_id)),
                                build_home_keyboard())

    def _show_fever_menu(self, chat_id: int, mid: Optional[int]) -> None:
        children = self.db.list_children(chat_id)
        self._safe_edit_or_send(chat_id, mid, format_fever_menu_html(children), build_fever_keyboard(children))

    def _show_fever_result(self, chat_id: int, mid: Optional[int], weight: float, months: int, child=None,
                           age_label: Optional[str] = None) -> None:
        products = self.db.list_medicines() or DEFAULT_MEDICINES
        results = calculate_all(weight, months, products)
        self._safe_edit_or_send(chat_id, mid, format_fever_result_html(results, child, age_label), build_fever_result_keyboard())

    def _show_hospital_regions(self, chat_id: int, mid: Optional[int]) -> None:
        text = ("🌙 <b>달빛어린이병원 찾기</b>\n밤·주말·공휴일 소아 진료 병원\n\n"
                "지역을 골라 주세요 👇\n\n📞 위급하면 <b>119</b>")
        self._safe_edit_or_send(chat_id, mid, text, build_hospital_region_keyboard())

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
        lines = ["🔔 <b>알림·아이 설정</b>", f"📍 기본 지역: <b>{esc(profile.get('default_region', '서울'))}</b>"]
        lines += [f"👧 {esc(c.nickname)} · {esc(c.birth_date)} · {c.weight_kg:g}kg" for c in children]
        if not children:
            lines.append("👧 아이 등록: <code>/child 별명 2023-05-01 11.2</code>")
        if preg is not None:
            lines.append(f"🤰 {esc(preg.nickname)} · 출산예정 {esc(preg.due_date)}")
        self._safe_edit_or_send(chat_id, mid, "\n".join(lines), build_settings_keyboard(profile, children, preg))

    # ---------- transport ----------
    def _safe_edit_or_send(self, chat_id: int, message_id: Optional[int], text: str,
                           reply_markup: Optional[dict[str, Any]] = None) -> None:
        """Edits in place ('message is not modified' = success); falls back to a new message."""
        if message_id and self.channel.edit_message_text(chat_id, message_id, text, reply_markup=reply_markup):
            return
        self.channel.send_message(chat_id, text, reply_markup=reply_markup)
