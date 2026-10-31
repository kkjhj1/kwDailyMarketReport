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
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
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
    body {
      margin: 0;
      font-family: 'Pretendard', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Noto Sans KR", sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
    }

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

    /* Filter Chips */
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
  </style>
</head>
<body>

  <!-- Top Minimal Header -->
  <header class="header">
    <div class="header-brand">
      <div class="header-logo">🤖</div>
      <div>
        <div class="header-title">텔레그램 봇 구독 서비스 통합 관리자</div>
        <div class="header-sub">Daily Market Brief &amp; Multi-Bot Tier Platform v2.0</div>
      </div>
    </div>
    <div class="header-actions">
      <span class="pill"><span class="dot"></span> <span id="topStatusText">실시간 정상 작동</span></span>
      <button class="btn btn-sm btn-dark" onclick="syncAllBots()">🔄 대화 즉시 수신</button>
      <button class="btn btn-sm btn-primary" onclick="refreshCurrentTab()">새로고침</button>
    </div>
  </header>

  <!-- 3 Core Tabs -->
  <nav class="tab-bar">
    <button class="tab-btn active" data-tab="users" onclick="switchTab('users')">
      👥 1. 유저 &amp; 등급 관리
    </button>
    <button class="tab-btn" data-tab="bots" onclick="switchTab('bots')">
      🤖 2. 봇 목록 &amp; 메시지 발송
    </button>
    <button class="tab-btn" data-tab="dashboard" onclick="switchTab('dashboard')">
      📊 3. 운영 현황
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
            <p class="card-desc">각 유저의 등급(화이트 1개 / 그린 3개 / 레드 5개 / 블랙 무제한)을 드롭다운으로 즉시 변경하고, 사용 중인 봇 슬롯을 관리합니다.</p>
          </div>
          <button class="btn btn-primary" onclick="openNewUserModal()">➕ 신규 유저 등록</button>
        </div>

        <!-- Search & Filter Bar -->
        <div style="display:flex; justify-content:space-between; align-items:center; gap:12px; flex-wrap:wrap; margin-bottom:16px;">
          <div class="filter-chips">
            <button class="filter-btn active" data-tier="" onclick="filterTier('')">전체 보기</button>
            <button class="filter-btn" data-tier="WHITE" onclick="filterTier('WHITE')">⚪ 화이트 (1개)</button>
            <button class="filter-btn" data-tier="GREEN" onclick="filterTier('GREEN')">🟢 그린 (3개)</button>
            <button class="filter-btn" data-tier="RED" onclick="filterTier('RED')">🔴 레드 (5개)</button>
            <button class="filter-btn" data-tier="BLACK" onclick="filterTier('BLACK')">⚫ 블랙 (무제한)</button>
          </div>
          <div style="width:260px;">
            <input type="text" id="userSearch" class="form-input" placeholder="이름, ID, 메모 검색..." oninput="debouncedSearchUsers()" />
          </div>
        </div>

        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>유저 이름 / 텔레그램 ID</th>
                <th>회원 등급 (원클릭 즉시 변경)</th>
                <th>구독 슬롯 현황</th>
                <th>현재 구독 중인 정보봇 (✕ 클릭 시 탈퇴)</th>
                <th>등급 유효기한</th>
                <th>관리</th>
              </tr>
            </thead>
            <tbody id="userTableBody">
              <tr><td colspan="6" style="text-align:center; padding:32px;">불러오는 중...</td></tr>
            </tbody>
          </table>
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
            <p class="card-desc">각 봇의 [🚀 정규 브리핑 발송 센터]를 누르면 정해진 실시간 콘텐츠(시황 데이터, 특가 항공권, 입시 큐레이션)를 즉시 생성하여 미리보고, 관리자 테스트 발송 및 전체 구독자 발송을 실행할 수 있습니다.</p>
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
      <div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(240px, 1fr)); gap:16px; margin-bottom:20px;">
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
          <div style="font-size:12px; font-weight:700; color:var(--text-muted);">운영 중인 봇 채널</div>
          <div style="font-size:32px; font-weight:800; margin:6px 0;" id="kpiBots">0개</div>
          <div style="font-size:12px; color:var(--text-muted);">메뉴봇 1 + 정보봇 4개</div>
        </div>
      </div>

      <!-- Tier Distribution Bar & Bot Popularity -->
      <div style="display:grid; grid-template-columns:1fr 1fr; gap:20px; margin-bottom:20px;">
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

      <div style="display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-bottom:10px;">
        <div>
          <label class="form-label">테스트 유저</label>
          <select id="simUserSel" class="form-select"></select>
        </div>
        <div>
          <label class="form-label">대화 상대 봇</label>
          <select id="simBotSel" class="form-select"></select>
        </div>
      </div>

      <div style="display:flex; gap:6px; margin-bottom:10px; flex-wrap:wrap;">
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
        <div style="display:grid; grid-template-columns:1fr 1fr; gap:10px;">
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
      <div style="display:grid; grid-template-columns:1fr 1fr; gap:10px;">
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
      <div style="display:grid; grid-template-columns:80px 1fr 120px; gap:10px;">
        <div class="form-group">
          <label class="form-label">아이콘</label>
          <input type="text" id="bmEmoji" class="form-input" value="🤖" />
        </div>
        <div class="form-group">
          <label class="form-label">봇 이름</label>
          <input type="text" id="bmName" class="form-input" />
        </div>
        <div class="form-group">
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
      <div style="display:grid; grid-template-columns:1fr 1fr; gap:10px;">
        <div class="form-group">
          <label class="form-label">텔레그램 User ID</label>
          <input type="text" id="umId" class="form-input" />
        </div>
        <div class="form-group">
          <label class="form-label">이름 / 닉네임</label>
          <input type="text" id="umName" class="form-input" />
        </div>
      </div>
      <div style="display:grid; grid-template-columns:1fr 1fr; gap:10px;">
        <div class="form-group">
          <label class="form-label">회원 등급</label>
          <select id="umTier" class="form-select">
            <option value="WHITE">⚪ 화이트 (1개)</option>
            <option value="GREEN">🟢 그린 (최대 3개)</option>
            <option value="RED">🔴 레드 (최대 5개)</option>
            <option value="BLACK">⚫ 블랙 (무제한)</option>
          </select>
        </div>
        <div class="form-group">
          <label class="form-label">등급 만료일 (비워두면 무기한)</label>
          <input type="date" id="umExp" class="form-input" />
        </div>
      </div>
      <div class="form-group">
        <label class="form-label">관리자 메모</label>
        <input type="text" id="umMemo" class="form-input" placeholder="메모 입력" />
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

  <div id="toastWrap"></div>

  <script>
    // State
    let S = {
      users: [],
      bots: [],
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
      document.querySelectorAll('.tab-pane').forEach(p => p.classList.toggle('active', p.id === `pane-${tab}`));
      if (tab === 'users') loadUsers();
      if (tab === 'bots') loadBots();
      if (tab === 'dashboard') loadDashboard();
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

    async function loadUsers() {
      const q = document.getElementById('userSearch').value.trim();
      const p = new URLSearchParams();
      if (q) p.set('q', q);
      if (S.tierFilter) p.set('tier', S.tierFilter);

      try {
        const [uRes, bRes] = await Promise.all([
          req('/api/platform/users?' + p.toString()),
          req('/api/platform/bots')
        ]);
        S.users = uRes.users || [];
        S.bots = bRes.bots || [];

        const tbody = document.getElementById('userTableBody');
        if (S.users.length === 0) {
          tbody.innerHTML = '<tr><td colspan="6" style="text-align:center; padding:28px; color:var(--text-muted);">해당 조건의 유저가 없습니다.</td></tr>';
          return;
        }

        tbody.innerHTML = S.users.map(u => {
          const pct = u.is_unlimited ? 30 : Math.min(100, Math.round((u.used_slots / Math.max(1, u.max_slots)) * 100));
          const barColor = u.is_over_quota ? '#ef4444' : (pct >= 100 ? '#f59e0b' : '#10b981');
          const slotTxt = u.is_unlimited ? `${u.used_slots} / 무제한` : `${u.used_slots} / ${u.max_slots}개`;

          const botChips = (u.active_subscriptions || []).map(s => `
            <span class="bot-chip">
              ${esc(s.icon_emoji)} ${esc(s.bot_name)}
              <span class="bot-chip-del" title="탈퇴 처리" onclick="unsubUser('${esc(u.telegram_user_id)}', '${esc(s.bot_id)}', '${esc(s.bot_name)}')">✕</span>
            </span>
          `).join('');

          const suspChips = (u.suspended_subscriptions || []).map(s => `
            <span class="bot-chip" style="background:#fee2e2; color:#991b1b; border-color:#fca5a5;">
              ⏸️ ${esc(s.icon_emoji)} ${esc(s.bot_name)}
              <span class="bot-chip-del" onclick="unsubUser('${esc(u.telegram_user_id)}', '${esc(s.bot_id)}', '${esc(s.bot_name)}')">✕</span>
            </span>
          `).join('');

          return `
            <tr>
              <td>
                <div style="font-weight:800;">${esc(u.full_name)} ${u.status === 'BLOCKED' ? '<span style="color:var(--danger); font-size:11px;">[차단]</span>' : ''}</div>
                <div style="font-size:12px; color:var(--text-muted);">ID: <code>${esc(u.telegram_user_id)}</code> ${u.username ? `· @${esc(u.username)}` : ''}</div>
              </td>
              <td>
                <select class="form-select" style="width:165px; font-weight:700; padding:6px 9px;" onchange="changeTier('${esc(u.telegram_user_id)}', this.value)">
                  <option value="WHITE" ${u.tier === 'WHITE' ? 'selected' : ''}>⚪ 화이트 (1개)</option>
                  <option value="GREEN" ${u.tier === 'GREEN' ? 'selected' : ''}>🟢 그린 (3개)</option>
                  <option value="RED" ${u.tier === 'RED' ? 'selected' : ''}>🔴 레드 (5개)</option>
                  <option value="BLACK" ${u.tier === 'BLACK' ? 'selected' : ''}>⚫ 블랙 (무제한)</option>
                </select>
              </td>
              <td>
                <div style="font-weight:800; font-size:12.5px;">${slotTxt}</div>
                <div class="slot-bar"><div class="slot-bar-fill" style="width:${pct}%; background:${barColor};"></div></div>
              </td>
              <td>
                <div style="display:flex; flex-wrap:wrap; align-items:center; gap:2px;">
                  ${botChips || '<span style="color:var(--text-muted); font-size:12px;">구독 없음</span>'}
                  ${suspChips}
                  <button class="btn btn-sm" style="padding:2px 7px; margin-left:3px;" onclick="openAddSubModal('${esc(u.telegram_user_id)}')">+ 추가</button>
                </div>
              </td>
              <td style="font-size:12px;">
                ${u.tier_expires_at ? `<code>${esc(u.tier_expires_at.slice(0,10))}</code>` : '<span style="color:var(--text-muted);">무기한</span>'}
              </td>
              <td>
                <div style="display:flex; gap:4px;">
                  <button class="btn btn-sm" onclick="openEditUserModal('${esc(u.telegram_user_id)}')">수정</button>
                  <button class="btn btn-sm btn-danger" onclick="deleteUser('${esc(u.telegram_user_id)}')">삭제</button>
                </div>
              </td>
            </tr>
          `;
        }).join('');
      } catch (e) {
        toast('유저 목록 로드 오류: ' + e.message);
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
      document.getElementById('umTier').value = u.tier || 'WHITE';
      document.getElementById('umExp').value = u.tier_expires_at ? u.tier_expires_at.slice(0,10) : '';
      document.getElementById('umMemo').value = u.memo || '';
      document.getElementById('userModal').classList.add('open');
    }

    async function saveUser() {
      const uid = document.getElementById('umId').value.trim();
      const name = document.getElementById('umName').value.trim();
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
            body: { telegram_user_id: uid, full_name: name, tier, tier_expires_at: exp, memo }
          });
        } else {
          await req(`/api/platform/users/${encodeURIComponent(uid)}`, {
            method: 'PUT',
            body: { full_name: name, memo }
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
          mBox.innerHTML = `
            <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:14px; padding:6px 0;">
              <div style="display:flex; align-items:center; gap:14px;">
                <span style="font-size:36px;">${esc(menuBot.icon_emoji)}</span>
                <div>
                  <div style="display:flex; align-items:center; gap:8px;">
                    <strong style="font-size:16px;">${esc(menuBot.name)}</strong>
                    <span style="font-size:12px; color:var(--primary); font-weight:700;">${uname}</span>
                    <span style="font-size:12px;">${tokenBadge}</span>
                    <span class="badge" style="background:#dbeafe; color:#1e40af;">가입·안내 포털 (쿼터 미차감)</span>
                  </div>
                  <div style="font-size:13px; color:var(--text-muted); margin-top:4px;">${esc(menuBot.description)}</div>
                </div>
              </div>
              <div style="display:flex; gap:8px;">
                <button class="btn btn-sm btn-dark" onclick="openSimModal('${esc(menuBot.bot_id)}')">👁️ 유저 시점 미리보기</button>
                <button class="btn btn-sm btn-primary" onclick="openEditBotModal('${esc(menuBot.bot_id)}')">⚙️ 설정·토큰 수정</button>
              </div>
            </div>
          `;
        }

        // Render Info Bots
        document.getElementById('infoBotGrid').innerHTML = infoBots.map(b => {
          const tokenBadge = b.has_token
            ? `<span style="color:var(--success); font-weight:700;">🟢 연결됨</span>`
            : `<span style="color:var(--warning); font-weight:700;">🟠 토큰 필요</span>`;

          return `
            <div class="bot-card">
              <div>
                <div style="display:flex; justify-content:space-between; align-items:flex-start; margin-bottom:8px;">
                  <div style="display:flex; align-items:center; gap:10px;">
                    <span style="font-size:28px;">${esc(b.icon_emoji)}</span>
                    <div>
                      <div style="font-size:15px; font-weight:800;">${esc(b.name)}</div>
                      <div style="font-size:12px; color:var(--text-muted);">${esc(b.category)} · ${tokenBadge}</div>
                    </div>
                  </div>
                  <span style="font-size:12px; font-weight:800; background:#dbeafe; color:#1e40af; padding:3px 8px; border-radius:6px;">
                    👥 ${b.active_subscribers}명 구독중
                  </span>
                </div>
                <div style="font-size:12.5px; color:#475569; margin:8px 0;">${esc(b.description || '설명 없음')}</div>
              </div>
              <div style="display:flex; justify-content:space-between; align-items:center; gap:6px; border-top:1px solid var(--border); padding-top:12px;">
                <button class="btn btn-sm btn-primary" onclick="openBriefingModal('${esc(b.bot_id)}')">
                  🚀 정규 브리핑 발송 센터
                </button>
                <div style="display:flex; gap:4px;">
                  <button class="btn btn-sm" onclick="openEditBotModal('${esc(b.bot_id)}')">설정</button>
                  <button class="btn btn-sm" onclick="openSimModal('${esc(b.bot_id)}')">시뮬</button>
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
        document.getElementById('kpiBots').textContent = `${ov.total_bots}개`;

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
          <div style="padding:7px 0; border-bottom:1px solid var(--border); font-size:12.5px; display:flex; justify-content:space-between;">
            <div><b>${esc(l.action)}</b> · ${esc(l.user_name)} ${l.bot_name ? `→ <span style="color:var(--primary);">${esc(l.bot_name)}</span>` : ''}</div>
            <span style="color:var(--text-muted); font-size:11.5px;">${esc((l.logged_at || '').slice(5, 16))}</span>
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

    // Initial load
    switchTab('users');
  </script>
</body>
</html>
"""
