# ============================================================
# PROJECT CONSTITUTION: Market Brief Starter (시황 브리핑 & 관리 콘솔)
# Scope: Project-Specific (c:\Users\환진\.gemini\antigravity\scratch\market_brief_starter)
# ============================================================

## 1. 프로젝트 개요 및 핵심 인프라
- **프로젝트 명**: `kwDailyMarketReport` / `market_brief_starter`
- **주요 파이프라인**:
  - 일일 장전 시황 브리핑 (오전 06:30 KST 자동 실행)
  - 긴급 변동성 브리핑 (장중 ±3% 이상 급등락 감지 시 발송)
  - 웹 리포트 정적 페이지 퍼블리싱 (GitHub Pages: `https://kkjhj1.github.io/kwDailyMarketReport/`)
  - 시황 발송 현황 관리자 콘솔 (`admin_server.py`)

## 2. 포트 및 개발 환경 가드 (전역 헌법 [DEV-PORT] 준수)
- **기본 관리자 포트**: `8002` (환경변수 `ADMIN_PORT=8002` 참조)
- **가용 포트 스마트 전환**: 
  - 관리자 콘솔(`admin_server.py`)은 독립 Single-Page 도구이므로 `find_available_port(8002)`를 통해 8002 포트 충돌 시 즉시 다음 가용 포트(8003, 8004...)로 안전 전환한다.
  - 포트가 전환될 경우 브라우저 접속을 위해 콘솔 및 터미널에 `[PORT FALLBACK]`과 함께 실제 열린 `http://127.0.0.1:<PORT>` URL을 반드시 명시한다.
- **다중 프로젝트 포트 격리 규약**:
  - `pension_platform`: 8000
  - `market_brief_starter`: 8002
  - 향후 신규 프로젝트: 8003+

## 3. 운영 안정성 원칙 (Zero-Breaking Policy)
- GitHub Actions에서 실행 중인 기존 `telegram_bot_runner.py --auto-schedule --live-naver` 워크플로우에 영향을 주지 않도록 하위 호환성을 100% 보장한다.
- 텔레그램 발송 시 `SafeTelegramDispatcher`와 토큰 버킷 제한기(25 msg/s, 1.0s/chat)를 적용하여 Rate Limit(429) 및 네트워크 타임아웃 중복 발송을 원천 차단한다.
