---
description: "운영 환경 실발송 통제(PROD-LIVE), 재기동 시 보상 발송 금지(STARTUP_INIT vs CRON_TICK), SSOT 배달원장 및 UNKNOWN 격리"
trigger: "model_decision"
tags: ["delivery", "prod-live", "ssot", "alerts-db"]
---

# [PROD-LIVE] 실사용자 발송 및 재발송 절대 통제

> 적용 대상: 스케줄러, 발송 큐/디스패처, 배달 원장(`delivery_jobs`), 서비스 기동·배포 경로, 관리자 수동 발송.
> 이 규칙은 Markdown 선언만으로 강제되지 않는다. 동일한 상태·전이·거부 조건이 운영 코드와 테스트에 구현되어 있어야 한다.

## 1. 실사용자 발송 기본 거부 (Deny-by-Default)
- 운영(Production) 환경, 실제 텔레그램 채널, 실구독자 대상 메시지는 **기본 거부**된다.
- 개발·배포·테스트 중에는 실제 발송 플래그 실행을 금지하며, `--demo` 또는 모의(Dry-run) 모드만 사용한다.
- 관리자 수동 발송(`MANUAL`)은 대상과 사유를 기록하고, 사용자의 명시적 승인이 있을 때만 **단 1회** 실행한다.

## 2. 재기동 시 보상성 자동 재발송 금지 (Trigger Context)
| 트리거 | 허용 동작 |
|---|---|
| `STARTUP_INIT` (서비스 시작, `systemd restart`, 배포 완료 초기화) | 과거 미발송 슬롯 자동 보상 발송 **0건 강제**. 현재 시각 이후의 미래 슬롯만 예약 |
| `CRON_TICK` (정시 스케줄러) | 수집·리포트 생성이 지연되더라도 동일 `execution_id` 세션 안에서 **TTL 2시간** 내 정상 발송 완료 가능 |

## 3. 발송 원장(SSOT) 단일화 및 UNKNOWN 격리
- 발송 여부의 단일 진실 공급원(SSOT)은 운영 DB `alerts.sqlite3`의 배달 원장(`delivery_jobs`, `delivery_job_recipients`)뿐이다.
- `portal.sqlite3`는 구독 대상 산출용으로만 사용한다.
- 동일 슬롯에 `SENT` 또는 네트워크 응답 불명확(`UNKNOWN`) 기록이 1건이라도 있으면 **발송 완료로 간주**하고 자동 재발송을 차단한다.
- 원격 OCI VM 상태 조회 실패, 로컬 DB와의 불일치 발생 시 발송 게이트를 즉시 `BLOCKED`(Fail-Closed) 처리한다.
