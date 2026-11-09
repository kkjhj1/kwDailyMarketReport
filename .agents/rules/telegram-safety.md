---
description: "텔레그램 봇 메시지 용량, Strict HTML escape, 64-byte 콜백, 429 방어 규격"
trigger: "model_decision"
tags: ["telegram", "bot", "message", "callback"]
---

# 텔레그램 봇 실전 수칙 (TG-01 ~ TG-07)

> 적용 대상: `*_bot_runner.py`, 메시지 템플릿, 인라인 키보드/콜백 핸들러, 다수 수신자 발송 로직.
> 실사용자 발송 통제는 `data-delivery-integrity.md`의 `[PROD-LIVE]`를 함께 따른다.

## [TG-01] 용량 상한 & 고정 슬림 템플릿
- 가시 텍스트(태그 제외) **1,800자 이내**, HTML 원문(`len`) **3,500자 이내**를 엄수한다.
- 4,096자 절벽 대응을 위한 임의 항목 삭제식 가변 축약을 금지하고, 고정 슬림 템플릿을 사용한다.
- 상세 표·설명은 웹 리포트 링크로 분리한다.

## [TG-02] Strict HTML Escape
- 모든 동적·외부 수집 문자열은 예외 없이 `html.escape()` 처리 후 주입한다.
- 허용 태그: `<b>`, `<i>`, `<code>`, `<pre>`, `<a href="...">` — 그 외 태그 사용 금지.

## [TG-03] 1행 1정보
- 모바일 3초 스캔이 가능하도록 `이모지 + 핵심명사 + 수치(Bold)` 단문으로 구성한다.
- 장황한 괄호, 원본 URL 노출을 금한다.
- 연결되는 웹 리포트의 `360px` 규격은 `web-report-ui.md`를 따른다.

## [TG-04] 0.1초 콜백 응답(MUST) & 64-Byte 규약
- 인라인 버튼 수신 즉시 `answerCallbackQuery`를 **무조건 선행 호출**하여 로딩 스피너를 제거한다.
- `callback_data`는 **64 bytes 이하 영문 축약 코드(`도메인:액션:ID`)**만 사용한다.
- 화면 수정은 `_safe_edit_or_send`(`message is not modified` 무시 및 폴백 포함)를 통한다.
- 파괴적 액션(구독 해지 등)은 제자리 2단계 확인(In-Place Confirm)을 거친다.

## [TG-05] Safe Dispatcher (429 Rate Limit 방어)
- 다수 수신자 발송 시 단순 루프 직발송을 금한다.
- `SafeTelegramDispatcher` + 토큰 버킷 제한기(전역 `25 msg/s`, 동일 채팅 `1.0s/chat`) + 지수 백오프 재시도로 429 및 네트워크 타임아웃 중복 발송을 차단한다.
- 디스패처는 `[PROD-LIVE]` 게이트를 통과한 정상 작업만 전달받아 발송한다.

## [TG-06] 토큰 프리 `--demo` & 운영 자산 무오염
- 모든 봇 러너는 실제 토큰 없이 1초 내 콘솔 검증이 가능한 `--demo` 플래그를 제공한다.
- 데모·테스트 실행 중 운영 DB(`*.sqlite3`) 및 배포 폴더(`reports/`)에 기록하지 않는다(`:memory:` 또는 `scratch/` 격리).
- 현행 과제: `travel_bot_runner.py`, `portal_bot_runner.py`는 `--demo` 완비. `telegram_bot_runner.py`, `admission_bot_runner.py`는 향후 L2 작업 시 `--demo`를 최우선 구현한다.

## [TG-07] 제로 브레이킹 & 선행 컴파일 검증
- GitHub Actions의 기존 크론 CLI(`telegram_bot_runner.py --auto-schedule --live-naver` 등)에 대해 하위 호환성을 100% 보장한다.
- 코드 수정 직후 `python -m py_compile` 및 관련 `pytest` 통과를 확인한 뒤에만 완료 보고한다.
- 완료 보고에는 실제 실행 명령과 실측 결과를 반드시 제시한다(미실행 검증 보고 금지).
