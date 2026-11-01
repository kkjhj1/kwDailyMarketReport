# 🚀 Portal Hub Bot (통합 메인메뉴 봇 아키텍처 & 클로드 협업 가이드)

## 1. 개요 및 목적
- **목적**: 시황봇, 입시정보봇, 여행봇 등 서로 다른 성격의 봇들을 한곳에서 안내하고, 사용자 등급(프리/그린/레드/블랙)에 따라 구독 슬롯을 제어하는 **중앙 통합 포털 봇(Hub Bot)**입니다.
- **철학**: 별도의 무거운 웹페이지 없이 텔레그램 네이티브 기능(인라인 키보드 + `editMessageText` 실시간 갱신 + `show_alert=True` 팝업 알림)만으로 모바일 앱 수준의 즉각적인 반응성(0.1초)을 제공합니다.

---

## 2. 등급(Tier) 체계 및 구독 슬롯 규칙

| 등급 (Tier) | 뱃지 | 테마 이모지 | 최대 구독 슬롯 | 주요 혜택 및 권한 |
| :--- | :---: | :---: | :---: | :--- |
| **프리 (FREE)** | `⚪ [FREE]` | `◽` | **최대 1개** | 기본 1개 서비스 무료 구독 및 체험 |
| **그린 (GREEN)** | `🟢 [GREEN]` | `🟩` | **최대 2개** | 2개 맞춤 봇 선택 구독 + 정기 리포트 |
| **레드 (RED)** | `🔴 [RED]` | `🟥` | **최대 3개** | 현재 3개 서비스 전체 이용 + 긴급 속보 알림 |
| **블랙 (BLACK)** | `⚫ [BLACK VIP]` | `⬛★` | **무제한 (All)** | 전 서비스 무제한 + 신규 베타 봇 우선 초대 |

---

## 3. UI/UX 화면 구성

### ① 메인 대시보드 (`/start`)
```text
👋 안녕하세요, 홍길동님!
통합 인텔리전스 허브(Hub)에 오신 것을 환영합니다.

━━━━━━━━━━━━━━━━━━━━
🏅 나의 회원 등급: 🟢 [GREEN]
📦 구독 가능 슬롯: 최대 2개
━━━━━━━━━━━━━━━━━━━━

[ ⚙️ 내 구독 & 슬롯 관리 ]
[ 🔍 봇 서비스 둘러보기 (3종) ]  [ 💎 등급 혜택 안내 ]
[ ❓ 이용 도움말 ]
```

### ② 내 구독 & 슬롯 센터 (`/subs` 또는 버튼 클릭 시)
```text
📋 [홍길동] 님의 구독 센터
━━━━━━━━━━━━━━━━━━━━
🏅 회원 등급: 🟢 [GREEN]
📊 구독 슬롯: [ 🟩 🟩 ] 2/2개 사용 중 (FULL)
━━━━━━━━━━━━━━━━━━━━

💡 버튼을 누르면 즉시 구독/해지 상태가 전환됩니다.

[ 🟢 [구독중] 📈 스마트 시황 브리핑 ]
[ 🟢 [구독중] 🎓 대입/수시 입시 나침반 ]
[ ⚪ [미구독] ✈️ 트래블 패스파인더 ]   <-- 클릭 시 한도 초과 네이티브 팝업 발생!
[ 💎 등급 업그레이드 (슬롯 확장) ]
[ 🔙 메인 메뉴로 돌아가기 ]
```

---

## 4. 파일 구조 및 모듈 역할

```text
market_brief_starter/
├── portal_hub/
│   ├── __init__.py          # 패키지 익스포트
│   ├── models.py            # UserTier(Enum), BotServiceType(Enum), UserProfile, SubscriptionRecord
│   ├── db.py                # SQLite 기반 원자적 구독 토글 및 슬롯 한도 검증 로직 (portal.sqlite3)
│   ├── telegram.py          # 경량 순수 파이썬(urllib) Telegram Client (editMessageText, show_alert 지원)
│   └── interactive.py       # 화면 렌더러(메인, 구독센터, 카탈로그, 등급안내) 및 콜백 쿼리 라우터
├── portal_bot_runner.py     # CLI 실행기 (--demo, --poll, --list-users, --set-tier)
└── PORTAL_BOT_GUIDE.md      # 본 아키텍처 문서
```

---

## 5. 실행 및 테스트 방법

### (1) 콘솔 데모 시뮬레이션 (봇 토큰 없이 즉시 검증 가능)
```bash
python portal_bot_runner.py --demo
```
- 프리(1개), 그린(2개), 레드(3개), 블랙(무제한) 회원의 슬롯 검증 및 게이지 렌더링 확인

### (2) 실제 텔레그램 봇 가동 (롱폴링)
1. `.env` 파일에 토큰 설정:
   ```env
   PORTAL_TELEGRAM_BOT_TOKEN="123456789:ABCdefGhI..."
   ADMIN_TELEGRAM_HANDLE="my_telegram_id"
   ```
2. 봇 구동:
   ```bash
   python portal_bot_runner.py --poll
   ```

### (3) 관리자 CLI 명령어
```bash
# 등록된 사용자 및 구독 현황 조회
python portal_bot_runner.py --list-users

# 특정 사용자의 등급 변경
python portal_bot_runner.py --set-tier 123456789 RED
python portal_bot_runner.py --set-tier 123456789 BLACK
```

---

## 6. 클로드(Claude)와의 협업 및 확장 포인트 (Next Steps)
클로드와 다음 작업을 진행할 때 아래 질문/작업을 요청하시면 매끄럽게 연결됩니다:
1. **발송 파이프라인 연동**: 기존 `telegram_bot_runner.py`, `admission_bot_runner.py`, `travel_bot_runner.py`가 스케줄 발송 시 `portal.sqlite3`의 `list_active_chat_ids(bot_type)`를 참조하도록 단 3줄 연동 코드 추가.
2. **웹 결제/자동 승인 연동**: 사용자가 토스/아임포트/Stripe 등으로 결제 시 Webhook을 받아 자동으로 `set_user_tier`를 실행하는 FastAPI 엔드포인트 구현.
3. **텔레그램 미니앱(TMA) 확장**: 원할 경우 `render_subscription_view`를 HTML5 모바일 웹앱 대시보드로 확장.
