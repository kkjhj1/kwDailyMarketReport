# Market Briefing Starter v0.3 (Production-Ready Architecture)

채널 독립형 금융시장 브리핑(06:30·15:40 정기 시황) 및 장중 ±3% 급변동 알림 시스템입니다.
첨부해주신 v0.2 코드를 기반으로 **실운영 아키텍처 요건**과 **직관적인 단일 파일 HTML 상세 리포트 생성기**를 통합했습니다.

## 1. v0.3 주요 업그레이드 내역

| 모듈 | 구현 기능 | 상태 |
| :--- | :--- | :--- |
| `market_brief/market_data/naver_provider.py` | **네이버 금융(증시·시장지표) 5대 자산군 수집기** 및 장애 시 자동 폴백(`CompositeMarketDataProvider`) | 구현·테스트 완료 |
| `market_brief/market_data/validation.py` | **데이터 정합성 검증(`SanityChecker`)**: 0원·결측치·단일 틱 ±8% 이상 비정상 스파이크 차단, ±3% 급변동 N회 연속 확인 | 구현·테스트 완료 |
| `market_brief/calendar_manager.py` | **시장별 거래일·세션 캘린더(`MarketCalendar`)**: 한국(KRX) 및 미국(서머타임 자동 감지) 정규장 시간·휴장일 판정 | 구현·테스트 완료 |
| `market_brief/report_engine.py` | **결정론적 지표 계산 + MCP 컨텍스트 빌더 + AI 환각 수치 검증(`verify_numeric_fidelity`)** | 구현·테스트 완료 |
| `market_brief/web_report.py` | **직관적인 반응형 HTML 상세 리포트 생성기(`HTMLReportRenderer`)** 및 서명된 보안 링크(`HMAC Signed URL`) 발급 | 구현·테스트 완료 |
| `market_brief/gateway.py` & `alert_store.py` | **지수 백오프 재시도 게이트웨이(`DeliveryGateway`)**, 텔레그램 인라인 리포트 버튼, 카카오 알림톡 연동 규격, 발송 감사 로그 | 구현·테스트 완료 |
| `Dockerfile` & `docker-compose.yml` | 사내 서버 또는 클라우드에서 24시간 무중단 실행(`restart: always`)을 위한 컨테이너 배포 설정 | 구성 완료 |

## 2. 빠른 실행 및 테스트 (Python 3.10+)

```bash
# 1) 전체 단위 및 통합 테스트 실행 (기존 12개 + 신규 6개 = 총 18개 테스트)
python3 -m unittest discover -s tests -v

# 2) 오프라인 시세 수집 및 검증 데모
python3 ingest_demo.py

# 3) End-to-End 브리핑 생성 + 상세 HTML 리포트 파일 생성 데모
python3 demo.py

# 4) 시황 발송 현황 관리자 콘솔 웹페이지 실행 (http://localhost:8002)
python telegram_bot_runner.py --admin
# 또는
python admin_server.py
```

`python telegram_bot_runner.py --admin`을 실행하면 브라우저에서 **수신자 관리, 원클릭 즉시 발송, 중복 방지, 발송 이력 및 HTML 리포트 열람, 긴급 임계치 설정**이 가능한 관리자 콘솔(`http://localhost:8002`)이 열립니다. (다른 프로세스가 8002를 점유 중인 경우 자동으로 다음 빈 포트로 충돌 없이 전환됩니다.)
