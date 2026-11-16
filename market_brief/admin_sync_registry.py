"""Admin Console sync registry (SSOT for Admin coverage of bot DB tables).

Every `CREATE TABLE IF NOT EXISTS <name>` declared in `market_brief/storage/db.py` and
`travel_brief/storage/db.py` MUST appear in exactly one of:

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
