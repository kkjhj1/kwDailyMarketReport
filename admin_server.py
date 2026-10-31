"""FastAPI Admin Console for Multi-Bot Subscription Platform & Daily Market Brief v2.0."""
from __future__ import annotations

from contextlib import asynccontextmanager
import json
import os
import sys
from datetime import datetime
from pathlib import Path
import urllib.request
import urllib.error
from zoneinfo import ZoneInfo

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn

from market_brief.admin_ui import ADMIN_DASHBOARD_HTML
from market_brief.channels.telegram import TelegramChannel
from market_brief.channels.telegram_limiter import SafeTelegramDispatcher, TelegramRateLimiter, sync_telegram_updates
from market_brief.market_data import CompositeMarketDataProvider, FixtureProvider, NaverFinanceProvider
from market_brief.market_data.naver_provider import fetch_naver_market_news
from market_brief.multi_bot_manager import DynamicBotManager, MultiBotInteractionRouter, TelegramBotTransport
from market_brief.pipeline import MarketBriefingPipeline, DEFAULT_SYMBOLS
from market_brief.storage.db import DEFAULT_THRESHOLDS, MarketBriefDatabase
from market_brief.storage.models import JobFinalStatus, Recipient
from market_brief.subscription_engine import SubscriptionEngine

try:
    from travel_brief.flight_engine import FlightEngine
    from travel_brief.budget_calculator import BudgetCalculator
    from travel_bot_runner import run_briefing as run_travel_briefing, format_telegram_briefing
    _HAS_TRAVEL_MODULE = True
except ImportError:
    _HAS_TRAVEL_MODULE = False

try:
    from admission_brief.models import (
        AdmissionBriefing,
        AdmissionEvent,
        BriefingType,
        ParentUserProfile,
        TargetGrade,
        TargetRegion,
    )
    from admission_brief.storage.db import AdmissionDatabase
    from admission_brief.ai_curator import AICurator
    from admission_brief.collector import AdmissionCollector
    from admission_brief.channels.telegram import TelegramChannel as AdmissionTelegramChannel, format_admission_html
    from admission_brief.channels.telegram_limiter import SafeTelegramDispatcher as AdmissionSafeDispatcher
    _HAS_ADMISSION_MODULE = True
except ImportError:
    _HAS_ADMISSION_MODULE = False

ROOT = Path(__file__).resolve().parent
KST = ZoneInfo("Asia/Seoul")


# =========================================================================
# Request Schemas (Legacy + Travel + Multi-Bot Platform v2.0)
# =========================================================================


class DispatchRequest(BaseModel):
    topic: str = "FINANCE"
    kind: str = "morning"  # morning, closing, urgent
    is_forced: bool = False
    force_reason: str | None = None


class RecipientRequest(BaseModel):
    chat_id: str
    name: str
    channel: str = "TELEGRAM"
    group_tag: str = "GENERAL"
    subscribed_topics: str = "FINANCE"


class RecipientNameUpdateRequest(BaseModel):
    name: str


class TravelDispatchRequest(BaseModel):
    dry_run: bool = False
    dest_code: str | None = None
    max_budget: int | None = None
    recipients: list[str] | None = None


class AdmissionEventCreateRequest(BaseModel):
    title: str
    organizer: str
    target_grade: str = "MIDDLE"
    target_region: str = "ALL"
    is_online_live: bool = False
    event_type: str = "TICKETING_ALERT"
    event_date_str: str
    booking_url: str | None = None
    is_first_come: bool = False
    summary_3lines: list[str] = []
    so_what: str = ""
    action_todos: list[str] = []


class AdmissionDispatchRequest(BaseModel):
    event_id: str | None = None
    target_grade: str = "MIDDLE"
    target_region: str = "ALL"
    dry_run: bool = False
    silent: bool | None = None


class ThresholdRequest(BaseModel):
    thresholds: dict


class BotUpsertRequest(BaseModel):
    bot_id: str
    name: str
    bot_type: str = "INFO"  # 'MENU' | 'INFO'
    telegram_username: str = ""
    token: str | None = None
    clear_token: bool = False
    category: str = "일반"
    icon_emoji: str = "🤖"
    description: str = ""
    topic_code: str = "GENERAL"
    is_active: bool = True
    sort_order: int = 10


class PlatformUserCreateRequest(BaseModel):
    telegram_user_id: str
    full_name: str
    username: str = ""
    tier: str = "WHITE"
    tier_expires_at: str | None = None
    status: str = "ACTIVE"
    memo: str = ""


class PlatformUserUpdateRequest(BaseModel):
    full_name: str | None = None
    username: str | None = None
    status: str | None = None
    memo: str | None = None


class PlatformTierUpdateRequest(BaseModel):
    tier: str
    tier_expires_at: str | None = None
    downgrade_mode: str = "GRACE"  # 'GRACE' | 'KEEP_RECENT' | 'KEEP_SELECTED' | 'CANCEL_ALL'
    keep_bot_ids: list[str] | None = None
    memo: str | None = None


class SubscriptionActionRequest(BaseModel):
    telegram_user_id: str
    bot_id: str
    bypass_quota: bool = False


class SubscriptionSwapRequest(BaseModel):
    telegram_user_id: str
    cancel_bot_id: str
    new_bot_id: str


class SimulateInteractionRequest(BaseModel):
    bot_id: str
    telegram_user_id: str
    full_name: str = ""
    username: str = ""
    text: str | None = None
    callback_data: str | None = None


class BotBroadcastRequest(BaseModel):
    bot_id: str
    message_html: str


class BriefingPreviewRequest(BaseModel):
    preset: str | None = None


class BriefingTestDispatchRequest(BaseModel):
    content_html: str
    inline_keyboard: list[list[dict[str, str]]] | None = None
    target_chat_id: str | None = None


class BriefingSubscriberDispatchRequest(BaseModel):
    content_html: str
    inline_keyboard: list[list[dict[str, str]]] | None = None


def fetch_telegram_profile_name(token: str, chat_id: str) -> str:
    if not token or not chat_id:
        return f"수신자_{chat_id[-4:]}" if chat_id else "수신자"
    url = f"https://api.telegram.org/bot{token}/getChat?chat_id={chat_id}"
    req = urllib.request.Request(url, headers={"User-Agent": "MarketBriefAdmin"})
    try:
        with urllib.request.urlopen(req, timeout=5) as res:
            data = json.loads(res.read().decode())
            r = data.get("result", {})
            first = r.get("first_name", "").strip()
            last = r.get("last_name", "").strip()
            title = r.get("title", "").strip()
            username = r.get("username", "").strip()
            if title:
                return title
            if last and first:
                if any("\uac00" <= c <= "\ud7a3" for c in last + first):
                    return f"{last}{first}"
                return f"{first} {last}"
            if first:
                return first
            if username:
                return f"@{username}"
            return f"수신자_{chat_id[-4:]}"
    except Exception:
        return f"수신자_{chat_id[-4:]}"


def create_admin_app(
    db_path: str = "alerts.sqlite3",
    env_path: Path | None = None,
    enable_bot_workers: bool = False,
) -> FastAPI:
    env_file = env_path or (ROOT / ".env")
    db = MarketBriefDatabase(db_path=db_path, env_path=env_file)
    sub_engine = SubscriptionEngine(db)
    router = MultiBotInteractionRouter(sub_engine)
    bot_manager = DynamicBotManager(sub_engine, router)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if enable_bot_workers:
            await bot_manager.start()
        try:
            yield
        finally:
            if enable_bot_workers:
                await bot_manager.stop()

    app = FastAPI(title="Multi-Bot Subscription & Market Brief Admin Console", version="2.0.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    reports_dir = ROOT / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/reports", StaticFiles(directory=str(reports_dir)), name="reports")

    app.state.db = db
    app.state.engine = sub_engine
    app.state.router = router
    app.state.bot_manager = bot_manager

    def get_bot_token() -> str:
        brief_bot = sub_engine.get_bot("brief_bot")
        if brief_bot and brief_bot.token and brief_bot.token.strip():
            return brief_bot.token.strip()
        token = ("" if env_path else os.environ.get("TELEGRAM_BOT_TOKEN", "")).strip()
        if not token and env_file.exists():
            for line in env_file.read_text(encoding="utf-8-sig").splitlines():
                if line.startswith("TELEGRAM_BOT_TOKEN="):
                    token = line.split("=", 1)[1].strip().strip('"').strip("'")
        return token

    def get_report_base_url() -> str:
        configured = os.environ.get("REPORT_BASE_URL", "").strip()
        if not configured and env_file.exists():
            for line in env_file.read_text(encoding="utf-8-sig").splitlines():
                if line.startswith("REPORT_BASE_URL="):
                    configured = line.split("=", 1)[1].strip().strip('"').strip("'")
        return configured or "https://kkjhj1.github.io/kwDailyMarketReport"

    def get_github_repo() -> str:
        repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
        if not repo and env_file.exists():
            for line in env_file.read_text(encoding="utf-8-sig").splitlines():
                if line.startswith("GITHUB_REPOSITORY="):
                    repo = line.split("=", 1)[1].strip().strip('"').strip("'")
        return repo or "kkjhj1/kwDailyMarketReport"

    def get_github_token() -> str:
        token = ("" if env_path else os.environ.get("GITHUB_TOKEN", "")).strip()
        if not token and env_file.exists():
            for line in env_file.read_text(encoding="utf-8-sig").splitlines():
                if line.startswith("GITHUB_TOKEN="):
                    token = line.split("=", 1)[1].strip().strip('"').strip("'")
        return token

    def get_github_variable(var_name: str) -> tuple[bool, str | None, str]:
        gh_token = get_github_token()
        if not gh_token:
            return False, None, "GITHUB_TOKEN이 설정되지 않았습니다."
        repo = get_github_repo()
        url = f"https://api.github.com/repos/{repo}/actions/variables/{var_name}"
        headers = {
            "Authorization": f"Bearer {gh_token}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "MarketBriefAdmin",
        }
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=10) as res:
                if res.status == 200:
                    data = json.loads(res.read().decode())
                    return True, str(data.get("value", "")), "조회 성공"
                return False, None, f"상태 코드: {res.status}"
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return False, None, "변수 없음(404)"
            err_body = e.read().decode(errors="replace")
            return False, None, f"GitHub API 오류 ({e.code}): {err_body}"
        except Exception as exc:
            return False, None, f"연결 오류: {exc}"

    def sync_github_variable(var_name: str, value: str) -> tuple[bool, str]:
        gh_token = get_github_token()
        if not gh_token:
            return False, "GITHUB_TOKEN이 설정되지 않았습니다."
        repo = get_github_repo()
        headers = {
            "Authorization": f"Bearer {gh_token}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "MarketBriefAdmin",
            "Content-Type": "application/json",
        }
        exists, _, _ = get_github_variable(var_name)
        if exists:
            url = f"https://api.github.com/repos/{repo}/actions/variables/{var_name}"
            payload = json.dumps({"name": var_name, "value": value}).encode("utf-8")
            req = urllib.request.Request(url, data=payload, headers=headers, method="PATCH")
        else:
            url = f"https://api.github.com/repos/{repo}/actions/variables"
            payload = json.dumps({"name": var_name, "value": value}).encode("utf-8")
            req = urllib.request.Request(url, data=payload, headers=headers, method="POST")

        try:
            with urllib.request.urlopen(req, timeout=10) as res:
                if res.status in (201, 204):
                    return True, "GitHub 크론 수신자 변수(TELEGRAM_CHAT_ID) 동기화 완료"
                return False, f"GitHub 응답 코드: {res.status}"
        except urllib.error.HTTPError as e:
            err_body = e.read().decode(errors="replace")
            return False, f"GitHub API 오류 ({e.code}): {err_body}"
        except Exception as exc:
            return False, f"연결 오류: {exc}"

    def dispatch_github_workflow(kind: str, recipients: list[str] | None = None) -> tuple[bool, str]:
        gh_token = get_github_token()
        if not gh_token:
            return False, "GITHUB_TOKEN이 설정되지 않았습니다."
        repo = get_github_repo()
        url = f"https://api.github.com/repos/{repo}/actions/workflows/market_briefing_cron.yml/dispatches"
        headers = {
            "Authorization": f"Bearer {gh_token}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "MarketBriefAdmin",
            "Content-Type": "application/json",
        }
        recipients_str = ",".join(recipients) if recipients else ""
        payload = json.dumps(
            {
                "ref": "main",
                "inputs": {
                    "mode": kind,
                    "recipients": recipients_str,
                },
            }
        ).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as res:
                if res.status in (200, 204):
                    return True, "GitHub Actions 워크플로우 원격 실행 요청 성공 (HTTP 204)"
                return False, f"GitHub 응답 코드: {res.status}"
        except urllib.error.HTTPError as e:
            err_body = e.read().decode(errors="replace")
            return False, f"GitHub API 오류 ({e.code}): {err_body}"
        except Exception as exc:
            return False, f"연결 오류: {exc}"

    # =========================================================================
    # Multi-Bot Platform API Routes (`/api/platform/*`)
    # =========================================================================

    @app.get("/api/platform/overview")
    def get_platform_overview():
        metrics = sub_engine.get_overview_metrics()
        worker_statuses = bot_manager.get_worker_statuses()
        metrics["active_workers"] = [bid for bid, st in worker_statuses.items() if st.get("worker_running")]
        return metrics

    @app.get("/api/platform/tiers")
    def get_platform_tiers():
        return {"tiers": sub_engine.get_tiers()}

    @app.get("/api/platform/bots")
    def list_platform_bots():
        bots = sub_engine.list_bots(include_raw_token=False)
        worker_statuses = bot_manager.get_worker_statuses()
        for b in bots:
            b["worker_running"] = bool(worker_statuses.get(b["bot_id"], {}).get("worker_running"))
        return {"bots": bots}

    @app.post("/api/platform/bots")
    async def upsert_platform_bot(req: BotUpsertRequest):
        username = req.telegram_username.strip().lstrip("@")
        if req.token and req.token.strip() and req.token.strip() != "__KEEP__" and not username:
            try:
                me = TelegramBotTransport(req.token.strip()).get_me()
                username = me.get("username") or ""
            except Exception:
                pass

        try:
            saved = sub_engine.upsert_bot(
                bot_id=req.bot_id,
                name=req.name,
                bot_type=req.bot_type,
                telegram_username=username,
                token=req.token,
                clear_token=req.clear_token,
                category=req.category,
                icon_emoji=req.icon_emoji,
                description=req.description,
                topic_code=req.topic_code,
                is_active=req.is_active,
                sort_order=req.sort_order,
            )
        except ValueError as err:
            raise HTTPException(status_code=400, detail=str(err))

        await bot_manager.reload_bots()
        return {"success": True, "bot": saved}

    @app.post("/api/platform/bots/{bot_id}/toggle")
    async def toggle_platform_bot(bot_id: str):
        try:
            res = sub_engine.toggle_bot(bot_id)
        except KeyError as err:
            raise HTTPException(status_code=404, detail=str(err))
        await bot_manager.reload_bots()
        return {"success": True, **res}

    @app.post("/api/platform/bots/{bot_id}/test")
    def test_platform_bot(bot_id: str):
        bot = sub_engine.get_bot(bot_id)
        if not bot:
            raise HTTPException(status_code=404, detail="봇을 찾을 수 없습니다.")
        if not bot.token or not bot.token.strip():
            raise HTTPException(status_code=400, detail="해당 봇에 등록된 토큰이 없습니다. 먼저 토큰을 입력해 주세요.")
        try:
            me = TelegramBotTransport(bot.token).get_me()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"텔레그램 getMe 실패: {exc}")

        resolved_uname = (me.get("username") or "").strip()
        if resolved_uname and resolved_uname != (bot.telegram_username or "").lstrip("@"):
            sub_engine.upsert_bot(
                bot_id=bot.bot_id,
                name=bot.name,
                bot_type=bot.bot_type,
                telegram_username=resolved_uname,
                token="__KEEP__",
                category=bot.category,
                icon_emoji=bot.icon_emoji,
                description=bot.description,
                topic_code=bot.topic_code,
                is_active=bot.is_active,
                sort_order=bot.sort_order,
            )
        return {"success": True, "bot_profile": me}

    @app.post("/api/platform/bots/sync-all")
    def sync_all_platform_bots():
        bots = sub_engine.list_bots(include_raw_token=True)
        total_processed = 0
        details = []
        for b in bots:
            if b["is_active"] and b.get("token") and b["token"].strip():
                try:
                    r = bot_manager.sync_bot_updates_once(b["bot_id"])
                    total_processed += r.get("processed", 0)
                    details.append(r)
                except Exception as exc:
                    details.append({"bot_id": b["bot_id"], "error": str(exc)})
        return {"success": True, "total_processed": total_processed, "details": details}

    @app.post("/api/platform/bots/{bot_id}/sync")
    def sync_single_platform_bot(bot_id: str):
        bot = sub_engine.get_bot(bot_id)
        if not bot:
            raise HTTPException(status_code=404, detail="봇을 찾을 수 없습니다.")
        if not bot.token or not bot.token.strip():
            raise HTTPException(status_code=400, detail="토큰이 등록되지 않은 봇입니다.")
        try:
            return bot_manager.sync_bot_updates_once(bot_id)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"텔레그램 업데이트 동기화 실패: {exc}")

    @app.delete("/api/platform/bots/{bot_id}")
    async def delete_platform_bot(bot_id: str):
        try:
            deleted = sub_engine.delete_bot(bot_id)
        except ValueError as err:
            raise HTTPException(status_code=400, detail=str(err))
        await bot_manager.reload_bots()
        return {"success": deleted}

    @app.get("/api/platform/users")
    def list_platform_users(q: str = "", tier: str = "", status: str = ""):
        return {"users": sub_engine.list_users_with_subscriptions(q=q, tier_filter=tier, status_filter=status)}

    @app.post("/api/platform/users")
    def create_platform_user(req: PlatformUserCreateRequest):
        try:
            user = sub_engine.upsert_user(
                telegram_user_id=req.telegram_user_id,
                full_name=req.full_name,
                username=req.username,
                tier=req.tier,
                tier_expires_at=req.tier_expires_at,
                status=req.status,
                memo=req.memo,
                actor="ADMIN",
            )
            return {"success": True, "user": user}
        except ValueError as err:
            raise HTTPException(status_code=400, detail=str(err))

    @app.put("/api/platform/users/{user_id}")
    def update_platform_user(user_id: str, req: PlatformUserUpdateRequest):
        try:
            user = sub_engine.update_user_details(
                telegram_user_id=user_id,
                full_name=req.full_name,
                username=req.username,
                status=req.status,
                memo=req.memo,
            )
            return {"success": True, "user": user}
        except KeyError as err:
            raise HTTPException(status_code=404, detail=str(err))

    @app.put("/api/platform/users/{user_id}/tier")
    def change_platform_user_tier(user_id: str, req: PlatformTierUpdateRequest):
        try:
            user = sub_engine.update_user_tier(
                telegram_user_id=user_id,
                new_tier=req.tier,
                tier_expires_at=req.tier_expires_at,
                downgrade_mode=req.downgrade_mode,
                keep_bot_ids=req.keep_bot_ids,
                memo=req.memo,
                actor="ADMIN",
            )
            return {"success": True, "user": user}
        except KeyError as err:
            raise HTTPException(status_code=404, detail=str(err))
        except ValueError as err:
            raise HTTPException(status_code=400, detail=str(err))

    @app.delete("/api/platform/users/{user_id}")
    def remove_platform_user(user_id: str):
        return {"success": sub_engine.delete_user(user_id)}

    @app.post("/api/platform/subscriptions/subscribe")
    def platform_subscribe(req: SubscriptionActionRequest):
        return sub_engine.attempt_subscribe(
            telegram_user_id=req.telegram_user_id,
            bot_id=req.bot_id,
            actor="ADMIN",
            bypass_quota=req.bypass_quota,
        )

    @app.post("/api/platform/subscriptions/unsubscribe")
    def platform_unsubscribe(req: SubscriptionActionRequest):
        return sub_engine.unsubscribe_bot(
            telegram_user_id=req.telegram_user_id,
            bot_id=req.bot_id,
            actor="ADMIN",
            reason="ADMIN_UNSUBSCRIBE",
        )

    @app.post("/api/platform/subscriptions/swap")
    def platform_swap(req: SubscriptionSwapRequest):
        try:
            return sub_engine.atomic_swap_subscription(
                telegram_user_id=req.telegram_user_id,
                cancel_bot_id=req.cancel_bot_id,
                new_bot_id=req.new_bot_id,
                actor="ADMIN",
            )
        except ValueError as err:
            raise HTTPException(status_code=400, detail=str(err))

    @app.get("/api/platform/matrix")
    def get_platform_matrix():
        return sub_engine.get_subscription_matrix()

    @app.post("/api/platform/simulate")
    def simulate_platform_bot_interaction(req: SimulateInteractionRequest):
        return router.handle_interaction(
            bot_id=req.bot_id,
            telegram_user_id=req.telegram_user_id,
            full_name=req.full_name,
            username=req.username,
            text=req.text,
            callback_data=req.callback_data,
        )

    @app.post("/api/platform/broadcast")
    def broadcast_to_bot_subscribers(req: BotBroadcastRequest):
        bot = sub_engine.get_bot(req.bot_id)
        if not bot:
            raise HTTPException(status_code=404, detail="봇을 찾을 수 없습니다.")
        if not bot.token or not bot.token.strip():
            raise HTTPException(status_code=400, detail=f"[{bot.name}]에 텔레그램 토큰이 등록되어 있지 않습니다.")

        subscribers = sub_engine.get_bot_active_recipients(req.bot_id)
        if not subscribers:
            raise HTTPException(status_code=400, detail=f"[{bot.name}]을 현재 구독 중인 활성 유저가 없습니다.")

        transport = TelegramBotTransport(bot.token)
        limiter = TelegramRateLimiter(max_global_rate=25.0, min_chat_interval=0.5)
        sent = 0
        failed = 0
        errors = []

        for r in subscribers:
            limiter.wait_before_send(r.chat_id)
            try:
                transport.send_html(r.chat_id, req.message_html)
                sent += 1
            except Exception as exc:
                failed += 1
                err_str = str(exc)
                errors.append({"chat_id": r.chat_id, "error": err_str})
                if "403" in err_str and "blocked" in err_str.lower():
                    sub_engine.unsubscribe_bot(r.chat_id, req.bot_id, actor="SYSTEM", reason="USER_BLOCKED_BOT")

        return {
            "success": True,
            "bot_id": req.bot_id,
            "total": len(subscribers),
            "sent": sent,
            "failed": failed,
            "errors": errors[:10],
        }

    # =========================================================================
    # Real-Time Domain Briefing Center Routes (Engine Integration)
    # =========================================================================

    @app.get("/api/platform/bots/{bot_id}/presets")
    def get_platform_bot_presets(bot_id: str):
        from market_brief.briefing_service import get_bot_presets
        return {"bot_id": bot_id, "presets": get_bot_presets(bot_id)}

    @app.post("/api/platform/bots/{bot_id}/preview")
    def preview_platform_bot_briefing(bot_id: str, req: BriefingPreviewRequest):
        from market_brief.briefing_service import generate_bot_briefing
        return generate_bot_briefing(bot_id=bot_id, preset=req.preset, db=sub_engine)

    @app.post("/api/platform/bots/{bot_id}/dispatch-test")
    def dispatch_test_briefing(bot_id: str, req: BriefingTestDispatchRequest):
        from market_brief.briefing_service import send_bot_test_message
        res = send_bot_test_message(
            bot_id=bot_id,
            content_html=req.content_html,
            inline_keyboard=req.inline_keyboard,
            target_chat_id=req.target_chat_id,
            db=sub_engine,
        )
        if not res.get("success"):
            raise HTTPException(status_code=400, detail=res.get("error", "테스트 발송 실패"))
        return res

    @app.post("/api/platform/bots/{bot_id}/dispatch-subscribers")
    def dispatch_subscribers_briefing(bot_id: str, req: BriefingSubscriberDispatchRequest):
        from market_brief.briefing_service import send_bot_broadcast_message
        res = send_bot_broadcast_message(
            bot_id=bot_id,
            content_html=req.content_html,
            inline_keyboard=req.inline_keyboard,
            db=sub_engine,
        )
        if not res.get("success"):
            raise HTTPException(status_code=400, detail=res.get("error", "구독자 발송 실패"))
        return res

    # =========================================================================
    # Legacy Zero-Breaking API Routes (100% Backward Compatible)
    # =========================================================================

    @app.get("/api/status")
    def get_system_status():
        now_kst = datetime.now(KST)
        today_str = now_kst.date().isoformat()
        token = get_bot_token()

        bot_info = None
        bot_online = False
        if token:
            try:
                tg = TelegramChannel(token)
                bot_info = tg.get_me()
                bot_online = True
            except Exception:
                bot_online = False

        morning_sent = db.is_regular_briefing_sent("morning", now_kst.date())
        closing_sent = db.is_regular_briefing_sent("closing", now_kst.date())
        active_recipients = db.get_active_recipients("FINANCE")

        return {
            "current_time_kst": now_kst.strftime("%Y-%m-%d %H:%M:%S"),
            "today": today_str,
            "bot_online": bot_online,
            "bot_username": bot_info.get("username") if bot_info else None,
            "morning_status": "COMPLETED" if morning_sent else "NOT_SENT",
            "closing_status": "COMPLETED" if closing_sent else "NOT_SENT",
            "active_recipients_count": len(active_recipients),
            "report_base_url": get_report_base_url(),
            "github_connected": bool(get_github_token()),
        }

    @app.get("/api/recipients")
    def list_recipients():
        return [r.to_dict() for r in db.get_all_recipients()]

    @app.post("/api/recipients")
    def save_recipient(req: RecipientRequest):
        token = get_bot_token()
        name = req.name.strip()
        if not name or name.startswith("수신자_"):
            name = fetch_telegram_profile_name(token, req.chat_id)
        success = db.add_recipient(
            chat_id=req.chat_id,
            name=name,
            channel=req.channel,
            group_tag=req.group_tag,
            subscribed_topics=req.subscribed_topics,
        )
        return {"success": success, "name": name}

    @app.post("/api/recipients/{chat_id}/name")
    @app.put("/api/recipients/{chat_id}/name")
    def update_recipient_name(chat_id: str, req: RecipientNameUpdateRequest):
        new_name = req.name.strip()
        if not new_name:
            raise HTTPException(status_code=400, detail="이름을 공백으로 설정할 수 없습니다.")
        success = db.update_recipient_name(chat_id, new_name)
        if not success:
            raise HTTPException(status_code=404, detail="수신자를 찾을 수 없습니다.")
        return {"success": True, "chat_id": chat_id, "name": new_name}

    @app.post("/api/recipients/{chat_id}/reset-name")
    def reset_recipient_name_from_bot(chat_id: str):
        token = get_bot_token()
        bot_name = fetch_telegram_profile_name(token, chat_id)
        db.update_recipient_name(chat_id, bot_name)
        return {"success": True, "chat_id": chat_id, "name": bot_name}

    @app.post("/api/recipients/{chat_id}/toggle")
    def toggle_recipient(chat_id: str):
        recipients = [r for r in db.get_all_recipients() if r.chat_id == chat_id]
        if not recipients:
            raise HTTPException(status_code=404, detail="수신자를 찾을 수 없습니다.")
        r = recipients[0]
        if r.is_active:
            db.deactivate_recipient(chat_id, reason="ADMIN_TOGGLE_OFF")
        else:
            db.add_recipient(chat_id, r.name, r.channel, r.group_tag, r.subscribed_topics)
        return {"success": True, "chat_id": chat_id, "is_active": not r.is_active}

    @app.delete("/api/recipients/{chat_id}")
    def remove_recipient(chat_id: str):
        success = db.delete_recipient(chat_id)
        return {"success": success}

    @app.post("/api/recipients/discover")
    def discover_telegram_users():
        token = get_bot_token()
        if not token:
            raise HTTPException(status_code=400, detail="TELEGRAM_BOT_TOKEN이 설정되지 않았습니다.")
        tg = TelegramChannel(token)
        discovered = tg.discover_chat_ids()
        existing = {r.chat_id: r for r in db.get_all_recipients()}
        results = []
        for d in discovered:
            cid = d["chat_id"]
            bot_profile_name = fetch_telegram_profile_name(token, cid)
            name_to_use = bot_profile_name or d.get("title") or f"수신자_{cid[-4:]}"
            if cid not in existing:
                db.add_recipient(cid, name=name_to_use)
            results.append(
                {
                    **d,
                    "title": name_to_use,
                    "is_registered": cid in existing,
                }
            )
        return {"candidates": results}

    @app.post("/api/recipients/sync-updates")
    def sync_updates():
        token = get_bot_token()
        res = sync_telegram_updates(token, db)
        return res

    @app.get("/api/recipients/github-sync-status")
    def get_github_sync_status():
        gh_token = get_github_token()
        if not gh_token:
            return {
                "github_connected": False,
                "is_synced": False,
                "message": "GITHUB_TOKEN이 설정되지 않아 깃허브 크론 상태를 확인할 수 없습니다.",
                "local_count": 0,
                "github_count": 0,
                "local_chat_ids": [],
                "github_chat_ids": [],
            }

        active_recipients = db.get_active_recipients("FINANCE")
        local_ids = sorted([r.chat_id for r in active_recipients])

        exists, gh_val, _ = get_github_variable("TELEGRAM_CHAT_ID")
        if not exists:
            return {
                "github_connected": True,
                "is_synced": False,
                "message": "GitHub에 TELEGRAM_CHAT_ID 변수가 아직 생성되지 않았습니다 (미동기화 상태).",
                "local_count": len(local_ids),
                "github_count": 0,
                "local_chat_ids": local_ids,
                "github_chat_ids": [],
            }

        gh_ids = sorted([x.strip() for x in (gh_val or "").split(",") if x.strip()])
        is_synced = local_ids == gh_ids

        return {
            "github_connected": True,
            "is_synced": is_synced,
            "message": "GitHub 크론 수신자 목록과 100% 일치합니다."
            if is_synced
            else "관리자 설정과 GitHub 크론 수신자 목록이 불일치합니다. [동기화] 버튼을 눌러주세요.",
            "local_count": len(local_ids),
            "github_count": len(gh_ids),
            "local_chat_ids": local_ids,
            "github_chat_ids": gh_ids,
        }

    @app.post("/api/recipients/sync-github")
    def sync_recipients_to_github():
        gh_token = get_github_token()
        if not gh_token:
            raise HTTPException(status_code=400, detail="GITHUB_TOKEN이 설정되지 않았습니다.")

        active_recipients = db.get_active_recipients("FINANCE")
        local_ids = [r.chat_id for r in active_recipients]
        if not local_ids:
            raise HTTPException(status_code=400, detail="활성화된 수신자가 0명입니다. 최소 1명 이상 활성화해야 합니다.")

        value_str = ",".join(local_ids)
        ok, msg = sync_github_variable("TELEGRAM_CHAT_ID", value_str)
        if not ok:
            raise HTTPException(status_code=502, detail=f"GitHub 동기화 실패: {msg}")

        names_summary = ", ".join([f"{r.name}({r.chat_id})" for r in active_recipients])
        return {
            "success": True,
            "message": f"✅ GitHub 크론 수신자(TELEGRAM_CHAT_ID) 동기화 완료! 총 {len(local_ids)}명 ({names_summary})",
            "synced_chat_ids": local_ids,
        }

    @app.get("/api/dispatch/preview")
    def preview_dispatch(kind: str = "morning"):
        now_kst = datetime.now(KST)
        fixture_p = FixtureProvider(str(ROOT / "fixtures/market_snapshot.json"))
        prov = CompositeMarketDataProvider(NaverFinanceProvider(), fixture_p)
        quotes = prov.fetch(DEFAULT_SYMBOLS)

        try:
            news = fetch_naver_market_news(3)
        except Exception:
            news = []

        from market_brief.report_engine import BriefingBuilder
        from market_brief.channels.telegram import format_telegram_html

        builder = BriefingBuilder()
        briefing = builder.build(
            quotes,
            kind=kind,
            as_of_label=f"{now_kst.strftime('%Y-%m-%d %H:%M')} KST",
            report_url=f"{get_report_base_url()}/reports/latest.html",
            news=news,
        )
        msg_text = format_telegram_html(briefing)
        return {
            "kind": kind,
            "headline": briefing.headline,
            "as_of": briefing.as_of,
            "telegram_html": msg_text,
            "quotes_count": len(quotes),
        }

    @app.post("/api/dispatch/send")
    def trigger_dispatch(req: DispatchRequest):
        token = get_bot_token()
        if not token:
            raise HTTPException(status_code=400, detail="텔레그램 봇 토큰이 없습니다.")

        now_kst = datetime.now(KST)
        today_str = now_kst.date().isoformat()
        batch_key = f"{req.topic.upper()}:{today_str}:{req.kind}"

        acquired, reason, job = db.acquire_batch_lease(
            batch_key=batch_key,
            topic=req.topic.upper(),
            kind=req.kind,
            scheduled_date=today_str,
            runner_id="admin_web_console",
            is_forced=req.is_forced,
            force_reason=req.force_reason,
        )

        if not acquired:
            if reason == "ALREADY_COMPLETED":
                raise HTTPException(
                    status_code=409,
                    detail=f"금일({today_str}) {req.kind} 브리핑은 이미 발송이 완료되었습니다. 재발송이 필요한 경우 '강제 재발송'을 체크하고 사유를 입력하세요.",
                )
            raise HTTPException(status_code=423, detail=f"작업 잠금 획득 실패: {reason}")

        gh_token = get_github_token()
        recipients = db.get_active_recipients(topic=req.topic.upper())
        if not recipients:
            db.fail_job(job.job_id, "활성 수신자가 0명입니다.")
            raise HTTPException(status_code=400, detail="해당 토픽을 구독하는 활성 수신자가 없습니다. 관리자 화면에서 최소 1명 이상 활성화하세요.")

        active_chat_ids = [r.chat_id for r in recipients]

        if gh_token:
            ok, msg = dispatch_github_workflow(kind=req.kind, recipients=active_chat_ids)
            if not ok:
                db.fail_job(job.job_id, f"GitHub Actions 트리거 실패: {msg}")
                raise HTTPException(status_code=502, detail=f"GitHub 원격 트리거 실패: {msg}")

            db.snapshot_job_recipients(job.job_id, recipients)
            db.complete_job(
                job.job_id,
                status=JobFinalStatus.SENT,
                total=len(recipients),
                success=len(recipients),
                fail=0,
                unknown=0,
                report_path="reports/latest.html",
            )
            names_summary = ", ".join([f"{r.name}({r.chat_id})" for r in recipients])
            return {
                "success": True,
                "mode": "GITHUB_ACTIONS_REMOTE",
                "job_id": job.job_id,
                "target_recipients": active_chat_ids,
                "message": f"🚀 [선택 발송] 활성 수신자 {len(recipients)}명({names_summary})에게만 {req.kind} 브리핑 배포 & 텔레그램 발송이 시작되었습니다! (비활성자 100% 제외)",
                "actions_url": "https://github.com/kkjhj1/kwDailyMarketReport/actions",
            }

        db.snapshot_job_recipients(job.job_id, recipients)

        fixture_p = FixtureProvider(str(ROOT / "fixtures/market_snapshot.json"))
        prov = CompositeMarketDataProvider(NaverFinanceProvider(), fixture_p)
        quotes = prov.fetch(DEFAULT_SYMBOLS)

        news = []
        try:
            news = fetch_naver_market_news(5)
        except Exception:
            pass

        from market_brief.report_engine import BriefingBuilder
        from market_brief.web_report import HTMLReportRenderer, generate_signed_report_url

        builder = BriefingBuilder()
        report_id = f"briefing_{now_kst.strftime('%Y%m%d_%H%M')}_{req.kind}"
        report_url = generate_signed_report_url(get_report_base_url(), report_id, "market-brief-secret-2026")
        as_of_label = f"{now_kst.strftime('%Y-%m-%d %H:%M')} KST"

        briefing = builder.build(
            quotes,
            kind=req.kind,
            as_of_label=as_of_label,
            report_url=report_url,
            news=news,
        )

        report_dir = ROOT / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / f"{report_id}.html"
        renderer = HTMLReportRenderer()
        renderer.render(briefing, quotes, news, output_path=report_path)

        tg = TelegramChannel(token, include_inline_button=True, use_html=True)
        dispatcher = SafeTelegramDispatcher(rate_limiter=TelegramRateLimiter(max_global_rate=25.0, min_chat_interval=0.5))

        success_count = 0
        fail_count = 0
        unknown_count = 0

        for r in recipients:
            status, msg_id, err_detail = dispatcher.send_to_recipient(tg, r, briefing, db=db)
            db.record_recipient_result(job.job_id, r.chat_id, status, provider_msg_id=msg_id, error_detail=err_detail)
            if status == "SENT":
                success_count += 1
            elif status == "UNKNOWN":
                unknown_count += 1
            else:
                fail_count += 1

        final_status = JobFinalStatus.SENT
        if fail_count > 0 or unknown_count > 0:
            final_status = JobFinalStatus.PARTIAL if success_count > 0 else JobFinalStatus.FAILED

        db.complete_job(
            job.job_id,
            status=final_status,
            total=len(recipients),
            success=success_count,
            fail=fail_count,
            unknown=unknown_count,
            report_path=str(report_path.relative_to(ROOT)),
        )

        return {
            "success": True,
            "job_id": job.job_id,
            "status": final_status.value,
            "total": len(recipients),
            "success_count": success_count,
            "fail_count": fail_count,
            "unknown_count": unknown_count,
            "report_path": str(report_path.name),
        }

    @app.get("/api/history")
    def get_history(limit: int = 15):
        return db.get_recent_jobs(limit=limit)

    @app.get("/api/history/{job_id}/details")
    def get_history_details(job_id: str):
        recipients = db.get_job_recipients(job_id)
        return {"job_id": job_id, "recipients": recipients}

    @app.get("/api/settings")
    def get_settings():
        thresholds_raw = db.get_setting("thresholds")
        thresholds = json.loads(thresholds_raw) if thresholds_raw else DEFAULT_THRESHOLDS
        schedules_raw = db.get_setting("schedules")
        schedules = json.loads(schedules_raw) if schedules_raw else {"morning": "06:30", "closing": "15:40"}
        return {"thresholds": thresholds, "schedules": schedules}

    @app.post("/api/settings")
    def save_settings(req: ThresholdRequest):
        db.set_setting("thresholds", json.dumps(req.thresholds, ensure_ascii=False), updated_by="ADMIN_WEB")
        return {"success": True}

    # =========================================================================
    # Travel Brief Bot APIs (Multi-Bot Integration)
    # =========================================================================

    if _HAS_TRAVEL_MODULE:
        @app.get("/api/travel/status")
        def get_travel_status():
            f_engine = FlightEngine()
            recipients = db.get_active_recipients(topic="TRAVEL")
            travel_report_latest = reports_dir / "travel_latest.html"
            return {
                "status": "ONLINE",
                "bot_topic": "TRAVEL",
                "destinations_count": len(f_engine.destinations),
                "subscribers_total": len(recipients),
                "reports_available": travel_report_latest.exists(),
                "report_url": f"{get_report_base_url()}/reports/travel_latest.html",
            }

        @app.get("/api/travel/destinations")
        def list_travel_destinations():
            f_engine = FlightEngine()
            return [
                {
                    "code": d.code,
                    "name": d.name,
                    "country": d.country,
                    "region": d.region,
                    "flight_time_hours": d.flight_time_hours,
                    "visa_info": d.visa_info,
                    "best_months": d.best_months,
                    "caution_months": d.caution_months,
                    "flight_baseline_krw": d.flight_baseline_krw,
                    "flight_deal_threshold_krw": d.flight_deal_threshold_krw,
                    "hotel_night_avg_krw": d.hotel_night_avg_krw,
                    "currency": d.currency,
                    "tips": d.tips,
                }
                for d in f_engine.list_destinations()
            ]

        @app.get("/api/travel/preview")
        def preview_travel_briefing(dest_code: str | None = None, max_budget: int | None = None):
            f_engine = FlightEngine()
            all_deals = f_engine.search_deals(dest_code=dest_code, months_ahead=6)
            recommendations = BudgetCalculator.rank_recommendations(
                destinations=f_engine.destinations,
                deals=all_deals,
                max_budget_per_person=max_budget,
                top_n=5,
            )
            report_url = f"{get_report_base_url()}/reports/travel_latest.html"
            text = format_telegram_briefing(recommendations, web_report_url=report_url)
            return {
                "recommendations_count": len(recommendations),
                "top_recommendations": [
                    {
                        "destination": r.destination.name,
                        "code": r.destination.code,
                        "price_krw": r.best_deal.price_krw,
                        "total_per_person_krw": r.budget.total_per_person_krw,
                        "departure": r.best_deal.departure_date,
                        "season_status": r.season_status,
                    }
                    for r in recommendations[:3]
                ],
                "formatted_message": text,
            }

        @app.post("/api/travel/dispatch")
        def dispatch_travel_briefing(req: TravelDispatchRequest):
            recipients_override = req.recipients if req.recipients else None
            res = run_travel_briefing(
                dest_code=req.dest_code,
                max_budget=req.max_budget,
                dry_run=req.dry_run,
                recipients_override=recipients_override,
                web_report_url=f"{get_report_base_url()}/reports/travel_latest.html",
                db_path=db_path,
            )
            return {"success": res, "dry_run": req.dry_run}

    if _HAS_ADMISSION_MODULE:
        adm_db = AdmissionDatabase(db_path=ROOT / "data/admission_brief.db")
        adm_curator = AICurator()

        @app.get("/api/admission/status")
        def get_admission_status():
            subscribers = adm_db.list_subscribers(active_only=True)
            events = adm_db.list_all_events()
            token = os.environ.get("ADMISSION_TELEGRAM_BOT_TOKEN") or get_bot_token()
            return {
                "status": "ONLINE",
                "subscribers_total": len(subscribers),
                "events_total": len(events),
                "has_token": bool(token),
                "grade_breakdown": {
                    "MIDDLE": len([s for s in subscribers if s.target_grade == TargetGrade.MIDDLE]),
                    "HIGH_1_2": len([s for s in subscribers if s.target_grade == TargetGrade.HIGH_1_2]),
                    "HIGH_3_N": len([s for s in subscribers if s.target_grade == TargetGrade.HIGH_3_N]),
                    "ALL": len([s for s in subscribers if s.target_grade == TargetGrade.ALL]),
                },
                "region_breakdown": {
                    "ONLINE": len([s for s in subscribers if s.target_region == TargetRegion.ONLINE]),
                    "SEOUL": len([s for s in subscribers if s.target_region == TargetRegion.SEOUL]),
                    "GYEONGGI": len([s for s in subscribers if s.target_region == TargetRegion.GYEONGGI]),
                    "LOCAL": len([s for s in subscribers if s.target_region == TargetRegion.LOCAL]),
                    "ALL": len([s for s in subscribers if s.target_region == TargetRegion.ALL]),
                },
            }

        @app.get("/api/admission/subscribers")
        def list_admission_subscribers():
            return {"subscribers": [s.to_dict() for s in adm_db.list_subscribers(active_only=False)]}

        @app.get("/api/admission/events")
        def list_admission_events():
            return {"events": [e.to_dict() for e in adm_db.list_all_events()]}

        @app.post("/api/admission/events")
        def create_admission_event(req: AdmissionEventCreateRequest):
            event = AdmissionEvent(
                event_id=f"evt_adm_{datetime.now().strftime('%Y%m%d%H%M%S')}",
                title=req.title,
                organizer=req.organizer,
                target_grade=TargetGrade(req.target_grade),
                target_region=TargetRegion(req.target_region),
                is_online_live=req.is_online_live,
                event_type=BriefingType(req.event_type),
                event_date_str=req.event_date_str,
                booking_url=req.booking_url,
                is_first_come=req.is_first_come,
                summary_3lines=req.summary_3lines or ["설명회 핵심 요약", "전형 변화 팩트체크", "방학 로드맵"],
                so_what=req.so_what or "학생부 세특 및 심화 학습 준비가 필수적입니다.",
                action_todos=req.action_todos or ["설명회 사전 예약 신청", "학생부 점검"],
            )
            adm_db.save_event(event)
            return {"success": True, "event": event.to_dict()}

        @app.post("/api/admission/collect")
        def collect_admission_events():
            collector = AdmissionCollector(db=adm_db, curator=adm_curator)
            results = collector.run_collection()
            return {"success": True, **results}

        @app.post("/api/admission/dispatch")
        def dispatch_admission_briefing(req: AdmissionDispatchRequest):
            target_grade = TargetGrade(req.target_grade)
            target_region = TargetRegion(req.target_region)
            if req.event_id:
                evt = adm_db.get_event(req.event_id)
                if not evt:
                    raise HTTPException(status_code=404, detail="입시 이벤트를 찾을 수 없습니다.")
                briefing = adm_curator.curate_event_briefing(evt)
            else:
                briefing = adm_curator.generate_weekly_todo_briefing(target_grade)

            briefing.target_region = target_region
            if req.silent is not None:
                briefing.silent = req.silent

            if req.dry_run:
                subs = adm_db.list_subscribers(
                    grade=target_grade,
                    region=target_region,
                    is_online_live=briefing.is_online_live,
                    active_only=True,
                )
                return {
                    "success": True,
                    "dry_run": True,
                    "target_subscribers_count": len(subs),
                    "formatted_html": format_admission_html(briefing),
                    "briefing": {
                        "headline": briefing.headline,
                        "grade": briefing.target_grade.value,
                        "region": briefing.target_region.value,
                        "silent": briefing.silent,
                    },
                }

            token = os.environ.get("ADMISSION_TELEGRAM_BOT_TOKEN") or get_bot_token()
            if not token:
                raise HTTPException(status_code=400, detail="ADMISSION_TELEGRAM_BOT_TOKEN이 설정되지 않았습니다.")

            channel = AdmissionTelegramChannel(bot_token=token)
            dispatcher = AdmissionSafeDispatcher()
            stats = dispatcher.dispatch(channel=channel, briefing=briefing, db=adm_db)
            return {"success": True, "dry_run": False, "stats": stats}

    @app.get("/", response_class=HTMLResponse)
    def render_admin_dashboard():
        return HTMLResponse(content=ADMIN_DASHBOARD_HTML)

    return app


def find_available_port(preferred_port: int = 8002, max_attempts: int = 20) -> int:
    """Checks if preferred_port is available; if occupied, finds the next free port automatically."""
    import socket

    for port_candidate in range(preferred_port, preferred_port + max_attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port_candidate))
                return port_candidate
            except OSError:
                continue
    return preferred_port


if __name__ == "__main__":
    configured_port = int(os.environ.get("ADMIN_PORT", 8002))
    port = find_available_port(configured_port)
    if port != configured_port:
        print(f"⚠️ [PORT FALLBACK: {configured_port} -> {port}] 포트 {configured_port}번 충돌로 인해 가용 포트 {port}번으로 안전 전환되었습니다.")
    print(f"🚀 [Multi-Bot Subscription Admin Console] URL: http://127.0.0.1:{port}")
    app = create_admin_app(enable_bot_workers=True)
    uvicorn.run(app, host="127.0.0.1", port=port)
