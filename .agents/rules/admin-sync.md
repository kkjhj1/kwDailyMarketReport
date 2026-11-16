---
description: "봇 DB 테이블·발송 채널·스케줄 잡 변경 시 관리자 콘솔(Admin) 동기화 의무 및 예외 등록 규약"
trigger: "glob"
globs: "**/storage/db.py, **/*_bot_runner.py, market_brief/admin_sync_registry.py, admin_server.py, market_brief/admin_ui.py"
---

# 관리자 콘솔 동기화 규약 ([ADMIN-SYNC])

> 목적: 봇에 신규 감시·발송·선호 기능이 추가됐는데 관리자 페이지에서 보이지 않는 "Admin Drift"를 차단한다.

## [ADMIN-SYNC-01] 적용 대상 (L2 이상)
- 봇 DB 테이블 추가·변경: 특히 `*_watches`, `*_deliveries`, `*_prefs`, 그 외 사용자별 상태/원장 테이블
- 신규 발송 채널(맞춤 알림·특가·긴급 등) 또는 신규 정기/스케줄 잡

## [ADMIN-SYNC-02] Phase 1 감사 의무
- 위 변경은 Phase 1 GPT 감사(`scratch/gpt_architecture*.md`)에서 **Admin Console 커버리지**를 반드시 판정한다.
  - 조회 API(`admin_server.py`) + UI 노출 위치(`market_brief/admin_ui.py`) + DB 리더 메서드
- 감사 산출물에 Admin 커버리지 판정이 없으면 `AUDIT_INCOMPLETE` — 계획 승인 요청·Phase 2 착수 금지.

## [ADMIN-SYNC-03] 레지스트리 등록 (SSOT)
- `market_brief/admin_sync_registry.py`에 둘 중 하나로 반드시 등록한다.
  - `ADMIN_SYNC_REGISTRY`: `admin_api`, `db_reader`, 비어 있지 않은 `description`
  - `ADMIN_EXEMPT_TABLES`: 내부 운영 상태(락·리스·offset·append-only 저널 등)만 허용, **비어 있지 않은 구체적 사유** 필수
- `tests/test_admin_sync_contract.py`가 `CREATE TABLE` 전수 대조로 강제한다. 테스트를 우회(삭제/skip/assert 축소)하는 변경은 금지.

## [ADMIN-SYNC-04] Admin 구현 불변조건
- Admin은 `MarketBriefDatabase`만 생성한다. 타 봇 DB 클래스(`TravelBriefDatabase` 등)를 Admin에서 생성 금지(빈 DB에 테이블을 만들어 버림).
- 타 봇 테이블 조회는 `_table_exists()` 가드 필수: 테이블 부재 시 `200` + 빈 목록 / `0` 카운트.
- Decimal TEXT 수치는 `float()` 변환 금지(`Decimal(str(v))`), UI에서도 `Number()` 재변환 금지.
- 기존 응답 필드는 additive로만 확장(제로 브레이킹). PII 포함 목록·변경 API는 `verify_admin_token`, 변경 시 `sub_engine.log_audit(...)`.
- UI는 `esc()` 처리 + `data-*` & `addEventListener`(inline JS 값 삽입 금지), 360px 무오버플로우·44px 터치 타깃(`web-report-ui.md`).
- Admin 조치(비활성화 등)는 사용자에게 텔레그램 실발송을 유발하지 않는다([PROD-LIVE]).
