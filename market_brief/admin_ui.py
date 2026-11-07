"""Modern, Simple & Intuitive 3-Tab Admin Console UI (Alternative A).

Tab Structure:
1. 👥 유저 & 등급 관리 (Default Tab - Search, 1-Click Tier Dropdown, Slot Gauge, Bot Subscriptions)
2. 🤖 봇 목록 & 발송 (Integrated Hub - Menu Bot Card, Info Bot Cards with direct [📢 메시지 발송] & [⚙️ 설정])
3. 📊 운영 현황 (Clean KPI Cards, Tier Distribution, Bot Popularity, Activity Log, Collapsible Advanced Settings)
+ Floating Simulator Modal [🧪 대화 시뮬레이터] for instant testing without page clutter.
"""

ADMIN_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover" />
  <meta name="theme-color" content="#0f172a" />
  <meta name="apple-mobile-web-app-capable" content="yes" />
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent" />
  <title>텔레그램 봇 구독 서비스 관리자</title>
  <style>
    :root {
      --bg: #f8fafc;
      --surface: #ffffff;
      --surface-sub: #f1f5f9;
      --border: #e2e8f0;
      --border-dark: #cbd5e1;
      --text: #0f172a;
      --text-muted: #64748b;
      --primary: #2563eb;
      --primary-hover: #1d4ed8;
      --primary-soft: #eff6ff;
      --success: #10b981;
      --success-soft: #ecfdf5;
      --warning: #f59e0b;
      --warning-soft: #fffbeb;
      --danger: #ef4444;
      --danger-soft: #fef2f2;
      --radius: 12px;
      --shadow: 0 1px 3px rgba(15, 23, 42, 0.05), 0 6px 16px rgba(15, 23, 42, 0.03);
    }

    * { box-sizing: border-box; }
    html, body {
      overflow-x: hidden;
      max-width: 100vw;
      width: 100%;
    }
    body {
      margin: 0;
      font-family: 'Pretendard', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Noto Sans KR", sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
    }
    .desktop-only { display: inline; }
    .mobile-only { display: none; }
    .desktop-only-cell { display: table-cell; }
    .mobile-only-flex { display: none; }

    /* Clean Minimal Header */
    .header {
      background: #0f172a;
      color: #fff;
      padding: 14px 28px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 12px;
      position: sticky;
      top: 0;
      z-index: 100;
    }
    .header-brand {
      display: flex;
      align-items: center;
      gap: 12px;
    }
    .header-logo {
      font-size: 24px;
      background: linear-gradient(135deg, #3b82f6, #6366f1);
      width: 38px;
      height: 38px;
      border-radius: 9px;
      display: flex;
      align-items: center;
      justify-content: center;
    }
    .header-title {
      font-size: 17px;
      font-weight: 800;
      letter-spacing: -0.3px;
    }
    .header-sub {
      font-size: 12px;
      color: #94a3b8;
    }
    .header-actions {
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
    }
    .pill {
      background: rgba(255, 255, 255, 0.08);
      color: #e2e8f0;
      font-size: 12px;
      font-weight: 600;
      padding: 5px 12px;
      border-radius: 999px;
      display: inline-flex;
      align-items: center;
      gap: 6px;
    }
    .dot { width: 8px; height: 8px; border-radius: 50%; background: #10b981; }

    /* 3 Intuitive Tabs */
    .tab-bar {
      background: #fff;
      border-bottom: 1px solid var(--border);
      padding: 0 28px;
      display: flex;
      gap: 8px;
    }
    .tab-btn {
      padding: 15px 22px;
      border: none;
      background: transparent;
      font-size: 15px;
      font-weight: 700;
      color: var(--text-muted);
      cursor: pointer;
      border-bottom: 3px solid transparent;
      display: inline-flex;
      align-items: center;
      gap: 8px;
      transition: all 0.15s;
    }
    .tab-btn:hover { color: var(--text); background: var(--surface-sub); }
    .tab-btn.active {
      color: var(--primary);
      border-bottom-color: var(--primary);
      background: var(--primary-soft);
    }

    /* Container */
    .container {
      max-width: 1240px;
      margin: 24px auto;
      padding: 0 24px 80px;
    }
    .tab-pane { display: none; }
    .tab-pane.active { display: block; animation: fadeIn 0.18s ease; }
    @keyframes fadeIn { from { opacity: 0; transform: translateY(3px); } to { opacity: 1; transform: translateY(0); } }

    /* Cards */
    .card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 22px;
      box-shadow: var(--shadow);
      margin-bottom: 20px;
    }
    .card-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      margin-bottom: 18px;
      flex-wrap: wrap;
    }
    .card-title {
      font-size: 16px;
      font-weight: 800;
      margin: 0;
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .card-desc {
      font-size: 13px;
      color: var(--text-muted);
      margin: 4px 0 0;
    }

    /* Buttons */
    .btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      padding: 8px 14px;
      border-radius: 8px;
      font-size: 13px;
      font-weight: 700;
      cursor: pointer;
      border: 1px solid var(--border);
      background: #fff;
      color: var(--text);
      transition: all 0.14s;
    }
    .btn:hover { background: var(--surface-sub); border-color: var(--border-dark); }
    .btn-sm { padding: 5px 10px; font-size: 12px; border-radius: 6px; }
    .btn-primary { background: var(--primary); color: #fff; border-color: var(--primary); }
    .btn-primary:hover { background: var(--primary-hover); color: #fff; }
    .btn-success { background: var(--success); color: #fff; border-color: var(--success); }
    .btn-success:hover { background: #059669; color: #fff; }
    .btn-danger { background: var(--danger-soft); color: var(--danger); border-color: #fecaca; }
    .btn-danger:hover { background: var(--danger); color: #fff; }
    .btn-dark { background: #1e293b; color: #fff; border-color: #1e293b; }
    .btn-dark:hover { background: #0f172a; }

    /* Form Controls */
    .form-input, .form-select, .form-textarea {
      width: 100%;
      padding: 9px 12px;
      border: 1px solid var(--border);
      border-radius: 8px;
      font-size: 13.5px;
      font-family: inherit;
      background: #fff;
      color: var(--text);
    }
    .form-input:focus, .form-select:focus, .form-textarea:focus {
      outline: none;
      border-color: var(--primary);
      box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.12);
    }
    .form-label { display: block; font-size: 12px; font-weight: 700; color: #334155; margin-bottom: 5px; }
    .form-group { margin-bottom: 14px; }

    /* Tables */
    .table-wrap { overflow-x: auto; border: 1px solid var(--border); border-radius: 10px; }
    table { width: 100%; border-collapse: collapse; font-size: 13.5px; background: #fff; }
    th {
      background: var(--surface-sub);
      color: #475569;
      font-weight: 700;
      text-align: left;
      padding: 12px 14px;
      border-bottom: 1px solid var(--border);
      white-space: nowrap;
    }
    td {
      padding: 13px 14px;
      border-bottom: 1px solid var(--border);
      vertical-align: middle;
    }
    tr:last-child td { border-bottom: none; }
    tr:hover td { background: #fbfdff; }

    /* Tier Badge */
    .tier-badge {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      padding: 3px 9px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 800;
    }
    .tier-WHITE { background: #f1f5f9; color: #334155; border: 1px solid #cbd5e1; }
    .tier-GREEN { background: #dcfce7; color: #065f46; border: 1px solid #86efac; }
    .tier-RED { background: #fee2e2; color: #991b1b; border: 1px solid #fca5a5; }
    .tier-BLACK { background: #0f172a; color: #f8fafc; border: 1px solid #334155; }

    /* Bot Chips */
    .bot-chip {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      background: #eff6ff;
      color: #1e40af;
      border: 1px solid #bfdbfe;
      padding: 3px 9px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 700;
      margin: 2px;
    }
    .bot-chip-del {
      cursor: pointer;
      color: #ef4444;
      font-weight: 900;
      margin-left: 3px;
      padding: 0 4px;
      border-radius: 4px;
    }
    .bot-chip-del:hover { background: #fee2e2; }

    /* Slot Progress */
    .slot-bar {
      width: 100px;
      height: 6px;
      background: #e2e8f0;
      border-radius: 999px;
      overflow: hidden;
      margin-top: 4px;
    }
    .slot-bar-fill { height: 100%; border-radius: 999px; }

    /* Filter & Search Bar */
    .user-filter-bar {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      flex-wrap: wrap;
      margin-bottom: 16px;
    }
    .user-search-wrap { width: 260px; }
    .filter-chips { display: flex; gap: 6px; flex-wrap: wrap; }
    .filter-btn {
      padding: 6px 13px;
      border-radius: 999px;
      border: 1px solid var(--border);
      background: #fff;
      font-size: 12.5px;
      font-weight: 700;
      cursor: pointer;
      color: var(--text-muted);
      transition: all 0.12s;
    }
    .filter-btn:hover { background: var(--surface-sub); color: var(--text); }
    .filter-btn.active { background: var(--primary); color: #fff; border-color: var(--primary); }

    /* KPI Grid & Dashboard 2-Col */
    .kpi-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
      gap: 16px;
      margin-bottom: 20px;
    }
    .dash-grid-2col {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 20px;
      margin-bottom: 20px;
    }

    /* Modal Form Grids */
    .form-grid-2col {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
    }
    .form-grid-bot-meta {
      display: grid;
      grid-template-columns: 80px 1fr 120px;
      gap: 10px;
    }
    .dispatch-actions-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
    }
    .sim-actions-row {
      display: flex;
      gap: 6px;
      margin-bottom: 10px;
      flex-wrap: wrap;
    }

    /* Bot Card Actions */
    .bot-card-actions {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 6px;
      border-top: 1px solid var(--border);
      padding-top: 12px;
    }
    .bot-card-sub-actions {
      display: flex;
      gap: 4px;
    }

    /* Menu Bot Box */
    .menu-bot-box {
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 14px;
      padding: 6px 0;
    }
    .menu-bot-info {
      display: flex;
      align-items: center;
      gap: 14px;
    }
    .menu-bot-btns {
      display: flex;
      gap: 8px;
    }

    /* Bot Cards Grid */
    .bot-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
      gap: 16px;
    }
    .bot-card {
      background: #fff;
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 18px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      gap: 14px;
      box-shadow: var(--shadow);
    }
    .bot-card.menu-portal {
      background: linear-gradient(135deg, #eff6ff 0%, #ffffff 70%);
      border: 2px solid #93c5fd;
    }

    /* Floating Simulator Button */
    .float-sim-btn {
      position: fixed;
      bottom: 24px;
      right: 24px;
      background: #0f172a;
      color: #fff;
      border: 2px solid #334155;
      padding: 11px 18px;
      border-radius: 999px;
      font-size: 13.5px;
      font-weight: 800;
      cursor: pointer;
      box-shadow: 0 8px 24px rgba(15, 23, 42, 0.25);
      z-index: 150;
      display: flex;
      align-items: center;
      gap: 8px;
      transition: all 0.15s;
    }
    .float-sim-btn:hover {
      background: #1e293b;
      transform: translateY(-2px);
      box-shadow: 0 12px 28px rgba(15, 23, 42, 0.32);
    }

    /* Modal Backdrop */
    .modal-backdrop {
      display: none;
      position: fixed;
      inset: 0;
      background: rgba(15, 23, 42, 0.55);
      z-index: 200;
      align-items: center;
      justify-content: center;
      padding: 16px;
    }
    .modal-backdrop.open { display: flex; }
    .modal-box {
      background: #fff;
      border-radius: 14px;
      width: 100%;
      max-width: 520px;
      max-height: 90vh;
      overflow-y: auto;
      padding: 24px;
      box-shadow: 0 20px 40px rgba(15, 23, 42, 0.25);
    }

    /* Chat Simulator Window */
    .sim-chat-box {
      background: #e5ddd5;
      border-radius: 10px;
      padding: 16px;
      min-height: 280px;
      max-height: 380px;
      overflow-y: auto;
      border: 1px solid var(--border);
      margin-bottom: 12px;
    }
    .sim-bubble {
      background: #fff;
      border-radius: 10px 10px 10px 2px;
      padding: 10px 14px;
      max-width: 90%;
      font-size: 13px;
      margin-bottom: 8px;
      box-shadow: 0 1px 2px rgba(0,0,0,0.08);
      white-space: pre-wrap;
    }
    .sim-user-bubble {
      background: #dcf8c6;
      border-radius: 10px 10px 2px 10px;
      padding: 8px 12px;
      margin-left: auto;
      margin-bottom: 8px;
      width: fit-content;
      max-width: 80%;
      font-size: 12.5px;
      font-weight: 700;
    }
    .sim-kb-btn {
      display: block;
      width: 100%;
      background: #fff;
      border: 1px solid #cbd5e1;
      border-radius: 6px;
      padding: 7px 10px;
      font-size: 12px;
      font-weight: 700;
      color: #1d4ed8;
      cursor: pointer;
      text-align: center;
      margin-top: 4px;
    }
    .sim-kb-btn:hover { background: #eff6ff; }

    /* Toast */
    #toastWrap {
      position: fixed;
      bottom: 24px;
      left: 24px;
      z-index: 300;
      display: flex;
      flex-direction: column;
      gap: 8px;
    }
    .toast {
      background: #0f172a;
      color: #fff;
      padding: 11px 16px;
      border-radius: 8px;
      font-size: 13px;
      font-weight: 600;
      box-shadow: 0 4px 12px rgba(0,0,0,0.18);
    }
    /* Mobile Bottom Navigation Bar (App Style) */
    .bottom-nav {
      display: none;
      position: fixed;
      bottom: 0;
      left: 0;
      right: 0;
      background: #ffffff;
      border-top: 1px solid var(--border);
      height: 60px;
      z-index: 180;
      justify-content: space-around;
      align-items: center;
      padding-bottom: env(safe-area-inset-bottom, 0px);
      box-shadow: 0 -2px 10px rgba(0, 0, 0, 0.05);
    }
    .bnav-btn {
      flex: 1;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      height: 100%;
      background: transparent;
      border: none;
      color: var(--text-muted);
      cursor: pointer;
      gap: 3px;
      padding: 0;
      -webkit-tap-highlight-color: transparent;
    }
    .bnav-btn.active {
      color: var(--primary);
    }
    .bnav-icon {
      font-size: 19px;
      line-height: 1;
    }
    .bnav-label {
      font-size: 11px;
      font-weight: 700;
    }

    /* Pending Upgrade Badges (Desktop Tab & Mobile Bottom Nav) */
    .tab-badge,
    .bnav-badge {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 18px;
      height: 18px;
      padding: 0 5px;
      border-radius: 9px;
      background: #ef4444;
      color: #fff;
      font-size: 11px;
      font-weight: 800;
      line-height: 1;
      box-sizing: border-box;
    }
    .tab-badge {
      margin-left: 6px;
      vertical-align: middle;
    }
    .bnav-btn {
      position: relative;
    }
    .bnav-badge {
      position: absolute;
      top: 4px;
      left: calc(50% + 6px);
      min-width: 16px;
      height: 16px;
      font-size: 10px;
      border: 1.5px solid #fff;
    }

    /* Deep-link Gold Pulse Highlight (3s) */
    @keyframes pulseGold {
      0%   { box-shadow: 0 0 0 0 rgba(245, 158, 11, 0.0); }
      25%  { box-shadow: 0 0 0 4px rgba(245, 158, 11, 0.4); }
      50%  { box-shadow: 0 0 0 8px rgba(245, 158, 11, 0.15); }
      75%  { box-shadow: 0 0 0 4px rgba(245, 158, 11, 0.4); }
      100% { box-shadow: 0 0 0 0 rgba(245, 158, 11, 0.0); }
    }
    .req-card-highlight {
      animation: pulseGold 1s ease-in-out 3;
      border-color: #f59e0b !important;
    }

    /* 7-Bot Traffic Light & Scheduler Countdown Strip */
    .bot-health-strip {
      background: #1e293b;
      color: #e2e8f0;
      padding: 6px 28px;
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 6px 12px;
      font-size: 12px;
      max-width: 100%;
      box-sizing: border-box;
    }
    .bot-traffic-lights {
      display: flex;
      flex-wrap: wrap;
      gap: 4px;
      min-width: 0;
    }
    .bot-light-chip {
      display: inline-flex;
      align-items: center;
      gap: 3px;
      padding: 2px 7px;
      border-radius: 999px;
      background: rgba(255, 255, 255, 0.08);
      font-weight: 700;
      white-space: nowrap;
    }
    .bot-light-chip.down {
      background: rgba(239, 68, 68, 0.18);
      color: #fecaca;
    }
    .scheduler-timer-wrap {
      font-weight: 700;
      color: #fde68a;
      min-width: 0;
      overflow-wrap: anywhere;
    }

    /* History Center (Dispatch & Audit) */
    .hist-subtabs {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 6px;
      background: var(--surface-sub);
      padding: 4px;
      border-radius: 10px;
      margin-bottom: 14px;
    }
    .hist-subtab-btn {
      min-height: 44px;
      min-width: 0;
      border: none;
      border-radius: 8px;
      background: transparent;
      font-size: 13px;
      font-weight: 700;
      color: var(--text-muted);
      cursor: pointer;
      padding: 6px 8px;
      overflow-wrap: anywhere;
    }
    .hist-subtab-btn.active { background: #fff; color: var(--primary); box-shadow: 0 1px 3px rgba(15, 23, 42, 0.08); }
    .hist-subpane { display: none; }
    .hist-subpane.active { display: block; }
    .hist-filter-bar {
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(0, 220px) auto;
      gap: 8px;
      align-items: center;
      margin-bottom: 12px;
    }
    #subpane-dispatch .hist-filter-bar { grid-template-columns: minmax(0, 1fr) auto; }
    .hist-touch { min-height: 44px; min-width: 44px; }
    .hist-close-btn { min-width: 44px; min-height: 44px; font-size: 16px; flex-shrink: 0; }
    .hist-modal-head {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 10px;
      margin-bottom: 12px;
    }
    .hist-table td { overflow-wrap: anywhere; word-break: break-word; }
    .hist-badge {
      display: inline-block;
      font-size: 11px;
      font-weight: 800;
      padding: 2px 8px;
      border-radius: 999px;
      border: 1px solid var(--border-dark);
      background: var(--surface-sub);
      color: #334155;
      white-space: nowrap;
    }
    .hist-badge.ok { background: var(--success-soft); color: #047857; border-color: #a7f3d0; }
    .hist-badge.warn { background: var(--warning-soft); color: #b45309; border-color: #fcd34d; }
    .hist-badge.bad { background: var(--danger-soft); color: #b91c1c; border-color: #fecaca; }
    .hist-badge.info { background: var(--primary-soft); color: #1d4ed8; border-color: #bfdbfe; }
    .hist-diff-line { font-size: 12.5px; line-height: 1.5; overflow-wrap: anywhere; }
    .hist-diff-from { color: #b91c1c; text-decoration: line-through; }
    .hist-diff-to { color: #047857; font-weight: 700; }
    .hist-pager {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 8px;
      margin-top: 12px;
    }
    .hist-card-list { display: grid; grid-template-columns: minmax(0, 1fr); gap: 8px; }
    .hist-card {
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 10px 12px;
      background: #fff;
      min-width: 0;
      overflow-wrap: anywhere;
    }
    .hist-card-top {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
      margin-bottom: 4px;
    }
    .hist-meta { font-size: 11.5px; color: var(--text-muted); }

    /* Mobile Responsive Overrides */
    @media (max-width: 768px) {
      .desktop-only { display: none !important; }
      .mobile-only { display: inline !important; }
      .desktop-only-cell { display: none !important; }
      .mobile-only-flex { display: flex !important; }

      html, body {
        overflow-x: hidden !important;
        max-width: 100vw !important;
        width: 100% !important;
      }

      .header {
        padding: 8px 12px;
        flex-wrap: nowrap;
        gap: 8px;
        max-width: 100vw;
        box-sizing: border-box;
      }
      .header-brand {
        display: flex;
        align-items: center;
        gap: 8px;
        min-width: 0;
        flex: 1 1 auto;
        overflow: hidden;
      }
      .header-logo {
        width: 32px;
        height: 32px;
        font-size: 18px;
        border-radius: 8px;
        flex-shrink: 0;
      }
      .header-title {
        font-size: 14px;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
        min-width: 0;
      }
      .header-sub {
        display: none;
      }
      .header-actions {
        gap: 6px;
        margin-left: auto;
        flex-shrink: 0;
        flex-wrap: nowrap;
        display: flex;
        align-items: center;
      }
      .pill {
        padding: 4px 6px;
        font-size: 11px;
        gap: 4px;
        flex-shrink: 0;
      }
      .pill #topStatusText {
        display: none;
      }
      .header-actions .btn {
        width: 34px;
        height: 34px;
        min-height: 34px;
        padding: 0;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        font-size: 14px;
        border-radius: 8px;
        flex-shrink: 0;
      }
      .header-actions .btn .btn-text {
        display: none !important;
      }

      .bot-health-strip {
        padding: 5px 12px;
        font-size: 11px;
        max-width: 100vw;
      }
      .bot-light-chip {
        padding: 2px 5px;
      }

      /* Hide Desktop Top Tab Bar on Mobile */
      .tab-bar {
        display: none !important;
      }

      /* Show Mobile App Bottom Navigation Bar */
      .bottom-nav {
        display: flex;
      }

      /* Container spacing for bottom nav & safe-area */
      .container {
        margin: 10px auto;
        padding: 0 10px calc(80px + env(safe-area-inset-bottom, 0px));
        max-width: 100vw;
        box-sizing: border-box;
      }

      /* Floating sim button hidden (integrated in bottom nav) */
      .float-sim-btn {
        display: none !important;
      }

      /* Cards on mobile */
      .card {
        padding: 14px;
        border-radius: 12px;
        margin-bottom: 12px;
        max-width: 100%;
        box-sizing: border-box;
      }
      .card-header {
        display: flex;
        flex-direction: column;
        align-items: stretch;
        gap: 10px;
        margin-bottom: 12px;
      }
      .card-header > .btn {
        width: 100%;
        min-height: 38px;
        font-size: 13px;
        justify-content: center;
      }
      .card-title {
        font-size: 15px;
      }
      .card-desc {
        font-size: 12px;
      }

      /* User Filter Bar on Mobile - Segmented Grid (Zero Horizontal Slide) */
      .user-filter-bar {
        flex-direction: column-reverse;
        align-items: stretch;
        gap: 8px;
        margin-bottom: 14px;
        width: 100%;
        box-sizing: border-box;
      }
      .user-search-wrap {
        width: 100%;
      }
      .filter-chips {
        display: grid;
        grid-template-columns: repeat(5, 1fr);
        gap: 4px;
        width: 100%;
        overflow-x: visible;
        padding-bottom: 0;
        box-sizing: border-box;
      }
      .filter-btn {
        white-space: nowrap;
        padding: 7px 2px;
        font-size: 11px;
        text-align: center;
        justify-content: center;
        border-radius: 8px;
        min-width: 0;
        width: 100%;
        box-sizing: border-box;
        font-weight: 700;
      }
      @media (max-width: 350px) {
        .filter-btn {
          font-size: 10px;
          padding: 6px 1px;
        }
      }

      /* User Table -> Mobile Card View Transformation */
      .table-wrap {
        border: none;
        background: transparent;
        border-radius: 0;
        overflow: visible;
      }
      .user-table,
      .user-table thead,
      .user-table tbody,
      .user-table th,
      .user-table td,
      .user-table tr {
        display: block;
      }
      .user-table thead {
        display: none;
      }
      .user-table tr {
        background: #ffffff;
        border: 1px solid var(--border);
        border-radius: 12px;
        margin-bottom: 10px;
        padding: 10px 12px;
        box-shadow: 0 1px 4px rgba(15, 23, 42, 0.04);
      }
      .user-table td {
        padding: 5px 0;
        border: none;
        border-bottom: 1px dashed #f1f5f9;
        display: flex;
        justify-content: space-between;
        align-items: center;
        font-size: 13px;
        flex-wrap: wrap;
        gap: 6px;
      }
      .user-table td::before {
        content: attr(data-label);
        font-weight: 700;
        color: var(--text-muted);
        font-size: 12px;
        flex-shrink: 0;
      }
      .user-table td.user-header-td {
        padding-top: 0;
        padding-bottom: 8px;
        border-bottom: 1px solid #e2e8f0;
        flex-direction: row;
        justify-content: space-between;
        align-items: center;
        width: 100%;
        flex-wrap: nowrap;
      }
      .user-table td.user-header-td::before {
        display: none;
      }
      .user-tier-select-mobile {
        width: auto;
        min-width: 95px;
        font-size: 11.5px;
        font-weight: 700;
        padding: 3px 6px;
        border-radius: 6px;
        border: 1px solid #cbd5e1;
        background-color: #f8fafc;
        color: #1e293b;
      }
      .user-table td.user-subs-cell {
        flex-direction: column;
        align-items: flex-start;
        padding: 6px 0;
      }
      .user-table td.user-subs-cell > div {
        width: 100%;
        margin-top: 2px;
      }
      .user-table td.user-expiry-cell {
        display: inline-flex;
        width: auto;
        border-bottom: none;
        padding: 3px 0;
        font-size: 11.5px;
      }
      .user-table td.user-actions-cell {
        border-bottom: none;
        border-top: 1px solid #f1f5f9;
        padding-top: 6px;
        padding-bottom: 0;
        margin-top: 2px;
        justify-content: space-between;
        align-items: center;
        width: 100%;
      }
      .user-table td.user-actions-cell::before {
        display: none;
      }
      .user-table td.user-actions-cell .btn {
        min-height: 28px;
        padding: 4px 10px;
        font-size: 11.5px;
      }
      .user-table td.empty-cell {
        display: block !important;
        width: 100% !important;
        border: none !important;
        text-align: center !important;
      }
      .bot-chip {
        padding: 4px 10px;
        font-size: 12.5px;
      }
      .bot-chip-del {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        min-width: 22px;
        min-height: 22px;
        font-size: 14px;
        margin-left: 5px;
        border-radius: 50%;
        touch-action: manipulation;
        cursor: pointer;
      }

      /* Bot Grid 1-column on mobile */
      .bot-grid {
        grid-template-columns: 1fr;
        gap: 12px;
      }
      .bot-card-actions {
        flex-direction: column;
        gap: 8px;
        align-items: stretch;
      }
      .btn-dispatch-center {
        width: 100%;
        padding: 10px 14px;
        font-size: 13.5px;
        min-height: 40px;
      }
      .bot-card-sub-actions {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 8px;
        width: 100%;
      }
      .bot-card-sub-actions .btn {
        min-height: 36px;
        font-size: 13px;
      }

      /* Bot Grid Mobile */
      .bot-grid {
        grid-template-columns: 1fr !important;
        gap: 12px !important;
      }

      /* Menu Bot Mobile */
      .menu-bot-box {
        flex-direction: column;
        align-items: stretch;
        gap: 12px;
      }
      .menu-bot-btns {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 8px;
        width: 100%;
      }
      .menu-bot-btns .btn {
        min-height: 36px;
        font-size: 12.5px;
      }

      /* Dashboard & Grids on Mobile */
      .kpi-grid {
        grid-template-columns: 1fr !important;
        gap: 10px !important;
        margin-bottom: 12px !important;
      }
      .dash-grid-2col {
        grid-template-columns: 1fr !important;
        gap: 12px !important;
        margin-bottom: 12px !important;
      }

      /* Bottom Sheet Modal on Mobile */
      .modal-backdrop {
        align-items: flex-end;
        padding: 0;
      }
      .modal-box {
        border-radius: 20px 20px 0 0;
        width: 100% !important;
        max-width: 100% !important;
        max-height: 88vh;
        overflow-y: auto;
        animation: slideUpModal 0.22s ease-out;
        padding: 18px 16px calc(24px + env(safe-area-inset-bottom, 0px));
      }
      @keyframes slideUpModal {
        from { transform: translateY(100%); }
        to { transform: translateY(0); }
      }

      /* Form Grids on Mobile */
      .form-grid-2col {
        grid-template-columns: 1fr !important;
        gap: 0 !important;
      }
      .form-grid-bot-meta {
        grid-template-columns: 70px 1fr !important;
        gap: 8px !important;
      }
      .form-grid-bot-meta .form-bot-category {
        grid-column: 1 / -1;
      }
      .dispatch-actions-grid {
        grid-template-columns: 1fr !important;
        gap: 10px !important;
      }
      .dispatch-actions-grid .btn {
        width: 100%;
        padding: 12px 14px !important;
        font-size: 13.5px;
        min-height: 44px;
      }
      .sim-actions-row {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 8px;
      }
      .sim-actions-row button:last-child {
        grid-column: 1 / -1;
      }
      .sim-actions-row .btn {
        min-height: 38px;
        font-size: 13px;
      }
      .sim-chat-box {
        min-height: 200px;
        max-height: 280px;
      }

      /* Form controls iOS Zoom Prevention */
      .form-input, .form-select, .form-textarea {
        font-size: 16px;
      }

      /* Tier Specs on Mobile */
      #tierSpecGrid {
        grid-template-columns: 1fr 1fr !important;
        gap: 8px !important;
      }

      /* History Center -> Mobile Card View (360px zero overflow) */
      .hist-filter-bar,
      #subpane-dispatch .hist-filter-bar {
        grid-template-columns: minmax(0, 1fr);
      }
      .hist-table,
      .hist-table thead,
      .hist-table tbody,
      .hist-table th,
      .hist-table td,
      .hist-table tr {
        display: block;
      }
      .hist-table thead { display: none; }
      .hist-table tr {
        background: #ffffff;
        border: 1px solid var(--border);
        border-radius: 12px;
        margin-bottom: 10px;
        padding: 8px 12px;
      }
      .hist-table td {
        display: grid;
        grid-template-columns: 84px minmax(0, 1fr);
        gap: 8px;
        align-items: center;
        padding: 5px 0;
        border: none;
        border-bottom: 1px dashed #f1f5f9;
        font-size: 13px;
      }
      .hist-table td:last-child { border-bottom: none; }
      .hist-table td::before {
        content: attr(data-label);
        font-weight: 700;
        color: var(--text-muted);
        font-size: 12px;
      }
      .hist-table td.empty-cell {
        display: block !important;
        text-align: center !important;
      }
      .hist-table td.empty-cell::before { display: none; }
      .hist-table .btn { min-height: 44px; }
      .user-table td.user-actions-cell .btn.user-history-btn { min-height: 44px; }
    }

    /* 👥 User 44px Slim 1-Line Accordion Hybrid Redesign */
    .user-accordion-list {
      display: flex;
      flex-direction: column;
      gap: 6px;
      margin-top: 10px;
    }
    .user-accordion-item {
      background: #ffffff;
      border: 1px solid var(--border);
      border-radius: 10px;
      overflow: hidden;
      transition: border-color 0.2s, box-shadow 0.2s, background-color 0.2s;
    }
    .user-accordion-item:hover {
      border-color: #cbd5e1;
      box-shadow: 0 2px 8px rgba(15, 23, 42, 0.05);
    }
    .user-accordion-item.is-open {
      border-color: #3b82f6;
      box-shadow: 0 4px 14px rgba(59, 130, 246, 0.12);
    }
    .user-row-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 10px 14px;
      min-height: 44px;
      cursor: pointer;
      user-select: none;
      gap: 8px;
      box-sizing: border-box;
      transition: background 0.15s;
    }
    .user-row-header:hover {
      background: #f8fafc;
    }
    .user-row-left {
      display: flex;
      align-items: center;
      gap: 8px;
      min-width: 0;
      flex: 1 1 auto;
    }
    .user-status-dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      flex-shrink: 0;
    }
    .user-row-name {
      font-weight: 800;
      font-size: 14px;
      color: var(--text);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      min-width: 0;
    }
    .user-row-uname {
      font-size: 12px;
      color: var(--text-muted);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      flex-shrink: 1;
    }
    .user-source-tag {
      font-size: 10.5px;
      padding: 1px 5px;
      border-radius: 4px;
      font-weight: 700;
      flex-shrink: 0;
    }
    .user-row-right {
      display: flex;
      align-items: center;
      gap: 6px;
      flex-shrink: 0;
    }
    .user-accordion-arrow {
      font-size: 11px;
      color: var(--text-muted);
      transition: transform 0.25s cubic-bezier(0.4, 0, 0.2, 1);
      width: 16px;
      text-align: center;
      display: inline-block;
    }
    .user-accordion-item.is-open .user-accordion-arrow {
      transform: rotate(180deg);
      color: #2563eb;
    }
    .user-row-drawer {
      display: none;
      padding: 14px 16px;
      border-top: 1px solid #e2e8f0;
      background: #f8fafc;
      animation: drawerSlide 0.2s ease-out;
    }
    .user-accordion-item.is-open .user-row-drawer {
      display: block;
    }
    @keyframes drawerSlide {
      from { opacity: 0; transform: translateY(-4px); }
      to { opacity: 1; transform: translateY(0); }
    }
    .drawer-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: 12px;
      margin-bottom: 12px;
    }
    .drawer-card {
      background: #ffffff;
      border: 1px solid #e2e8f0;
      border-radius: 8px;
      padding: 10px 12px;
      box-sizing: border-box;
    }
    .drawer-card-title {
      font-size: 11.5px;
      font-weight: 700;
      color: var(--text-muted);
      margin-bottom: 6px;
      display: flex;
      align-items: center;
      gap: 4px;
    }
    .drawer-actions {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      justify-content: flex-end;
      padding-top: 8px;
      border-top: 1px solid #e2e8f0;
      margin-top: 6px;
    }
    .drawer-actions .btn {
      min-height: 34px;
      padding: 4px 12px;
      font-size: 12px;
      border-radius: 6px;
    }

    /* 🚀 Fullscreen App Splash & Cold-Start Loader */
    .app-splash-loader {
      position: fixed;
      inset: 0;
      z-index: 999999;
      background: radial-gradient(circle at 50% 30%, #1e293b 0%, #0b1120 100%);
      display: flex;
      justify-content: center;
      align-items: center;
      padding: 16px;
      box-sizing: border-box;
      transition: opacity 0.4s ease, visibility 0.4s ease;
      overflow-y: auto;
    }
    .app-splash-loader.fade-out {
      opacity: 0;
      visibility: hidden;
      pointer-events: none;
    }
    .splash-card {
      width: 100%;
      max-width: 380px;
      margin: auto;
      background: rgba(15, 23, 42, 0.78);
      border: 1px solid rgba(255, 255, 255, 0.12);
      border-radius: 24px;
      padding: 20px 18px 22px;
      box-shadow: 0 20px 50px rgba(0, 0, 0, 0.6), 0 0 35px rgba(56, 189, 248, 0.18);
      backdrop-filter: blur(16px);
      -webkit-backdrop-filter: blur(16px);
      display: flex;
      flex-direction: column;
      align-items: center;
      text-align: center;
      box-sizing: border-box;
      animation: splashCardEnter 0.5s cubic-bezier(0.16, 1, 0.3, 1);
    }
    @keyframes splashCardEnter {
      from { transform: scale(0.92) translateY(12px); opacity: 0; }
      to { transform: scale(1) translateY(0); opacity: 1; }
    }
    .splash-img-wrap {
      position: relative;
      width: 100%;
      max-width: 270px;
      margin-bottom: 14px;
    }
    .splash-img {
      width: 100%;
      height: auto;
      max-height: 310px;
      border-radius: 18px;
      object-fit: cover;
      display: block;
      box-shadow: 0 10px 25px rgba(0, 0, 0, 0.5);
      animation: splashPulse 3.5s ease-in-out infinite;
    }
    @keyframes splashPulse {
      0%, 100% { transform: translateY(0); box-shadow: 0 8px 24px rgba(0, 0, 0, 0.4), 0 0 15px rgba(56, 189, 248, 0.12); }
      50% { transform: translateY(-4px); box-shadow: 0 14px 30px rgba(0, 0, 0, 0.55), 0 0 25px rgba(56, 189, 248, 0.28); }
    }
    .splash-title {
      font-size: 18px;
      font-weight: 800;
      color: #ffffff;
      letter-spacing: -0.3px;
      margin-bottom: 6px;
      text-shadow: 0 2px 10px rgba(0,0,0,0.5);
    }
    .splash-badge-row {
      display: flex;
      gap: 6px;
      justify-content: center;
      margin-bottom: 16px;
      flex-wrap: wrap;
    }
    .splash-pill {
      background: rgba(255, 255, 255, 0.08);
      color: #94a3b8;
      border: 1px solid rgba(255, 255, 255, 0.12);
      font-size: 11px;
      font-weight: 700;
      padding: 3px 9px;
      border-radius: 9999px;
    }
    .splash-spinner-wrap {
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 9px;
      margin-bottom: 10px;
    }
    .splash-spinner {
      width: 16px;
      height: 16px;
      border: 2.5px solid rgba(255, 255, 255, 0.15);
      border-top-color: #38bdf8;
      border-radius: 50%;
      animation: spin 0.75s linear infinite;
    }
    @keyframes spin {
      to { transform: rotate(360deg); }
    }
    .splash-status-text {
      font-size: 13px;
      font-weight: 700;
      color: #e2e8f0;
      letter-spacing: -0.2px;
    }
    .splash-progress-wrap {
      width: 100%;
      margin-top: 4px;
      margin-bottom: 10px;
      animation: fadeIn 0.4s ease;
    }
    .splash-progress-bar {
      width: 100%;
      height: 6px;
      background: rgba(255, 255, 255, 0.1);
      border-radius: 9999px;
      overflow: hidden;
      margin-bottom: 7px;
    }
    .splash-progress-fill {
      width: 15%;
      height: 100%;
      background: linear-gradient(90deg, #38bdf8, #818cf8, #34d399);
      border-radius: 9999px;
      animation: progressIndeterminate 8s ease-out forwards;
    }
    @keyframes progressIndeterminate {
      0% { width: 15%; }
      30% { width: 45%; }
      70% { width: 75%; }
      90% { width: 92%; }
      100% { width: 98%; }
    }
    .splash-sub-text {
      font-size: 11.5px;
      color: #cbd5e1;
      line-height: 1.45;
    }
    .splash-footer {
      font-size: 10.5px;
      color: #64748b;
      margin-top: 8px;
      font-weight: 600;
    }
    @keyframes fadeIn {
      from { opacity: 0; transform: translateY(4px); }
      to { opacity: 1; transform: translateY(0); }
    }
  </style>
</head>
<body>

  <!-- 🚀 Fullscreen App Splash & Cold-Start Loader -->
  <div id="appSplashLoader" class="app-splash-loader">
    <div class="splash-card">
      <div class="splash-img-wrap">
        <img src="/static/splash.jpg" alt="Multi-Bot Intelligence Platform" class="splash-img" onerror="this.onerror=null; this.src='/reports/assets/splash.jpg';" />
      </div>
      <div class="splash-title">Multi-Bot Intelligence</div>
      <div class="splash-badge-row">
        <span class="splash-pill">📈 시황</span>
        <span class="splash-pill">✈️ 여행</span>
        <span class="splash-pill">🎓 입시</span>
        <span class="splash-pill">👵 실버</span>
        <span class="splash-pill">👶 자녀</span>
        <span class="splash-pill">🏄 욜로</span>
      </div>
      
      <div class="splash-spinner-wrap">
        <div class="splash-spinner"></div>
        <span id="splashStatusText" class="splash-status-text">⚡ 플랫폼 데이터를 불러오는 중...</span>
      </div>

      <!-- 스마트 콜드스타트 프로그레스바 (서버 지연 시 자동 활성화) -->
      <div id="splashProgressWrap" class="splash-progress-wrap" style="display:none;">
        <div class="splash-progress-bar">
          <div id="splashProgressFill" class="splash-progress-fill"></div>
        </div>
        <div class="splash-sub-text">⚡ 플랫폼 데이터 응답 대기 중입니다<br><span style="color:#94a3b8; font-size:10.5px;">(초기 연결 시 잠시만 기다려주세요)</span></div>
      </div>

      <div class="splash-footer">Daily Market Brief &amp; Multi-Bot Platform v2.0</div>
    </div>
  </div>

  <!-- Top Minimal Header -->
  <header class="header">
    <div class="header-brand">
      <div class="header-logo">🤖</div>
      <div style="min-width:0; overflow:hidden;">
        <div class="header-title">
          <span class="desktop-only">텔레그램 봇 구독 서비스 통합 관리자</span>
          <span class="mobile-only">봇 구독 관리자</span>
        </div>
        <div class="header-sub">Daily Market Brief &amp; Multi-Bot Tier Platform v2.0</div>
      </div>
    </div>
    <div class="header-actions">
      <span class="pill" title="실시간 정상 작동"><span class="dot"></span> <span id="topStatusText">실시간 정상 작동</span></span>
      <button class="btn btn-sm btn-dark" onclick="syncAllBots()" title="전체 봇 대화 즉시 수신">
        <span>🔄</span> <span class="btn-text">대화 즉시 수신</span>
      </button>
      <button class="btn btn-sm btn-primary" onclick="refreshCurrentTab()" title="새로고침">
        <span>🔃</span> <span class="btn-text">새로고침</span>
      </button>
    </div>
  </header>

  <!-- 7-Bot Traffic Lights & Scheduler Countdown -->
  <div id="botHealthStrip" class="bot-health-strip">
    <div id="botTrafficLights" class="bot-traffic-lights">
      <span class="bot-light-chip">⏳ 봇 상태 확인 중...</span>
    </div>
    <div id="schedulerTimer" class="scheduler-timer-wrap">⏰ 다음 발송: 확인 중...</div>
  </div>

  <!-- 3 Core Tabs -->
  <nav class="tab-bar">
    <button class="tab-btn active" data-tab="users" onclick="switchTab('users')">
      👥 1. 유저 &amp; 등급 관리<span id="userPendingBadge" class="tab-badge" style="display:none;"></span>
    </button>
    <button class="tab-btn" data-tab="bots" onclick="switchTab('bots')">
      🤖 2. 봇 목록 &amp; 메시지 발송
    </button>
    <button class="tab-btn" data-tab="dashboard" onclick="switchTab('dashboard')">
      📊 3. 운영 현황
    </button>
    <button class="tab-btn" data-tab="history" onclick="switchTab('history')">
      📜 이력 센터
    </button>
  </nav>

  <!-- Mobile Bottom Navigation Bar (App Navigation) -->
  <nav class="bottom-nav">
    <button class="bnav-btn active" data-bnav="users" onclick="switchTab('users')">
      <span class="bnav-icon">👥</span>
      <span class="bnav-label">유저 관리</span>
      <span id="bnavPendingBadge" class="bnav-badge" style="display:none;"></span>
    </button>
    <button class="bnav-btn" data-bnav="bots" onclick="switchTab('bots')">
      <span class="bnav-icon">🤖</span>
      <span class="bnav-label">봇 &amp; 발송</span>
    </button>
    <button class="bnav-btn" data-bnav="dashboard" onclick="switchTab('dashboard')">
      <span class="bnav-icon">📊</span>
      <span class="bnav-label">운영 현황</span>
    </button>
    <button class="bnav-btn" data-bnav="history" onclick="switchTab('history')">
      <span class="bnav-icon">📜</span>
      <span class="bnav-label">이력 센터</span>
    </button>
    <button class="bnav-btn" onclick="openSimModal()">
      <span class="bnav-icon">🧪</span>
      <span class="bnav-label">시뮬레이터</span>
    </button>
  </nav>

  <main class="container">

    <!-- =================================================================== -->
    <!-- TAB 1: USERS & TIER MANAGEMENT (DEFAULT FIRST TAB)                  -->
    <!-- =================================================================== -->
    <section id="pane-users" class="tab-pane active">
      <div class="card">
        <div class="card-header">
          <div>
            <h2 class="card-title">👥 회원 등급 및 봇 구독 현황</h2>
            <span style="display:none;" aria-hidden="true">유저 실명 (별칭) / ID</span>
            <p class="card-desc">각 유저의 등급(화이트 1개 / 그린 3개 / 레드 5개 / 블랙 무제한)을 드롭다운으로 즉시 변경하고, 사용 중인 봇 슬롯을 관리합니다.</p>
          </div>
          <div style="display:flex; gap:8px; flex-wrap:wrap;">
            <button class="btn btn-primary" onclick="openNewUserModal()">➕ 신규 유저 등록</button>
            <button class="btn btn-outline" style="background:#f0fdf4; color:#166534; border-color:#bbf7d0; font-weight:700;" onclick="triggerProfileBatchSync()" title="수동 수정 실명은 보존하고 텔레그램 프로필을 조회하여 실명을 동기화합니다.">🔄 프로필 실명 일괄 동기화</button>
          </div>
        </div>

        <!-- 🔔 등급 업그레이드 신청 대기 큐 섹션 -->
        <div id="upgradeRequestsSection" style="margin-bottom: 20px;"></div>

        <!-- Search & Filter Bar -->
        <div class="user-filter-bar">
          <div class="filter-chips">
            <button class="filter-btn active" data-tier="" onclick="filterTier('')">전체<span class="desktop-only"> 보기</span></button>
            <button class="filter-btn" data-tier="WHITE" onclick="filterTier('WHITE')">⚪ 화이트<span class="desktop-only"> (1슬롯)</span></button>
            <button class="filter-btn" data-tier="GREEN" onclick="filterTier('GREEN')">🟢 그린<span class="desktop-only"> (3슬롯)</span></button>
            <button class="filter-btn" data-tier="RED" onclick="filterTier('RED')">🔴 레드<span class="desktop-only"> (5슬롯)</span></button>
            <button class="filter-btn" data-tier="BLACK" onclick="filterTier('BLACK')">⚫ 블랙<span class="desktop-only"> (무제한)</span></button>
          </div>
          <div class="user-search-wrap">
            <input type="text" id="userSearch" class="form-input" placeholder="🔍 이름, ID, 메모 검색..." oninput="debouncedSearchUsers()" />
          </div>
        </div>

        <!-- Accordion User List (44px Slim 1-Line Header + Drawer) -->
        <div id="userAccordionContainer" class="user-accordion-list">
          <div style="text-align:center; padding:32px; color:var(--text-muted);">회원 목록을 불러오는 중...</div>
        </div>
      </div>
    </section>

    <!-- =================================================================== -->
    <!-- TAB 2: BOTS & DIRECT BROADCAST HUB                                  -->
    <!-- =================================================================== -->
    <section id="pane-bots" class="tab-pane">
      <!-- Integrated Single Menu Bot Banner Card -->
      <div class="card" style="background:linear-gradient(135deg, #eff6ff 0%, #ffffff 80%); border:2px solid #bfdbfe; margin-bottom:20px;">
        <div id="menuBotSection"></div>
      </div>

      <!-- Info Bots Grid with Direct Broadcast Buttons -->
      <div class="card">
        <div class="card-header">
          <div>
            <h2 class="card-title">📡 운영 중인 정보봇 목록 &amp; 원클릭 즉시 발송 센터</h2>
            <p class="card-desc">각 봇의 [🚀 정규 브리핑 발송 센터]를 누르면 정해진 실시간 콘텐츠(시황 데이터, 여행 특가, 입시 나침반, 실버케어 복지, 자녀 나침반, 욜로 1인 라이프)를 즉시 생성하여 미리보고, 관리자 테스트 발송 및 전체 구독자 발송을 실행할 수 있습니다.</p>
          </div>
          <button class="btn btn-primary" onclick="openNewBotModal()">➕ 새 정보봇 추가</button>
        </div>
        <div class="bot-grid" id="infoBotGrid"></div>
      </div>
    </section>

    <!-- =================================================================== -->
    <!-- TAB 3: CLEAN DASHBOARD & OPERATIONS                                 -->
    <!-- =================================================================== -->
    <section id="pane-dashboard" class="tab-pane">
      <!-- 3 Clean KPI Cards -->
      <div class="kpi-grid">
        <div class="card" style="margin-bottom:0;">
          <div style="font-size:12px; font-weight:700; color:var(--text-muted);">전체 회원 수</div>
          <div style="font-size:32px; font-weight:800; margin:6px 0;" id="kpiUsers">0명</div>
          <div style="font-size:12px; color:var(--text-muted);" id="kpiUsersSub">정상 이용 중</div>
        </div>
        <div class="card" style="margin-bottom:0;">
          <div style="font-size:12px; font-weight:700; color:var(--text-muted);">총 활성 구독 연결 수</div>
          <div style="font-size:32px; font-weight:800; margin:6px 0;" id="kpiSubs">0건</div>
          <div style="font-size:12px; color:var(--text-muted);">유저 × 정보봇 활성 합계</div>
        </div>
        <div class="card" style="margin-bottom:0;">
          <div style="font-size:12px; font-weight:700; color:var(--text-muted);">운영 중인 정보봇</div>
          <div style="font-size:32px; font-weight:800; margin:6px 0;" id="kpiBots">0개</div>
          <div style="font-size:12px; color:var(--text-muted);" id="kpiBotsSub">정보봇 6개 (포털 메뉴봇 1개 별도)</div>
        </div>
      </div>

      <!-- Tier Distribution Bar & Bot Popularity -->
      <div class="dash-grid-2col">
        <div class="card" style="margin-bottom:0;">
          <h3 class="card-title" style="margin-bottom:12px;">👑 회원 등급별 현황</h3>
          <div id="tierDistList"></div>
        </div>
        <div class="card" style="margin-bottom:0;">
          <h3 class="card-title" style="margin-bottom:12px;">📡 정보봇별 구독자 순위</h3>
          <div id="botRankList"></div>
        </div>
      </div>

      <!-- Recent Audit Activity Log -->
      <div class="card">
        <div class="card-header">
          <h3 class="card-title">⚡ 최근 구독 및 등급 변경 이력</h3>
          <span style="font-size:12px; color:var(--text-muted);">최근 15건</span>
        </div>
        <div id="auditLogList" style="max-height:280px; overflow-y:auto;"></div>
      </div>

      <!-- Collapsible Advanced Settings -->
      <details class="card">
        <summary style="font-size:14px; font-weight:800; cursor:pointer; user-select:none;">
          ⚙️ 고급 시스템 설정 (긴급 알림 임계치 &amp; GitHub Actions 크론)
        </summary>
        <div style="margin-top:16px;">
          <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;">
            <div style="font-size:13px; font-weight:700;">자산군별 급변동 긴급 알림 임계치</div>
            <button class="btn btn-sm btn-primary" onclick="saveThresholds()">임계치 저장</button>
          </div>
          <div id="thresholdEditor" style="display:grid; grid-template-columns:repeat(auto-fit, minmax(200px, 1fr)); gap:10px; margin-bottom:16px;"></div>
          <div style="padding:12px; border-radius:8px; background:var(--surface-sub); font-size:12.5px; display:flex; justify-content:space-between; align-items:center;">
            <span id="ghSyncText">GitHub Actions 크론 수신자 동기화 확인중...</span>
            <button class="btn btn-sm btn-dark" onclick="syncGithub()">GitHub 변수 동기화</button>
          </div>
        </div>
      </details>
    </section>

    <!-- =================================================================== -->
    <!-- TAB 4: HISTORY CENTER (DISPATCH & PLATFORM AUDIT)                   -->
    <!-- =================================================================== -->
    <section id="pane-history" class="tab-pane">
      <div class="card">
        <div class="card-header">
          <div>
            <h2 class="card-title">📜 이력 센터 (발송 및 플랫폼 감사)</h2>
            <p class="card-desc">정기·수동·긴급 봇 발송 결과와 유저 정보·등급·구독 변경 감사 이력을 한 곳에서 추적합니다.</p>
          </div>
        </div>

        <div class="hist-subtabs">
          <button class="hist-subtab-btn active" data-subpane="dispatch" onclick="switchHistorySubpane('dispatch')">📨 봇 발송 이력</button>
          <button class="hist-subtab-btn" data-subpane="audit" onclick="switchHistorySubpane('audit')">👥 플랫폼 감사 이력</button>
        </div>

        <!-- Sub-pane: Dispatch History -->
        <div id="subpane-dispatch" class="hist-subpane active">
          <div class="hist-filter-bar">
            <span style="font-size:12px; color:var(--text-muted);">최근 발송 50건</span>
            <button class="btn btn-sm hist-touch" onclick="loadDispatchHistory()">🔃 새로고침</button>
          </div>
          <div class="table-wrap">
            <table class="hist-table">
              <thead>
                <tr>
                  <th>발송 일시</th>
                  <th>발송 봇 / 토픽</th>
                  <th>유형</th>
                  <th>성공률 (성공/전체)</th>
                  <th>상태</th>
                  <th>상세</th>
                </tr>
              </thead>
              <tbody id="dispatchHistoryBody">
                <tr><td colspan="6" class="empty-cell" style="text-align:center; padding:28px;">불러오는 중...</td></tr>
              </tbody>
            </table>
          </div>
        </div>

        <!-- Sub-pane: Platform Audit Logs -->
        <div id="subpane-audit" class="hist-subpane">
          <div class="hist-filter-bar">
            <input type="text" id="auditSearch" class="form-input" placeholder="🔍 유저 ID, 실명, 별칭 검색..." oninput="debouncedLoadAuditLogs()" />
            <select id="auditActionSel" class="form-select" onchange="loadAuditLogs()">
              <option value="">전체 구분</option>
              <option value="USER_UPDATED">USER_UPDATED (정보 수정)</option>
              <option value="TIER_CHANGE">TIER_CHANGE (등급 변경)</option>
              <option value="SUBSCRIBE">SUBSCRIBE (구독)</option>
              <option value="UNSUBSCRIBE">UNSUBSCRIBE (구독 해지)</option>
              <option value="SWAP">SWAP (봇 교체)</option>
              <option value="UPGRADE_REQUESTED">UPGRADE_REQUESTED (등급 신청)</option>
              <option value="QUOTA_REJECTED">QUOTA_REJECTED (한도 초과)</option>
              <option value="TIER_EXPIRED">TIER_EXPIRED (등급 만료)</option>
              <option value="USER_CREATED">USER_CREATED (유저 등록)</option>
              <option value="USER_JOINED">USER_JOINED (유저 가입)</option>
              <option value="USER_DELETED">USER_DELETED (유저 삭제)</option>
            </select>
            <button class="btn btn-sm hist-touch" onclick="loadAuditLogs()">🔃 새로고침</button>
          </div>
          <div class="table-wrap">
            <table class="hist-table">
              <thead>
                <tr>
                  <th>일시</th>
                  <th>대상 유저</th>
                  <th>구분</th>
                  <th>변경 상세</th>
                  <th>작업자</th>
                </tr>
              </thead>
              <tbody id="auditHistoryBody">
                <tr><td colspan="5" class="empty-cell" style="text-align:center; padding:28px;">불러오는 중...</td></tr>
              </tbody>
            </table>
          </div>
          <div class="hist-pager">
            <button class="btn btn-sm hist-touch" id="auditPrevBtn" onclick="pageAuditLogs(-1)">◀ 이전</button>
            <span id="auditPageInfo" style="font-size:12px; color:var(--text-muted);"></span>
            <button class="btn btn-sm hist-touch" id="auditNextBtn" onclick="pageAuditLogs(1)">다음 ▶</button>
          </div>
        </div>
      </div>
    </section>

  </main>

  <!-- ===================================================================== -->
  <!-- FLOATING SIMULATOR BUTTON & MODAL                                     -->
  <!-- ===================================================================== -->
  <button class="float-sim-btn" onclick="openSimModal()">
    🧪 봇 대화 &amp; 쿼터 시뮬레이터
  </button>

  <div class="modal-backdrop" id="simModal">
    <div class="modal-box" style="max-width:540px;">
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;">
        <h3 style="margin:0; font-size:16px;">🧪 텔레그램 봇 대화 &amp; 등급 검증 시뮬레이터</h3>
        <button class="btn btn-sm" onclick="closeModal('simModal')">닫기 ✕</button>
      </div>

      <div class="form-grid-2col" style="margin-bottom:10px;">
        <div>
          <label class="form-label">테스트 유저</label>
          <select id="simUserSel" class="form-select"></select>
        </div>
        <div>
          <label class="form-label">대화 상대 봇</label>
          <select id="simBotSel" class="form-select"></select>
        </div>
      </div>

      <div class="sim-actions-row">
        <button class="btn btn-sm btn-primary" onclick="runSim('/start')">/start (진입·구독검증)</button>
        <button class="btn btn-sm btn-danger" onclick="runSim('/stop')">/stop (탈퇴·슬롯반환)</button>
        <button class="btn btn-sm" onclick="clearSim()">대화창 비우기</button>
      </div>

      <div class="sim-chat-box" id="simChatBox">
        <div style="text-align:center; color:#64748b; font-size:12px; padding:40px 10px;">
          위에서 유저와 봇을 선택하고 <b>[/start]</b>를 누르면<br/>등급 한도 검증 및 원클릭 교체(Swap) 동작을 확인할 수 있습니다.
        </div>
      </div>
    </div>
  </div>

  <!-- ===================================================================== -->
  <!-- MODAL: SMART REAL-TIME BRIEFING DISPATCH CENTER                       -->
  <!-- ===================================================================== -->
  <div class="modal-backdrop" id="briefingModal">
    <div class="modal-box" style="max-width: 620px;">
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;">
        <h3 style="margin:0; font-size:17px;" id="bfModalTitle">🚀 정규 브리핑 생성 &amp; 발송 센터</h3>
        <button class="btn btn-sm" onclick="closeModal('briefingModal')">✕</button>
      </div>
      <input type="hidden" id="bfBotId" />

      <!-- STEP 1: PRESET SELECTION -->
      <div style="background:var(--surface-sub); border:1px solid var(--border); border-radius:8px; padding:12px; margin-bottom:14px;">
        <div style="font-size:12px; font-weight:800; color:var(--text-muted); margin-bottom:8px;">
          1단계: 정해진 정규 브리핑 유형 선택 (클릭 시 실시간 데이터 수집 &amp; 생성)
        </div>
        <div id="bfPresetButtons" style="display:flex; gap:8px; flex-wrap:wrap;"></div>
      </div>

      <!-- STEP 2: TELEGRAM HTML PREVIEW BOX -->
      <div style="margin-bottom:14px;">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;">
          <span style="font-size:12px; font-weight:800; color:var(--text-muted);">2단계: 텔레그램 실제 수신 미리보기 (HTML 서식 적용)</span>
          <span id="bfGeneratedAt" style="font-size:11px; color:#64748b;"></span>
        </div>
        <div id="bfPreviewBox" style="background:#f0fdf4; border:1px solid #bbf7d0; border-radius:10px; padding:14px; max-height:220px; overflow-y:auto; font-size:13px; line-height:1.6; color:#0f172a; white-space:pre-wrap; word-break:break-word;">
          데이터 생성 중...
        </div>
        <div id="bfPreviewButtons" style="display:flex; gap:6px; margin-top:8px; flex-wrap:wrap;"></div>
      </div>

      <!-- STEP 3: DISPATCH ACTIONS -->
      <div style="background:#fff; border:1px solid var(--border); border-radius:8px; padding:12px; margin-bottom:14px;">
        <div style="font-size:12px; font-weight:800; color:var(--text-muted); margin-bottom:8px;">
          3단계: 안전 발송 실행
        </div>
        <div class="dispatch-actions-grid">
          <button class="btn btn-dark" onclick="dispatchBriefingTest()" style="padding:10px;">
            📱 관리자 나에게만 테스트 발송 (1건)
          </button>
          <button class="btn btn-primary" onclick="dispatchBriefingSubscribers()" style="padding:10px;">
            🚀 전체 활성 구독자 일괄 발송 (<span id="bfSubCountText">0</span>명)
          </button>
        </div>
        <div style="font-size:11.5px; color:#64748b; margin-top:8px;">
          💡 <strong>테스트 발송 권장:</strong> 관리자 텔레그램으로 먼저 1건 전송하여 스마트폰 앱에서 서식과 버튼 링크가 깨지지 않는지 확인한 후 전체 발송을 실행하세요.
        </div>
      </div>

      <!-- OPTIONAL ACCORDION: MANUAL TEXT BROADCAST -->
      <details style="border-top:1px dashed var(--border); padding-top:10px;">
        <summary style="font-size:12px; font-weight:700; color:var(--text-muted); cursor:pointer;">
          📝 직접 작성한 긴급 공지 전송 (선택사항)
        </summary>
        <div style="margin-top:8px;">
          <textarea id="bfCustomText" class="form-textarea" rows="3" placeholder="서버 점검 등 직접 텍스트 공지가 필요한 경우에만 입력하세요..."></textarea>
          <button class="btn btn-sm btn-outline" style="margin-top:6px;" onclick="dispatchCustomNotice()">이 수동 메시지로 구독자 발송</button>
        </div>
      </details>
    </div>
  </div>

  <!-- ===================================================================== -->
  <!-- MODAL: BOT EDIT / CREATE                                              -->
  <!-- ===================================================================== -->
  <div class="modal-backdrop" id="botModal">
    <div class="modal-box">
      <h3 style="margin:0 0 16px; font-size:17px;" id="botModalTitle">⚙️ 봇 설정</h3>
      <div class="form-grid-2col">
        <div class="form-group">
          <label class="form-label">봇 고유 ID</label>
          <input type="text" id="bmId" class="form-input" />
        </div>
        <div class="form-group">
          <label class="form-label">봇 역할</label>
          <select id="bmType" class="form-select">
            <option value="INFO">정보봇 (유저 구독 콘텐츠 봇)</option>
            <option value="MENU">메뉴봇 (가입 포털 봇)</option>
          </select>
        </div>
      </div>
      <div class="form-grid-bot-meta">
        <div class="form-group">
          <label class="form-label">아이콘</label>
          <input type="text" id="bmEmoji" class="form-input" value="🤖" />
        </div>
        <div class="form-group">
          <label class="form-label">봇 이름</label>
          <input type="text" id="bmName" class="form-input" />
        </div>
        <div class="form-group form-bot-category">
          <label class="form-label">카테고리</label>
          <input type="text" id="bmCategory" class="form-input" value="일반" />
        </div>
      </div>
      <div class="form-group">
        <label class="form-label">텔레그램 @봇유저네임 (선택)</label>
        <input type="text" id="bmUsername" class="form-input" placeholder="예: my_info_bot" />
      </div>
      <div class="form-group">
        <label class="form-label">텔레그램 Bot API Token (@BotFather 발급 토큰)</label>
        <input type="password" id="bmToken" class="form-input" placeholder="변경 시에만 입력하세요" />
        <div id="bmTokenHint" style="font-size:11.5px; color:var(--text-muted); margin-top:3px;"></div>
      </div>
      <div class="form-group">
        <label class="form-label">봇 소개 설명문 (메뉴봇에서 유저에게 노출)</label>
        <textarea id="bmDesc" class="form-textarea" rows="3"></textarea>
      </div>
      <div style="display:flex; justify-content:space-between; align-items:center; margin-top:16px;">
        <label style="display:inline-flex; align-items:center; gap:6px; font-size:13px; font-weight:700;">
          <input type="checkbox" id="bmActive" checked /> 봇 활성화
        </label>
        <div style="display:flex; gap:8px;">
          <button class="btn" onclick="closeModal('botModal')">취소</button>
          <button class="btn btn-primary" onclick="saveBot()">저장</button>
        </div>
      </div>
    </div>
  </div>

  <!-- ===================================================================== -->
  <!-- MODAL: USER EDIT / CREATE                                             -->
  <!-- ===================================================================== -->
  <div class="modal-backdrop" id="userModal">
    <div class="modal-box">
      <h3 style="margin:0 0 16px; font-size:17px;" id="userModalTitle">👤 유저 상세 관리</h3>
      <div class="form-grid-2col">
        <div class="form-group">
          <label class="form-label">텔레그램 User ID <span style="color:var(--danger)">*</span></label>
          <input type="text" id="umId" class="form-input" placeholder="숫자 ID (예: 12345678)" />
        </div>
        <div class="form-group">
          <label class="form-label">실명 <span style="color:var(--danger)">*</span></label>
          <input type="text" id="umName" class="form-input" placeholder="예: 홍길동" />
        </div>
      </div>
      <div class="form-grid-2col">
        <div class="form-group">
          <label class="form-label">별칭 (텔레그램 @username)</label>
          <input type="text" id="umUsername" class="form-input" placeholder="예: @kildong (선택)" />
        </div>
        <div class="form-group">
          <label class="form-label">회원 등급</label>
          <select id="umTier" class="form-select">
            <option value="WHITE">⚪ 화이트 (1슬롯)</option>
            <option value="GREEN">🟢 그린 (3슬롯)</option>
            <option value="RED">🔴 레드 (5슬롯)</option>
            <option value="BLACK">⚫ 블랙 (무제한)</option>
          </select>
        </div>
      </div>
      <div class="form-grid-2col">
        <div class="form-group">
          <label class="form-label">등급 만료일 (비워두면 무기한)</label>
          <input type="date" id="umExp" class="form-input" />
        </div>
        <div class="form-group">
          <label class="form-label">관리자 메모</label>
          <input type="text" id="umMemo" class="form-input" placeholder="메모 입력" />
        </div>
      </div>
      <div style="display:flex; justify-content:flex-end; gap:8px; margin-top:16px;">
        <button class="btn" onclick="closeModal('userModal')">취소</button>
        <button class="btn btn-primary" onclick="saveUser()">저장</button>
      </div>
    </div>
  </div>

  <!-- ===================================================================== -->
  <!-- MODAL: ADD SUBSCRIPTION                                               -->
  <!-- ===================================================================== -->
  <div class="modal-backdrop" id="addSubModal">
    <div class="modal-box" style="max-width:400px;">
      <h3 style="margin:0 0 10px; font-size:16px;">➕ 봇 구독 추가</h3>
      <div id="addSubLabel" style="font-size:13px; color:var(--text-muted); margin-bottom:12px;"></div>
      <input type="hidden" id="asUserId" />
      <div class="form-group">
        <label class="form-label">구독할 정보봇</label>
        <select id="asBotSel" class="form-select"></select>
      </div>
      <div style="display:flex; justify-content:flex-end; gap:8px;">
        <button class="btn" onclick="closeModal('addSubModal')">취소</button>
        <button class="btn btn-primary" onclick="confirmAddSub()">구독 추가</button>
      </div>
    </div>
  </div>

  <!-- ===================================================================== -->
  <!-- MODAL: DISPATCH JOB RECIPIENT BREAKDOWN                               -->
  <!-- ===================================================================== -->
  <div class="modal-backdrop" id="jobDetailModal">
    <div class="modal-box" style="max-width:620px;">
      <div class="hist-modal-head">
        <h3 style="margin:0; font-size:16px; min-width:0; overflow-wrap:anywhere;" id="jobDetailTitle">📨 발송 상세</h3>
        <button class="btn btn-sm hist-close-btn" onclick="closeModal('jobDetailModal')" aria-label="닫기">✕</button>
      </div>
      <div id="jobDetailSummary" style="font-size:12.5px; color:var(--text-muted); margin-bottom:10px; overflow-wrap:anywhere;"></div>
      <div id="jobDetailList" class="hist-card-list"></div>
    </div>
  </div>

  <!-- ===================================================================== -->
  <!-- MODAL: PER-USER HISTORY (AUDIT TIMELINE + DELIVERY HISTORY)           -->
  <!-- ===================================================================== -->
  <div class="modal-backdrop" id="userHistoryModal">
    <div class="modal-box" style="max-width:620px;">
      <div class="hist-modal-head">
        <h3 style="margin:0; font-size:16px; min-width:0; overflow-wrap:anywhere;" id="userHistoryTitle">📜 유저 이력</h3>
        <button class="btn btn-sm hist-close-btn" onclick="closeModal('userHistoryModal')" aria-label="닫기">✕</button>
      </div>
      <div class="hist-subtabs">
        <button class="hist-subtab-btn uh-tab-btn active" data-uhtab="audit" onclick="switchUserHistoryTab('audit')">정보 &amp; 등급 변경 이력</button>
        <button class="hist-subtab-btn uh-tab-btn" data-uhtab="delivery" onclick="switchUserHistoryTab('delivery')">메시지 수신 이력</button>
      </div>
      <div id="uhPaneAudit" class="hist-card-list"></div>
      <div id="uhPaneDelivery" class="hist-card-list" style="display:none;"></div>
    </div>
  </div>

  <div id="toastWrap"></div>

  <script>
    // State
    let S = {
      users: [],
      bots: [],
      upgradeRequests: [],
      overview: null,
      tierFilter: '',
      activeTab: 'users',
      thresholds: {},
    };
    let searchDebounce = null;

    function esc(s) {
      if (s === null || s === undefined) return '';
      return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    function toast(msg) {
      const w = document.getElementById('toastWrap');
      const el = document.createElement('div');
      el.className = 'toast';
      el.textContent = msg;
      w.appendChild(el);
      setTimeout(() => el.remove(), 3200);
    }

    async function req(url, opts = {}) {
      const headers = opts.headers || {};
      const token = localStorage.getItem('admin_api_token') || new URLSearchParams(window.location.search).get('token') || '';
      if (token) {
        headers['X-Admin-Token'] = token;
      }
      if (opts.body && typeof opts.body === 'object') {
        headers['Content-Type'] = 'application/json';
        opts.body = JSON.stringify(opts.body);
      }
      const res = await fetch(url, { ...opts, headers });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || data.message || `HTTP ${res.status}`);
      return data;
    }

    function switchTab(tab) {
      S.activeTab = tab;
      document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
      document.querySelectorAll('.bnav-btn').forEach(b => b.classList.toggle('active', b.dataset.bnav === tab));
      document.querySelectorAll('.tab-pane').forEach(p => p.classList.toggle('active', p.id === `pane-${tab}`));
      if (tab === 'users') return loadUsers();
      if (tab === 'bots') return loadBots();
      if (tab === 'dashboard') return loadDashboard();
      if (tab === 'history') return loadHistoryCenter();
    }

    function refreshCurrentTab() {
      switchTab(S.activeTab);
    }

    function closeModal(id) {
      document.getElementById(id).classList.remove('open');
    }

    function tierBadge(tier) {
      const t = (tier || 'WHITE').toUpperCase();
      const icons = { WHITE: '⚪', GREEN: '🟢', RED: '🔴', BLACK: '⚫' };
      const names = { WHITE: '화이트(1)', GREEN: '그린(3)', RED: '레드(5)', BLACK: '블랙(무제한)' };
      return `<span class="tier-badge tier-${t}">${icons[t] || '⚪'} ${names[t] || t}</span>`;
    }

    // =========================================================================
    // TAB 1: USERS
    // =========================================================================
    function filterTier(t) {
      S.tierFilter = t;
      document.querySelectorAll('.filter-btn').forEach(b => b.classList.toggle('active', b.dataset.tier === t));
      loadUsers();
    }

    function debouncedSearchUsers() {
      clearTimeout(searchDebounce);
      searchDebounce = setTimeout(loadUsers, 200);
    }

    function updateTierFilterCounts(counts) {
      if (!counts) return;
      const btnAll = document.querySelector('.filter-btn[data-tier=""]');
      if (btnAll) btnAll.innerHTML = `전체 (${counts.ALL || 0})<span class="desktop-only"> 보기</span>`;
      const btnWhite = document.querySelector('.filter-btn[data-tier="WHITE"]');
      if (btnWhite) btnWhite.innerHTML = `⚪ 화이트 (${counts.WHITE || 0})<span class="desktop-only"> (1슬롯)</span>`;
      const btnGreen = document.querySelector('.filter-btn[data-tier="GREEN"]');
      if (btnGreen) btnGreen.innerHTML = `🟢 그린 (${counts.GREEN || 0})<span class="desktop-only"> (3슬롯)</span>`;
      const btnRed = document.querySelector('.filter-btn[data-tier="RED"]');
      if (btnRed) btnRed.innerHTML = `🔴 레드 (${counts.RED || 0})<span class="desktop-only"> (5슬롯)</span>`;
      const btnBlack = document.querySelector('.filter-btn[data-tier="BLACK"]');
      if (btnBlack) btnBlack.innerHTML = `⚫ 블랙 (${counts.BLACK || 0})<span class="desktop-only"> (무제한)</span>`;
    }

    async function handleBatchApproveClick(count) {
      if (!confirm(`대기 중인 ${count}건의 등급 업그레이드 신청을 모두 승인하시겠습니까?\n(회원 등급이 즉시 갱신되며 각 회원에게 텔레그램 축하 알림이 발송됩니다)`)) {
        return;
      }
      toast('⏳ 등급 업그레이드 일괄 승인 처리 중...');
      try {
        const res = await req('/api/platform/upgrade-requests/batch-approve', { method: 'POST' });
        toast(`✅ 총 ${res.approved_count || 0}건 등업 일괄 승인 완료!`);
        await loadUsers();
      } catch (e) {
        toast('❌ 일괄 승인 실패: ' + e.message);
      }
    }

    async function dispatchBotToUser(userId, botId, botName, userName) {
      if (!confirm(`[${userName}] 님에게 [${botName}] 최신 브리핑을 1:1로 즉시 개별 발송하시겠습니까?`)) {
        return;
      }
      toast(`⏳ [${userName}] 님에게 [${botName}] 발송 중...`);
      try {
        await req(`/api/platform/users/${encodeURIComponent(userId)}/dispatch-bot`, {
          method: 'POST',
          body: { bot_id: botId }
        });
        toast(`✅ [${userName}] 님에게 [${botName}] 브리핑 발송 완료!`);
      } catch (e) {
        toast(`❌ 발송 실패: ` + e.message);
      }
    }

    function toggleUserAccordion(userId, forceOpen) {
      const item = document.querySelector(`[data-user-card-id="${userId}"]`);
      if (!item) return;
      if (forceOpen === true) {
        item.classList.add('is-open');
      } else if (forceOpen === false) {
        item.classList.remove('is-open');
      } else {
        item.classList.toggle('is-open');
      }
    }

    async function triggerProfileBatchSync() {
      if (!confirm('텔레그램 프로필을 조회하여 실명을 일괄 동기화합니다.\n(관리자가 직접 수정한 실명은 안전하게 보존됩니다)\n진행하시겠습니까?')) {
        return;
      }
      toast('⏳ 텔레그램 프로필 실명 동기화 중...');
      try {
        const res = await req('/api/platform/users/sync-telegram-names', { method: 'POST' });
        toast(`✅ 동기화 완료: ${res.updated || 0}명 갱신 (수동보존 ${res.skipped_custom || 0}명, 유지 ${res.unchanged || 0}명)`);
        await loadUsers();
      } catch (e) {
        toast('❌ 동기화 실패: ' + e.message);
      }
    }

    async function resetUserNameToTelegram(userId) {
      if (!confirm('이 유저의 이름을 텔레그램 최신 프로필명으로 복원하시겠습니까?\n(수동 지정 상태가 해제됩니다)')) {
        return;
      }
      try {
        const res = await req(`/api/platform/users/${encodeURIComponent(userId)}/reset-name`, { method: 'POST' });
        toast(`✅ 텔레그램 이름으로 복원 완료: ${res.name || ''}`);
        await loadUsers();
        toggleUserAccordion(userId, true);
      } catch (e) {
        toast('❌ 복원 실패: ' + e.message);
      }
    }

    async function loadUsers() {
      const q = document.getElementById('userSearch').value.trim();
      const p = new URLSearchParams();
      if (q) p.set('q', q);
      if (S.tierFilter) p.set('tier', S.tierFilter);

      try {
        const [uRes, bRes, reqRes] = await Promise.all([
          req('/api/platform/users?' + p.toString()),
          req('/api/platform/bots'),
          req('/api/platform/upgrade-requests')
        ]);
        S.users = uRes.users || [];
        S.bots = bRes.bots || [];
        S.upgradeRequests = reqRes.requests || [];

        updateTierFilterCounts(uRes.tier_counts);
        renderUpgradeRequests(S.upgradeRequests);

        const container = document.getElementById('userAccordionContainer');
        if (!container) return;

        if (S.users.length === 0) {
          container.innerHTML = '<div style="text-align:center; padding:32px; color:var(--text-muted); background:#fff; border:1px dashed var(--border); border-radius:10px;">해당 조건의 유저가 없습니다.</div>';
          return;
        }

        container.innerHTML = S.users.map(u => {
          const slotShort = u.is_unlimited ? `${u.used_slots}/∞` : `${u.used_slots}/${u.max_slots}`;
          const isBlocked = u.status === 'BLOCKED';
          const dotColor = isBlocked ? '#ef4444' : '#10b981';
          const cleanUname = (u.username || '').trim().replace(/^@/, '');
          const aliasHtml = cleanUname ? `@${esc(cleanUname)}` : '';
          const isCustom = Number(u.is_name_custom || 0) === 1;

          const pendingReq = (S.upgradeRequests || []).find(r => r.telegram_user_id === u.telegram_user_id && r.status === 'PENDING');
          const pendingBadge = pendingReq
            ? `<span class="pill" style="background:#fef3c7; color:#b45309; border:1px solid #fcd34d; font-size:11px; font-weight:800; padding:2px 7px;" title="승인 대기 중">🔔 ${esc(pendingReq.requested_tier)}</span>`
            : '';

          const botChips = (u.active_subscriptions || []).map(s => `
            <span class="bot-chip" style="display:inline-flex; align-items:center; gap:4px;">
              <span>${esc(s.icon_emoji)} ${esc(s.bot_name)}</span>
              <button class="btn btn-sm" style="background:#2563eb; color:#fff; border:none; padding:1px 6px; font-size:10.5px; font-weight:700; border-radius:4px; margin-left:2px;" title="이 유저에게 최신 브리핑 즉시 1:1 발송" onclick="event.stopPropagation(); dispatchBotToUser('${esc(u.telegram_user_id)}', '${esc(s.bot_id)}', '${esc(s.bot_name)}', '${esc(u.full_name)}')">🚀 발송</button>
              <span class="bot-chip-del" title="탈퇴 처리" onclick="event.stopPropagation(); unsubUser('${esc(u.telegram_user_id)}', '${esc(s.bot_id)}', '${esc(s.bot_name)}')">✕</span>
            </span>
          `).join('');

          const suspChips = (u.suspended_subscriptions || []).map(s => `
            <span class="bot-chip" style="background:#fee2e2; color:#991b1b; border-color:#fca5a5;">
              ⏸️ ${esc(s.icon_emoji)} ${esc(s.bot_name)}
              <span class="bot-chip-del" onclick="event.stopPropagation(); unsubUser('${esc(u.telegram_user_id)}', '${esc(s.bot_id)}', '${esc(s.bot_name)}')">✕</span>
            </span>
          `).join('');

          const quickApproveBtn = pendingReq
            ? `<button class="btn btn-sm" style="background:#10b981; color:#fff; font-size:11.5px; font-weight:800; padding:3px 9px; border-radius:6px;" data-req-id="${pendingReq.id}" data-user-name="${esc(u.full_name)}" data-requested-tier="${esc(pendingReq.requested_tier)}" onclick="event.stopPropagation(); handleApproveBtnClick(this)">✅ 승인</button>`
            : '';

          return `
            <div class="user-accordion-item" data-user-card-id="${esc(u.telegram_user_id)}">
              <!-- 44px Slim 1-Line Row Header -->
              <div class="user-row-header" onclick="toggleUserAccordion('${esc(u.telegram_user_id)}')">
                <div class="user-row-left">
                  <span class="user-status-dot" style="background:${dotColor};" title="${isBlocked ? '차단됨' : '정상 가동'}"></span>
                  <span class="user-row-name">${esc(u.full_name)}</span>
                  ${isCustom ? '<span class="user-source-tag" style="background:#fef3c7; color:#92400e; border:1px solid #fde68a;">수동</span>' : ''}
                  ${aliasHtml ? `<span class="user-row-uname">(${aliasHtml})</span>` : ''}
                </div>
                <div class="user-row-right">
                  ${tierBadge(u.tier)}
                  <span class="pill" style="background:#f1f5f9; color:#475569; font-size:11px; font-weight:700; border:1px solid #cbd5e1; padding:2px 6px;">봇 ${slotShort}</span>
                  ${pendingBadge}
                  <span class="user-accordion-arrow">▼</span>
                </div>
              </div>

              <!-- Accordion Drawer (Expanded on Click) -->
              <div class="user-row-drawer">
                <div class="drawer-grid">
                  <!-- 식별 및 계정 정보 -->
                  <div class="drawer-card">
                    <div class="drawer-card-title">🆔 계정 및 식별 정보</div>
                    <div style="font-size:12.5px; display:flex; flex-direction:column; gap:4px;">
                      <div>ID: <code style="font-weight:700;">${esc(u.telegram_user_id)}</code> <button class="btn btn-sm" style="padding:1px 6px; font-size:10.5px; margin-left:4px;" onclick="event.stopPropagation(); navigator.clipboard.writeText('${esc(u.telegram_user_id)}'); toast('ID가 복사되었습니다');">복사</button></div>
                      <div>사용자명: <b>@${esc(cleanUname || '없음')}</b></div>
                      <div>실명 출처: <b>${isCustom ? '✍️ 관리자 직접 지정' : '🤖 텔레그램 프로필 연동'}</b> ${u.name_synced_at ? `<span style="font-size:11px; color:var(--text-muted);">(${esc(u.name_synced_at.slice(0,10))})</span>` : ''}</div>
                      <div>가입일시: <span style="color:var(--text-muted); font-size:11.5px;">${esc(u.created_at || '-')}</span></div>
                    </div>
                  </div>

                  <!-- 등급 및 슬롯 관리 -->
                  <div class="drawer-card">
                    <div class="drawer-card-title">🏅 회원 등급 및 만료일</div>
                    <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap;">
                      <select class="form-select user-tier-select" style="min-width:140px; font-weight:700; font-size:12px; padding:4px 8px;" onchange="changeTier('${esc(u.telegram_user_id)}', this.value)">
                        <option value="WHITE" ${u.tier === 'WHITE' ? 'selected' : ''}>⚪ 화이트 (1슬롯)</option>
                        <option value="GREEN" ${u.tier === 'GREEN' ? 'selected' : ''}>🟢 그린 (3슬롯)</option>
                        <option value="RED" ${u.tier === 'RED' ? 'selected' : ''}>🔴 레드 (5슬롯)</option>
                        <option value="BLACK" ${u.tier === 'BLACK' ? 'selected' : ''}>⚫ 블랙 (무제한)</option>
                      </select>
                      ${quickApproveBtn}
                    </div>
                    <div style="margin-top:6px; font-size:12px; color:var(--text-muted);">
                      유효기한: ${u.tier_expires_at ? `<code>${esc(u.tier_expires_at.slice(0,10))}</code>` : '<span style="font-weight:600;">무기한</span>'}
                    </div>
                  </div>
                </div>

                <!-- 구독 중인 봇 목록 -->
                <div class="drawer-card" style="margin-bottom:10px;">
                  <div class="drawer-card-title" style="justify-content:space-between;">
                    <span>🤖 구독 중인 정보봇 (${slotShort} 슬롯)</span>
                    <button class="btn btn-sm btn-primary" style="padding:2px 8px; font-size:11.5px;" onclick="event.stopPropagation(); openAddSubModal('${esc(u.telegram_user_id)}')">+ 봇 추가</button>
                  </div>
                  <div style="display:flex; flex-wrap:wrap; align-items:center; gap:6px; margin-top:6px;">
                    ${botChips || '<span style="color:var(--text-muted); font-size:12px;">구독 중인 봇이 없습니다.</span>'}
                    ${suspChips}
                  </div>
                </div>

                <!-- 관리자 메모 (있을 때만) -->
                ${u.memo ? `
                  <div class="drawer-card" style="margin-bottom:10px; background:#fffbeb; border-color:#fde68a;">
                    <div class="drawer-card-title" style="color:#b45309;">📝 관리자 메모</div>
                    <div style="font-size:12.5px; color:#92400e; white-space:pre-wrap;">${esc(u.memo)}</div>
                  </div>
                ` : ''}

                <!-- 드로어 하단 액션 버튼 바 -->
                <div class="drawer-actions">
                  <button class="btn btn-sm" onclick="event.stopPropagation(); openEditUserModal('${esc(u.telegram_user_id)}')">✏️ 실명/메모 수정</button>
                  <button class="btn btn-sm user-history-btn" onclick="event.stopPropagation(); openUserHistoryModal('${esc(u.telegram_user_id)}')">📜 변경·수신 이력</button>
                  ${isCustom ? `<button class="btn btn-sm btn-outline" style="color:#0284c7; border-color:#bae6fd;" onclick="event.stopPropagation(); resetUserNameToTelegram('${esc(u.telegram_user_id)}')">🔄 텔레그램명으로 복원</button>` : ''}
                  <button class="btn btn-sm btn-danger" onclick="event.stopPropagation(); deleteUser('${esc(u.telegram_user_id)}')">🗑️ 회원 삭제</button>
                </div>
              </div>
            </div>
          `;
        }).join('');
      } catch (e) {
        toast('유저 목록 로드 오류: ' + e.message);
      } finally {
        hideSplashLoader();
      }
    }

    function renderUpgradeRequests(requests) {
      const wrap = document.getElementById('upgradeRequestsSection');
      if (!wrap) return;

      const allReqs = requests || [];
      const pending = allReqs.filter(r => r.status === 'PENDING');
      const history = allReqs.filter(r => r.status !== 'PENDING');
      updatePendingBadges(pending.length);

      const tierColors = {
        WHITE: { bg: '#f1f5f9', fg: '#475569' },
        GREEN: { bg: '#ecfdf5', fg: '#047857' },
        RED: { bg: '#fef2f2', fg: '#b91c1c' },
        BLACK: { bg: '#0f172a', fg: '#f8fafc' },
      };

      let pendingHtml = '';
      if (pending.length === 0) {
        pendingHtml = `
          <div style="background:var(--surface-sub); border:1px dashed var(--border); border-radius:10px; padding:12px 18px; display:flex; justify-content:space-between; align-items:center; font-size:12.5px; color:var(--text-muted); flex-wrap:wrap; gap:8px;">
            <span>✅ <b>대기 중인 등급 업그레이드 신청이 없습니다.</b></span>
            <span style="font-size:11.5px;">봇에서 신청이 접수되면 이곳에 1-클릭 승인 카드가 즉시 나타납니다.</span>
          </div>
        `;
      } else {
        pendingHtml = `
          <div style="background:linear-gradient(135deg, #fffbeb 0%, #fef3c7 100%); border:1.5px solid #fcd34d; border-radius:12px; padding:16px 20px; box-shadow:0 4px 12px rgba(245, 158, 11, 0.12);">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px; flex-wrap:wrap; gap:8px;">
              <div style="display:flex; align-items:center; gap:8px;">
                <span style="font-size:20px;">🔔</span>
                <span style="font-weight:800; font-size:15px; color:#92400e;">등급 업그레이드 신청 대기</span>
                <span class="pill" style="background:#f59e0b; color:#fff; font-size:11px; padding:2px 8px; font-weight:800;">${pending.length}건 대기</span>
              </div>
              <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap;">
                <button class="btn btn-sm" style="background:#059669; color:#fff; border-color:#047857; font-weight:800; padding:5px 12px; border-radius:6px; font-size:12px; box-shadow:0 2px 6px rgba(5, 150, 105, 0.2);" onclick="handleBatchApproveClick(${pending.length})">⚡ ${pending.length}건 전체 일괄 승인</button>
                <span style="font-size:12px; color:#b45309;">👉 클릭 시 등급 즉시 변경 및 축하 알림 발송</span>
              </div>
            </div>
            <div style="display:flex; flex-direction:column; gap:8px;">
              ${pending.map(r => {
                const reqColor = tierColors[r.requested_tier] || { bg: '#ecfdf5', fg: '#047857' };
                return `
                  <div data-req-card-id="${esc(r.id)}" style="background:#ffffff; border:1px solid #fde68a; border-radius:8px; padding:12px 16px; display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:12px;">
                    <div>
                      <div style="display:flex; align-items:center; gap:6px;">
                        <span style="font-weight:800; font-size:14.5px;">${esc(r.full_name)}</span>
                        <span style="font-size:12px; color:var(--text-muted);">(ID: <code>${esc(r.telegram_user_id)}</code> ${r.username ? `· @${esc(r.username)}` : ''})</span>
                      </div>
                      <div style="margin-top:5px; font-size:13px; display:flex; align-items:center; gap:6px; flex-wrap:wrap;">
                        <span class="pill" style="background:#e2e8f0; color:#475569; font-size:11px;">현재: ${esc(r.current_tier)}</span>
                        <span>➔</span>
                        <span class="pill" style="background:${reqColor.bg}; color:${reqColor.fg}; font-size:11px; font-weight:800; border:1px solid ${reqColor.fg};">신청: ${esc(r.requested_tier)}</span>
                        <span style="font-size:11.5px; color:#94a3b8; margin-left:6px;">접수: <code>${esc(r.requested_at)}</code></span>
                      </div>
                    </div>
                    <div style="display:flex; gap:8px;">
                      <button class="btn btn-sm" style="background:#10b981; color:#fff; border-color:#059669; font-weight:800; padding:6px 16px; border-radius:6px; font-size:13px;" data-req-id="${r.id}" data-user-name="${esc(r.full_name)}" data-requested-tier="${esc(r.requested_tier)}" onclick="handleApproveBtnClick(this)">
                        ✅ 즉시 승인
                      </button>
                      <button class="btn btn-sm btn-outline" style="color:#64748b; padding:6px 12px; border-radius:6px; font-size:12px;" data-req-id="${r.id}" data-user-name="${esc(r.full_name)}" onclick="handleRejectBtnClick(this)">
                        ✕ 반려
                      </button>
                    </div>
                  </div>
                `;
              }).join('')}
            </div>
          </div>
        `;
      }

      let historyHtml = '';
      if (history.length > 0) {
        historyHtml = `
          <div style="margin-top:12px; background:#ffffff; border:1px solid var(--border); border-radius:10px; padding:12px 18px; box-shadow:0 1px 3px rgba(0,0,0,0.04);">
            <div style="display:flex; justify-content:space-between; align-items:center; cursor:pointer;" onclick="toggleUpgradeHistory()">
              <div style="display:flex; align-items:center; gap:8px;">
                <span style="font-size:15px;">📜</span>
                <span style="font-weight:700; font-size:13.5px; color:var(--text);">최근 심사 처리 이력 (${history.length}건)</span>
                <span style="font-size:11.5px; color:var(--text-muted);">(승인·반려 완료된 신청 내역)</span>
              </div>
              <span id="upgHistoryArrow" style="font-size:12px; font-weight:700; color:var(--primary); background:var(--surface-sub); padding:3px 8px; border-radius:4px;">펼치기 ▼</span>
            </div>
            <div id="upgHistoryList" style="display:none; margin-top:12px; border-top:1px solid var(--border); padding-top:10px; overflow-x:auto;">
              <table style="width:100%; border-collapse:collapse; font-size:12px;">
                <thead>
                  <tr style="color:var(--text-muted); text-align:left; border-bottom:1px solid var(--border); background:var(--surface-sub);">
                    <th style="padding:8px 10px;">ID</th>
                    <th style="padding:8px 10px;">신청 회원</th>
                    <th style="padding:8px 10px;">신청 등급</th>
                    <th style="padding:8px 10px;">처리 결과</th>
                    <th style="padding:8px 10px;">신청 일시</th>
                    <th style="padding:8px 10px;">심사 완료일시</th>
                    <th style="padding:8px 10px;">심사자</th>
                  </tr>
                </thead>
                <tbody>
                  ${history.map(h => {
                    const isApproved = h.status === 'APPROVED';
                    const badge = isApproved
                      ? '<span class="pill" style="background:#ecfdf5; color:#047857; font-weight:800; border:1px solid #10b981; font-size:11px;">✅ 승인 완료</span>'
                      : '<span class="pill" style="background:#fef2f2; color:#b91c1c; font-weight:800; border:1px solid #ef4444; font-size:11px;">❌ 반려됨</span>';
                    return `
                      <tr style="border-bottom:1px dashed var(--border);">
                        <td style="padding:8px 10px; color:var(--text-muted);">#${h.id}</td>
                        <td style="padding:8px 10px;">
                          <b>${esc(h.full_name)}</b> <span style="color:var(--text-muted); font-size:11px;">(<code>${esc(h.telegram_user_id)}</code>)</span>
                        </td>
                        <td style="padding:8px 10px;">
                          ${esc(h.current_tier)} ➔ <b>${esc(h.requested_tier)}</b>
                        </td>
                        <td style="padding:8px 10px;">${badge}</td>
                        <td style="padding:8px 10px; color:var(--text-muted);"><code>${esc(h.requested_at || '-')}</code></td>
                        <td style="padding:8px 10px; color:var(--text); font-weight:600;"><code>${esc(h.reviewed_at || '-')}</code></td>
                        <td style="padding:8px 10px;"><code style="background:#f1f5f9; padding:2px 6px; border-radius:4px;">${esc(h.reviewed_by || 'ADMIN')}</code></td>
                      </tr>
                    `;
                  }).join('')}
                </tbody>
              </table>
            </div>
          </div>
        `;
      }

      wrap.innerHTML = pendingHtml + historyHtml;
    }

    function toggleUpgradeHistory() {
      const el = document.getElementById('upgHistoryList');
      const arrow = document.getElementById('upgHistoryArrow');
      if (!el) return;
      if (el.style.display === 'none' || el.style.display === '') {
        el.style.display = 'block';
        if (arrow) arrow.textContent = '접기 ▲';
      } else {
        el.style.display = 'none';
        if (arrow) arrow.textContent = '펼치기 ▼';
      }
    }

    function handleApproveBtnClick(btn) {
      const id = btn.dataset.reqId;
      const userName = btn.dataset.userName;
      const requestedTier = btn.dataset.requestedTier;
      approveUpgradeRequest(id, userName, requestedTier);
    }

    function handleRejectBtnClick(btn) {
      const id = btn.dataset.reqId;
      const userName = btn.dataset.userName;
      rejectUpgradeRequest(id, userName);
    }

    async function approveUpgradeRequest(requestId, userName, requestedTier) {
      if (!confirm(`[${userName}] 님의 ${requestedTier} 등급 업그레이드를 승인하시겠습니까?\n승인 시 유저 등급이 즉시 상향되고 텔레그램으로 완료 안내가 자동 발송됩니다.`)) {
        return;
      }
      try {
        await req(`/api/platform/upgrade-requests/${requestId}/approve`, { method: 'POST' });
        toast(`🎉 [${userName}] 님의 ${requestedTier} 등급 승인이 완료되었습니다!`);
        await loadUsers();
      } catch (err) {
        alert('승인 처리 실패: ' + err.message);
        await loadUsers();
      }
    }

    async function rejectUpgradeRequest(requestId, userName) {
      const reason = prompt(`[${userName}] 님의 등급 업그레이드를 반려하시겠습니까?\n반려 사유를 입력하세요 (선택):`, '운영 정책 기준 미달');
      if (reason === null) return;
      try {
        await req(`/api/platform/upgrade-requests/${requestId}/reject`, {
          method: 'POST',
          body: { reason: reason || '운영 정책 기준 미달' }
        });
        toast(`반려 처리되었습니다.`);
        await loadUsers();
      } catch (err) {
        alert('반려 처리 실패: ' + err.message);
        await loadUsers();
      }
    }

    async function changeTier(userId, newTier) {
      const u = S.users.find(x => x.telegram_user_id === userId);
      const limits = { WHITE: 1, GREEN: 3, RED: 5, BLACK: 999999 };
      let mode = 'GRACE';
      if (u && u.used_slots > limits[newTier]) {
        const keep = confirm(`⚠️ [${u.full_name}]님은 현재 ${u.used_slots}개 봇을 구독 중입니다.\n새 등급 한도(${limits[newTier]}개)를 초과하여 최근 ${limits[newTier]}개만 남기고 나머지를 자동 탈퇴 처리할까요?`);
        mode = keep ? 'KEEP_RECENT' : 'GRACE';
      }
      try {
        await req(`/api/platform/users/${encodeURIComponent(userId)}/tier`, {
          method: 'PUT',
          body: { tier: newTier, downgrade_mode: mode }
        });
        toast(`✅ 등급이 ${newTier}(으)로 변경되었습니다.`);
        loadUsers();
      } catch (e) {
        alert(e.message);
        loadUsers();
      }
    }

    async function unsubUser(userId, botId, botName) {
      if (!confirm(`[${botName}] 구독을 탈퇴(해지) 처리하시겠습니까?`)) return;
      try {
        const res = await req('/api/platform/subscriptions/unsubscribe', {
          method: 'POST',
          body: { telegram_user_id: userId, bot_id: botId }
        });
        toast(res.message);
        loadUsers();
      } catch (e) {
        alert(e.message);
      }
    }

    function openAddSubModal(userId) {
      const u = S.users.find(x => x.telegram_user_id === userId);
      if (!u) return;
      document.getElementById('asUserId').value = userId;
      document.getElementById('addSubLabel').innerHTML = `대상: <b>${esc(u.full_name)}</b> (${tierBadge(u.tier)} · 슬롯 ${u.used_slots}/${u.is_unlimited ? '∞' : u.max_slots})`;
      const infoBots = S.bots.filter(b => b.bot_type === 'INFO');
      document.getElementById('asBotSel').innerHTML = infoBots.map(b =>
        `<option value="${esc(b.bot_id)}">${esc(b.icon_emoji)} ${esc(b.name)} (${esc(b.category)})</option>`
      ).join('');
      document.getElementById('addSubModal').classList.add('open');
    }

    async function confirmAddSub() {
      const uid = document.getElementById('asUserId').value;
      const bid = document.getElementById('asBotSel').value;
      try {
        const res = await req('/api/platform/subscriptions/subscribe', {
          method: 'POST',
          body: { telegram_user_id: uid, bot_id: bid }
        });
        if (!res.success && res.code === 'QUOTA_EXCEEDED') {
          const first = (res.active_subscriptions || [])[0];
          if (first && confirm(`🚫 ${res.message}\n\n기존 [${first.bot_name}]을 탈퇴하고 선택한 봇으로 즉시 교체(Swap)하시겠습니까?`)) {
            await req('/api/platform/subscriptions/swap', {
              method: 'POST',
              body: { telegram_user_id: uid, cancel_bot_id: first.bot_id, new_bot_id: bid }
            });
            closeModal('addSubModal');
            toast('🔄 봇 교체 완료!');
            loadUsers();
            return;
          }
          return;
        }
        closeModal('addSubModal');
        toast(res.message);
        loadUsers();
      } catch (e) {
        alert(e.message);
      }
    }

    function openNewUserModal() {
      document.getElementById('userModalTitle').textContent = '➕ 신규 유저 등록';
      document.getElementById('umId').value = '';
      document.getElementById('umId').disabled = false;
      document.getElementById('umName').value = '';
      document.getElementById('umUsername').value = '';
      document.getElementById('umTier').value = 'WHITE';
      document.getElementById('umExp').value = '';
      document.getElementById('umMemo').value = '';
      document.getElementById('userModal').classList.add('open');
    }

    function openEditUserModal(userId) {
      const u = S.users.find(x => x.telegram_user_id === userId);
      if (!u) return;
      document.getElementById('userModalTitle').textContent = `👤 [${u.full_name}] 유저 관리`;
      document.getElementById('umId').value = u.telegram_user_id;
      document.getElementById('umId').disabled = true;
      document.getElementById('umName').value = u.full_name || '';
      document.getElementById('umUsername').value = u.username ? ('@' + u.username.replace(/^@/, '')) : '';
      document.getElementById('umTier').value = u.tier || 'WHITE';
      document.getElementById('umExp').value = u.tier_expires_at ? u.tier_expires_at.slice(0,10) : '';
      document.getElementById('umMemo').value = u.memo || '';
      document.getElementById('userModal').classList.add('open');
    }

    async function saveUser() {
      const uid = document.getElementById('umId').value.trim();
      const name = document.getElementById('umName').value.trim();
      const uname = document.getElementById('umUsername').value.trim().replace(/^@/, '');
      const tier = document.getElementById('umTier').value;
      const exp = document.getElementById('umExp').value || null;
      const memo = document.getElementById('umMemo').value.trim();

      if (!uid || !name) {
        alert('User ID와 이름은 필수입니다.');
        return;
      }
      try {
        if (!document.getElementById('umId').disabled) {
          await req('/api/platform/users', {
            method: 'POST',
            body: { telegram_user_id: uid, full_name: name, username: uname, tier, tier_expires_at: exp, memo }
          });
        } else {
          await req(`/api/platform/users/${encodeURIComponent(uid)}`, {
            method: 'PUT',
            body: { full_name: name, username: uname, memo }
          });
          await req(`/api/platform/users/${encodeURIComponent(uid)}/tier`, {
            method: 'PUT',
            body: { tier, tier_expires_at: exp, memo }
          });
        }
        closeModal('userModal');
        toast('✅ 유저 정보가 저장되었습니다.');
        loadUsers();
      } catch (e) {
        alert(e.message);
      }
    }

    async function deleteUser(userId) {
      if (!confirm(`유저(${userId})를 삭제하시겠습니까?`)) return;
      try {
        await req(`/api/platform/users/${encodeURIComponent(userId)}`, { method: 'DELETE' });
        toast('🗑️ 삭제되었습니다.');
        loadUsers();
      } catch (e) {
        alert(e.message);
      }
    }

    // =========================================================================
    // TAB 2: BOTS & DIRECT BROADCAST
    // =========================================================================
    let currentBriefingData = {
      bot_id: '',
      preset: '',
      content_html: '',
      inline_keyboard: []
    };

    async function loadBots() {
      try {
        const res = await req('/api/platform/bots');
        S.bots = res.bots || [];
        const menuBot = S.bots.find(b => b.bot_type === 'MENU');
        const infoBots = S.bots.filter(b => b.bot_type === 'INFO');

        // Render Menu Bot Section
        const mBox = document.getElementById('menuBotSection');
        if (!menuBot) {
          mBox.innerHTML = '<div style="color:var(--text-muted);">메뉴봇이 없습니다.</div>';
        } else {
          const uname = menuBot.telegram_username ? `@${menuBot.telegram_username}` : '토큰 등록 필요';
          const tokenBadge = menuBot.has_token
            ? `<span style="color:var(--success); font-weight:700;">🟢 토큰 등록됨</span>`
            : `<span style="color:var(--warning); font-weight:700;">🟠 토큰 필요</span>`;
          const workerBadge = menuBot.worker_running
            ? `<span style="background:#dcfce7; color:#15803d; font-size:11px; padding:2px 6px; border-radius:4px; font-weight:700;">⚡ 실시간 폴링 가동중</span>`
            : '';
          mBox.innerHTML = `
            <div class="menu-bot-box">
              <div class="menu-bot-info">
                <span style="font-size:36px; flex-shrink:0;">${esc(menuBot.icon_emoji)}</span>
                <div>
                  <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap;">
                    <strong style="font-size:16px;">${esc(menuBot.name)}</strong>
                    <span style="font-size:12px; color:var(--primary); font-weight:700;">${uname}</span>
                    <span style="font-size:12px;">${tokenBadge}</span>
                    ${workerBadge}
                    <span class="badge" style="background:#dbeafe; color:#1e40af; font-size:11.5px; padding:2px 7px; border-radius:4px; font-weight:700;">가입·안내 포털 (6대 봇 통합 허브)</span>
                  </div>
                  <div style="font-size:13px; color:var(--text-muted); margin-top:4px;">${esc(menuBot.description)}</div>
                </div>
              </div>
              <div class="menu-bot-btns">
                <button class="btn btn-sm btn-primary" onclick="openBriefingModal('${esc(menuBot.bot_id)}')">🚀 포털 카탈로그 발송 센터</button>
                <button class="btn btn-sm btn-dark" onclick="openSimModal('${esc(menuBot.bot_id)}')">👁️ 유저 시점 미리보기</button>
                <button class="btn btn-sm" onclick="openEditBotModal('${esc(menuBot.bot_id)}')">⚙️ 설정·토큰 수정</button>
              </div>
            </div>
          `;
        }

        // Render Info Bots
        document.getElementById('infoBotGrid').innerHTML = infoBots.map(b => {
          const tokenBadge = b.has_token
            ? `<span style="color:var(--success); font-weight:700;">🟢 연결됨</span>`
            : `<span style="color:var(--warning); font-weight:700;">🟠 토큰 필요</span>`;
          const unameTag = b.telegram_username
            ? `<span style="color:var(--primary); font-weight:700;">@${esc(b.telegram_username)}</span> · `
            : '';
          const workerTag = b.worker_running
            ? ` · <span style="color:#15803d; font-weight:700;">⚡ 폴링중</span>`
            : '';

          return `
            <div class="bot-card">
              <div>
                <div style="display:flex; justify-content:space-between; align-items:flex-start; margin-bottom:8px;">
                  <div style="display:flex; align-items:center; gap:10px;">
                    <span style="font-size:28px;">${esc(b.icon_emoji)}</span>
                    <div>
                      <div style="font-size:15px; font-weight:800;">${esc(b.name)}</div>
                      <div style="font-size:12px; color:var(--text-muted);">${unameTag}${esc(b.category)} · ${tokenBadge}${workerTag}</div>
                    </div>
                  </div>
                  <span style="font-size:12px; font-weight:800; background:#dbeafe; color:#1e40af; padding:3px 8px; border-radius:6px; flex-shrink:0;">
                    👥 ${b.active_subscribers}명 구독중
                  </span>
                </div>
                <div style="font-size:12.5px; color:#475569; margin:8px 0;">${esc(b.description || '설명 없음')}</div>
              </div>
              <div class="bot-card-actions">
                <button class="btn btn-sm btn-primary btn-dispatch-center" onclick="openBriefingModal('${esc(b.bot_id)}')">
                  🚀 정규 브리핑 발송 센터
                </button>
                <div class="bot-card-sub-actions">
                  <button class="btn btn-sm" onclick="openEditBotModal('${esc(b.bot_id)}')">⚙️ 설정</button>
                  <button class="btn btn-sm" onclick="openSimModal('${esc(b.bot_id)}')">🧪 시뮬</button>
                </div>
              </div>
            </div>
          `;
        }).join('') || '<div style="color:var(--text-muted);">등록된 정보봇이 없습니다.</div>';
      } catch (e) {
        toast('봇 목록 로드 실패: ' + e.message);
      }
    }

    async function openBriefingModal(botId) {
      const bot = S.bots.find(b => b.bot_id === botId);
      if (!bot) return;
      document.getElementById('bfBotId').value = botId;
      document.getElementById('bfModalTitle').textContent = `🚀 [${bot.name}] 정규 브리핑 생성 & 발송 센터`;
      document.getElementById('bfSubCountText').textContent = bot.active_subscribers || 0;
      document.getElementById('bfCustomText').value = '';

      try {
        const pRes = await req(`/api/platform/bots/${botId}/presets`);
        const presets = pRes.presets || [];
        const pBox = document.getElementById('bfPresetButtons');
        pBox.innerHTML = presets.map((p, idx) => `
          <button class="btn btn-sm ${idx === 0 ? 'btn-primary' : 'btn-outline'}" onclick="loadPresetPreview('${botId}', '${p.id}', this)">
            ${esc(p.label)}
          </button>
        `).join('');

        document.getElementById('briefingModal').classList.add('open');
        if (presets.length > 0) {
          loadPresetPreview(botId, presets[0].id);
        }
      } catch (e) {
        toast('프리셋 로드 실패: ' + e.message);
      }
    }

    async function loadPresetPreview(botId, presetId, btnElem = null) {
      if (btnElem) {
        document.querySelectorAll('#bfPresetButtons button').forEach(b => {
          b.className = 'btn btn-sm btn-outline';
        });
        btnElem.className = 'btn btn-sm btn-primary';
      }
      document.getElementById('bfPreviewBox').textContent = '데이터 수집 및 실시간 브리핑 생성 중...';
      try {
        const res = await req(`/api/platform/bots/${botId}/preview`, {
          method: 'POST',
          body: { preset: presetId }
        });
        currentBriefingData = res;
        document.getElementById('bfGeneratedAt').textContent = `생성시각: ${res.generated_at}`;
        document.getElementById('bfPreviewBox').innerHTML = res.content_html;

        const btnBox = document.getElementById('bfPreviewButtons');
        if (res.inline_keyboard && res.inline_keyboard.length > 0) {
          btnBox.innerHTML = res.inline_keyboard.flat().map(b => `
            <span class="btn btn-sm" style="background:#e0f2fe; color:#0369a1; border:1px solid #bae6fd; font-size:11.5px; cursor:default;">
              🔗 ${esc(b.text)}
            </span>
          `).join('');
        } else {
          btnBox.innerHTML = '';
        }
      } catch (e) {
        document.getElementById('bfPreviewBox').textContent = '브리핑 생성 실패: ' + e.message;
      }
    }

    async function dispatchBriefingTest() {
      const bid = document.getElementById('bfBotId').value;
      if (!currentBriefingData.content_html) {
        alert('먼저 브리핑 내용을 생성해 주세요.');
        return;
      }
      try {
        toast('📱 관리자 텔레그램으로 테스트 발송 중...');
        const res = await req(`/api/platform/bots/${bid}/dispatch-test`, {
          method: 'POST',
          body: {
            content_html: currentBriefingData.content_html,
            inline_keyboard: currentBriefingData.inline_keyboard
          }
        });
        alert(res.message || '✅ 관리자 텔레그램으로 테스트 발송 완료!');
      } catch (e) {
        alert('테스트 발송 실패: ' + e.message);
      }
    }

    async function dispatchBriefingSubscribers() {
      const bid = document.getElementById('bfBotId').value;
      if (!currentBriefingData.content_html) {
        alert('먼저 브리핑 내용을 생성해 주세요.');
        return;
      }
      const subCount = document.getElementById('bfSubCountText').textContent;
      if (!confirm(`정말로 활성 구독자 (${subCount}명) 전체에게 이 정규 브리핑을 발송하시겠습니까?`)) return;

      try {
        toast('🚀 구독자들에게 안전 발송 중...');
        const res = await req(`/api/platform/bots/${bid}/dispatch-subscribers`, {
          method: 'POST',
          body: {
            content_html: currentBriefingData.content_html,
            inline_keyboard: currentBriefingData.inline_keyboard
          }
        });
        alert(res.message || '✅ 구독자 발송 완료!');
        closeModal('briefingModal');
      } catch (e) {
        alert('구독자 발송 실패: ' + e.message);
      }
    }

    async function dispatchCustomNotice() {
      const bid = document.getElementById('bfBotId').value;
      const text = document.getElementById('bfCustomText').value.trim();
      if (!text) {
        alert('발송할 수동 메시지를 입력하세요.');
        return;
      }
      if (!confirm('입력하신 수동 메시지를 전체 구독자에게 발송하시겠습니까?')) return;
      try {
        const res = await req(`/api/platform/bots/${bid}/dispatch-subscribers`, {
          method: 'POST',
          body: { content_html: text }
        });
        alert(res.message || '✅ 발송 완료!');
        closeModal('briefingModal');
      } catch (e) {
        alert('수동 발송 실패: ' + e.message);
      }
    }

    // Compatibility shim
    function openBroadcastModal(botId) {
      openBriefingModal(botId);
    }

    function openNewBotModal() {
      document.getElementById('botModalTitle').textContent = '➕ 새 정보봇 추가';
      document.getElementById('bmId').value = '';
      document.getElementById('bmId').disabled = false;
      document.getElementById('bmType').value = 'INFO';
      document.getElementById('bmEmoji').value = '🤖';
      document.getElementById('bmName').value = '';
      document.getElementById('bmCategory').value = '일반';
      document.getElementById('bmUsername').value = '';
      document.getElementById('bmToken').value = '';
      document.getElementById('bmTokenHint').textContent = '@BotFather 발급 토큰 입력 시 자동 연동됩니다.';
      document.getElementById('bmDesc').value = '';
      document.getElementById('bmActive').checked = true;
      document.getElementById('botModal').classList.add('open');
    }

    function openEditBotModal(botId) {
      const b = S.bots.find(x => x.bot_id === botId);
      if (!b) return;
      document.getElementById('botModalTitle').textContent = `⚙️ [${b.name}] 설정`;
      document.getElementById('bmId').value = b.bot_id;
      document.getElementById('bmId').disabled = true;
      document.getElementById('bmType').value = b.bot_type;
      document.getElementById('bmEmoji').value = b.icon_emoji || '🤖';
      document.getElementById('bmName').value = b.name || '';
      document.getElementById('bmCategory').value = b.category || '';
      document.getElementById('bmUsername').value = b.telegram_username || '';
      document.getElementById('bmToken').value = '';
      document.getElementById('bmTokenHint').textContent = b.has_token ? `저장된 토큰: ${b.token_masked} (비워두면 유지)` : '토큰 미등록';
      document.getElementById('bmDesc').value = b.description || '';
      document.getElementById('bmActive').checked = !!b.is_active;
      document.getElementById('botModal').classList.add('open');
    }

    async function saveBot() {
      const payload = {
        bot_id: document.getElementById('bmId').value.trim(),
        bot_type: document.getElementById('bmType').value,
        icon_emoji: document.getElementById('bmEmoji').value.trim() || '🤖',
        name: document.getElementById('bmName').value.trim(),
        category: document.getElementById('bmCategory').value.trim() || '일반',
        telegram_username: document.getElementById('bmUsername').value.trim(),
        token: document.getElementById('bmToken').value.trim() || '__KEEP__',
        description: document.getElementById('bmDesc').value.trim(),
        is_active: document.getElementById('bmActive').checked,
      };
      if (!payload.bot_id || !payload.name) {
        alert('봇 ID와 이름은 필수입니다.');
        return;
      }
      try {
        await req('/api/platform/bots', { method: 'POST', body: payload });
        closeModal('botModal');
        toast('✅ 봇 정보가 저장되었습니다.');
        loadBots();
      } catch (e) {
        alert(e.message);
      }
    }

    // =========================================================================
    // TAB 3: DASHBOARD
    // =========================================================================
    async function loadDashboard() {
      try {
        const [ov, setRes, gh] = await Promise.all([
          req('/api/platform/overview'),
          req('/api/settings'),
          req('/api/recipients/github-sync-status'),
        ]);
        S.overview = ov;
        document.getElementById('kpiUsers').textContent = `${ov.total_users}명`;
        document.getElementById('kpiUsersSub').textContent = ov.over_quota_users > 0 ? `⚠️ 초과 정리 필요: ${ov.over_quota_users}명` : '모든 유저 한도 준수 중';
        document.getElementById('kpiSubs').textContent = `${ov.total_active_subscriptions}건`;
        const infoBotsCount = ov.info_bots_count ?? (ov.bot_rankings ? ov.bot_rankings.length : 6);
        document.getElementById('kpiBots').textContent = `${infoBotsCount}개`;
        const kpiBotsSubEl = document.getElementById('kpiBotsSub');
        if (kpiBotsSubEl) {
          kpiBotsSubEl.textContent = `정보봇 ${infoBotsCount}개 (포털 메뉴봇 1개 별도)`;
        }

        // Tier Bars
        document.getElementById('tierDistList').innerHTML = ov.tier_distribution.map(t => `
          <div style="display:flex; justify-content:space-between; align-items:center; padding:8px 0; border-bottom:1px solid var(--border);">
            <span>${tierBadge(t.tier_code)} <span style="font-size:12.5px; color:var(--text-muted);">${esc(t.description.slice(0, 24))}...</span></span>
            <span style="font-weight:800; font-size:14px;">${t.user_count}명</span>
          </div>
        `).join('');

        // Bot Rankings
        document.getElementById('botRankList').innerHTML = ov.bot_rankings.map(b => `
          <div style="display:flex; justify-content:space-between; align-items:center; padding:8px 0; border-bottom:1px solid var(--border);">
            <span>${esc(b.icon_emoji)} <b>${esc(b.name)}</b></span>
            <span style="font-weight:800; color:var(--primary); font-size:13.5px;">${b.active_subscribers}명</span>
          </div>
        `).join('');

        // Audit Logs
        document.getElementById('auditLogList').innerHTML = (ov.recent_audit_logs || []).map(l => `
          <div style="padding:8px 0; border-bottom:1px solid var(--border); font-size:12.5px; display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:4px;">
            <div><b>${esc(l.action)}</b> · ${esc(l.user_name)} ${l.bot_name ? `→ <span style="color:var(--primary); font-weight:700;">${esc(l.bot_name)}</span>` : ''}</div>
            <span style="color:var(--text-muted); font-size:11.5px; flex-shrink:0;">${esc((l.logged_at || '').slice(5, 16))}</span>
          </div>
        `).join('') || '<div style="color:var(--text-muted);">이력 없음</div>';

        // Advanced Settings
        S.thresholds = setRes.thresholds || {};
        document.getElementById('thresholdEditor').innerHTML = Object.entries(S.thresholds).map(([sym, cfg]) => `
          <div style="border:1px solid var(--border); border-radius:6px; padding:8px; background:#fff;">
            <div style="font-size:12px; font-weight:700;">${esc(sym)}</div>
            <input type="number" step="0.1" class="form-input" style="padding:4px 8px; font-size:12px; margin-top:4px;" value="${cfg.threshold}" onchange="S.thresholds['${esc(sym)}'].threshold = parseFloat(this.value)" />
          </div>
        `).join('');

        document.getElementById('ghSyncText').textContent = gh.github_connected
          ? (gh.is_synced ? `🟢 GitHub Actions 수신자 변수 동기화 일치 (${gh.local_count}명)` : `🟠 불일치 (로컬 ${gh.local_count} vs 깃허브 ${gh.github_count})`)
          : '⚪ GITHUB_TOKEN 미설정';
      } catch (e) {
        toast('대시보드 로드 실패: ' + e.message);
      }
    }

    // =========================================================================
    // TAB 4: HISTORY CENTER (DISPATCH & PLATFORM AUDIT)
    // =========================================================================
    const HIST = { subpane: 'dispatch', auditOffset: 0, auditLimit: 30, auditTotal: 0 };
    let auditSearchDebounce = null;

    // delivery_* timestamps are stored as SQLite CURRENT_TIMESTAMP (UTC) -> render as KST
    function utcToKst(s) {
      if (!s) return '';
      const d = new Date(String(s).replace(' ', 'T') + 'Z');
      if (isNaN(d.getTime())) return String(s);
      return new Date(d.getTime() + 9 * 3600000).toISOString().slice(0, 16).replace('T', ' ');
    }

    function dispatchKindLabel(kind, isForced) {
      const k = String(kind || '').toLowerCase();
      let label = esc(kind || '-');
      let cls = '';
      if (k === 'morning' || k === 'closing' || k === 'weekly' || k === 'regular') { label = '정기'; cls = 'ok'; }
      else if (k === 'urgent') { label = '긴급'; cls = 'bad'; }
      else if (k === 'manual') { label = '수동'; cls = 'info'; }
      const forced = (isForced && k !== 'manual') ? ' <span class="hist-badge warn">강제</span>' : '';
      return `<span class="hist-badge ${cls}">${label}</span>${forced}`;
    }

    function deliveryStatusBadge(status) {
      const s = String(status || 'PENDING').toUpperCase();
      const map = { SENT: ['ok', '✅ 성공'], PARTIAL: ['warn', '⚠️ 부분'], FAILED: ['bad', '❌ 실패'], UNKNOWN: ['warn', '❔ 불명'], PENDING: ['', '⏳ 대기'], RATE_LIMITED: ['warn', '⏱️ 제한'] };
      const [cls, label] = map[s] || ['', esc(s)];
      return `<span class="hist-badge ${cls}">${label}</span>`;
    }

    function auditActionBadge(action) {
      const a = String(action || '').toUpperCase();
      let cls = '';
      if (a === 'USER_UPDATED' || a === 'TIER_CHANGE' || a === 'SWAP') cls = 'info';
      else if (a === 'SUBSCRIBE' || a === 'USER_CREATED' || a === 'USER_JOINED') cls = 'ok';
      else if (a === 'UNSUBSCRIBE' || a === 'USER_DELETED' || a === 'QUOTA_REJECTED' || a.indexOf('EXPIRED') >= 0 || a.indexOf('DOWNGRADE') >= 0) cls = 'bad';
      else if (a.indexOf('UPGRADE') >= 0) cls = 'warn';
      return `<span class="hist-badge ${cls}">${esc(a || '-')}</span>`;
    }

    const AUDIT_FIELD_LABELS = { full_name: '실명', username: '별칭', status: '상태', memo: '메모' };

    function fmtDiffVal(field, v) {
      const s = (v === null || v === undefined) ? '' : String(v);
      if (field === 'username' && s) return '@' + s.replace(/^@/, '');
      return s;
    }

    function formatAuditDetail(log) {
      const d = log.detail;
      if (d === null || d === undefined || d === '') return '<span class="hist-meta">-</span>';
      if (typeof d !== 'object') return `<div class="hist-diff-line">${esc(d)}</div>`;

      if (d.changes && typeof d.changes === 'object') {
        const lines = Object.entries(d.changes).map(([field, ch]) => {
          const label = esc(AUDIT_FIELD_LABELS[field] || field);
          const from = fmtDiffVal(field, ch && ch.from);
          const to = fmtDiffVal(field, ch && ch.to);
          if (!from) return `<div class="hist-diff-line"><b>${label}:</b> <span class="hist-diff-to">"${esc(to)}"</span></div>`;
          if (!to) return `<div class="hist-diff-line"><b>${label}:</b> <span class="hist-diff-from">"${esc(from)}"</span> ➔ <span class="hist-meta">(삭제)</span></div>`;
          return `<div class="hist-diff-line"><b>${label}:</b> <span class="hist-diff-from">"${esc(from)}"</span> ➔ <span class="hist-diff-to">"${esc(to)}"</span></div>`;
        });
        return lines.join('') || '<span class="hist-meta">-</span>';
      }

      const parts = [];
      if (d.old_tier !== undefined || d.new_tier !== undefined) {
        parts.push(`<div class="hist-diff-line"><b>등급:</b> <span class="hist-diff-from">${esc(d.old_tier || '-')}</span> ➔ <span class="hist-diff-to">${esc(d.new_tier || '-')}</span></div>`);
      }
      Object.entries(d).forEach(([k, v]) => {
        if (k === 'old_tier' || k === 'new_tier') return;
        const val = (v !== null && typeof v === 'object') ? JSON.stringify(v) : String(v);
        parts.push(`<div class="hist-diff-line"><span class="hist-meta">${esc(k)}:</span> ${esc(val.length > 120 ? val.slice(0, 120) + '…' : val)}</div>`);
      });
      if (log.bot_name) parts.unshift(`<div class="hist-diff-line"><b>봇:</b> ${esc(log.bot_name)}</div>`);
      return parts.join('') || '<span class="hist-meta">-</span>';
    }

    function auditUserLabel(log) {
      const name = log.user_name || log.telegram_user_id || '-';
      const uname = (log.username || '').replace(/^@/, '');
      const alias = (uname && uname !== name) ? ` <span class="hist-meta">(@${esc(uname)})</span>` : '';
      return `<b>${esc(name)}</b>${alias}<div class="hist-meta"><code>${esc(log.telegram_user_id)}</code></div>`;
    }

    function loadHistoryCenter() {
      switchHistorySubpane(HIST.subpane);
    }

    function switchHistorySubpane(sp) {
      HIST.subpane = sp;
      document.querySelectorAll('#pane-history .hist-subtab-btn').forEach(b => b.classList.toggle('active', b.dataset.subpane === sp));
      document.querySelectorAll('#pane-history .hist-subpane').forEach(p => p.classList.toggle('active', p.id === `subpane-${sp}`));
      if (sp === 'dispatch') loadDispatchHistory();
      if (sp === 'audit') loadAuditLogs();
    }

    async function loadDispatchHistory() {
      const tbody = document.getElementById('dispatchHistoryBody');
      try {
        const jobs = await req('/api/history?limit=50');
        if (!Array.isArray(jobs) || jobs.length === 0) {
          tbody.innerHTML = '<tr><td colspan="6" class="empty-cell" style="text-align:center; padding:28px; color:var(--text-muted);">발송 이력이 없습니다.</td></tr>';
          return;
        }
        const BOT_TOPIC_LABELS = {
          brief_bot: '📈 시황 브리핑',
          travel_bot: '✈️ 여행 특가',
          admission_bot: '🎓 입시 나침반',
          silver_bot: '👵 실버케어',
          kids_bot: '👶 자녀 나침반',
          yolo_bot: '🏄 욜로 라이프',
          menu_bot: '🧭 통합 메뉴봇',
          FINANCE: '📈 시황 브리핑',
          TRAVEL: '✈️ 여행 특가',
          EDUCATION: '🎓 입시 나침반',
          ADMISSION: '🎓 입시 나침반',
          SILVER: '👵 실버케어',
          KIDS: '👶 자녀 나침반',
          YOLO: '🏄 욜로 라이프',
          PORTAL: '🧭 통합 메뉴봇',
        };
        tbody.innerHTML = jobs.map(j => {
          const total = Number(j.total_targets || 0);
          const ok = Number(j.success_count || 0);
          const pct = total > 0 ? Math.round((ok / total) * 100) : 0;
          const bk = String(j.batch_key || '');
          const manualBot = bk.indexOf('MANUAL:') === 0 ? bk.split(':')[1] : '';
          const rawKey = manualBot || j.topic || '-';
          const friendlyBot = BOT_TOPIC_LABELS[rawKey] || rawKey;
          return `
            <tr>
              <td data-label="발송 일시">${esc(utcToKst(j.finished_at || j.started_at))}</td>
              <td data-label="봇 / 토픽"><b>${esc(friendlyBot)}</b>${manualBot ? ` <span class="hist-meta">(${esc(manualBot)})</span>` : ` <span class="hist-meta">${esc(j.topic || '')}</span>`}</td>
              <td data-label="유형">${dispatchKindLabel(j.kind, j.is_forced)}</td>
              <td data-label="성공률"><b>${pct}%</b> <span class="hist-meta">(${ok}/${total})</span></td>
              <td data-label="상태">${deliveryStatusBadge(j.status)}</td>
              <td data-label="상세"><button class="btn btn-sm hist-touch" data-job-id="${esc(j.job_id)}" onclick="openJobDetail(this.dataset.jobId)">상세보기</button></td>
            </tr>`;
        }).join('');
      } catch (e) {
        tbody.innerHTML = `<tr><td colspan="6" class="empty-cell" style="text-align:center; padding:28px; color:var(--danger);">발송 이력 로드 실패: ${esc(e.message)}</td></tr>`;
      }
    }

    async function openJobDetail(jobId) {
      document.getElementById('jobDetailTitle').textContent = `📨 발송 상세 · ${jobId}`;
      document.getElementById('jobDetailSummary').textContent = '';
      const list = document.getElementById('jobDetailList');
      list.innerHTML = '<div class="hist-meta">불러오는 중...</div>';
      document.getElementById('jobDetailModal').classList.add('open');
      try {
        const res = await req(`/api/history/${encodeURIComponent(jobId)}/details`);
        const rows = res.recipients || [];
        const cnt = { SENT: 0, FAILED: 0, OTHER: 0 };
        rows.forEach(r => { const s = String(r.status || '').toUpperCase(); if (s === 'SENT') cnt.SENT++; else if (s === 'FAILED') cnt.FAILED++; else cnt.OTHER++; });
        document.getElementById('jobDetailSummary').textContent = `총 ${rows.length}명 · 성공 ${cnt.SENT} · 실패 ${cnt.FAILED} · 기타 ${cnt.OTHER}`;
        list.innerHTML = rows.map(r => `
          <div class="hist-card">
            <div class="hist-card-top">
              <span><b>${esc(r.name || '-')}</b> <span class="hist-meta"><code>${esc(r.chat_id)}</code></span></span>
              ${deliveryStatusBadge(r.status)}
            </div>
            ${r.error_detail ? `<div class="hist-diff-line" style="color:var(--danger);">${esc(r.error_detail)}</div>` : ''}
            <div class="hist-meta">${esc(utcToKst(r.updated_at))}</div>
          </div>`).join('') || '<div class="hist-meta">수신자 스냅샷이 없습니다.</div>';
      } catch (e) {
        list.innerHTML = `<div style="color:var(--danger);">상세 로드 실패: ${esc(e.message)}</div>`;
      }
    }

    function debouncedLoadAuditLogs() {
      clearTimeout(auditSearchDebounce);
      auditSearchDebounce = setTimeout(() => { HIST.auditOffset = 0; loadAuditLogs(); }, 250);
    }

    function pageAuditLogs(dir) {
      const next = HIST.auditOffset + dir * HIST.auditLimit;
      if (next < 0 || next >= Math.max(HIST.auditTotal, 1)) return;
      HIST.auditOffset = next;
      loadAuditLogs();
    }

    async function loadAuditLogs() {
      const tbody = document.getElementById('auditHistoryBody');
      const p = new URLSearchParams();
      const q = document.getElementById('auditSearch').value.trim();
      const action = document.getElementById('auditActionSel').value;
      if (q) p.set('q', q);
      if (action) p.set('action', action);
      p.set('limit', HIST.auditLimit);
      p.set('offset', HIST.auditOffset);
      try {
        const res = await req('/api/platform/audit-logs?' + p.toString());
        const logs = res.logs || [];
        HIST.auditTotal = Number(res.total || 0);
        const start = HIST.auditTotal === 0 ? 0 : HIST.auditOffset + 1;
        document.getElementById('auditPageInfo').textContent = `${start}–${HIST.auditOffset + logs.length} / 총 ${HIST.auditTotal}건`;
        document.getElementById('auditPrevBtn').disabled = HIST.auditOffset <= 0;
        document.getElementById('auditNextBtn').disabled = HIST.auditOffset + HIST.auditLimit >= HIST.auditTotal;
        if (logs.length === 0) {
          tbody.innerHTML = '<tr><td colspan="5" class="empty-cell" style="text-align:center; padding:28px; color:var(--text-muted);">해당 조건의 감사 이력이 없습니다.</td></tr>';
          return;
        }
        tbody.innerHTML = logs.map(l => `
          <tr>
            <td data-label="일시" style="white-space:nowrap;">${esc((l.logged_at || '').slice(0, 16))}</td>
            <td data-label="대상 유저"><div>${auditUserLabel(l)}</div></td>
            <td data-label="구분">${auditActionBadge(l.action)}</td>
            <td data-label="변경 상세"><div>${formatAuditDetail(l)}</div></td>
            <td data-label="작업자">${esc(l.actor || '-')}</td>
          </tr>`).join('');
      } catch (e) {
        tbody.innerHTML = `<tr><td colspan="5" class="empty-cell" style="text-align:center; padding:28px; color:var(--danger);">감사 이력 로드 실패: ${esc(e.message)}</td></tr>`;
      }
    }

    function switchUserHistoryTab(tab) {
      document.querySelectorAll('#userHistoryModal .uh-tab-btn').forEach(b => b.classList.toggle('active', b.dataset.uhtab === tab));
      document.getElementById('uhPaneAudit').style.display = tab === 'audit' ? '' : 'none';
      document.getElementById('uhPaneDelivery').style.display = tab === 'delivery' ? '' : 'none';
    }

    async function openUserHistoryModal(userId) {
      const u = (S.users || []).find(x => String(x.telegram_user_id) === String(userId));
      document.getElementById('userHistoryTitle').textContent = `📜 ${u ? (u.full_name || userId) : userId} 이력`;
      const auditEl = document.getElementById('uhPaneAudit');
      const delivEl = document.getElementById('uhPaneDelivery');
      auditEl.innerHTML = '<div class="hist-meta">불러오는 중...</div>';
      delivEl.innerHTML = '<div class="hist-meta">불러오는 중...</div>';
      switchUserHistoryTab('audit');
      document.getElementById('userHistoryModal').classList.add('open');
      try {
        const res = await req(`/api/platform/users/${encodeURIComponent(userId)}/history`);
        auditEl.innerHTML = (res.audit_logs || []).map(l => `
          <div class="hist-card">
            <div class="hist-card-top">
              ${auditActionBadge(l.action)}
              <span class="hist-meta">${esc((l.logged_at || '').slice(0, 16))} · ${esc(l.actor || '-')}</span>
            </div>
            ${formatAuditDetail(l)}
          </div>`).join('') || '<div class="hist-meta">정보·등급 변경 이력이 없습니다.</div>';
        delivEl.innerHTML = (res.delivery_history || []).map(h => `
          <div class="hist-card">
            <div class="hist-card-top">
              <span><b>${esc(h.topic || '-')}</b> ${dispatchKindLabel(h.kind, h.is_forced)}</span>
              ${deliveryStatusBadge(h.recipient_status)}
            </div>
            ${h.error_detail ? `<div class="hist-diff-line" style="color:var(--danger);">${esc(h.error_detail)}</div>` : ''}
            <div class="hist-meta">${esc(utcToKst(h.finished_at || h.started_at || h.updated_at))} · <code>${esc(h.job_id)}</code></div>
          </div>`).join('') || '<div class="hist-meta">메시지 수신 이력이 없습니다.</div>';
      } catch (e) {
        auditEl.innerHTML = `<div style="color:var(--danger);">이력 로드 실패: ${esc(e.message)}</div>`;
        delivEl.innerHTML = '';
      }
    }

    async function saveThresholds() {
      try {
        await req('/api/settings', { method: 'POST', body: { thresholds: S.thresholds } });
        toast('✅ 임계치가 저장되었습니다.');
      } catch (e) {
        alert(e.message);
      }
    }

    async function syncGithub() {
      try {
        const res = await req('/api/recipients/sync-github', { method: 'POST' });
        alert(res.message);
        loadDashboard();
      } catch (e) {
        alert(e.message);
      }
    }

    // =========================================================================
    // FLOATING SIMULATOR
    // =========================================================================
    async function openSimModal(defaultBotId = null) {
      const [uRes, bRes] = await Promise.all([req('/api/platform/users'), req('/api/platform/bots')]);
      S.users = uRes.users || [];
      S.bots = bRes.bots || [];

      const uSel = document.getElementById('simUserSel');
      uSel.innerHTML = S.users.map(u =>
        `<option value="${esc(u.telegram_user_id)}">${esc(u.full_name)} [${u.tier} · ${u.used_slots}/${u.is_unlimited ? '∞' : u.max_slots}]</option>`
      ).join('') || '<option value="99001">테스트유저</option>';

      const bSel = document.getElementById('simBotSel');
      bSel.innerHTML = S.bots.map(b =>
        `<option value="${esc(b.bot_id)}">${esc(b.icon_emoji)} ${esc(b.name)}</option>`
      ).join('');
      if (defaultBotId) bSel.value = defaultBotId;

      document.getElementById('simModal').classList.add('open');
      if (defaultBotId) runSim('/start');
    }

    function clearSim() {
      document.getElementById('simChatBox').innerHTML = '';
    }

    async function runSim(cmd, callbackData = null, targetBotId = null) {
      const uid = document.getElementById('simUserSel').value || '99001';
      const bid = targetBotId || document.getElementById('simBotSel').value;
      if (targetBotId) document.getElementById('simBotSel').value = targetBotId;

      const box = document.getElementById('simChatBox');
      if (box.querySelector('div[style*="text-align:center"]')) box.innerHTML = '';

      if (cmd) {
        const ub = document.createElement('div');
        ub.className = 'sim-user-bubble';
        ub.textContent = `[${bid}] ${cmd}`;
        box.appendChild(ub);
      }

      try {
        const res = await req('/api/platform/simulate', {
          method: 'POST',
          body: { bot_id: bid, telegram_user_id: uid, text: cmd, callback_data: callbackData }
        });
        if (res.callback_toast) toast(res.callback_toast);

        const bb = document.createElement('div');
        bb.className = 'sim-bubble';
        bb.innerHTML = res.reply_html;
        box.appendChild(bb);

        if (res.inline_keyboard && res.inline_keyboard.length > 0) {
          res.inline_keyboard.forEach(row => {
            row.forEach(btn => {
              const b = document.createElement('button');
              b.className = 'sim-kb-btn';
              b.textContent = btn.text;
              b.onclick = () => {
                if (btn.callback_data) runSim(null, btn.callback_data, bid);
                else if (btn.url) window.open(btn.url, '_blank');
              };
              box.appendChild(b);
            });
          });
        }
        box.scrollTop = box.scrollHeight;
        if (S.activeTab === 'users') loadUsers();
      } catch (e) {
        toast('시뮬레이션 에러: ' + e.message);
      }
    }

    async function syncAllBots() {
      try {
        const res = await req('/api/platform/bots/sync-all', { method: 'POST' });
        toast(`🔄 대화 동기화 완료 (${res.total_processed}건 처리)`);
        refreshCurrentTab();
      } catch (e) {
        toast(e.message);
      }
    }

    // Splash Loader Control
    let splashTimer = null;
    let splashHidden = false;

    function initSplashLoader() {
      // If server takes more than 2.5s (Cold Start on Render), show helpful message and progress bar
      splashTimer = setTimeout(() => {
        if (!splashHidden) {
          const wrap = document.getElementById('splashProgressWrap');
          const txt = document.getElementById('splashStatusText');
          if (wrap) wrap.style.display = 'block';
          if (txt) txt.textContent = '⚡ 데이터 응답 대기 중...';
        }
      }, 2500);

      // Maximum 8s safety fallback to prevent UI block if an endpoint fails
      setTimeout(hideSplashLoader, 8000);
    }

    function hideSplashLoader() {
      if (splashHidden) return;
      splashHidden = true;
      if (splashTimer) clearTimeout(splashTimer);
      const el = document.getElementById('appSplashLoader');
      if (el) {
        el.classList.add('fade-out');
        setTimeout(() => { el.style.display = 'none'; }, 450);
      }
    }

    // =========================================================================
    // PENDING UPGRADE BADGES
    // =========================================================================
    function updatePendingBadges(count) {
      ['userPendingBadge', 'bnavPendingBadge'].forEach(id => {
        const badge = document.getElementById(id);
        if (!badge) return;
        if (count > 0) {
          badge.textContent = count > 99 ? '99+' : String(count);
          badge.style.display = 'inline-flex';
        } else {
          badge.textContent = '';
          badge.style.display = 'none';
        }
      });
    }

    // =========================================================================
    // TELEGRAM DEEP-LINK (?tab=users&req_id=123)
    // =========================================================================
    const DEEPLINK_TABS = ['users', 'bots', 'dashboard', 'history'];

    function highlightRequestCard(reqId) {
      const safeId = String(reqId).replace(/[^0-9A-Za-z_-]/g, '');
      if (!safeId) return;
      let card = document.querySelector('[data-req-card-id="' + safeId + '"]');
      if (!card) {
        const btn = document.querySelector('[data-req-id="' + safeId + '"]');
        card = btn ? (btn.closest('[data-req-card-id]') || btn.closest('tr') || btn) : null;
      }
      if (!card) {
        toast('ℹ️ 해당 등업 신청은 이미 처리되었거나 찾을 수 없습니다.');
        return;
      }
      card.scrollIntoView({ behavior: 'smooth', block: 'center' });
      card.classList.remove('req-card-highlight');
      void card.offsetWidth;
      card.classList.add('req-card-highlight');
      setTimeout(() => card.classList.remove('req-card-highlight'), 3000);

      // Auto-expand and highlight corresponding user accordion item
      const reqObj = (S.upgradeRequests || []).find(r => String(r.id) === String(safeId));
      if (reqObj && reqObj.telegram_user_id) {
        toggleUserAccordion(reqObj.telegram_user_id, true);
        const userItem = document.querySelector(`[data-user-card-id="${reqObj.telegram_user_id}"]`);
        if (userItem) {
          userItem.classList.remove('req-card-highlight');
          void userItem.offsetWidth;
          userItem.classList.add('req-card-highlight');
          setTimeout(() => userItem.classList.remove('req-card-highlight'), 3000);
        }
      }
    }

    async function applyDeepLink() {
      const params = new URLSearchParams(window.location.search);
      let tab = (params.get('tab') || '').toLowerCase();
      const reqId = params.get('req_id');
      if (tab === 'upgrade' || (!tab && reqId)) tab = 'users';
      if (!DEEPLINK_TABS.includes(tab)) tab = 'users';

      // Strip one-shot deep-link params so a manual refresh doesn't re-trigger the highlight
      if (params.has('tab') || params.has('req_id')) {
        params.delete('tab');
        params.delete('req_id');
        const qs = params.toString();
        try {
          window.history.replaceState(null, '', window.location.pathname + (qs ? '?' + qs : '') + window.location.hash);
        } catch (e) { /* ignore */ }
      }

      await switchTab(tab);
      if (tab === 'users' && reqId) highlightRequestCard(reqId);
    }

    // =========================================================================
    // 7-BOT TRAFFIC LIGHTS & SCHEDULER COUNTDOWN
    // =========================================================================
    const HEALTH_BOTS = [
      { ids: ['menu_bot'], label: '🧭 메뉴' },
      { ids: ['brief_bot'], label: '📈 시황' },
      { ids: ['travel_bot'], label: '✈️ 여행' },
      { ids: ['admission_bot', 'edu_bot'], label: '🎓 입시' },
      { ids: ['silver_bot'], label: '👵 실버' },
      { ids: ['kids_bot'], label: '👶 자녀' },
      { ids: ['yolo_bot'], label: '🏄 욜로' },
    ];
    const HEALTH_POLL_MS = 20000;
    const WEEKDAYS_KO = ['일', '월', '화', '수', '목', '금', '토'];
    const H = { nextTargetMs: null, nextLabel: '', clockOffsetMs: 0, schedulerState: 'loading', refreshing: false, lastFetchMs: 0 };

    function parseKstToMs(s) {
      if (!s) return null;
      const clean = String(s).replace(' KST', '').trim().replace(' ', 'T');
      const ms = Date.parse(clean + '+09:00');
      return isNaN(ms) ? null : ms;
    }

    function buildScheduleLabel(s) {
      const clean = String(s).replace(' KST', '').trim();
      const [datePart, timePart] = clean.split(' ');
      const [y, m, d] = (datePart || '').split('-').map(Number);
      const hhmm = (timePart || '').slice(0, 5);
      const wd = (y && m && d) ? WEEKDAYS_KO[new Date(Date.UTC(y, m - 1, d)).getUTCDay()] : '';
      const kind = hhmm.startsWith('06') ? '장전 시황 브리핑' : (hhmm.startsWith('15') ? '마감 시황 브리핑' : '시황 브리핑');
      return `${m}/${d}(${wd}) ${hhmm} ${kind}`;
    }

    function renderTrafficLights(data) {
      const wrap = document.getElementById('botTrafficLights');
      if (!wrap) return;
      const active = new Set((data && data.active_workers) || []);
      wrap.innerHTML = HEALTH_BOTS.map(b => {
        const up = b.ids.some(id => active.has(id));
        return `<span class="bot-light-chip${up ? '' : ' down'}" title="${up ? '실시간 폴링 가동 중' : '중단 또는 토큰 없음'}">${up ? '🟢' : '🔴'} ${esc(b.label)}</span>`;
      }).join('');
    }

    function tickSchedulerTimer() {
      const el = document.getElementById('schedulerTimer');
      if (!el) return;
      if (H.schedulerState === 'loading') { el.textContent = '⏰ 다음 발송: 확인 중...'; return; }
      if (H.schedulerState === 'error') { el.textContent = '⏰ 다음 발송: 상태 조회 실패'; return; }
      if (H.schedulerState === 'off' || !H.nextTargetMs) { el.textContent = '⏰ 다음 발송: 스케줄러 비활성'; return; }

      const remainSec = Math.floor((H.nextTargetMs - (Date.now() + H.clockOffsetMs)) / 1000);
      if (remainSec <= 0) {
        el.textContent = `⏰ ${H.nextLabel} 발송 진행 중...`;
        if (remainSec < -5 && Date.now() - H.lastFetchMs > 10000) refreshBotHealth();
        return;
      }
      const days = Math.floor(remainSec / 86400);
      const rest = remainSec % 86400;
      const pad = n => String(n).padStart(2, '0');
      const hms = `${pad(Math.floor(rest / 3600))}:${pad(Math.floor((rest % 3600) / 60))}:${pad(rest % 60)}`;
      el.textContent = `⏰ 다음 발송: ${H.nextLabel} (D-${days} ${hms} 남음)`;
    }

    async function refreshBotHealth() {
      if (H.refreshing) return;
      H.refreshing = true;
      H.lastFetchMs = Date.now();
      try {
        // /healthz returns 503 with a valid JSON body when DEGRADED, so read it regardless of status
        const res = await fetch('/healthz', { cache: 'no-store' });
        const data = await res.json();
        renderTrafficLights(data);

        const serverNow = parseKstToMs(data.timestamp_kst);
        if (serverNow) H.clockOffsetMs = serverNow - Date.now();

        const sched = data.briefing_scheduler || {};
        const nextStr = sched.next_scheduled_kst;
        H.nextTargetMs = parseKstToMs(nextStr);
        H.nextLabel = nextStr ? buildScheduleLabel(nextStr) : '';
        H.schedulerState = (sched.is_running && H.nextTargetMs) ? 'on' : 'off';
      } catch (e) {
        renderTrafficLights(null);
        H.schedulerState = 'error';
      } finally {
        H.refreshing = false;
        tickSchedulerTimer();
      }
    }

    function initBotHealthStrip() {
      refreshBotHealth();
      setInterval(() => { if (!document.hidden) refreshBotHealth(); }, HEALTH_POLL_MS);
      setInterval(tickSchedulerTimer, 1000);
      document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshBotHealth(); });
    }

    // Initial load
    initSplashLoader();
    initBotHealthStrip();
    applyDeepLink();
  </script>
</body>
</html>
"""
