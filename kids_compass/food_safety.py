"""Pull-only food, complementary-feeding and food-safety catalogue.

This module deliberately contains no scheduling, delivery or database code.  It
is safe to use from the interactive Kids Compass menu and from token-free demos.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Literal, Optional


FoodCategory = Literal["FORBIDDEN", "PROTEIN", "PRODUCE", "DAIRY_GRAIN", "ALLERGY"]
FoodRiskLevel = Literal["FORBIDDEN_UNDER_12M", "CHOKING_HAZARD", "HIGH_ALLERGY", "SAFE"]

FOOD_DISCLAIMER = (
    "일반적인 이유식·영양 참고 정보이며 알레르기 반응이나 개인 질환이 있는 경우 "
    "소아청소년과 전문의와 상담하세요."
)


@dataclass(frozen=True)
class FoodItem:
    food_id: str
    name: str
    aliases: tuple[str, ...]
    category: FoodCategory
    min_months: int
    risk_level: FoodRiskLevel
    prep_tip: str
    allergy_note: str
    safety_warning: str


FOOD_ITEMS: tuple[FoodItem, ...] = (
    FoodItem("honey", "꿀", ("벌꿀", "꿀물"), "FORBIDDEN", 12, "FORBIDDEN_UNDER_12M",
             "돌 전에는 조리·음료에 넣지 마세요.", "알레르기보다 영아 보툴리누스증 위험이 먼저입니다.",
             "영아 보툴리누스증 위험으로 돌 전 절대 금지"),
    FoodItem("cow_milk", "생우유", ("우유", "소 우유"), "FORBIDDEN", 12, "FORBIDDEN_UNDER_12M",
             "돌 이후에도 처음에는 소량을 음식에 사용하고 음료 대체는 천천히 시작하세요.",
             "우유 단백질 반응이 의심되면 중단하고 상담하세요.", "돌 전 주 음료로 주지 말고, 음료 대체는 돌 이후 권장"),
    FoodItem("whole_nuts", "통견과류", ("견과류", "땅콩", "아몬드", "호두"), "FORBIDDEN", 48, "CHOKING_HAZARD",
             "통째로 주지 말고 곱게 간 가루나 묽은 견과버터로 준비하세요.",
             "가루·버터는 6~7개월 이후 소량으로 알레르기 테스트를 고려하세요.",
             "만 4세 미만 질식 위험; 가루·버터는 6~7개월 이후 소량 테스트"),
    FoodItem("cherry_tomato_grape", "방울토마토·포도", ("방울토마토", "토마토", "포도", "거봉"), "FORBIDDEN", 8, "CHOKING_HAZARD",
             "깨끗이 씻어 만 4세 미만은 반드시 세로 4등분으로 잘라 주세요.",
             "처음에는 한 종류씩 소량으로 반응을 살피세요.", "만 4세 미만 반드시 세로 4등분 절단"),
    FoodItem("rice_cake_candy", "떡·사탕·젤리", ("떡", "사탕", "젤리", "캔디"), "FORBIDDEN", 36, "CHOKING_HAZARD",
             "가능한 늦게 시작하고, 먹을 때는 앉아서 보호자가 지켜보세요.",
             "당류가 많고 알레르기 원료가 섞일 수 있어 성분표를 확인하세요.", "끈적하거나 둥근 형태는 질식 위험이 큽니다."),
    FoodItem("beef", "소고기", ("쇠고기", "한우"), "PROTEIN", 6, "SAFE",
             "기름기 적은 살코기를 푹 익혀 곱게 다지거나 결대로 찢어 주세요.",
             "처음에는 한 가지 단백질로 소량 테스트하세요.", "덜 익힌 고기는 피하고 속까지 충분히 익히세요."),
    FoodItem("chicken", "닭고기", ("닭", "닭가슴살"), "PROTEIN", 6, "SAFE",
             "껍질과 뼈를 제거하고 푹 익혀 곱게 찢거나 다져 주세요.",
             "처음에는 소량으로 반응을 살피세요.", "뼈 조각이 남지 않았는지 확인하세요."),
    FoodItem("egg", "달걀", ("계란", "달걀노른자", "계란노른자"), "PROTEIN", 6, "HIGH_ALLERGY",
             "완숙으로 익혀 아주 소량부터 시작하고, 처음에는 다른 새 음식과 겹치지 마세요.",
             "3대 알레르기 식품 중 하나로, 첫 시도 후 반응을 관찰하세요.", "완숙 소량 테스트 · 3대 알레르기 식품"),
    FoodItem("tofu", "두부·콩", ("두부", "콩", "대두"), "PROTEIN", 6, "SAFE",
             "부드럽게 익혀 으깨고 간은 하지 마세요.", "콩 알레르기 반응이 있으면 중단하세요.", "목에 걸리지 않게 으깨거나 작게 잘라 주세요."),
    FoodItem("white_fish", "흰살생선", ("생선", "대구", "명태"), "PROTEIN", 7, "SAFE",
             "완전히 익혀 가시를 여러 번 확인하고 곱게 부숴 주세요.", "처음에는 한 종류씩 소량으로 시작하세요.", "생선회·덜 익힌 생선은 피하고 가시를 제거하세요."),
    FoodItem("salmon", "연어", ("훈제연어",), "PROTEIN", 8, "SAFE",
             "생연어를 완전히 익혀 가시를 제거하고, 훈제 제품은 피하세요.", "처음에는 소량으로 반응을 관찰하세요.", "생연어·훈제연어보다 충분히 익힌 연어를 선택하세요."),
    FoodItem("shrimp_crab", "새우·게 갑각류", ("새우", "게", "대게", "꽃게", "갑각류"), "PROTEIN", 12, "HIGH_ALLERGY",
             "완전히 익히고 잘게 다져 아주 소량으로 시작하세요.", "고위험 알레르기 식품이므로 첫 시도 후 반응을 관찰하세요.", "12개월 이후 소량 테스트 · 두드러기·호흡곤란 시 즉시 진료"),
    FoodItem("peach", "복숭아", ("복숭아즙",), "PRODUCE", 7, "HIGH_ALLERGY",
             "껍질과 씨를 제거하고 잘 익은 과육을 으깨거나 작게 잘라 주세요.", "고위험 알레르기 식품으로 처음에는 한두 숟가락만 주세요.", "입 주변 발진 등 반응을 살피세요."),
    FoodItem("strawberry", "딸기", ("딸기류",), "PRODUCE", 8, "SAFE",
             "깨끗이 씻고 꼭지를 제거해 잘게 으깨거나 잘라 주세요.", "산도 때문에 입 주변이 붉어질 수 있어 반응을 관찰하세요.", "작고 단단한 조각은 질식하지 않게 충분히 잘라 주세요."),
    FoodItem("kiwi", "키위", ("골드키위", "참다래"), "PRODUCE", 12, "SAFE",
             "껍질을 벗기고 충분히 익은 과육을 으깨 주세요.", "입 주변 자극이나 알레르기 반응을 살피세요.", "씨와 산도가 있어 소량부터 시작하세요."),
    FoodItem("citrus", "귤·오렌지", ("귤", "오렌지", "감귤", "자몽"), "PRODUCE", 12, "SAFE",
             "껍질과 씨, 질긴 막을 제거해 잘게 나눠 주세요.", "산도에 의한 자극이 있으면 양을 줄이세요.", "통째로 물고 먹지 않도록 조각을 작게 나눠 주세요."),
    FoodItem("apple_pear", "사과·배", ("사과", "배"), "PRODUCE", 6, "SAFE",
             "초기에는 껍질·씨를 제거하고 익혀서 으깨 주세요.", "처음에는 한 종류씩 소량으로 시작하세요.", "생사과·생배의 단단한 조각은 질식 위험이 있어 손질하세요."),
    FoodItem("banana", "바나나", ("바나나" ,), "PRODUCE", 6, "SAFE",
             "잘 익은 바나나를 으깨거나 길쭉하게 잘라 주세요.", "드물게 알레르기 반응이 있을 수 있어 처음에는 소량으로 주세요.", "미끄러운 조각은 보호자가 지켜보며 먹이세요."),
    FoodItem("spinach", "시금치", ("시금치나물",), "PRODUCE", 6, "SAFE",
             "깨끗이 씻어 데친 뒤 물기를 짜고 곱게 다져 주세요.", "처음에는 소량으로 시작하세요.", "데치지 않은 잎채소를 통째로 주지 마세요."),
    FoodItem("zucchini", "애호박", ("호박",), "PRODUCE", 6, "SAFE",
             "껍질과 씨를 확인해 부드럽게 익힌 뒤 으깨거나 잘게 썰어 주세요.", "특이 반응이 드물지만 처음에는 소량으로 주세요.", "단단한 조각은 부드럽게 익혀 주세요."),
    FoodItem("carrot_sweetpotato", "당근·고구마", ("당근", "고구마"), "PRODUCE", 6, "SAFE",
             "푹 쪄서 부드럽게 으깨고, 섬유질이 많으면 체에 걸러 주세요.", "처음에는 한 종류씩 소량으로 주세요.", "단단하거나 퍽퍽한 덩어리는 질식 위험이 있어 충분히 부드럽게 하세요."),
    FoodItem("rice_porridge", "쌀미음", ("쌀죽", "쌀"), "DAIRY_GRAIN", 4, "SAFE",
             "묽고 부드럽게 끓여 한두 숟가락부터 시작하세요.", "쌀 알레르기는 드물지만 반응을 관찰하세요.", "뜨겁지 않게 식혀 농도와 온도를 확인하세요."),
    FoodItem("yogurt", "플레인 요거트", ("요구르트", "무가당 요거트"), "DAIRY_GRAIN", 7, "SAFE",
             "무가당·살균 제품을 소량부터 주세요.", "우유 단백질 반응이 있으면 소아과와 상담하세요.", "당이 첨가된 제품은 피하고 냉장 보관하세요."),
    FoodItem("cheese", "아기치즈", ("치즈", "유아치즈"), "DAIRY_GRAIN", 8, "SAFE",
             "저염 제품을 작게 잘라 식사 중에 소량 주세요.", "우유 단백질 알레르기 여부를 확인하세요.", "나트륨이 높을 수 있어 양을 제한하세요."),
    FoodItem("peanut_butter", "땅콩버터·가루", ("땅콩버터", "땅콩가루", "피넛버터"), "ALLERGY", 6, "HIGH_ALLERGY",
             "무가당·무염 제품을 물이나 죽에 아주 묽게 풀어 소량 테스트하세요.", "고위험 알레르기 식품이므로 처음에는 낮 시간에 한 가지씩 시도하세요.", "통땅콩은 금지하고, 끈적한 덩어리째 주지 마세요."),
    FoodItem("processed_meat", "햄·소시지·어묵", ("햄", "소시지", "어묵", "가공육"), "DAIRY_GRAIN", 24, "SAFE",
             "가능하면 늦게 시작하고, 먹이더라도 데쳐서 잘게 잘라 주세요.", "제품별 우유·밀·대두 등 원료를 확인하세요.", "24개월 이후에도 나트륨·첨가물 주의"),
)


_FOODS_BY_ID = {item.food_id: item for item in FOOD_ITEMS}


def _food_from_catalog_row(row: dict) -> FoodItem:
    detail = row.get("detail", row.get("detail_json", {}))
    if isinstance(detail, str):
        try:
            detail = json.loads(detail)
        except json.JSONDecodeError:
            detail = {}
    detail = detail if isinstance(detail, dict) else {}
    return FoodItem(
        food_id=str(row["content_id"]), name=str(row["title"]),
        aliases=tuple(str(value) for value in detail.get("aliases", ()) if str(value).strip()),
        category=str(row["stage_code"]), min_months=int(row.get("min_months", 0)),
        risk_level=str(detail.get("risk_level", "SAFE")),
        prep_tip=str(detail.get("prep_tip", "")),
        allergy_note=str(detail.get("allergy_note", "")),
        safety_warning=str(row.get("safety_note", "")),
    )


def get_food_item(food_id: str, db=None) -> Optional[FoodItem]:
    """Return a catalogue item by its stable ASCII identifier."""
    if db is not None:
        try:
            row = db.get_catalog_item(str(food_id), active_only=True)
            if row is not None and row.get("domain") == "FOOD":
                return _food_from_catalog_row(row)
        except (AttributeError, TypeError):
            pass
    return _FOODS_BY_ID.get(str(food_id))


def get_food(food_id: str, db=None) -> Optional[FoodItem]:
    return get_food_item(food_id, db=db)


def evaluate_food_for_child(food: FoodItem, age_months: Optional[int], is_pregnancy: bool = False) -> tuple[str, str]:
    """Return ``(status, human-readable guidance)`` for a child or pregnancy profile."""
    if is_pregnancy:
        return "INFO", "🤰 임신기 안내: 날것·생선회·카페인은 주의하고 식재료는 충분히 익혀 드세요."
    if age_months is None:
        return "INFO", f"권장 시작: 생후 {food.min_months}개월 이후"
    if age_months < food.min_months:
        if food.risk_level == "FORBIDDEN_UNDER_12M":
            return "BLOCKED", "🚨 돌(12개월) 미만 절대 금지!"
        return "BLOCKED", f"🚫 아직 이릅니다! (생후 {food.min_months}개월 이후 권장)"
    return "OK", f"✅ 현재 월령({age_months}개월) 섭취 가능 · 조리·주의사항 확인"


def _search_key(value: str) -> str:
    # Keep Unicode letters/numbers (including Korean), remove whitespace and punctuation.
    return re.sub(r"[^\w가-힣]+", "", str(value).casefold(), flags=re.UNICODE)


def search_foods(query: str, limit: int = 8, db=None) -> list[FoodItem]:
    """Find catalogue items by partial name or alias, safely ignoring spacing/punctuation."""
    key = _search_key(query)
    if not key or limit <= 0:
        return []
    source = FOOD_ITEMS
    if db is not None:
        try:
            rows = db.list_catalog_items("FOOD", limit=1000)
            if rows:
                source = tuple(_food_from_catalog_row(row) for row in rows)
        except (AttributeError, TypeError):
            pass
    matches = [item for item in source
               if any(key in _search_key(candidate) for candidate in (item.name, *item.aliases))]
    return matches[: int(limit)]


def list_foods_by_category(category: str, db=None) -> list[FoodItem]:
    """Return catalogue items in a category; unknown categories return an empty list."""
    wanted = str(category or "").strip().upper()
    source = FOOD_ITEMS
    if db is not None:
        try:
            rows = db.list_catalog_items("FOOD", wanted, limit=1000)
            if rows:
                source = tuple(_food_from_catalog_row(row) for row in rows)
        except (AttributeError, TypeError):
            pass
    return [item for item in source if item.category == wanted]


def build_food_catalog_seed_rows() -> list[dict]:
    return [
        {
            "content_id": item.food_id,
            "domain": "FOOD",
            "stage_code": item.category,
            "title": item.name,
            "summary": item.safety_warning,
            "detail": {
                "aliases": list(item.aliases), "risk_level": item.risk_level,
                "prep_tip": item.prep_tip, "allergy_note": item.allergy_note,
            },
            "min_months": item.min_months, "max_months": 240,
            "safety_note": item.safety_warning, "source": "SEED",
        }
        for item in FOOD_ITEMS
    ]

