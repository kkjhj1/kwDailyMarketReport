"""Admin Console sync registry (SSOT for Admin coverage of bot DB tables).

Every `CREATE TABLE IF NOT EXISTS <name>` declared in a `SCHEMA_SOURCES` file (market, travel, chungyak
sidecar) MUST appear in exactly one of:

- `ADMIN_SYNC_REGISTRY`: the table is surfaced in the Admin Console (API endpoint(s) + DB reader).
- `ADMIN_EXEMPT_TABLES`: the table is internal operational state, with an explicit non-empty reason.

`tests/test_admin_sync_contract.py` enforces this so that a new watch / delivery / prefs table cannot ship
without Admin visibility or a recorded exemption (see `.agents/rules/admin-sync.md`).
"""
from __future__ import annotations

from typing import Any

SCHEMA_SOURCES: tuple[str, ...] = (
    "market_brief/storage/db.py",
    "travel_brief/storage/db.py",
    "chungyak_brief/storage/db.py",
)

ADMIN_SYNC_REGISTRY: dict[str, dict[str, Any]] = {
    # --- Platform / subscription master data ---------------------------------------------------------
    "recipients": {
        "bot_id": "all",
        "admin_api": ["GET /api/recipients"],
        "db_reader": "MarketBriefDatabase.get_all_recipients",
        "description": "수신자 마스터(토픽별 구독·활성 상태) — 수신자 관리 탭",
    },
    "users": {
        "bot_id": "all",
        "admin_api": ["GET /api/platform/users"],
        "db_reader": "SubscriptionEngine.list_users_with_subscriptions",
        "description": "플랫폼 사용자(등급·상태·실명) — 사용자 탭",
    },
    "subscriptions": {
        "bot_id": "all",
        "admin_api": ["GET /api/platform/users", "GET /api/platform/matrix"],
        "db_reader": "SubscriptionEngine.list_users_with_subscriptions",
        "description": "사용자별 봇 구독 관계 — 사용자 탭 구독 배지/매트릭스",
    },
    "tier_policies": {
        "bot_id": "all",
        "admin_api": ["GET /api/platform/tiers"],
        "db_reader": "SubscriptionEngine.get_tiers",
        "description": "등급 정책(최대 구독 봇 수) — 플랫폼 개요",
    },
    "bots": {
        "bot_id": "all",
        "admin_api": ["GET /api/platform/bots"],
        "db_reader": "SubscriptionEngine.list_bots",
        "description": "봇 레지스트리(토큰·활성 여부) — 봇 탭",
    },
    "upgrade_requests": {
        "bot_id": "all",
        "admin_api": ["GET /api/platform/upgrade-requests"],
        "db_reader": "SubscriptionEngine.get_upgrade_requests",
        "description": "등급 업그레이드 신청 — 승인 대기 큐",
    },
    "subscription_audit_logs": {
        "bot_id": "all",
        "admin_api": ["GET /api/platform/audit-logs", "GET /api/platform/users/{user_id}/history"],
        "db_reader": "SubscriptionEngine.list_audit_logs",
        "description": "플랫폼 감사 이력(구독·등급·관리자 조치) — 이력 센터 > 플랫폼 감사 이력",
    },
    "system_settings": {
        "bot_id": "brief_bot",
        "admin_api": ["GET /api/settings", "GET /api/platform/schedules"],
        "db_reader": "MarketBriefDatabase.get_setting",
        "description": "긴급 임계치·스케줄 설정 SSOT — 설정/스케줄 화면",
    },
    "bot_schedules_pending": {
        "bot_id": "all",
        "admin_api": ["GET /api/platform/schedules"],
        "db_reader": "MarketBriefDatabase.get_latest_schedule_journal",
        "description": "크론 스케줄 동기화 저널(PENDING/APPLIED/SYNC_FAILED) — 스케줄 화면 상태 표시",
    },
    # --- Regular broadcast delivery ledger ------------------------------------------------------------
    "delivery_batches": {
        "bot_id": "all",
        "admin_api": ["GET /api/history"],
        "db_reader": "MarketBriefDatabase.get_recent_jobs",
        "description": "정기/긴급 배치 마스터 — 이력 센터 > 봇 발송 이력",
    },
    "delivery_jobs": {
        "bot_id": "all",
        "admin_api": ["GET /api/history"],
        "db_reader": "MarketBriefDatabase.get_recent_jobs",
        "description": "배치 실행 잡(성공/실패 카운트) — 이력 센터 > 봇 발송 이력",
    },
    "delivery_job_recipients": {
        "bot_id": "all",
        "admin_api": ["GET /api/history/{job_id}/details", "GET /api/platform/users/{user_id}/history"],
        "db_reader": "MarketBriefDatabase.get_job_recipients",
        "description": "잡별 수신자 스냅샷 — 발송 상세 모달",
    },
    # --- Custom watches / custom deliveries / prefs (Admin Sync v1) ----------------------------------
    "market_volatility_watches": {
        "bot_id": "brief_bot",
        "admin_api": [
            "GET /api/platform/watches",
            "POST /api/platform/watches/{bot_id}/{watch_id}/deactivate",
            "GET /api/status",
        ],
        "db_reader": "MarketBriefDatabase.list_all_watches",
        "description": "시황봇 사용자 맞춤 변동성 감시 — 이력 센터 > 맞춤 발송 이력 > 맞춤 감시 현황",
    },
    "market_watch_deliveries": {
        "bot_id": "brief_bot",
        "admin_api": ["GET /api/platform/custom-deliveries", "GET /api/status"],
        "db_reader": "MarketBriefDatabase.list_custom_watch_deliveries",
        "description": "시황봇 맞춤 감시 발송 원장 — 이력 센터 > 맞춤 발송 이력",
    },
    "travel_price_watches": {
        "bot_id": "travel_bot",
        "admin_api": [
            "GET /api/platform/watches",
            "POST /api/platform/watches/{bot_id}/{watch_id}/deactivate",
            "GET /api/travel/status",
        ],
        "db_reader": "MarketBriefDatabase.list_all_watches",
        "description": "여행봇 목적지 특가 감시 — 이력 센터 > 맞춤 발송 이력 > 맞춤 감시 현황",
    },
    "travel_watch_deliveries": {
        "bot_id": "travel_bot",
        "admin_api": ["GET /api/platform/custom-deliveries", "GET /api/travel/status"],
        "db_reader": "MarketBriefDatabase.list_custom_watch_deliveries",
        "description": "여행봇 특가(DROP/DAILY) 발송 원장 — 이력 센터 > 맞춤 발송 이력",
    },
    "travel_user_prefs": {
        "bot_id": "travel_bot",
        "admin_api": ["GET /api/platform/watches", "GET /api/travel/status"],
        "db_reader": "MarketBriefDatabase.list_all_watches",
        "description": "여행봇 2-Track 선호(정기 ON/OFF·특가 일시중지) — 맞춤 감시·선호 현황",
    },
    "kids_content_catalog": {
        "bot_id": "kids_bot",
        "admin_api": ["GET /api/kids/catalog", "POST /api/kids/catalog/import"],
        "db_reader": "KidsCompassDatabase.list_catalog_items",
        "description": "키즈 놀이·발달·먹거리 콘텐츠 카탈로그 — 관리자 조회·JSON 임포트",
    },
    # --- Chungyak bot (sidecar `chungyak_brief.sqlite3`, read-only from Admin) -------------------------
    "chungyak_notices": {
        "bot_id": "chungyak_bot",
        "db_file": "chungyak_brief.sqlite3",
        "admin_api": ["GET /api/chungyak/notices"],
        "db_reader": "MarketBriefDatabase.list_chungyak_notices",
        "description": "청약봇 청약홈 공고(정정 revision·content_hash) — 맞춤 발송 이력 > 청약 알림 현황",
    },
    "chungyak_user_profiles": {
        "bot_id": "chungyak_bot",
        "db_file": "chungyak_brief.sqlite3",
        "admin_api": ["GET /api/chungyak/watches"],
        "db_reader": "MarketBriefDatabase.list_chungyak_user_profiles",
        "description": "청약봇 사용자 맞춤 자격 조건(마스킹 chat_id) — 맞춤 발송 이력 > 청약 알림 현황",
    },
    "chungyak_regular_subs": {
        "bot_id": "chungyak_bot",
        "db_file": "chungyak_brief.sqlite3",
        "admin_api": ["GET /api/chungyak/subscribers"],
        "db_reader": "MarketBriefDatabase.list_chungyak_regular_subs",
        "description": "청약봇 08:00 정기 다이제스트 구독(PII, Fail-Closed 토큰) — 청약 알림 현황",
    },
    "chungyak_deliveries": {
        "bot_id": "chungyak_bot",
        "db_file": "chungyak_brief.sqlite3",
        "admin_api": ["GET /api/chungyak/deliveries"],
        "db_reader": "MarketBriefDatabase.list_chungyak_deliveries",
        "description": "청약봇 발송 원장(CUSTOM_MATCH/REGULAR_DAILY, QUEUED_MORNING vs UNKNOWN) — 청약 알림 현황",
    },
}

# Admin endpoints exposing raw chat_id lists (PII): token is REQUIRED (503 when ADMIN_API_TOKEN is unset).
ADMIN_FAIL_CLOSED_PII_APIS: tuple[str, ...] = ("GET /api/chungyak/subscribers",)

ADMIN_USER_CONTEXT_REGISTRY: dict[str, dict[str, Any]] = {
    "admission_subscribers": {
        "bot_id": "admission_bot",
        "db_file": "alerts.sqlite3",
        "admin_api": ["GET /api/platform/users/{user_id}/bot-context"],
        "db_reader": "MarketBriefDatabase.get_user_bot_context",
        "description": "입시봇 사용자의 대상 학년·지역 및 활성 구독 설정",
    },
    "kids_user_profiles": {
        "bot_id": "kids_bot",
        "db_file": "kids_compass.sqlite3",
        "admin_api": ["GET /api/platform/users/{user_id}/bot-context"],
        "db_reader": "MarketBriefDatabase.get_user_bot_context",
        "description": "자녀봇 사용자 지역·주말·D-day 알림 맞춤 설정",
    },
    "kids_children": {
        "bot_id": "kids_bot",
        "db_file": "kids_compass.sqlite3",
        "admin_api": ["GET /api/platform/users/{user_id}/bot-context"],
        "db_reader": "MarketBriefDatabase.get_user_bot_context",
        "description": "자녀봇 사용자별 등록 자녀 및 주요 자녀 정보",
    },
    "kids_pregnancy_profiles": {
        "bot_id": "kids_bot",
        "db_file": "kids_compass.sqlite3",
        "admin_api": ["GET /api/platform/users/{user_id}/bot-context"],
        "db_reader": "MarketBriefDatabase.get_user_bot_context",
        "description": "자녀봇 임신 프로필의 태명·출산 예정일 설정",
    },
    "kids_dose_timers": {
        "bot_id": "kids_bot",
        "db_file": "kids_compass.sqlite3",
        "admin_api": ["GET /api/platform/users/{user_id}/bot-context"],
        "db_reader": "MarketBriefDatabase.get_user_bot_context",
        "description": "자녀봇 복약 알림 중 PENDING·IN_FLIGHT 타이머 현황",
    },
    "yolo_users": {
        "bot_id": "yolo_bot",
        "db_file": "yolo_brief.sqlite3",
        "admin_api": ["GET /api/platform/users/{user_id}/bot-context"],
        "db_reader": "MarketBriefDatabase.get_user_bot_context",
        "description": "욜로봇 사용자의 아키타입·프로필 진행 상태",
    },
    "silver_parents": {
        "bot_id": "silver_bot",
        "db_file": "silver_care.sqlite3",
        "admin_api": ["GET /api/platform/users/{user_id}/bot-context"],
        "db_reader": "MarketBriefDatabase.get_user_bot_context",
        "description": "실버봇 사용자의 부모 대상자별 맞춤 프로필",
    },
}

ADMIN_EXEMPT_TABLES: dict[str, str] = {
    "alert_reservations": "정기/긴급 발송 멱등성 예약 키(REGULAR:/URGENT:) — 결과는 delivery_jobs로 이미 노출되는 내부 락 상태",
    "delivery_audit_logs": "레거시 채널 단위 발송 로그(구 AlertStore 호환) — 신규 원장(delivery_job_recipients)으로 대체됨",
    "system_config_history": "설정 변경 버전 이력(append-only) — 운영 롤백 추적용 내부 저널, 현재 값은 system_settings로 노출",
    "telegram_sync_state": "텔레그램 getUpdates offset 싱글톤 — 폴링 커서 내부 상태",
    "travel_watch_tick_leases": "여행 감시 tick 싱글톤 리스(manager vs cron 교차 프로세스 가드) — 단명 락 상태",
}


def all_registered_tables() -> set[str]:
    return set(ADMIN_SYNC_REGISTRY) | set(ADMIN_EXEMPT_TABLES)
