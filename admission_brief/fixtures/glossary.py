"""1초 입시 은어·용어 팩트사전 — 15대 고빈도 입시 용어 시드 데이터.

code는 callback_data(adm:gl:<CODE>, adm:sh:gl:<CODE>)에 그대로 쓰이므로 ASCII 대문자만 사용한다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class GlossaryEntry:
    code: str          # 영문 축약 식별자 (콜백 64-byte 규약)
    term: str          # 정식 한글 명칭
    short_name: str    # 인라인 버튼 표출용 축약명 (최대 8자)
    definition: str    # 1~2줄 핵심 개념
    impact: str        # 학부모 실전 득실
    caution: str       # 주의사항 & 꿀팁


GLOSSARY_ENTRIES: tuple[GlossaryEntry, ...] = (
    GlossaryEntry(
        code="NAPCHI",
        term="수시납치",
        short_name="수시납치",
        definition="수능을 기대보다 잘 봤는데, 이미 붙은 하향 수시 합격 때문에 정시 지원 자체가 막히는 현상입니다.",
        impact="수시 합격자는 등록 여부와 관계없이 정시 지원이 금지되어, 더 좋은 대학 기회를 통째로 잃을 수 있습니다.",
        caution="수능 이후 면접·논술 전형은 가채점 결과를 보고 응시 여부를 결정하세요. 6장 모두 정시 예상선 이상으로 쓰는 것이 원칙입니다.",
    ),
    GlossaryEntry(
        code="BYEONPYO",
        term="변환표준점수(변표)",
        short_name="변환표준점수",
        definition="탐구 영역 선택과목 간 난이도 차이를 보정하려고 대학이 백분위를 자체 표준점수로 바꾼 점수입니다.",
        impact="같은 백분위라도 대학마다 변표가 달라, 과탐·사탐 선택에 따라 합격선이 수 점씩 뒤바뀝니다.",
        caution="변표는 보통 수능 성적 발표 후 12월에 공개됩니다. 가채점 단계 배치표의 탐구 점수는 추정치임을 기억하세요.",
    ),
    GlossaryEntry(
        code="PYOJEOM",
        term="표준점수(표점)",
        short_name="표준점수",
        definition="원점수를 시험 난이도와 응시자 평균에 맞춰 환산한 점수로, 시험이 어려울수록 만점 표점이 올라갑니다.",
        impact="국어·수학이 어렵게 출제된 해에는 표점 차이가 커져, 정시에서 국·수 비중 높은 대학이 유리해집니다.",
        caution="원점수가 같아도 선택과목에 따라 표점이 다를 수 있습니다. 성적표의 표점·백분위를 함께 확인하세요.",
    ),
    GlossaryEntry(
        code="BAEK",
        term="백분위",
        short_name="백분위",
        definition="내 점수보다 낮은 응시자가 전체의 몇 %인지 나타낸 상대 위치(0~100)입니다.",
        impact="탐구 영역이나 백분위 반영 대학에서는 표점 대신 백분위로 줄을 세워 유불리가 달라집니다.",
        caution="백분위 99와 100은 표점으로 여러 점 차이가 날 수 있습니다. 지원 대학의 반영 지표를 먼저 확인하세요.",
    ),
    GlossaryEntry(
        code="CHOECHO",
        term="수능최저학력기준(수능최저)",
        short_name="수능최저",
        definition="수시 합격을 위해 수능에서 반드시 넘어야 하는 최소 등급 조건입니다(예: 3개 영역 등급 합 6 이내).",
        impact="최저를 못 맞추면 서류·면접이 1등이어도 불합격이라, 실질 경쟁률이 크게 낮아지는 기회이기도 합니다.",
        caution="영어·한국사 포함 여부, 탐구 1과목/2과목 평균 등 대학별 세부 기준이 다르니 모집요강을 꼭 대조하세요.",
    ),
    GlossaryEntry(
        code="CHUNGWON",
        term="충원율·추가합격",
        short_name="충원율",
        definition="최초 합격자가 등록하지 않아 빈자리를 예비번호 순으로 채우는 비율입니다(충원율 100% = 모집인원만큼 추합).",
        impact="충원율이 높은 학과는 예비번호가 길어도 합격 가능성이 있어, 겉보기 경쟁률보다 실제 문턱이 낮습니다.",
        caution="추합 전화는 짧은 시간 안에 응답해야 합니다. 발표 기간에는 연락처를 꼭 열어 두세요.",
    ),
    GlossaryEntry(
        code="SETEUK",
        term="세부능력 및 특기사항(세특)",
        short_name="세특",
        definition="교과 선생님이 수업 중 학생의 탐구 과정·역량을 학생부에 서술한 기록입니다.",
        impact="학생부종합전형의 핵심 평가자료로, 같은 등급이라도 세특의 깊이에 따라 합격이 갈립니다.",
        caution="학원 보고서를 그대로 옮긴 듯한 활동은 오히려 감점 요인입니다. 수업 속 질문과 후속 탐구가 핵심입니다.",
    ),
    GlossaryEntry(
        code="RECOMMEND",
        term="전공연계 권장이수과목",
        short_name="권장이수과목",
        definition="대학이 학과별로 고교에서 이수하길 권장·필수로 지정한 선택과목입니다(예: 공대 → 미적분·물리학).",
        impact="고교학점제 이후 미이수 시 서류 평가에서 불리하거나 지원 자체가 제한될 수 있습니다.",
        caution="고1 2학기 과목 선택 전에 희망 대학 권장과목 안내서를 확인하세요. 학교에 개설되지 않으면 공동교육과정을 알아보세요.",
    ),
    GlossaryEntry(
        code="GRADE5",
        term="내신 5등급제",
        short_name="내신5등급",
        definition="2025 고1부터 내신이 9등급에서 5등급 상대평가로 바뀌어, 1등급 비율이 상위 4%에서 10%로 늘어났습니다.",
        impact="1등급 변별력이 떨어져 상위권 대학은 세특·과목 선택·수능 성적 비중을 더 높일 가능성이 큽니다.",
        caution="등급만 보고 안심하지 마세요. 원점수·성취도·과목별 수강자 수도 함께 기록되어 평가에 쓰입니다.",
    ),
    GlossaryEntry(
        code="GUN",
        term="정시 가·나·다군",
        short_name="정시 가나다군",
        definition="정시는 대학을 가·나·다 3개 모집군으로 나누며, 군마다 1곳씩 최대 3번 지원할 수 있습니다.",
        impact="같은 군끼리는 하나만 지원 가능해, 군별 상향·적정·안정 조합이 정시 성패를 좌우합니다.",
        caution="다군은 모집 대학이 적어 경쟁률·추합 변동이 큽니다. 다군을 막연한 안전망으로 여기지 마세요.",
    ),
    GlossaryEntry(
        code="CONVERT",
        term="대학별 환산점수",
        short_name="환산점수",
        definition="대학이 정한 영역별 반영비율·가산점을 적용해 수능 성적을 자체 점수로 다시 계산한 값입니다.",
        impact="총점이 같아도 내 강점 과목 비중이 큰 대학에서는 환산점수가 높아져 유리한 대학이 달라집니다.",
        caution="가산점·영어 등급 감점표는 대학마다 다릅니다. 반드시 대학 공식 환산 계산기로 다시 확인하세요.",
    ),
    GlossaryEntry(
        code="BOOM",
        term="펑크와 폭발",
        short_name="펑크·폭발",
        definition="펑크는 지원자가 몰리지 않아 합격선이 크게 내려간 경우, 폭발은 지원자가 몰려 합격선이 치솟은 경우입니다.",
        impact="전년도 펑크 학과는 올해 폭발하기 쉬워, 작년 입결만 보고 지원하면 낭패를 볼 수 있습니다.",
        caution="최소 3개년 입결과 올해 실시간 경쟁률 추이를 함께 보세요. 마감 직전 눈치 지원 쏠림에도 주의하세요.",
    ),
    GlossaryEntry(
        code="HAKJONG",
        term="학생부종합전형(학종)",
        short_name="학생부종합",
        definition="내신 성적뿐 아니라 세특·창체·진로 활동 등 학생부 전체를 정성 평가하는 수시 전형입니다.",
        impact="내신이 조금 부족해도 전공 적합성과 탐구 깊이로 상위권 대학에 합격할 수 있는 통로입니다.",
        caution="면접이 있는 경우 학생부 내용을 본인이 설명할 수 있어야 합니다. 기록과 실제 역량이 일치해야 합니다.",
    ),
    GlossaryEntry(
        code="GYOGWA",
        term="학생부교과전형(교과)",
        short_name="학생부교과",
        definition="주로 내신 등급(교과 성적)을 정량 반영해 선발하는 수시 전형으로, 학교장 추천이 필요한 경우가 많습니다.",
        impact="내신이 탄탄한 일반고 학생에게 가장 예측 가능한 전형이며, 수능최저 충족 여부가 실질 관문입니다.",
        caution="학교장 추천 인원 제한이 있으니 교내 추천 일정과 기준을 1학기 중에 미리 확인하세요.",
    ),
    GlossaryEntry(
        code="NONSUL",
        term="논술전형",
        short_name="논술전형",
        definition="대학별 논술고사 성적을 중심으로 선발하는 수시 전형으로, 내신 반영 비중이 낮은 편입니다.",
        impact="내신이 불리한 학생에게 상위권 대학 역전 기회지만, 경쟁률이 수십 대 1로 매우 높습니다.",
        caution="수능 이후 시행 대학이 많아 수시납치 위험을 함께 따져야 합니다. 대학별 기출·모범답안으로 준비하세요.",
    ),
)

_BY_CODE: dict[str, GlossaryEntry] = {e.code: e for e in GLOSSARY_ENTRIES}


def get_glossary_entry(code: str) -> Optional[GlossaryEntry]:
    """Returns the entry for an ASCII code (case-insensitive), or None if unknown."""
    return _BY_CODE.get((code or "").strip().upper())


def list_glossary_entries() -> list[GlossaryEntry]:
    """Returns all glossary entries in display order."""
    return list(GLOSSARY_ENTRIES)
