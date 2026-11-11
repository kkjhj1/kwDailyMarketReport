#!/usr/bin/env bash
APP_DIR="/home/ubuntu/kwDailyMarketReport"
cd "$APP_DIR" || exit 1

# .env 환경변수 로드
if [ -f "$APP_DIR/.env" ]; then
    set -a
    source "$APP_DIR/.env"
    set +a
fi

TASK="$1"
LOG_FILE="$APP_DIR/logs/${TASK}_$(date +\%Y\%m\%d).log"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting task: $TASK" >> "$LOG_FILE"

case "$TASK" in
    market_auto)
        "$APP_DIR/venv/bin/python" telegram_bot_runner.py --auto-schedule --live-naver >> "$LOG_FILE" 2>&1
        ;;
    market_morning)
        "$APP_DIR/venv/bin/python" telegram_bot_runner.py --send-now morning --live-naver >> "$LOG_FILE" 2>&1
        ;;
    market_closing)
        "$APP_DIR/venv/bin/python" telegram_bot_runner.py --send-now closing --live-naver >> "$LOG_FILE" 2>&1
        ;;
    market_check_urgent)
        "$APP_DIR/venv/bin/python" telegram_bot_runner.py --check-urgent --live-naver >> "$LOG_FILE" 2>&1
        ;;
    travel_weekly)
        "$APP_DIR/venv/bin/python" travel_bot_runner.py --live-naver --send-brief >> "$LOG_FILE" 2>&1
        ;;
    travel_watches)
        # 여행 특가 알림 tick: OS cron 전용 (multibot.service 기동 시 자동 실행 금지 [PROD-LIVE])
        "$APP_DIR/venv/bin/python" travel_bot_runner.py --check-watches --live-naver >> "$LOG_FILE" 2>&1
        ;;
    admission_collect)
        "$APP_DIR/venv/bin/python" admission_bot_runner.py --collect >> "$LOG_FILE" 2>&1
        ;;
    admission_weekly)
        "$APP_DIR/venv/bin/python" admission_bot_runner.py --collect >> "$LOG_FILE" 2>&1
        "$APP_DIR/venv/bin/python" admission_bot_runner.py --dispatch --grade ALL >> "$LOG_FILE" 2>&1
        ;;
    *)
        echo "Unknown task: $TASK" >> "$LOG_FILE"
        exit 1
        ;;
esac

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Finished task: $TASK" >> "$LOG_FILE"
