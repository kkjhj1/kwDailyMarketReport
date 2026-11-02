# ============================================================
# PROJECT CONSTITUTION: Multi-Bot Intelligence Platform
# Scope: Project-Specific (c:\Users\환진\.gemini\antigravity\scratch\market_brief_starter)
# Version: 2.1 (Telegram 7 Golden Rules & Multi-AI 1-Pass Audit Edition)
# ============================================================

## 1. 프로젝트 개요 및 멀티봇 파이프라인
- **프로젝트 명**: `kwDailyMarketReport` / `market_brief_starter` (Multi-Bot Platform v2.0)
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
- **[TG-07] 제로 브레이킹(Zero-Breaking Policy) & 선행 컴파일 검증**:
  - GitHub Actions에서 실행 중인 기존 `telegram_bot_runner.py --auto-schedule --live-naver` 등 크론 CLI 워크플로우에 영향을 주지 않도록 하위 호환성을 100% 보장한다.
  - 코드 수정 직후 반드시 `python -m py_compile` 및 관련 `pytest` 통과를 확인한 뒤에만 완료 보고한다.

## 4. 멀티 AI 역할 분담 및 1-Pass 최종 감사 규약 (Multi-AI Audit Protocol)
- **3인 역할 분담 (전역 헌법 `[META-03]` 준수)**:
  - **총괄 판결 & 단일 Writer (`Gemini` / 주 세션)**: 사용자 맥락 보존, 초안·코드·테스트 작성 및 기계적 실행, 외부 감사 의견의 `1-Pass` 최종 수용/기각 판결 및 실제 파일 수정 전담.
  - **기획·UI/UX 최종 감사관 (`GPT` / `codex`)**: 신규 기획안·메시지 템플릿·모바일 웹 UI 설계(L2/L3) 확정 전 가독성·프롬프트·UX 누락을 1회 최종 감사한다.
  - **개발·테스트 최종 감사관 (`Claude` / `claude`)**: 신규 기능·핵심 로직 구현 및 테스트 개편(L2/L3) 시, **기계적 선행 검증(`py_compile` + `pytest` + `--demo`) 통과 직후** 코드 결함·제3장 7대 수칙 위반·엣지케이스 누락을 1회 최종 감사한다.
- **4대 운영 안전핀 (Mandatory Safeguards)**:
  1. **위험도 비례 발동**: 단순 오타·문구·소규모 패치(L0/L1)는 단독 즉시 완결하며, **구조적 기획·개발·테스트(L2/L3) 또는 사용자 명시 요청 시**에만 발동한다.
  2. **Read-Only 격리 실행 & 재귀 금지**: Handoff 입출력은 `scratch/`로 격리하며, 파일 수정·쉘 실행(재귀 호출)이 물리적으로 차단된 읽기 전용 명령어만 사용한다:
     - **GPT 기획 감사**: `Get-Content -Raw "scratch\handoff_to_gpt.md" -Encoding UTF8 | codex exec --skip-git-repo-check -s read-only -o "scratch\gpt_audit_result.md" -`
     - **Claude 개발·테스트 감사**: `Get-Content -Raw "scratch\handoff_to_claude.md" -Encoding UTF8 | claude -p --tools "Read,Grep,Glob" --permission-prompts none --no-session-persistence`
  3. **`1-Pass` 종결 & 기준 서열**: 감사 결과 중 실제 결함·헌법 위반(`Critical`/`Major`)만 즉시 반영하고 맥락 외 취향 지적(`Minor`)은 기각 사유만 요약해 **재호출 없이 1라운드에 종결**한다(기획·개발 피드백 충돌 시 제3장 7대 수칙 우선).
  4. **`Fail-Open` 비상 우회**: 외부 CLI 쿼터 소진·인증 만료·90초 타임아웃 발생 시 작업을 멈추지 않고 완료 보고서에 `[외부 감사 생략: 사유]`를 정직하게 명시한다.
