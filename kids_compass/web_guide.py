"""Validated catalogue imports and small, XSS-safe mobile guide pages."""
from __future__ import annotations

import html
import re
from collections.abc import Mapping
from typing import Any


CONTENT_ID_RE = re.compile(r"^[a-z0-9_]{1,64}$")
DOMAINS = {"PLAY", "DEV_GUIDE", "FOOD"}
DETAIL_KEYS = {
    "steps", "materials", "duration_min", "dev_benefit", "checkpoints",
    "parent_tips", "consult_signs", "prep_tip", "allergy_note", "risk_level", "aliases",
}
_TAG_RE = re.compile(r"[<>]")
_DETAIL_LIST_KEYS = {"steps", "checkpoints", "parent_tips", "consult_signs", "aliases"}
_DETAIL_STRING_KEYS = {"materials", "dev_benefit", "prep_tip", "allergy_note", "risk_level"}


def _reject_markup(value: str, field: str) -> str:
    if _TAG_RE.search(value):
        raise ValueError(f"{field} contains HTML-like markup")
    if len(value) > 2000:
        raise ValueError(f"{field} is too long")
    return value


def _validate_detail(value: Any, depth: int = 0) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("detail must be an object")
    if depth > 2:
        raise ValueError("detail nesting is too deep")
    unknown = set(value) - DETAIL_KEYS
    if unknown:
        raise ValueError(f"unknown detail key: {sorted(unknown)[0]}")
    result: dict[str, Any] = {}
    for key, raw in value.items():
        if key in _DETAIL_LIST_KEYS:
            if not isinstance(raw, (list, tuple)) or len(raw) > 50:
                raise ValueError(f"detail.{key} must be a list of at most 50 strings")
            if not all(isinstance(entry, str) for entry in raw):
                raise ValueError(f"detail.{key} entries must be strings")
            result[key] = [_reject_markup(entry, f"detail.{key}") for entry in raw]
        elif key == "duration_min":
            if isinstance(raw, bool) or not isinstance(raw, int) or not 1 <= raw <= 1440:
                raise ValueError("detail.duration_min must be an integer from 1 to 1440")
            result[key] = raw
        elif key in _DETAIL_STRING_KEYS:
            if not isinstance(raw, str):
                raise ValueError(f"detail.{key} must be a string")
            result[key] = _reject_markup(raw, f"detail.{key}")
        else:  # defensive: DETAIL_KEYS and the explicit branches must stay in sync
            raise ValueError(f"unsupported detail key: {key}")
    return result


def validate_catalog_import_payload(payload: Any) -> list[dict[str, Any]]:
    """Validate and normalize an import body, returning server-owned IMPORT rows.

    Both ``[{...}]`` and ``{"items": [{...}]}`` are accepted for small callers;
    the returned rows always carry ``source='IMPORT'`` regardless of input.
    """
    if isinstance(payload, Mapping):
        raw_items = payload.get("items")
    else:
        raw_items = payload
    if not isinstance(raw_items, list):
        raise ValueError("catalog import payload must contain an items list")
    if len(raw_items) > 100:
        raise ValueError("catalog import accepts at most 100 items")

    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, Mapping):
            raise ValueError(f"item {index} must be an object")
        content_id = raw.get("content_id")
        if not isinstance(content_id, str) or not CONTENT_ID_RE.fullmatch(content_id):
            raise ValueError(f"item {index} has an invalid content_id")
        if content_id in seen:
            raise ValueError(f"duplicate content_id: {content_id}")
        seen.add(content_id)

        domain = raw.get("domain")
        if domain not in DOMAINS:
            raise ValueError(f"item {index} has an invalid domain")
        stage_code = raw.get("stage_code")
        if not isinstance(stage_code, str) or not 1 <= len(stage_code) <= 64:
            raise ValueError(f"item {index} has an invalid stage_code")
        stage_code = _reject_markup(stage_code, f"item {index}.stage_code")
        title = raw.get("title")
        summary = raw.get("summary")
        safety_note = raw.get("safety_note", "")
        if not isinstance(title, str) or not title.strip() or len(title) > 120:
            raise ValueError(f"item {index} title must be at most 120 characters")
        if not isinstance(summary, str) or not summary.strip() or len(summary) > 400:
            raise ValueError(f"item {index} summary must be at most 400 characters")
        if not isinstance(safety_note, str) or len(safety_note) > 400:
            raise ValueError(f"item {index} safety_note must be at most 400 characters")
        title = _reject_markup(title, f"item {index}.title")
        summary = _reject_markup(summary, f"item {index}.summary")
        safety_note = _reject_markup(safety_note, f"item {index}.safety_note")

        min_months = raw.get("min_months", 0)
        max_months = raw.get("max_months", 240)
        if (isinstance(min_months, bool) or isinstance(max_months, bool)
                or not isinstance(min_months, int) or not isinstance(max_months, int)
                or not 0 <= min_months <= max_months <= 240):
            raise ValueError(f"item {index} has an invalid month range")

        result.append({
            "content_id": content_id,
            "domain": domain,
            "stage_code": stage_code,
            "title": title,
            "summary": summary,
            "detail": _validate_detail(raw.get("detail", {})),
            "min_months": min_months,
            "max_months": max_months,
            "safety_note": safety_note,
            "source": "IMPORT",
            "is_active": 1,
        })
    return result


def _escaped(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def _lines(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, (list, tuple)):
        return [_escaped(value) for value in values if str(value).strip()]
    return [_escaped(values)] if str(values).strip() else []


def _section(title: str, values: Any) -> str:
    lines = _lines(values)
    if not lines:
        return ""
    return "<section><h2>" + _escaped(title) + "</h2><ul>" + "".join(
        f"<li>{line}</li>" for line in lines
    ) + "</ul></section>"


def render_guide_html(item: dict) -> str:
    """Render a complete 360px-safe guide page with escaped dynamic content."""
    detail = item.get("detail", item.get("detail_json", {}))
    if isinstance(detail, str):
        try:
            import json
            detail = json.loads(detail)
        except (ValueError, TypeError):
            detail = {}
    detail = detail if isinstance(detail, dict) else {}
    title = _escaped(item.get("title", "키즈 나침반 가이드"))
    summary = _escaped(item.get("summary", ""))
    safety = _escaped(item.get("safety_note", ""))
    sections = [
        _section("준비물", detail.get("materials")),
        _section("단계별 실행법", detail.get("steps")),
        _section("체크리스트", detail.get("checkpoints")),
        _section("부모 팁", detail.get("parent_tips")),
        _section("알레르기·손질", [detail.get("prep_tip"), detail.get("allergy_note")]),
        _section("상담 권장 신호", detail.get("consult_signs")),
    ]
    if detail.get("dev_benefit"):
        sections.insert(2, _section("기대되는 발달 도움", detail.get("dev_benefit")))
    risk = detail.get("risk_level")
    if risk:
        sections.append(_section("위험 수준", risk))
    if safety:
        sections.append(f"<section class=\"warning\"><h2>안전 주의사항</h2><p>{safety}</p></section>")
    return """<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>""" + title + """</title><style>
*{box-sizing:border-box}html,body{margin:0;padding:0;overflow-x:hidden;background:#f7f8fb;color:#172033;font-family:system-ui,-apple-system,"Segoe UI",sans-serif}
main{width:100%;max-width:720px;margin:0 auto;padding:16px;display:grid;grid-template-columns:minmax(0,1fr);gap:12px}
header,section{background:#fff;border:1px solid #e4e8f0;border-radius:14px;padding:16px;min-width:0}h1,h2,p{overflow-wrap:anywhere}h1{margin:0 0 8px;font-size:1.35rem}h2{margin:0 0 8px;font-size:1rem}p{margin:0;line-height:1.55}ul{margin:0;padding-left:20px}li{line-height:1.55;margin:4px 0}.warning{border-color:#f0b4a8;background:#fff8f6}.disclaimer{font-size:.88rem;color:#596579}a,button{display:inline-flex;align-items:center;justify-content:center;min-height:44px;padding:8px 12px;border-radius:10px}a{color:#1558a6;text-decoration:none;background:#edf5ff}
</style></head><body><main><header><div aria-label="카탈로그 영역">🧭 키즈 나침반</div><h1>""" + title + """</h1><p>""" + summary + """</p></header>""" + "".join(sections) + """<p class="disclaimer">면책 안내: 일반적인 참고 안내이며 아이마다 다릅니다. 증상·알레르기·응급 상황은 의료진의 판단을 우선하세요.</p></main></body></html>"""


def render_not_found_html() -> str:
    return """<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>가이드를 찾을 수 없음</title><style>*{box-sizing:border-box}html,body{margin:0;padding:0;overflow-x:hidden;font-family:system-ui,sans-serif;background:#f7f8fb}main{min-height:100vh;display:grid;place-items:center;grid-template-columns:minmax(0,1fr);padding:24px;text-align:center}a{display:inline-flex;align-items:center;justify-content:center;min-height:44px;padding:8px 12px;border-radius:10px;background:#edf5ff;color:#1558a6;text-decoration:none}</style></head><body><main><div><h1>가이드를 찾을 수 없어요</h1><p>요청한 콘텐츠가 없거나 비활성화되었습니다.</p><a href="/">홈으로</a></div></main></body></html>"""
