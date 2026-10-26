"""FastAPI Local Admin Console for Market Brief Delivery Management."""
from __future__ import annotations

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

from market_brief.channels.telegram import TelegramChannel
from market_brief.channels.telegram_limiter import SafeTelegramDispatcher, TelegramRateLimiter, sync_telegram_updates
from market_brief.market_data import CompositeMarketDataProvider, FixtureProvider, NaverFinanceProvider
from market_brief.market_data.naver_provider import fetch_naver_market_news
from market_brief.pipeline import MarketBriefingPipeline, DEFAULT_SYMBOLS
from market_brief.storage.db import MarketBriefDatabase
from market_brief.storage.models import JobFinalStatus, Recipient

ROOT = Path(__file__).resolve().parent
KST = ZoneInfo("Asia/Seoul")


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


class ThresholdRequest(BaseModel):
    thresholds: dict


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
                if any('\uac00' <= c <= '\ud7a3' for c in last + first):
                    return f"{last}{first}"
                return f"{first} {last}"
            if first:
                return first
            if username:
                return f"@{username}"
            return f"수신자_{chat_id[-4:]}"
    except Exception:
        return f"수신자_{chat_id[-4:]}"


def create_admin_app(db_path: str = "alerts.sqlite3", env_path: Path | None = None) -> FastAPI:
    app = FastAPI(title="Market Brief Admin Console", version="0.4.0")
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

    env_file = env_path or (ROOT / ".env")
    db = MarketBriefDatabase(db_path=db_path, env_path=env_file)

    def get_bot_token() -> str:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
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
        token = os.environ.get("GITHUB_TOKEN", "").strip()
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
        payload = json.dumps({
            "ref": "main",
            "inputs": {
                "mode": kind,
                "recipients": recipients_str,
            }
        }).encode("utf-8")
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

    # -------------------------------------------------------------------------
    # API Routes
    # -------------------------------------------------------------------------

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
            results.append({
                **d,
                "title": name_to_use,
                "is_registered": cid in existing,
            })
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

        exists, gh_val, msg = get_github_variable("TELEGRAM_CHAT_ID")
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
        is_synced = (local_ids == gh_ids)

        return {
            "github_connected": True,
            "is_synced": is_synced,
            "message": "GitHub 크론 수신자 목록과 100% 일치합니다." if is_synced else "관리자 설정과 GitHub 크론 수신자 목록이 불일치합니다. [동기화] 버튼을 눌러주세요.",
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

        # 1. Atomic Lease Lock Acquisition
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
                    detail=f"금일({today_str}) {req.kind} 브리핑은 이미 발송이 완료되었습니다. 재발송이 필요한 경우 '강제 재발송'을 체크하고 사유를 입력하세요."
                )
            raise HTTPException(status_code=423, detail=f"작업 잠금 획득 실패: {reason}")

        # 2. Check if GitHub Actions remote dispatch is enabled
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

        # 3. Fallback to Local Dispatch (if no GITHUB_TOKEN)
        db.snapshot_job_recipients(job.job_id, recipients)

        # Build & Dispatch Briefing locally
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

        # Render HTML Report file
        report_dir = ROOT / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / f"{report_id}.html"
        renderer = HTMLReportRenderer()
        renderer.render(briefing, quotes, news, output_path=report_path)

        # 4. Dispatch with Telegram Limiter & Error Isolation
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

    # -------------------------------------------------------------------------
    # Embedded HTML Admin Dashboard UI (Modern, Glassmorphism, Dark Mode)
    # -------------------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def render_admin_dashboard():
        return HTMLResponse(content=ADMIN_DASHBOARD_HTML)

    return app


ADMIN_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Market Brief - 통합 발송 관리자 콘솔</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap">
  <style>
    :root {
      --bg: #f8fafc;
      --card-bg: #ffffff;
      --card-border: #e2e8f0;
      --text-main: #0f172a;
      --text-muted: #64748b;
      --text-sub: #334155;
      --primary: #2563eb;
      --primary-hover: #1d4ed8;
      --success: #10b981;
      --warning: #f59e0b;
      --danger: #ef4444;
      --shadow: 0 4px 6px -1px rgba(0,0,0,0.05), 0 2px 4px -2px rgba(0,0,0,0.05);
      --shadow-lg: 0 10px 15px -3px rgba(0,0,0,0.1), 0 4px 6px -4px rgba(0,0,0,0.1);
    }
    [data-theme="dark"] {
      --bg: #0b0f19;
      --card-bg: #151d2f;
      --card-border: #232f48;
      --text-main: #f8fafc;
      --text-muted: #94a3b8;
      --text-sub: #cbd5e1;
      --primary: #3b82f6;
      --primary-hover: #2563eb;
      --shadow: 0 4px 6px -1px rgba(0,0,0,0.4);
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
      background: var(--bg);
      color: var(--text-main);
      min-height: 100vh;
      display: flex;
      flex-direction: column;
    }
    .header {
      background: var(--card-bg);
      border-bottom: 1px solid var(--card-border);
      padding: 14px 24px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      position: sticky;
      top: 0;
      z-index: 50;
    }
    .brand { display: flex; align-items: center; gap: 10px; font-size: 18px; font-weight: 800; color: var(--primary); }
    .brand span { color: var(--text-main); }
    .header-right { display: flex; align-items: center; gap: 14px; }
    .badge { padding: 4px 10px; border-radius: 9999px; font-size: 12px; font-weight: 600; display: inline-flex; align-items: center; gap: 6px; }
    .badge.online { background: #d1fae5; color: #065f46; }
    .badge.offline { background: #fee2e2; color: #991b1b; }
    .nav-tabs {
      background: var(--card-bg);
      border-bottom: 1px solid var(--card-border);
      display: flex;
      padding: 0 24px;
      gap: 8px;
    }
    .nav-tab {
      padding: 12px 18px;
      font-size: 14px;
      font-weight: 600;
      color: var(--text-muted);
      cursor: pointer;
      border-bottom: 2px solid transparent;
      transition: all 0.2s;
    }
    .nav-tab:hover { color: var(--text-main); }
    .nav-tab.active { color: var(--primary); border-bottom-color: var(--primary); }
    .container { max-width: 1200px; width: 100%; margin: 24px auto; padding: 0 20px; flex: 1; }
    .grid-cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 16px; margin-bottom: 24px; }
    .card { background: var(--card-bg); border: 1px solid var(--card-border); border-radius: 12px; padding: 20px; box-shadow: var(--shadow); }
    .card-title { font-size: 13px; font-weight: 600; color: var(--text-muted); margin-bottom: 8px; text-transform: uppercase; letter-spacing: 0.5px; }
    .card-value { font-size: 26px; font-weight: 800; margin-bottom: 4px; }
    .card-sub { font-size: 12px; color: var(--text-muted); }
    .btn {
      padding: 9px 16px;
      border-radius: 8px;
      font-size: 13px;
      font-weight: 600;
      cursor: pointer;
      border: none;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      transition: all 0.15s;
    }
    .btn-primary { background: var(--primary); color: #fff; }
    .btn-primary:hover { background: var(--primary-hover); }
    .btn-secondary { background: var(--card-border); color: var(--text-main); }
    .btn-danger { background: var(--danger); color: #fff; }
    .section-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
    .section-title { font-size: 16px; font-weight: 700; color: var(--text-main); }
    .table-container { background: var(--card-bg); border: 1px solid var(--card-border); border-radius: 12px; overflow: hidden; box-shadow: var(--shadow); }
    table { width: 100%; border-collapse: collapse; text-align: left; font-size: 13px; }
    th { background: rgba(0,0,0,0.02); padding: 12px 16px; font-weight: 600; color: var(--text-muted); border-bottom: 1px solid var(--card-border); }
    td { padding: 14px 16px; border-bottom: 1px solid var(--card-border); color: var(--text-main); }
    tr:last-child td { border-bottom: none; }
    .modal-overlay {
      position: fixed; top: 0; left: 0; right: 0; bottom: 0;
      background: rgba(0,0,0,0.6); display: none; justify-content: center; align-items: center; z-index: 100;
    }
    .modal-box {
      background: var(--card-bg); border: 1px solid var(--card-border); border-radius: 14px;
      max-width: 650px; width: 90%; max-height: 85vh; overflow-y: auto; padding: 24px; box-shadow: var(--shadow-lg);
    }
    .form-group { margin-bottom: 14px; }
    .form-label { display: block; font-size: 12px; font-weight: 600; margin-bottom: 6px; color: var(--text-sub); }
    .form-input {
      width: 100%; padding: 10px 12px; border-radius: 8px; border: 1px solid var(--card-border);
      background: var(--card-bg); color: var(--text-main); font-size: 13px; outline: none;
    }
    .preview-box {
      background: rgba(0,0,0,0.03); border: 1px dashed var(--card-border); border-radius: 8px;
      padding: 12px; font-family: monospace; font-size: 12px; white-space: pre-wrap; max-height: 220px; overflow-y: auto; margin-top: 10px;
    }
  </style>
</head>
<body>
  <header class="header">
    <div class="brand">📈 <span>Daily Market Brief</span> Console</div>
    <div class="header-right">
      <div id="ghBadge" class="badge online">☁️ GitHub 확인 중...</div>
      <div id="botBadge" class="badge online">● 봇 연결 확인 중...</div>
      <span id="liveClock" style="font-size:12px; color:var(--text-muted); font-weight:600;"></span>
      <button class="btn btn-secondary" onclick="toggleTheme()" id="themeBtn">🌙</button>
    </div>
  </header>

  <nav class="nav-tabs">
    <div class="nav-tab active" onclick="switchTab('tab-overview')">📊 종합 현황</div>
    <div class="nav-tab" onclick="switchTab('tab-recipients')">👥 수신자 관리</div>
    <div class="nav-tab" onclick="switchTab('tab-history')">📋 발송 이력 및 리포트</div>
    <div class="nav-tab" onclick="switchTab('tab-settings')">⚙️ 임계치 설정</div>
  </nav>

  <main class="container">
    <!-- TAB 1: OVERVIEW -->
    <section id="tab-overview" class="tab-content">
      <div class="grid-cards">
        <div class="card">
          <div class="card-title">🌅 06:30 장전 모닝 브리핑</div>
          <div class="card-value" id="morningStatusText">-</div>
          <div class="card-sub" id="morningStatusSub">한국시간 평일 오전 6:30 자동 크론</div>
        </div>
        <div class="card">
          <div class="card-title">📊 15:40 장마감 시황 브리핑</div>
          <div class="card-value" id="closingStatusText">-</div>
          <div class="card-sub" id="closingStatusSub">한국시간 평일 오후 3:40 자동 크론</div>
        </div>
        <div class="card">
          <div class="card-title">👥 활성 발송 대상자</div>
          <div class="card-value" id="activeRecipientsCount">-</div>
          <div class="card-sub">텔레그램 수신 동의 계정</div>
        </div>
      </div>

      <div class="card" style="margin-bottom: 24px;">
        <div class="section-header">
          <div class="section-title">🚀 원클릭 즉시 발송 센터</div>
          <button class="btn btn-secondary" onclick="openPreviewModal()">👁️ 발송 미리보기</button>
        </div>
        <p style="font-size:13px; color:var(--text-muted); margin-bottom:16px;">
          버튼을 누르면 현재 실시간 시세를 수집·검증하여 즉시 상세 HTML 리포트를 배포하고 텔레그램 메시지를 발송합니다.
          (이미 발송 완료된 경우 중복 발송 방지 가드가 작동합니다.)
        </p>
        <div style="display:flex; gap:12px; flex-wrap:wrap;">
          <button class="btn btn-primary" onclick="triggerDispatch('morning', false)">🌅 06:30 아침 장전 브리핑 즉시 발송</button>
          <button class="btn btn-primary" style="background:#059669;" onclick="triggerDispatch('closing', false)">📊 15:40 장마감 브리핑 즉시 발송</button>
          <button class="btn btn-danger" onclick="triggerDispatch('urgent', false)">🚨 ±3% 긴급 변동 감시 실행</button>
          <button class="btn btn-secondary" onclick="openForceModal()">⚠️ 강제 재발송 (사유입력)</button>
        </div>
      </div>
    </section>

    <!-- TAB 2: RECIPIENTS -->
    <section id="tab-recipients" class="tab-content" style="display:none;">
      <div class="section-header">
        <div style="display:flex; align-items:center; gap:12px;">
          <div class="section-title">수신자 마스터 목록</div>
          <div id="ghSyncBadge" class="badge online" style="font-size:11px;">☁️ GitHub 동기화 확인 중...</div>
        </div>
        <div style="display:flex; gap:8px; align-items:center;">
          <button class="btn btn-primary" style="background:#0284c7;" onclick="syncRecipientsToGithub()" title="현재 활성 수신자 목록을 GitHub 크론 환경(TELEGRAM_CHAT_ID)에 동기화합니다.">☁️ GitHub 크론 수신자 동기화</button>
          <button class="btn btn-secondary" onclick="discoverBotUsers()">🔍 봇 대화방 자동감지 (/start)</button>
          <button class="btn btn-primary" onclick="openAddRecipientModal()">+ 수신자 수동 등록</button>
        </div>
      </div>
      <div class="table-container">
        <table>
          <thead>
            <tr>
              <th>이름</th>
              <th>Chat ID</th>
              <th>채널</th>
              <th>그룹</th>
              <th>구독 토픽</th>
              <th>상태</th>
              <th>관리</th>
            </tr>
          </thead>
          <tbody id="recipientsTableBody">
            <tr><td colspan="7" style="text-align:center;">수신자 정보를 불러오는 중...</td></tr>
          </tbody>
        </table>
      </div>
    </section>

    <!-- TAB 3: HISTORY -->
    <section id="tab-history" class="tab-content" style="display:none;">
      <div class="section-header">
        <div class="section-title">최근 발송 이력 (감사 로그)</div>
        <button class="btn btn-secondary" onclick="loadHistory()">🔄 새로고침</button>
      </div>
      <div class="table-container">
        <table>
          <thead>
            <tr>
              <th>시작 일시 (KST)</th>
              <th>토픽/종류</th>
              <th>최종 상태</th>
              <th>대상</th>
              <th>성공</th>
              <th>실패</th>
              <th>불명확(UNKNOWN)</th>
              <th>리포트</th>
              <th>상세</th>
            </tr>
          </thead>
          <tbody id="historyTableBody">
            <tr><td colspan="9" style="text-align:center;">이력을 불러오는 중...</td></tr>
          </tbody>
        </table>
      </div>
    </section>

    <!-- TAB 4: SETTINGS -->
    <section id="tab-settings" class="tab-content" style="display:none;">
      <div class="card">
        <div class="section-header">
          <div class="section-title">자산별 긴급 알림 변동률 임계치 & 쿨다운 설정</div>
          <button class="btn btn-primary" onclick="saveSettings()">💾 설정 저장</button>
        </div>
        <p style="font-size:13px; color:var(--text-muted); margin-bottom:16px;">
          장중 시세가 설정된 임계치를 돌파하면 긴급 알림이 발송됩니다. 과도한 스팸을 방지하기 위해 쿨다운 및 회복 조건을 설정합니다.
        </p>
        <div id="thresholdsEditor" style="display:grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap:16px;">
          <!-- Dynamically populated -->
        </div>
      </div>
    </section>
  </main>

  <!-- MODALS -->
  <div id="forceModal" class="modal-overlay">
    <div class="modal-box">
      <h3 style="margin-bottom:14px; color:var(--danger);">⚠️ 강제 재발송 (중복 방지 오버라이드)</h3>
      <p style="font-size:13px; color:var(--text-muted); margin-bottom:14px;">
        금일 이미 발송 완료된 브리핑을 강제로 다시 전송합니다. 감사 로그에 재발송 사유가 영구 기록됩니다.
      </p>
      <div class="form-group">
        <label class="form-label">발송 종류 선택</label>
        <select id="forceKind" class="form-input">
          <option value="morning">🌅 아침 장전 브리핑</option>
          <option value="closing">📊 장마감 시황 브리핑</option>
        </select>
      </div>
      <div class="form-group">
        <label class="form-label">강제 재발송 사유 (필수 입력)</label>
        <input type="text" id="forceReason" class="form-input" placeholder="예: 시세 데이터 수정으로 인한 재발송">
      </div>
      <div style="display:flex; justify-content:flex-end; gap:8px; margin-top:20px;">
        <button class="btn btn-secondary" onclick="closeModals()">취소</button>
        <button class="btn btn-danger" onclick="executeForceDispatch()">강제 재발송 실행</button>
      </div>
    </div>
  </div>

  <div id="previewModal" class="modal-overlay">
    <div class="modal-box">
      <h3 style="margin-bottom:10px;">👁️ 실시간 발송 메시지 미리보기</h3>
      <div style="display:flex; gap:8px; margin-bottom:10px;">
        <button class="btn btn-secondary" onclick="loadPreview('morning')">아침 브리핑</button>
        <button class="btn btn-secondary" onclick="loadPreview('closing')">장마감 브리핑</button>
      </div>
      <div class="preview-box" id="previewContent">시세 및 메시지를 생성 중...</div>
      <div style="display:flex; justify-content:flex-end; margin-top:16px;">
        <button class="btn btn-secondary" onclick="closeModals()">닫기</button>
      </div>
    </div>
  </div>

  <div id="addRecipientModal" class="modal-overlay">
    <div class="modal-box">
      <h3 style="margin-bottom:14px;">👥 수신자 수동 등록</h3>
      <div class="form-group">
        <label class="form-label">텔레그램 Chat ID (숫자)</label>
        <input type="text" id="newChatId" class="form-input" placeholder="예: 7820975792">
      </div>
      <div class="form-group">
        <label class="form-label">수신자 이름 / 별명 (비워두면 봇 프로필명 자동 조회)</label>
        <input type="text" id="newName" class="form-input" placeholder="예: 김환진 대표">
      </div>
      <div class="form-group">
        <label class="form-label">그룹 태그</label>
        <input type="text" id="newGroup" class="form-input" value="GENERAL">
      </div>
      <div style="display:flex; justify-content:flex-end; gap:8px; margin-top:20px;">
        <button class="btn btn-secondary" onclick="closeModals()">취소</button>
        <button class="btn btn-primary" onclick="submitAddRecipient()">등록</button>
      </div>
    </div>
  </div>

  <script>
    function updateClock() {
      const now = new Date();
      document.getElementById('liveClock').textContent = now.toLocaleTimeString('ko-KR') + ' KST';
    }
    setInterval(updateClock, 1000);
    updateClock();

    function toggleTheme() {
      const cur = document.documentElement.getAttribute('data-theme');
      const target = cur === 'dark' ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', target);
      document.getElementById('themeBtn').textContent = target === 'dark' ? '☀️' : '🌙';
    }

    function switchTab(tabId) {
      document.querySelectorAll('.tab-content').forEach(el => el.style.display = 'none');
      document.querySelectorAll('.nav-tab').forEach(el => el.classList.remove('active'));
      document.getElementById(tabId).style.display = 'block';
      event.target.classList.add('active');

      if (tabId === 'tab-recipients') loadRecipients();
      if (tabId === 'tab-history') loadHistory();
      if (tabId === 'tab-settings') loadSettings();
    }

    async function loadStatus() {
      try {
        const res = await fetch('/api/status');
        const d = await res.json();
        const badge = document.getElementById('botBadge');
        if (d.bot_online) {
          badge.className = 'badge online';
          badge.textContent = `● 봇 연결됨 (@${d.bot_username || 'market_bot'})`;
        } else {
          badge.className = 'badge offline';
          badge.textContent = '⚠️ 봇 토큰 미연결';
        }

        const ghBadge = document.getElementById('ghBadge');
        if (ghBadge) {
          if (d.github_connected) {
            ghBadge.className = 'badge online';
            ghBadge.textContent = '☁️ GitHub 원격 연동됨 (방법 A)';
          } else {
            ghBadge.className = 'badge offline';
            ghBadge.textContent = '⚠️ GitHub 토큰 미연결';
          }
        }

        document.getElementById('morningStatusText').textContent = d.morning_status === 'COMPLETED' ? '발송 완료 ✅' : '미발송 (대기 중)';
        document.getElementById('closingStatusText').textContent = d.closing_status === 'COMPLETED' ? '발송 완료 ✅' : '미발송 (대기 중)';
        document.getElementById('activeRecipientsCount').textContent = `${d.active_recipients_count} 명`;
      } catch (e) {
        console.error(e);
      }
    }

    async function triggerDispatch(kind, isForced = false, forceReason = null) {
      const modeLabel = 'GitHub Actions 원격 자동 배포 & 텔레그램 발송';
      if (!confirm(`[${kind.toUpperCase()}] ${modeLabel}을 진행하시겠습니까?`)) return;
      try {
        const res = await fetch('/api/dispatch/send', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({topic: 'FINANCE', kind, is_forced: isForced, force_reason: forceReason})
        });
        const d = await res.json();
        if (res.ok) {
          if (d.mode === 'GITHUB_ACTIONS_REMOTE') {
            alert(`🚀 [원격 발송 성공!]\n\n${d.message}\n\nGitHub 서버에서 리포트 배포와 텔레그램 발송이 한 번에 완료되므로 404가 발생하지 않습니다.`);
            if (d.actions_url && confirm('GitHub Actions 진행 상황 화면을 새 탭에서 열어보시겠습니까?')) {
              window.open(d.actions_url, '_blank');
            }
          } else {
            alert(`✅ 발송 완료! (성공: ${d.success_count}명, 실패: ${d.fail_count}명, 불명확: ${d.unknown_count}명)`);
          }
          loadStatus();
        } else {
          alert(`⚠️ 발송 중단: ${d.detail || '오류 발생'}`);
        }
      } catch (e) {
        alert('발송 요청 에러: ' + e);
      }
    }

    function openForceModal() { document.getElementById('forceModal').style.display = 'flex'; }
    function closeModals() { document.querySelectorAll('.modal-overlay').forEach(el => el.style.display = 'none'); }

    function executeForceDispatch() {
      const kind = document.getElementById('forceKind').value;
      const reason = document.getElementById('forceReason').value.trim();
      if (!reason) { alert('재발송 사유를 반드시 입력해야 합니다.'); return; }
      closeModals();
      triggerDispatch(kind, true, reason);
    }

    async function openPreviewModal() {
      document.getElementById('previewModal').style.display = 'flex';
      loadPreview('morning');
    }

    async function loadPreview(kind) {
      const el = document.getElementById('previewContent');
      el.textContent = '실시간 시세 수집 및 미리보기 렌더링 중...';
      try {
        const res = await fetch(`/api/dispatch/preview?kind=${kind}`);
        const d = await res.json();
        el.textContent = d.telegram_html;
      } catch (e) {
        el.textContent = '미리보기 실패: ' + e;
      }
    }

    async function loadRecipients() {
      const tbody = document.getElementById('recipientsTableBody');
      try {
        const res = await fetch('/api/recipients');
        const list = await res.json();
        if (list.length === 0) {
          tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;">등록된 수신자가 없습니다.</td></tr>';
          return;
        }
        tbody.innerHTML = list.map(r => `
          <tr>
            <td>
              <div style="display:flex; align-items:center; gap:6px;">
                <span style="font-weight:700;">${r.name}</span>
                <button class="btn btn-secondary" style="padding:2px 6px; font-size:11px;" title="관리자 이름 수정" onclick="editRecipientName('${r.chat_id}', '${r.name.replace(/'/g, "\\'")}')">✏️</button>
                <button class="btn btn-secondary" style="padding:2px 6px; font-size:11px;" title="텔레그램 봇 이름으로 다시 동기화" onclick="resetRecipientName('${r.chat_id}')">🔄</button>
              </div>
            </td>
            <td><code>${r.chat_id}</code></td>
            <td>${r.channel}</td>
            <td><span class="badge" style="background:rgba(0,0,0,0.05);">${r.group_tag}</span></td>
            <td>${r.subscribed_topics.join(', ')}</td>
            <td>
              <span class="badge ${r.is_active ? 'online' : 'offline'}">${r.is_active ? '수신 중' : '해지됨'}</span>
            </td>
            <td>
              <button class="btn btn-secondary" style="padding:4px 8px; font-size:11px;" onclick="toggleRecipient('${r.chat_id}')">
                ${r.is_active ? '비활성화' : '활성화'}
              </button>
            </td>
          </tr>
        `).join('');
        loadGithubSyncStatus();
      } catch (e) {
        tbody.innerHTML = '<tr><td colspan="7" style="text-align:center; color:red;">수신자 목록 조회 실패</td></tr>';
      }
    }

    async function loadGithubSyncStatus() {
      const badge = document.getElementById('ghSyncBadge');
      if (!badge) return;
      try {
        const res = await fetch('/api/recipients/github-sync-status');
        const d = await res.json();
        if (!d.github_connected) {
          badge.className = 'badge offline';
          badge.textContent = '⚠️ GitHub 토큰 미연결 (크론 수동 관리)';
        } else if (d.is_synced) {
          badge.className = 'badge online';
          badge.style.background = '#d1fae5';
          badge.style.color = '#065f46';
          badge.textContent = `☁️ GitHub 크론 동기화 일치 (${d.local_count}명)`;
        } else {
          badge.className = 'badge';
          badge.style.background = '#fef3c7';
          badge.style.color = '#92400e';
          badge.textContent = `⚠️ GitHub 크론 수신자 불일치 (로컬 ${d.local_count}명 vs 깃허브 ${d.github_count}명)`;
        }
      } catch (e) {
        badge.className = 'badge offline';
        badge.textContent = '☁️ 동기화 상태 확인 실패';
      }
    }

    async function syncRecipientsToGithub() {
      if (!confirm('현재 활성화된 수신자 목록을 GitHub Actions 크론 환경(TELEGRAM_CHAT_ID)에 즉시 동기화하시겠습니까?\n\n동기화 후 매일 15:42 장마감 / 06:30 모닝 크론 실행 시 관리자 설정과 동일한 인원에게 자동 발송됩니다.')) return;
      try {
        const res = await fetch('/api/recipients/sync-github', {method: 'POST'});
        const d = await res.json();
        if (res.ok) {
          alert(d.message);
          loadGithubSyncStatus();
        } else {
          alert('동기화 실패: ' + (d.detail || '오류 발생'));
        }
      } catch (e) {
        alert('동기화 요청 에러: ' + e);
      }
    }

    function openAddRecipientModal() {
      document.getElementById('newChatId').value = '';
      document.getElementById('newName').value = '';
      document.getElementById('newGroup').value = 'GENERAL';
      document.getElementById('addRecipientModal').style.display = 'flex';
    }

    async function submitAddRecipient() {
      const chatId = document.getElementById('newChatId').value.trim();
      const name = document.getElementById('newName').value.trim();
      const groupTag = document.getElementById('newGroup').value.trim() || 'GENERAL';
      if (!chatId) {
        alert('Chat ID를 입력해주세요.');
        return;
      }
      closeModals();
      try {
        const res = await fetch('/api/recipients', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({chat_id: chatId, name: name || '수신자', group_tag: groupTag, subscribed_topics: 'FINANCE'})
        });
        const d = await res.json();
        if (res.ok) {
          alert(`✅ 수신자 등록 완료: ${d.name} (${chatId})`);
          loadRecipients();
        } else {
          alert('등록 실패: ' + (d.detail || '오류'));
        }
      } catch (e) {
        alert('등록 에러: ' + e);
      }
    }

    async function editRecipientName(chatId, currentName) {
      const newName = prompt(`[Chat ID: ${chatId}] 수신자의 이름을 수정하세요:`, currentName);
      if (newName === null) return;
      const trimmed = newName.trim();
      if (!trimmed) {
        alert('이름은 비워둘 수 없습니다.');
        return;
      }
      try {
        const res = await fetch(`/api/recipients/${chatId}/name`, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({name: trimmed})
        });
        const d = await res.json();
        if (res.ok) {
          loadRecipients();
        } else {
          alert('이름 수정 실패: ' + (d.detail || '오류'));
        }
      } catch (e) {
        alert('이름 수정 에러: ' + e);
      }
    }

    async function resetRecipientName(chatId) {
      if (!confirm('텔레그램 봇에서 최신 프로필 이름을 다시 조회하여 디폴트 이름으로 재설정하시겠습니까?')) return;
      try {
        const res = await fetch(`/api/recipients/${chatId}/reset-name`, {method: 'POST'});
        const d = await res.json();
        if (res.ok) {
          alert(`✅ 봇 프로필 이름으로 복원되었습니다: ${d.name}`);
          loadRecipients();
        } else {
          alert('초기화 실패: ' + (d.detail || '오류'));
        }
      } catch (e) {
        alert('초기화 에러: ' + e);
      }
    }

    async function toggleRecipient(chatId) {
      await fetch(`/api/recipients/${chatId}/toggle`, {method: 'POST'});
      loadRecipients();
      loadStatus();
      loadGithubSyncStatus();
    }

    async function discoverBotUsers() {
      const res = await fetch('/api/recipients/discover', {method: 'POST'});
      const d = await res.json();
      if (!d.candidates || d.candidates.length === 0) {
        alert('최근 봇에게 말을 건 신규 사용자가 없습니다. 텔레그램에서 봇에게 /start를 보낸 뒤 다시 시도하세요.');
        return;
      }
      let msg = '📋 감지된 대화방 목록:\n';
      d.candidates.forEach(c => {
        msg += `- [${c.chat_id}] ${c.title} (${c.is_registered ? '이미 등록됨' : '신규 감지'})\n`;
      });
      alert(msg);
      loadRecipients();
    }

    async function loadHistory() {
      const tbody = document.getElementById('historyTableBody');
      try {
        const res = await fetch('/api/history');
        const list = await res.json();
        if (list.length === 0) {
          tbody.innerHTML = '<tr><td colspan="9" style="text-align:center;">발송 이력이 없습니다.</td></tr>';
          return;
        }
        tbody.innerHTML = list.map(j => `
          <tr>
            <td>${j.started_at}</td>
            <td><strong>${j.topic}</strong> (${j.kind}) ${j.is_forced ? '<span style="color:red;">[강제]</span>' : ''}</td>
            <td><span class="badge ${j.status === 'SENT' ? 'online' : 'offline'}">${j.status}</span></td>
            <td>${j.total_targets}명</td>
            <td style="color:#10b981; font-weight:700;">${j.success_count}</td>
            <td style="color:#ef4444; font-weight:700;">${j.fail_count}</td>
            <td style="color:#f59e0b; font-weight:700;">${j.unknown_count}</td>
            <td>${j.report_path ? `<a href="/reports/${j.report_path.split(/[\\\\/]/).pop()}" target="_blank" style="color:var(--primary); font-weight:600;">HTML 리포트</a>` : '-'}</td>
            <td><code>${j.job_id.substring(0,8)}</code></td>
          </tr>
        `).join('');
      } catch (e) {
        tbody.innerHTML = '<tr><td colspan="9" style="text-align:center; color:red;">이력 조회 실패</td></tr>';
      }
    }

    let globalThresholds = {};
    async function loadSettings() {
      const container = document.getElementById('thresholdsEditor');
      try {
        const res = await fetch('/api/settings');
        const d = await res.json();
        globalThresholds = d.thresholds;
        container.innerHTML = Object.entries(globalThresholds).map(([sym, cfg]) => `
          <div style="border:1px solid var(--card-border); border-radius:8px; padding:12px;">
            <div style="font-weight:700; margin-bottom:8px;">${sym} (${cfg.unit})</div>
            <div class="form-group">
              <label class="form-label">임계치 (${cfg.unit === 'PERCENT' ? '±%' : '±bp'})</label>
              <input type="number" step="0.1" class="form-input" value="${cfg.threshold}" onchange="updateThreshold('${sym}', 'threshold', parseFloat(this.value))">
            </div>
            <div class="form-group">
              <label class="form-label">쿨다운 (분)</label>
              <input type="number" class="form-input" value="${cfg.cooldown_minutes || 120}" onchange="updateThreshold('${sym}', 'cooldown_minutes', parseInt(this.value))">
            </div>
          </div>
        `).join('');
      } catch (e) {
        container.innerHTML = '설정 로드 실패';
      }
    }

    function updateThreshold(sym, key, val) {
      if (globalThresholds[sym]) globalThresholds[sym][key] = val;
    }

    async function saveSettings() {
      await fetch('/api/settings', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({thresholds: globalThresholds})
      });
      alert('✅ 임계치 설정이 안전하게 저장되었습니다!');
    }

    loadStatus();
  </script>
</body>
</html>
"""

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
        print(f"⚠️ [포트 충돌 방어] 포트 {configured_port}번이 이미 사용 중이어서 빈 포트 {port}번으로 자동 전환했습니다.")
    print(f"🚀 [Market Brief Admin Console] Starting server on http://localhost:{port}")
    app = create_admin_app()
    uvicorn.run(app, host="127.0.0.1", port=port)
