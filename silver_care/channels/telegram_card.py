"""Telegram HTML cards & KakaoTalk share text for SilverCare.
[TG-01] fixed slim templates (visible <= 1,800 / HTML <= 3,500), [TG-02] every dynamic string escaped,
[TG-03] one line = one fact (emoji + noun + bold number), no raw URLs.
"""
from __future__ import annotations
import html
import re
from datetime import date
from typing import Optional, Sequence

from silver_care.models import (
    BenefitCategory,
    BenefitMatch,
    LivingType,
    MissionStatus,
    ParentProfile,
    ReliabilityGrade,
    SilverBenefit,
    SilverMission,
    TargetIncome,
    age_band_label,
    age_band_of,
    sido_name,
)

TG_MAX_HTML_LEN = 3500
TG_MAX_VISIBLE_LEN = 1800
LIST_PAGE_SIZE = 6
WEEKLY_MAX_PARENTS = 3
WEEKLY_ITEMS_PER_PARENT = 3

DIVIDER = "━━━━━━━━━━━━"
_TAG_RE = re.compile(r"<[^>]+>")

# 자녀 대리신청 4대 지참물 (주민센터·보건소 공통)
PROXY_DOCS: tuple[str, ...] = (
    "부모님 신분증",
    "자녀(신청인) 신분증",
    "가족관계증명서",
    "위임장 (부모님 서명)",
)

_LIVING_PHRASE = {
    LivingType.ALONE.value: "혼자 사세요",
    LivingType.COUPLE.value: "두 분이 함께 사세요",
    LivingType.FAM.value: "가족과 함께 사세요",
}
_CHECK_ICON = {True: "✔️", None: "❔", False: "✖️"}


def esc(value: object) -> str:
    return html.escape(str(value), quote=False)


def measure_message(html_text: str) -> tuple[int, int]:
    """Returns (raw HTML length, visible text length after stripping tags and unescaping entities)."""
    return len(html_text), len(html.unescape(_TAG_RE.sub("", html_text)))


def _ref_year(ref_year: Optional[int]) -> int:
    return ref_year or date.today().year


def age_range_label(parent: ParentProfile, ref_year: Optional[int] = None) -> str:
    """'만 71~72세' — birth year only, so the age depends on whether the birthday has passed."""
    reached = parent.age_in(_ref_year(ref_year))
    return f"만 {reached - 1}~{reached}세"


def parent_age_label(parent: ParentProfile, ref_year: Optional[int] = None) -> str:
    """'만 65세 이상' / '만 60~64세' — 호칭 없는 연령대 기준선."""
    return age_band_label(age_band_of(parent.age_in(_ref_year(ref_year))))


def parent_name(parent: ParentProfile, ref_year: Optional[int] = None) -> str:
    """Display name (plain): 등록 호칭, 없으면 연령대 기준선."""
    return parent.alias or parent_age_label(parent, ref_year)


def parent_line(parent: ParentProfile, ref_year: Optional[int] = None) -> str:
    if parent.is_age_only:
        return f"👵 <b>{parent_age_label(parent, ref_year)}</b> 부모님 · 📍 전국 공통"
    living = LivingType(parent.living_type).label
    return (
        f"👵 <b>{esc(parent.alias)}</b> · {parent.birth_year}년생 ({age_range_label(parent, ref_year)})\n"
        f"📍 {esc(parent.region_label)} · 🏠 {living}"
    )


def grade_tag(grade: ReliabilityGrade) -> str:
    return f"{grade.badge} <b>[{grade.code}] {grade.value}</b>"


# ── Main menu ─────────────────────────────────────────────────
MAIN_TITLE = "👵 <b>실버케어 · 부모님 복지 대리신청 비서</b>"


def format_main_menu(parents: Sequence[ParentProfile] = (), ref_year: Optional[int] = None) -> str:
    """호칭·지역 설문 없이 연령대 버튼 2개로 바로 진입. parents는 하위호환용 (미표시)."""
    return "\n".join([
        MAIN_TITLE,
        "동사무소 갈 필요 없이, 자녀가 대신 신청하는 2026 핵심 복지입니다.",
        DIVIDER,
        "부모님 연령대를 선택하시면 즉시 대리신청 서류와 방법을 안내해 드립니다.",
    ])


# ── Wizard prompts ────────────────────────────────────────────
WIZARD_PROMPTS: dict[str, str] = {
    "alias": "1/6 · 누구의 혜택을 찾을까요?",
    "decade": "2/6 · 출생 연도대를 골라주세요",
    "year": "3/6 · 출생 연도를 골라주세요",
    "region": "4/6 · 사시는 시·도를 골라주세요",
    "sigungu": "5/6 · 시·군·구 이름을 입력해주세요\n(예: <code>원주시</code>) 또는 [건너뛰기]",
    "living": "6/6 · 누구와 함께 사시나요?",
    "income": "마지막 · 기초연금·수급 여부 (모르면 '모름')",
}


def format_wizard_prompt(step: str, draft: dict) -> str:
    done = []
    if draft.get("alias"):
        done.append(esc(draft["alias"]))
    if draft.get("birth_year"):
        done.append(f"{int(draft['birth_year'])}년생")
    if draft.get("sido_code"):
        region = sido_name(draft["sido_code"])
        if draft.get("sigungu_name"):
            region += f" {draft['sigungu_name']}"
        done.append(esc(region))
    lines = ["👵 <b>부모님 정보 등록</b>"]
    if done:
        lines.append("✅ " + " · ".join(done))
    lines.extend([DIVIDER, WIZARD_PROMPTS[step]])
    return "\n".join(lines)


# ── Candidate list (1단계 결과) ───────────────────────────────
def page_bounds(total: int, page: int, page_size: int = LIST_PAGE_SIZE) -> tuple[int, int, int]:
    """Returns (clamped page, start, last_page)."""
    last_page = max(0, (total - 1) // page_size)
    page = min(max(0, page), last_page)
    return page, page * page_size, last_page


def format_candidate_list(
    parent: ParentProfile, matches: Sequence[BenefitMatch], page: int = 0, ref_year: Optional[int] = None
) -> str:
    total = len(matches)
    page, start, last_page = page_bounds(total, page)
    counts = {g: sum(1 for m in matches if m.grade == g) for g in ReliabilityGrade}
    lines = [
        parent_line(parent, ref_year),
        DIVIDER,
        f"🎁 받을 수 있는 혜택 <b>{total}개</b>",
        " · ".join(f"{g.badge} {g.value} <b>{counts[g]}</b>" for g in ReliabilityGrade),
        "",
    ]
    if not matches:
        lines.append("조건에 맞는 혜택이 아직 없어요. 정보를 다시 확인해주세요.")
    for idx, m in enumerate(matches[start:start + LIST_PAGE_SIZE], start + 1):
        b = m.benefit
        warn = " 🚨" if b.is_pre_approval_required else ""
        lines.append(f"{idx}. {m.grade.badge} <b>{esc(b.title)}</b>{warn}")
        lines.append(f"   └ {esc(b.support_amount)}")
    lines.extend(["", f"📄 {page + 1}/{last_page + 1} 페이지 · 버튼을 눌러 자격 확인"])
    return "\n".join(lines)


# ── Benefit detail card (2단계) ────────────────────────────────
def format_benefit_card(
    parent: ParentProfile, match: BenefitMatch, bookmarked: bool = False, ref_year: Optional[int] = None
) -> str:
    b = match.benefit
    star = " ⭐" if bookmarked else ""
    lines = [
        f"{grade_tag(match.grade)} · {b.category_label}{star}",
        f"📌 <b>{esc(b.title)}</b>",
        f"💰 <b>{esc(b.support_amount)}</b>",
        f"🏛 {esc(b.agency_name)}",
        f"🗓 {esc(b.application_period)}",
        DIVIDER,
        f"✅ <b>{esc(parent_name(parent, ref_year))} 자격 확인</b>",
    ]
    for label, ok in match.checks:
        lines.append(f"{_CHECK_ICON[ok]} {esc(label)}")
    if match.cautions:
        lines.extend(["", "⚠️ <b>필수 주의사항</b>"])
        lines.extend(esc(c) for c in match.cautions)
    lines.extend(["", "📝 <b>안내</b>", esc(b.detail_guide), "", "🎒 <b>자녀 대리신청 지참물</b>"])
    docs = list(PROXY_DOCS)
    if b.requires_prescription:
        docs.append("의사 진단서·처방전")
    lines.extend(f"{i}. {esc(d)}" for i, d in enumerate(docs, 1))
    return "\n".join(lines)


# ── 자녀 대리신청 1순위 미션 카드 ─────────────────────────────
PROXY_KIT_DOCS: tuple[str, ...] = (
    "부모님 신분증 원본",
    "자녀(신청인) 신분증",
    "가족관계증명서",
    "위임장 (부모님 서명)",
)
PRE_APPROVAL_WARNING = "🚨 <b>수술·구입 전 사전승인 필수</b> (사후 신청 불가)"


def _mission_status(mission: Optional[SilverMission]) -> MissionStatus:
    return MissionStatus(mission.status) if mission else MissionStatus.NOT_STARTED


def format_single_mission_card(
    parent: ParentProfile,
    match: BenefitMatch,
    mission: Optional[SilverMission] = None,
    mission_index: int = 1,
    total_missions: int = 3,
    total_matches: int = 28,
    ref_year: Optional[int] = None,
) -> str:
    """연령대 기준 핵심 혜택 1개만 집중 노출 (호칭 미표시, 전체 목록은 별도 버튼)."""
    b = match.benefit
    status = _mission_status(mission)
    lines = [
        MAIN_TITLE,
        DIVIDER,
        f"🎯 <b>{parent_age_label(parent, ref_year)} 자녀가 대신 신청할 1순위 복지</b> ({mission_index}/{total_missions})",
        f"[{match.grade.badge}] <b>{esc(b.title)}</b>",
        f"💰 <b>{esc(b.support_amount)}</b>",
        f"🏛 {esc(b.agency_name)}",
        f"📌 현재 상태: {status.badge} <b>{status.label}</b>",
    ]
    if b.is_pre_approval_required:
        lines.append(PRE_APPROVAL_WARNING)
    lines.extend([
        "",
        "💡 <i>\"부모님이 가실 필요 없이, 직장인 자녀가 서류를 챙겨 주민센터/공단에서 대신 신청할 수 있어요.\"</i>",
        "",
        f"📚 전체 혜택 <b>{total_matches}개</b>",
    ])
    return "\n".join(lines)


def format_no_mission(parent: ParentProfile, ref_year: Optional[int] = None) -> str:
    return "\n".join([
        MAIN_TITLE,
        DIVIDER,
        f"🎯 <b>{parent_age_label(parent, ref_year)}</b> 조건에 맞는 혜택이 아직 없어요. 연령대를 다시 선택해주세요.",
    ])


def format_mission_status_picker(benefit: SilverBenefit, mission: Optional[SilverMission] = None) -> str:
    status = _mission_status(mission)
    return "\n".join([
        f"📌 <b>{esc(benefit.title)}</b>",
        f"현재 상태: {status.badge} <b>{status.label}</b>",
        DIVIDER,
        "진행 상태를 골라주세요",
    ])


def format_proxy_action_kit(parent: ParentProfile, benefit: SilverBenefit) -> str:
    """자녀 대리신청 준비물·행정 주의·접수처."""
    lines = [
        "🎒 <b>자녀 대리신청 키트</b>",
        f"📌 <b>{esc(benefit.title)}</b>" + (f" · {esc(parent.alias)}" if parent.alias else ""),
        DIVIDER,
        "✅ <b>4대 준비물</b>",
    ]
    docs = list(PROXY_KIT_DOCS)
    if benefit.requires_prescription:
        docs.append("의사 진단서·처방전")
    lines.extend(f"{i}. {esc(d)}" for i, d in enumerate(docs, 1))
    if benefit.target_income != TargetIncome.ALL.value:
        lines.extend([
            "",
            "⚠️ <b>행정 주의</b>: <b>금융정보등 제공동의서</b>는 반드시 부모님 자필 서명·인감 필수 (자녀 대리 서명 불가)",
        ])
    if benefit.is_pre_approval_required:
        lines.extend(["", PRE_APPROVAL_WARNING])
    lines.append("")
    if "주민센터" in benefit.agency_name:
        lines.append("📍 <b>어디로</b>: 부모님 주소지 읍면동 주민센터 복지팀")
    else:
        lines.append(f"📍 <b>어디로</b>: {esc(benefit.agency_name)} (부모님 주소지 관할)")
    return "\n".join(lines)


def format_parent_peace_text(benefit: SilverBenefit) -> str:
    """부모님 안심 2줄 카톡 (plain text). 호칭 없이 '이번에 …'로 시작 — 혜택명 외 PII 미포함."""
    if benefit.category == BenefitCategory.FINANCE.value:
        action = f"이번에 {benefit.title} 알아보고 매달 통장으로 혜택 나오게 제가 신청해둘게요."
    elif benefit.is_pre_approval_required:
        action = f"이번에 {benefit.title} 알아봤는데 병원 가시기 전에 제가 미리 신청해둘게요."
    else:
        action = f"이번에 {benefit.title} 알아봤는데 제가 신청해둘게요."
    return "\n".join([
        action,
        "서류는 제가 다 알아서 챙길 테니 따로 신경 쓰지 마시고 편히 계세요 💕",
    ])


def format_parent_peace_card(benefit: SilverBenefit) -> str:
    return "\n".join([
        "💌 <b>부모님 안심 카톡</b> (길게 눌러 복사)",
        DIVIDER,
        f"<pre>{esc(format_parent_peace_text(benefit))}</pre>",
    ])


# ── Phone script & KakaoTalk text ─────────────────────────────
def format_phone_script(benefit: SilverBenefit, parent: Optional[ParentProfile] = None) -> str:
    """동사무소 복지팀 직통 통화 대본 (HTML)."""
    if parent is not None and parent.is_age_only:
        intro = f"저는 {parent_age_label(parent)} 어르신 자녀입니다."
    elif parent is not None:
        who = f"{parent.region_label} 사시는 {parent.birth_year}년생 어르신"
        intro = f"저는 {who}의 자녀입니다. 부모님은 {_LIVING_PHRASE[parent.living_type]}."
    else:
        intro = "저는 어르신 자녀입니다."
    lines = [
        "📞 <b>통화 대본</b>",
        f"🏛 {esc(benefit.agency_name)} · 복지 담당",
        DIVIDER,
        f"🙋 \"안녕하세요, {esc(intro)}\"",
        f"🙋 \"<b>{esc(benefit.title)}</b> 관련해서 여쭤보려고요. {esc(benefit.phone_script)}\"",
        "",
        "✅ <b>꼭 물어볼 3가지</b>",
        "1. 우리 부모님이 대상이 되나요?",
        "2. 필요한 서류는 무엇인가요?",
        "3. 자녀 대리신청·온라인 신청이 되나요?",
    ]
    if benefit.is_pre_approval_required:
        lines.append("4. 🚨 수술·구입 전 승인까지 며칠 걸리나요?")
    lines.extend(["", "📝 담당자 이름·직통번호를 메모해두세요"])
    return "\n".join(lines)


def format_kakao_share_text(benefit: SilverBenefit, parent_alias: str) -> str:
    """부모님께 카톡으로 보낼 다정한 요약문 (plain text, 복사용)."""
    alias = (parent_alias or "엄마").strip()
    body = benefit.parent_msg_template.replace("{alias}", alias)
    lines = [
        body,
        "",
        f"✔ {benefit.title}",
        f"✔ {benefit.support_amount}",
        f"✔ 문의: {benefit.agency_name}",
    ]
    if benefit.is_pre_approval_required:
        lines.append("✔ 꼭 먼저 신청하고 나서 진행하셔야 해요!")
    lines.append("신청은 제가 도와드릴게요 💕")
    return "\n".join(lines)


def format_kakao_share_html(benefit: SilverBenefit, parent_alias: str) -> str:
    return "\n".join([
        "💌 <b>카톡 전송문</b> (길게 눌러 복사)",
        DIVIDER,
        f"<pre>{esc(format_kakao_share_text(benefit, parent_alias))}</pre>",
    ])


# ── Bookmarks ─────────────────────────────────────────────────
def format_bookmarks(items: Sequence[tuple[ParentProfile, SilverBenefit]]) -> str:
    lines = ["⭐ <b>찜한 혜택</b>", DIVIDER]
    if not items:
        lines.append("아직 찜한 혜택이 없어요. 혜택 상세에서 ⭐를 눌러주세요.")
    for p, b in items:
        lines.append(f"▪ <b>{esc(b.title)}</b> · {esc(parent_name(p))}")
    return "\n".join(lines)


# ── Weekly briefing (일요일 20:00) ─────────────────────────────
def format_weekly_briefing(
    sections: Sequence[tuple[ParentProfile, Sequence[BenefitMatch]]],
    bookmarked_titles: Sequence[str] = (),
    week_label: str = "",
    ref_year: Optional[int] = None,
) -> str:
    lines = [f"💌 <b>이번 주 효도 브리핑</b>{(' · ' + esc(week_label)) if week_label else ''}", DIVIDER]
    for parent, matches in list(sections)[:WEEKLY_MAX_PARENTS]:
        lines.append(f"👵 <b>{esc(parent_name(parent, ref_year))}</b> · {esc(parent.region_label)}")
        if not matches:
            lines.append("   └ 새로 확인할 혜택이 없어요")
        for m in list(matches)[:WEEKLY_ITEMS_PER_PARENT]:
            warn = " 🚨" if m.benefit.is_pre_approval_required else ""
            lines.append(f"{m.grade.badge} <b>{esc(m.benefit.title)}</b>{warn}")
            lines.append(f"   └ {esc(m.benefit.support_amount)}")
        lines.append("")
    if bookmarked_titles:
        lines.append("⭐ <b>찜해둔 혜택 신청하셨나요?</b>")
        lines.extend(f"▪ {esc(t)}" for t in list(bookmarked_titles)[:3])
        lines.append("")
    lines.append("📞 이번 주 부모님께 안부 전화 한 통 어떠세요?")
    return "\n".join(lines)
