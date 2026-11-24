"""Pull-only home play and development guidance catalogue for Kids Compass.

This module is deliberately independent from the database and push builders.  A
stage is derived at request time, so changing the primary child immediately
changes the home-play card shown by the interactive bot.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json


class PlayStage(str, Enum):
    M0_11 = "M0_11"
    M12_23 = "M12_23"
    M24_47 = "M24_47"
    Y4_7 = "Y4_7"
    Y8_13 = "Y8_13"
    PREG = "PREG"


class DevStage(str, Enum):
    D0_3 = "D0_3"
    D4_6 = "D4_6"
    D7_12 = "D7_12"
    D13_24 = "D13_24"
    D25_36 = "D25_36"
    DY4_7 = "DY4_7"
    DY8_13 = "DY8_13"
    DPREG = "DPREG"


@dataclass(frozen=True)
class HomePlayItem:
    play_id: str
    stage: str
    title: str
    duration_min: int
    materials: str
    how_to: str
    dev_benefit: str
    safety_note: str
    steps: tuple[str, ...] = ()


@dataclass(frozen=True)
class DevGuideItem:
    guide_id: str
    stage: str
    title: str
    checkpoints: list[str]
    parent_tips: list[str]
    consult_signs: list[str]


DEV_DISCLAIMER = (
    "일반적인 발달 참고 안내이며 아이마다 속도가 다릅니다. 걱정되는 증상은 "
    "소아청소년과 전문의와 상담하세요."
)

PLAY_STAGE_LABELS: dict[str, str] = {
    PlayStage.M0_11.value: "0~11개월",
    PlayStage.M12_23.value: "12~23개월",
    PlayStage.M24_47.value: "24~47개월",
    PlayStage.Y4_7.value: "4~7세",
    PlayStage.Y8_13.value: "8~13세",
    PlayStage.PREG.value: "임신기",
}

DEV_STAGE_LABELS: dict[str, str] = {
    DevStage.D0_3.value: "0~3개월",
    DevStage.D4_6.value: "4~6개월",
    DevStage.D7_12.value: "7~12개월",
    DevStage.D13_24.value: "13~24개월",
    DevStage.D25_36.value: "25~36개월",
    DevStage.DY4_7.value: "4~7세",
    DevStage.DY8_13.value: "8~13세",
    DevStage.DPREG.value: "임신기",
}


def _play(play_id: str, stage: PlayStage, title: str, duration: int, materials: str,
          how_to: str, benefit: str, safety: str, steps: tuple[str, ...] | None = None) -> HomePlayItem:
    detailed_steps = steps or (
        f"{materials}을 준비하고 놀이 공간의 위험 요소를 치워요.",
        how_to,
        "아이의 반응에 맞춰 속도와 난이도를 조절해요.",
        "놀이가 끝나면 작은 물건과 도구를 바로 정리해요.",
    )
    return HomePlayItem(play_id, stage.value, title, duration, materials, how_to, benefit, safety, detailed_steps)


# Five short, no-cost/low-cost activities per play bucket.  Infant activities
# keep the safety note explicit even when the activity itself uses no object.
HOME_PLAY_ITEMS: tuple[HomePlayItem, ...] = (
    _play("m0_face", PlayStage.M0_11, "거울 속 표정 찾기", 5, "안전한 거울",
          "아이와 얼굴을 비추고 천천히 표정을 바꿔요.", "시각 추적·사회적 상호작용", "거울 모서리와 깨짐을 확인하고 작은 거울은 입에 넣지 않게 지켜보세요."),
    _play("m0_sound", PlayStage.M0_11, "종이 바스락 소리", 5, "큰 종이 한 장",
          "아이 옆에서 종이를 천천히 접었다 펴며 소리를 들려줘요.", "청각 주의·원인과 결과", "종이를 찢어 삼키지 않도록 손이 닿는 동안 함께하고, 비닐·작은 조각은 쓰지 마세요."),
    _play("m0_tummy", PlayStage.M0_11, "수건 터미타임", 3, "얇은 수건",
          "깨어 있을 때만 가슴 아래에 수건을 받치고 짧게 엎드려 놀아요.", "목·등 근육과 자세 조절", "잠들 때는 반드시 바로 눕히고, 질식 위험이 있는 두꺼운 쿠션·끈은 치워 주세요."),
    _play("m12_sort", PlayStage.M12_23, "양말 바구니 넣기", 10, "큰 양말 3~4개·바구니",
          "양말을 하나씩 넣고 꺼내며 ‘쏙’ 소리를 말해요.", "손 조절·분류·언어", "작은 단추나 장식이 붙은 양말은 삼킴·질식 위험이 있어 제외하고 옆에서 지켜보세요."),
    _play("m12_tape", PlayStage.M12_23, "테이프 길 만들기", 10, "종이테이프",
          "바닥에 짧은 길을 붙이고 따라 걷거나 자동차를 굴려요.", "대근육·공간 이해", "테이프를 입에 넣지 않도록 하고 미끄럽지 않은 바닥에서 놀아요."),
    _play("m12_cup", PlayStage.M12_23, "컵 옮기기", 10, "큰 플라스틱 컵 2개",
          "빈 컵을 옮기고 쌓으며 ‘하나 더’ 말을 붙여요.", "양손 협응·초기 수 개념", "컵은 깨지지 않는 큰 제품만 쓰고, 작은 컵 조각·뚜껑은 삼킴 위험이 있어요."),
    _play("m24_pillow", PlayStage.M24_47, "베개 섬 징검다리", 15, "낮은 쿠션·베개",
          "바닥에 간격을 두고 놓아 섬처럼 건너가요.", "균형·계획·자신감", "낮고 단단한 쿠션만 사용하고 미끄럼·가구 모서리를 먼저 정리하세요."),
    _play("m24_story", PlayStage.M24_47, "세 장면 이야기", 10, "종이 3장·크레용",
          "시작·중간·끝을 그린 뒤 아이가 이어서 이야기를 만들어요.", "표현력·순서 이해·상상력", "크레용은 큰 유아용 제품을 쓰고 입에 넣거나 작은 조각을 삼키지 않게 지켜보세요."),
    _play("m24_wash", PlayStage.M24_47, "장난감 세차장", 15, "대야·수건·큰 장난감",
          "수건으로 장난감을 닦고 ‘깨끗해졌다’ 역할놀이를 해요.", "자조 습관·감각 놀이", "물그릇은 얕게 쓰고 작은 부품이 빠지는 장난감은 질식 위험 때문에 제외하세요."),
    _play("y4_shadow", PlayStage.Y4_7, "손 그림자 극장", 15, "손전등·벽",
          "벽에 손 그림자를 만들고 짧은 이야기를 함께 지어요.", "상상력·언어·관찰", "손전등을 눈에 비추지 말고 전선에 걸려 넘어지지 않게 정리하세요."),
    _play("y4_measure", PlayStage.Y4_7, "집 안 길이 탐정", 15, "실·종이·연필",
          "실로 물건 길이를 재고 더 긴 것과 짧은 것을 찾아요.", "비교·측정·문제 해결", "실을 목이나 손가락에 감지 않도록 활동 뒤 바로 치워 주세요."),
    _play("y4_recipe", PlayStage.Y4_7, "무불 요리 가게", 20, "종이·색연필·빈 용기",
          "메뉴판을 만들고 주문·계산 역할을 번갈아 해요.", "역할놀이·수 개념·대화", "실제 음식이나 날카로운 도구 대신 빈 용기를 쓰고, 작은 소품은 삼키지 않게 하세요."),
    _play("y8_bridge", PlayStage.Y8_13, "종이 다리 설계", 20, "종이·책 2권·동전",
          "종이를 접거나 말아 다리를 만들고 동전을 몇 개 버티는지 시험해요.", "설계·가설·회고", "동전은 입에 넣지 않도록 하고 실험 뒤 작은 부품을 모두 회수하세요."),
    _play("y8_budget", PlayStage.Y8_13, "일주일 용돈 예산", 20, "종이·연필",
          "가상의 금액을 정해 필요·원하는 것·저축으로 나눠요.", "경제 감각·우선순위·의사결정", "실제 금융상품 가입이나 개인정보 공유 없이 가상 금액으로 진행하세요."),
    _play("y8_news", PlayStage.Y8_13, "가족 미니 뉴스", 20, "종이·연필 또는 녹음기",
          "오늘의 집 안 소식을 3문장으로 정리해 발표해요.", "요약·말하기·경청", "가족의 사생활이나 얼굴을 온라인에 올리지 않는 규칙을 먼저 정하세요."),
    _play("preg_breath", PlayStage.PREG, "5분 호흡 쉼표", 5, "편한 의자",
          "어깨를 편히 두고 무리 없는 속도로 숨을 세 번 깊게 쉬어요.", "긴장 완화·몸의 신호 알아차리기", "어지럽거나 불편하면 즉시 멈추고, 임신 중 운동·호흡은 담당 의료진 안내를 우선하세요."),
    _play("preg_song", PlayStage.PREG, "태담 플레이리스트", 10, "메모장·편한 음악",
          "편안한 음악 한 곡을 고르고 아기에게 오늘을 짧게 이야기해요.", "정서적 연결·휴식 루틴", "소리는 편안한 크기로 유지하고 통증·출혈 등 이상 증상은 놀이보다 진료가 우선이에요."),
    _play("preg_sort", PlayStage.PREG, "출산 준비 우선순위", 15, "종이·펜",
          "‘이번 주·나중·도움 요청’ 세 칸으로 할 일을 나눠요.", "계획·부담 조절·가족 협력", "무거운 물건을 들지 말고, 일정·복약은 의료진과 확인한 내용을 기준으로 적으세요."),
    _play("m0_touch", PlayStage.M0_11, "부드러운 촉감 바구니", 5, "큰 천 조각 2~3개",
          "서로 다른 천을 만져 보고 촉감을 말해 줘요.", "촉각 탐색·공동 주의", "작은 조각·끈·단추가 없는 큰 천만 사용하고 계속 지켜보세요."),
    _play("m0_voice", PlayStage.M0_11, "목소리 숨바꼭질", 5, "보호자의 목소리",
          "아이의 시야 가까이에서 이름을 부르고 천천히 얼굴을 보여 줘요.", "청각 추적·애착 상호작용", "큰 소리로 놀라게 하지 말고 아이가 피곤해하면 바로 쉬어요."),
    _play("m12_box", PlayStage.M12_23, "상자 속 보물 찾기", 10, "큰 상자·큰 공 2개",
          "공을 상자에 넣고 꺼내며 위치와 동작을 말해 줘요.", "대상 영속성·손 협응·언어", "손가락이 끼지 않는 큰 상자와 삼키지 않을 크기의 물건만 사용하세요."),
    _play("m12_bubble", PlayStage.M12_23, "비눗방울 따라가기", 10, "유아용 비눗방울",
          "비눗방울을 천천히 불고 눈으로 따라가거나 터뜨려요.", "시각 추적·대근육·차례 지키기", "용액을 마시지 않도록 하고 미끄러운 바닥은 즉시 닦아요."),
    _play("m24_color", PlayStage.M24_47, "색깔 보물 분류", 10, "큰 색 카드·바구니 2개",
          "색을 골라 같은 색 바구니에 넣고 이름을 말해요.", "분류·어휘·집중", "작은 카드나 조각 대신 손바닥보다 큰 자료를 사용하세요."),
    _play("m24_ice", PlayStage.M24_47, "얼음 녹이기 관찰", 15, "큰 얼음·대야·수건",
          "얼음이 녹는 과정을 만져 보고 따뜻한 물과 비교해요.", "관찰·감각·원인과 결과", "물은 얕게 사용하고 미끄럼을 닦으며 얼음을 입에 넣지 않게 하세요."),
    _play("y4_map", PlayStage.Y4_7, "보물지도 만들기", 20, "종이·색연필·스티커",
          "방 안의 장소를 간단한 기호로 그리고 보물 위치를 표시해요.", "공간 이해·계획·표현", "실제 주소나 개인정보를 지도에 쓰지 않고 가위는 보호자가 관리하세요."),
    _play("y4_rhyme", PlayStage.Y4_7, "끝말 소리 놀이", 10, "그림 카드 또는 주변 물건",
          "단어를 고르고 비슷한 끝소리 단어를 번갈아 찾아요.", "음운 인식·어휘·차례", "틀린 답을 놀리지 말고 아이가 즐거워하는 수준에서 멈춰요."),
    _play("y8_cipher", PlayStage.Y8_13, "가족 암호 편지", 20, "종이·연필",
          "간단한 기호표를 만들고 짧은 비밀 문장을 서로 풀어요.", "논리·규칙 만들기·읽기", "온라인 계정 비밀번호나 개인정보를 암호로 쓰지 않도록 안내하세요."),
    _play("y8_bridge2", PlayStage.Y8_13, "실패에서 다시 설계", 25, "종이컵·종이·테이프",
          "작은 구조물을 만들고 무너진 이유를 적은 뒤 한 가지를 바꿔 다시 시험해요.", "회고·문제 해결·끈기", "무너지기 쉬운 구조물은 얼굴 가까이 두지 말고 바닥에서 시험하세요."),
    _play("preg_body", PlayStage.PREG, "몸의 신호 기록", 10, "메모장·펜",
          "오늘의 피로·기분·불편함을 짧게 기록하고 도움 요청을 정해요.", "자기 돌봄·의사소통·계획", "새 증상을 진단하려 하지 말고 이상 신호는 의료기관에 문의하세요."),
    _play("preg_nest", PlayStage.PREG, "안전한 아기 공간 상상", 15, "종이·펜",
          "아기와 보호자가 편히 지낼 공간의 필요한 물건을 우선순위로 적어요.", "준비·가족 협력·부담 조절", "무거운 가구를 직접 옮기지 말고 설치는 주변 도움을 받으세요."),
)


DEV_GUIDE_ITEMS: tuple[DevGuideItem, ...] = (
    DevGuideItem("d0_social", DevStage.D0_3.value, "눈맞춤과 반응", ["큰 소리·얼굴·빛을 잠깐 따라볼 수 있어요.", "울음 외에도 표정과 몸짓으로 반응해요."], ["수유·기저귀·잠 루틴을 우선하고 짧게 말을 걸어 주세요.", "반응이 늦어도 조급하게 비교하지 마세요."], ["소리나 밝은 얼굴에 전혀 반응하지 않거나 축 처짐이 계속돼요."]),
    DevGuideItem("d4_roll", DevStage.D4_6.value, "뒤집기와 손 탐색", ["손을 뻗어 물건을 잡고 입으로 탐색해요.", "엎드렸을 때 머리와 가슴을 들어요."], ["바닥에서 짧게 자주 놀고 주변 작은 물건을 치워 주세요.", "뒤집기는 속도보다 안전한 움직임을 봐 주세요."], ["한쪽만 계속 쓰거나 머리를 가누기 어렵고 반응이 줄어요."]),
    DevGuideItem("d7_move", DevStage.D7_12.value, "이동과 낯선 사람", ["앉기·기기 등 이동 방식이 다양해져요.", "이름이나 익숙한 목소리에 반응해요."], ["안전한 바닥에서 탐색을 허용하고 금지보다 대안을 제시하세요.", "낯가림은 안심시키며 천천히 적응시켜요."], ["도움 없이 앉지 못하거나 소리·이름에 거의 반응하지 않아요."]),
    DevGuideItem("d13_words", DevStage.D13_24.value, "첫 말과 자기주장", ["간단한 말·손짓으로 원하는 것을 표현해요.", "걷기와 오르내리기를 시도해요."], ["아이의 말을 한두 단어 늘려 되말해 주세요.", "선택지 두 개와 짧은 규칙으로 훈육하세요."], ["의미 있는 말이나 손짓이 거의 없고 이전 기능을 잃어요."]),
    DevGuideItem("d25_play", DevStage.D25_36.value, "모방과 함께 놀이", ["두세 단어 조합과 역할놀이가 늘어요.", "간단한 지시를 듣고 따라 해요."], ["결과보다 시도와 감정을 말로 인정해 주세요.", "위험 행동에는 짧고 일관된 경계를 사용하세요."], ["두 단어 조합이 없거나 사람·놀이에 거의 관심이 없어요."]),
    DevGuideItem("dy4_self", DevStage.DY4_7.value, "자기조절 연습", ["규칙이 있는 놀이와 또래 관계를 배워요.", "감정을 말로 설명하는 시간이 늘어요."], ["감정 이름을 붙이고 선택 가능한 해결책을 함께 찾으세요.", "수면·식사·활동의 예측 가능한 순서를 유지하세요."], ["의사소통·놀이·일상 적응의 어려움이 지속되어 생활을 크게 방해해요."]),
    DevGuideItem("dy8_plan", DevStage.DY8_13.value, "계획과 독립", ["해야 할 일을 나누고 결과를 돌아볼 수 있어요.", "친구 관계와 소속감의 영향이 커져요."], ["숙제·집안일을 작은 단계로 나누고 선택권을 주세요.", "성적보다 과정과 수면·식사를 함께 살펴요."], ["기분 저하·불안·수면 변화가 오래가거나 학교생활이 무너져요."]),
    DevGuideItem("dpreg_check", DevStage.DPREG.value, "임신기 몸과 마음 살피기", ["주차에 따라 몸의 변화와 피로가 달라져요.", "검진 일정과 복용 약을 확인해요."], ["휴식·수분·균형 잡힌 식사를 우선하고 주변 도움을 구하세요.", "온라인 정보보다 담당 산부인과 안내를 기준으로 삼으세요."], ["심한 복통·출혈·호흡곤란·심한 두통 등 급격한 증상이 있어요."]),
)


def play_stage_for_months(months: int) -> str:
    """Return the play bucket for completed months; negative means pregnancy."""
    months = int(months)
    if months < 0:
        return PlayStage.PREG.value
    if months < 12:
        return PlayStage.M0_11.value
    if months < 24:
        return PlayStage.M12_23.value
    if months < 48:
        return PlayStage.M24_47.value
    if months < 96:
        return PlayStage.Y4_7.value
    return PlayStage.Y8_13.value


def dev_stage_for_months(months: int) -> str:
    """Return the development bucket for completed months; negative means pregnancy."""
    months = int(months)
    if months < 0:
        return DevStage.DPREG.value
    if months < 4:
        return DevStage.D0_3.value
    if months < 7:
        return DevStage.D4_6.value
    if months < 13:
        return DevStage.D7_12.value
    if months < 25:
        return DevStage.D13_24.value
    if months < 37:
        return DevStage.D25_36.value
    if months < 96:
        return DevStage.DY4_7.value
    return DevStage.DY8_13.value


def _detail(row: dict) -> dict:
    detail = row.get("detail", row.get("detail_json", {}))
    if isinstance(detail, str):
        try:
            detail = json.loads(detail)
        except json.JSONDecodeError:
            detail = {}
    return detail if isinstance(detail, dict) else {}


def _play_from_row(row: dict) -> HomePlayItem:
    detail = _detail(row)
    steps = tuple(str(value) for value in detail.get("steps", ()) if str(value).strip())
    return HomePlayItem(
        str(row["content_id"]), str(row["stage_code"]), str(row["title"]),
        int(detail.get("duration_min", 5)), str(detail.get("materials", "")),
        str(row.get("summary", "")), str(detail.get("dev_benefit", "")),
        str(row.get("safety_note", "")), steps or (str(row.get("summary", "")),),
    )


def _dev_from_row(row: dict) -> DevGuideItem:
    detail = _detail(row)
    return DevGuideItem(
        str(row["content_id"]), str(row["stage_code"]), str(row["title"]),
        [str(value) for value in detail.get("checkpoints", ())],
        [str(value) for value in detail.get("parent_tips", ())],
        [str(value) for value in detail.get("consult_signs", ())],
    )


def pick_home_plays(stage: str, offset: int = 0, limit: int = 2, db=None) -> list[HomePlayItem]:
    """Pick a stable, circular window from one stage without mutating the catalogue."""
    stage = stage.value if isinstance(stage, PlayStage) else str(stage)
    items = []
    if db is not None:
        try:
            items = [_play_from_row(row) for row in db.list_catalog_items("PLAY", stage)]
        except (AttributeError, TypeError):
            items = []
    if not items:
        items = [item for item in HOME_PLAY_ITEMS if item.stage == stage]
    if not items:
        raise ValueError(f"unknown play stage: {stage}")
    if limit <= 0:
        return []
    start = int(offset) % len(items)
    return [items[(start + i) % len(items)] for i in range(min(int(limit), len(items)))]


def get_dev_guides(stage: str, db=None) -> list[DevGuideItem]:
    """Return all concise development guides for a stage."""
    stage = stage.value if isinstance(stage, DevStage) else str(stage)
    if stage not in DEV_STAGE_LABELS:
        raise ValueError(f"unknown development stage: {stage}")
    if db is not None:
        try:
            rows = db.list_catalog_items("DEV_GUIDE", stage)
            if rows:
                return [_dev_from_row(row) for row in rows]
        except (AttributeError, TypeError):
            pass
    return [item for item in DEV_GUIDE_ITEMS if item.stage == stage]


def build_catalog_seed_rows() -> list[dict]:
    rows: list[dict] = []
    for item in HOME_PLAY_ITEMS:
        rows.append({
            "content_id": item.play_id,
            "domain": "PLAY",
            "stage_code": item.stage,
            "title": item.title,
            "summary": item.how_to,
            "detail": {
                "steps": list(item.steps), "materials": item.materials,
                "duration_min": item.duration_min, "dev_benefit": item.dev_benefit,
            },
            "min_months": 0, "max_months": 240,
            "safety_note": item.safety_note, "source": "SEED",
        })
    for item in DEV_GUIDE_ITEMS:
        rows.append({
            "content_id": item.guide_id,
            "domain": "DEV_GUIDE",
            "stage_code": item.stage,
            "title": item.title,
            "summary": item.checkpoints[0] if item.checkpoints else item.title,
            "detail": {
                "checkpoints": list(item.checkpoints), "parent_tips": list(item.parent_tips),
                "consult_signs": list(item.consult_signs),
            },
            "min_months": 0, "max_months": 240,
            "safety_note": DEV_DISCLAIMER, "source": "SEED",
        })
    return rows

