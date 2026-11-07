# ============================================================
# PROJECT CONSTITUTION: Multi-Bot Intelligence Platform
# Scope: Project-Specific (c:\Users\환진\.gemini\antigravity\scratch\market_brief_starter)
# Version: 2.3 (Two-Phase Multi-AI Pipeline & Autonomous Planning Audit Edition)
# ============================================================

## 1. 프로젝트 개요 및 멀티봇 파이프라인
- **프로젝트 명**: `kwDailyMarketReport` / `market_brief_starter` (Multi-Bot Platform v2.0)
- **운영 인프라 환경**:
  - **운영 메인 서버**: **오라클 클라우드(OCI) VM** (`161.33.196.211`, `systemd` 서비스 `multibot.service`, 포트 `8002`, 24/7 상시 가동)
  - **웹 대시보드 및 정적 리포트 퍼블리싱**: GitHub Pages (`https://kkjhj1.github.io/kwDailyMarketReport/`)
  - **클라우드 퇴역 완료**: Render Cloud는 완전히 폐기되었으며 일체 사용하지 않음
- **주요 파이프라인 및 구성 서비스**:
  - **시황 브리핑 (`telegram_bot_runner.py`)**: 일일 장전 브리핑(오전 06:30 KST 자동 실행) 및 긴급 변동성 브리핑(장중 ±3% 이상 급등락 감지 시 발송)
  - **트래블 패스파인더 (`travel_bot_runner.py`)**: 네이버 실시간 항공/숙소/예산 검증 및 6개월 특가 브리핑
  - **입시 나침반 (`admission_bot_runner.py`)**: 주간 대입 일정 큐레이션 및 인터랙티브 상담
  - **통합 포털 허브 (`portal_bot_runner.py`)**: 회원 등급(FREE/GREEN/RED/BLACK) 기반 구독 슬롯 제어 허브
  - **웹 리포트 정적 페이지 퍼블리싱**: GitHub Pages (`https://kkjhj1.github.io/kwDailyMarketReport/`)
  - **시황 및 멀티봇 발송 현황 관리자 콘솔**: `admin_server.py`

## 2. 포트 및 개발 환경 가드 (전역 헌법 [DEV-PORT] 준수)
- **기본 관리자 포트**: `8002` (환경변수 `ADMIN_PORT=8002` 참조)
- **가용 포트 스마트 전환**:
  - 관리자 콘솔(`admin_server.py`)은 독립 Single-Page 도구이므로 `find_available_port(8002)`를 통해 8002 포트 충돌 시 즉시 다음 가용 포트(8003, 8004...)로 안전 전환한다.
  - 포트가 전환될 경우 브라우저 접속을 위해 콘솔 및 터미널에 `[PORT FALLBACK]`과 함께 실제 열린 `http://127.0.0.1:<PORT>` URL을 반드시 명시한다.
- **다중 프로젝트 포트 격리 규약**:
  - `pension_platform`: 8000
  - `market_brief_starter`: 8002
  - 향후 신규 프로젝트: 8003+

## 3. 텔레그램 봇 7대 실전 수칙 및 운영 안정성 (Mandatory Telegram Rules)
- **[TG-01] 용량 상한 & 고정 슬림 템플릿**:
  - 가시 텍스트(태그 제외) **1,800자 이내**, HTML 원문(`len`) **3,500자 이내**를 엄수한다(4,096자 절벽 및 임의 항목 삭제식 가변 축약 금지). 상세 표·설명은 웹 리포트 링크로 분리한다.
- **[TG-02] Strict HTML Escape**:
  - 모든 동적·외부 수집 문자열은 예외 없이 `html.escape()` 처리 후 주입하며, 텔레그램 허용 태그(`<b>`, `<i>`, `<code>`, `<pre>`, `<a href="...">`) 외 태그 사용을 금한다.
- **[TG-03] 1행 1정보 & 인앱 웹(`360px`) 연동**:
  - 모바일 3초 스캔이 가능하도록 `이모지 + 핵심명사 + 수치(Bold)` 단문으로 구성하고 장황한 괄호·원본 URL 노출을 금한다.
  - 텔레그램에서 연결되는 웹 리포트는 `360px` 뷰포트 가로 오버플로우 제로(`minmax(0, 1fr)`, 터치 타깃 `44px` 이상)를 보장한다.
- **[TG-04] 0.1초 콜백 응답(MUST) & `64-Byte` 규약**:
  - 인라인 버튼 수신 즉시 `answerCallbackQuery`를 **무조건 선행 호출**하여 로딩 스피너를 제거한다.
  - `callback_data`는 **64 bytes 이하 영문 축약 코드(`도메인:액션:ID`)**만 사용하며, 화면 수정은 `_safe_edit_or_send`(`message is not modified` 무시 및 폴백 포함)를 통하고 파괴적 액션(구독 해지 등)은 제자리 2단계 확인(`In-Place Confirm`)을 거친다.
- **[TG-05] Safe Dispatcher (429 Rate Limit 방어)**:
  - 다수 수신자 발송 시 단순 루프 직발송을 금하며, `SafeTelegramDispatcher`와 토큰 버킷 제한기(전역 `25 msg/s`, 동일 채팅 `1.0s/chat`) 및 지수 백오프 재시도를 적용하여 Rate Limit(429) 및 네트워크 타임아웃 중복 발송을 원천 차단한다.
- **[TG-06] 토큰 프리 `--demo` & 운영 자산 무오염**:
  - 모든 봇 러너는 실제 토큰 없이 1초 내 콘솔 검증이 가능한 `--demo` 플래그를 제공해야 하며, 데모·테스트 실행 중 운영 DB(`*.sqlite3`) 및 배포 폴더(`reports/`)에 파일을 기록하지 않는다(`:memory:` 또는 `scratch/` 격리).
  - *[현행 과제]*: `travel_bot_runner.py`와 `portal_bot_runner.py`는 `--demo` 완비 상태이나, 미구현된 `telegram_bot_runner.py`와 `admission_bot_runner.py`는 향후 L2 작업 시 `--demo`를 최우선 구현 과제로 둔다.
- **[TG-07] 제로 브레이킹(Zero-Breaking Policy) & 선행 컴파일 검증**:
  - GitHub Actions에서 실행 중인 기존 `telegram_bot_runner.py --auto-schedule --live-naver` 등 크론 CLI 워크플로우에 영향을 주지 않도록 하위 호환성을 100% 보장한다.
  - 코드 수정 직후 반드시 `python -m py_compile` 및 관련 `pytest` 통과를 확인한 뒤에만 완료 보고하며, 완료 보고 시 실제 실행 명령과 실측 결과를 반드시 제시한다(미실행 검증 보고 금지).

## 4. 멀티 AI 2단계 강제 파이프라인 (Two-Phase Multi-AI Protocol)

### 4.1 위험도별 라우팅 및 게이트 원칙
- **L0 (문구, 주석, 오타, 마크다운)**: Gemini 단독 처리 (외부 호출 전면 금지, 토큰 소모 0%).
- **L1 (단일 파일 국소 버그, 단일 테스트 보완)**: GPT 설계 생략, Gemini ➔ Claude(Write Lease) 직행 구현.
- **L2/L3 (비즈니스 로직, 메시지 템플릿, 웹 UI, DB 스키마, 기획안 리팩토링, 대규모 리팩토링)**:
  - **[Phase 1: 기획·아키텍처 자율 선행 감사 (Non-Destructive)]**:
    1. **Gemini (요구정리)**: 사용자 요청 분석, 1차 명세 및 `scratch/handoff_to_gpt.md` 작성.
    2. **GPT (수석 감사관 - codex CLI)**: 산식·DB·상태·불변조건·UI/UX 감사를 **사용자 승인 대기 없이 즉시 자동 실행** (`-s read-only`).
    3. **Fail-Closed 원칙**: GPT 감사 산출물(`scratch/gpt_architecture.md`)이 없는 L2/L3 계획의 임의 제안·확정·사용자 승인 요청은 일체 금지한다 (`AUDIT_INCOMPLETE`).
    4. **Gemini (계획서 확정 및 보고)**: GPT 감사가 반영된 **「확정 계획서(Artifact)」**를 생성하여 사용자에게 보고.
  - **[Phase 2: 실제 개발 착수 게이트 (Destructive / Two-Phase Gate [AUTH-01])]**:
    5. **사용자 승인 게이트**: 최신 GPT 감사 계획서에 대한 사용자의 명확한 자연어 개발 착수 승인(예: "개발 진행해", "개발진행하라", "그대로 진행") 확인 후 착수 (단순 맞장구는 승인으로 보지 않음).
    6. **Gemini 코딩 절대 락(Lock)**: Phase 2 진입 즉시 Gemini의 모든 파일 수정 도구(`edit_file`, `write_file`) 및 쉘 파일 수정 명령 호출을 물리적으로 엄금한다.
    7. **Claude (시니어 구현 - claude CLI)**: Single-Writer Lease 권한을 부여받아 실제 코드 작성 및 단위 테스트 실행.
    8. **Claude 실패 시 대체 금지**: Claude 구현 실패 시 Gemini가 코딩을 가로채지 못하며, `IMPLEMENTATION_FAILED` 또는 `BLOCKED` 상태로 사용자에게 즉각 보고한다.
    9. **Gemini (최종 판결 & 통합)**: git diff 기계적 신선 대조(Fresh Read) 및 7대 수칙 검증 후 최종 통합 완료.

### 4.2 외부 에이전트 CLI 실행 표준 및 Handoff 규약

#### ① GPT 아키텍처 수석 설계 호출 (Read-Only)
```powershell
Get-Content -Raw "scratch\handoff_to_gpt.md" -Encoding UTF8 | codex exec --skip-git-repo-check -s read-only -o "scratch\gpt_architecture.md" -
```
- **Handoff 및 산출물**: 전체 소스 복사를 금하고 대상 인터페이스만 1~2KB로 압축 전달(`Targeted Handoff`). 결과물에는 불변조건(Invariants), DB/수식 계약, 엣지케이스, 구현 금지사항을 명시한다.

#### ② Claude 시니어 구현 호출 (Lease 범위 내 Write 허용)
```powershell
Get-Content -Raw "scratch\handoff_to_claude.md" -Encoding UTF8 | claude -p --tools "Read,Grep,Glob,Edit,Write,Bash" --permission-prompts none --no-session-persistence
```
- **Claude 작업 수칙**:
  - `allowed_paths`에 명시된 파일만 수정하며, `forbidden_paths`(.env, *.sqlite3, reports/)는 절대 수정 금지.
  - 수정 직후 스스로 `python -m py_compile` 및 관련 `pytest` 통과 확인 후 결과 요약 제출.
  - 완료 시 변경 내역과 함께 **"설계와 달라진 점(Drift Self-Report)"**을 필수 제출.
  - Claude 작업 중 Gemini는 파일 수정을 일체 중단하고 대기 (Single-Writer Lease).

### 4.3 Git 상태 동기화 및 최종 판결 (Fresh Read & Anti-Drift)
- 작업 착수 전 `git rev-parse HEAD` 및 `git status --short`로 기준점을 고정한다.
- Claude 작업 반환 즉시 Gemini는 실제 `git diff`를 통해 변경분을 신선하게 대조(Fresh Read)하고, 7대 수칙(`[TG-01]`~`[TG-07]`) 및 불변조건 준수 여부를 최종 판정하여 통합한다.
- **Fail-Closed**: L3 배포 및 운영 DB 마이그레이션은 외부 감사가 누락되거나 테스트가 실패한 경우 즉시 작업을 중단한다.
