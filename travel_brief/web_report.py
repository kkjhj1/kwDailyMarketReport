"""Web report generator for Travel Briefing.
Generates responsive, glassmorphism HTML reports for GitHub Pages and admin console preview.
Features high-readability price heatmap, 100% one-click Naver Flight & Hotel live verification links,
transparent data source attribution, and interactive region filters.
"""
from __future__ import annotations

import html
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from travel_brief.models import (
    DestinationMeta,
    FlightDeal,
    TravelRecommendation,
    TravelSnapshot,
    reconcile_man_breakdown,
    round_man,
)
from travel_brief.flight_engine import FlightEngine
from travel_brief.budget_calculator import BudgetCalculator
from travel_brief.ai_prompts import AIPromptGenerator


DEST_FLAGS: dict[str, str] = {
    "FUK": "🇯🇵",
    "NRT": "🇯🇵",
    "KIX": "🇯🇵",
    "DAD": "🇻🇳",
    "CXR": "🇻🇳",
    "BKK": "🇹🇭",
    "TPE": "🇹🇼",
    "GUM": "🇺🇸",
    "DPS": "🇮🇩",
    "SIN": "🇸🇬",
}

DEST_REGION_TAGS: dict[str, str] = {
    "FUK": "japan",
    "NRT": "japan",
    "KIX": "japan",
    "DAD": "vietnam",
    "CXR": "vietnam",
    "BKK": "asia_city",
    "TPE": "asia_city",
    "SIN": "asia_city",
    "GUM": "resort",
    "DPS": "resort",
}


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>✈️ 주말 해외여행 6개월 특가 & 예산 나침반 (네이버 실시간 관측형)</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Pretendard:wght@400;500;600;700;800;900&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg-gradient: linear-gradient(135deg, #070b14 0%, #0d1527 50%, #090e1a 100%);
      --card-bg: rgba(18, 26, 43, 0.78);
      --card-border: rgba(255, 255, 255, 0.09);
      --card-hover: rgba(30, 41, 65, 0.9);
      --text-main: #f8fafc;
      --text-sub: #94a3b8;
      --accent-cyan: #38bdf8;
      --accent-emerald: #34d399;
      --accent-amber: #fbbf24;
      --accent-rose: #f43f5e;
      --accent-violet: #a855f7;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: 'Pretendard', -apple-system, BlinkMacSystemFont, system-ui, Roboto, sans-serif;
      background: var(--bg-gradient);
      color: var(--text-main);
      min-height: 100vh;
      padding: 2.5rem 1rem;
      line-height: 1.6;
    }
    .container {
      max-width: 1260px;
      margin: 0 auto;
    }

    /* Header */
    header {
      text-align: center;
      margin-bottom: 2.5rem;
      padding: 2.5rem 2rem;
      background: rgba(15, 23, 42, 0.65);
      border-radius: 24px;
      border: 1px solid var(--card-border);
      backdrop-filter: blur(16px);
      box-shadow: 0 16px 40px rgba(0, 0, 0, 0.4);
      position: relative;
      overflow: hidden;
    }
    header::after {
      content: '';
      position: absolute;
      top: 0; left: 0; right: 0;
      height: 4px;
      background: linear-gradient(90deg, #38bdf8, #34d399, #fbbf24);
    }
    .badge {
      display: inline-block;
      padding: 0.3rem 0.85rem;
      border-radius: 9999px;
      font-size: 0.75rem;
      font-weight: 800;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      margin-bottom: 0.85rem;
    }
    .badge-deal { background: rgba(52, 211, 153, 0.15); color: var(--accent-emerald); border: 1px solid rgba(52, 211, 153, 0.35); }
    .badge-best { background: rgba(251, 191, 36, 0.15); color: var(--accent-amber); border: 1px solid rgba(251, 191, 36, 0.35); }
    .badge-caution { background: rgba(244, 63, 94, 0.15); color: var(--accent-rose); border: 1px solid rgba(244, 63, 94, 0.35); }
    .badge-normal { background: rgba(56, 189, 248, 0.15); color: var(--accent-cyan); border: 1px solid rgba(56, 189, 248, 0.35); }

    h1 {
      font-size: 2.3rem;
      font-weight: 900;
      margin-bottom: 0.6rem;
      letter-spacing: -0.03em;
      background: linear-gradient(135deg, #ffffff 0%, #38bdf8 60%, #34d399 100%);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
    }
    .subtitle {
      color: #cbd5e1;
      font-size: 1.05rem;
      font-weight: 500;
    }
    .meta-bar {
      margin-top: 1.25rem;
      display: flex;
      justify-content: center;
      flex-wrap: wrap;
      gap: 1.5rem;
      font-size: 0.88rem;
      color: #94a3b8;
    }
    .meta-item {
      display: flex;
      align-items: center;
      gap: 0.4rem;
    }

    h2 {
      font-size: 1.6rem;
      font-weight: 800;
      margin: 2.5rem 0 1.25rem;
      display: flex;
      align-items: center;
      gap: 0.6rem;
      letter-spacing: -0.02em;
      color: #ffffff;
    }
    .section-desc {
      color: var(--text-sub);
      font-size: 0.92rem;
      margin-top: -0.8rem;
      margin-bottom: 1.25rem;
    }


    /* TOP 3 Cards Grid */
    .top-cards-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
      gap: 1.75rem;
      margin-bottom: 3.5rem;
    }
    .top-card {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 20px;
      padding: 1.75rem;
      backdrop-filter: blur(14px);
      transition: all 0.28s cubic-bezier(0.4, 0, 0.2, 1);
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      position: relative;
      overflow: hidden;
      box-shadow: 0 12px 30px rgba(0, 0, 0, 0.35);
    }
    .top-card:hover {
      transform: translateY(-5px);
      background: var(--card-hover);
      border-color: rgba(56, 189, 248, 0.45);
      box-shadow: 0 20px 44px rgba(0, 0, 0, 0.55);
    }
    .top-card-ribbon {
      position: absolute;
      top: 14px;
      right: 14px;
      padding: 0.25rem 0.75rem;
      border-radius: 9999px;
      font-size: 0.72rem;
      font-weight: 800;
      letter-spacing: 0.05em;
      background: linear-gradient(135deg, #0284c7, #06b6d4);
      color: #fff;
      box-shadow: 0 4px 12px rgba(6, 182, 212, 0.35);
    }
    .top-card-header {
      margin-bottom: 0.85rem;
    }
    .dest-region-tag {
      font-size: 0.85rem;
      color: var(--text-sub);
      margin-bottom: 0.3rem;
    }
    .dest-main-name {
      font-size: 1.55rem;
      font-weight: 800;
      color: #ffffff;
      letter-spacing: -0.02em;
    }
    .price-highlight-block {
      margin-top: 0.6rem;
      display: flex;
      align-items: baseline;
      gap: 0.6rem;
    }
    .price-val {
      font-size: 1.7rem;
      font-weight: 900;
      color: var(--accent-emerald);
      letter-spacing: -0.03em;
    }
    .price-sub-tag {
      font-size: 0.85rem;
      color: var(--text-sub);
    }
    .badges-row {
      margin-bottom: 1rem;
      display: flex;
      flex-wrap: wrap;
      gap: 0.4rem;
    }
    .schedule-box {
      background: rgba(15, 23, 42, 0.6);
      border: 1px solid rgba(255, 255, 255, 0.06);
      border-radius: 12px;
      padding: 0.9rem 1rem;
      margin-bottom: 0.9rem;
      font-size: 0.86rem;
    }
    .schedule-row {
      display: flex;
      justify-content: space-between;
      margin-bottom: 0.4rem;
      gap: 0.5rem;
    }
    .schedule-row:last-child { margin-bottom: 0; }
    .sched-label { color: var(--text-sub); white-space: nowrap; font-weight: 600; }
    .sched-val { text-align: right; color: #f1f5f9; }

    .hotel-curation-box {
      background: rgba(30, 41, 59, 0.55);
      border-left: 3px solid #38bdf8;
      border-radius: 8px;
      padding: 0.85rem 1rem;
      margin-bottom: 0.9rem;
      font-size: 0.86rem;
    }
    .hotel-title-line {
      display: flex;
      align-items: center;
      gap: 0.4rem;
      margin-bottom: 0.25rem;
      color: #fff;
    }
    .hotel-grade-badge {
      background: rgba(56, 189, 248, 0.2);
      color: #38bdf8;
      font-size: 0.72rem;
      padding: 0.15rem 0.45rem;
      border-radius: 4px;
      font-weight: 700;
    }
    .hotel-feature-line {
      font-size: 0.8rem;
      color: #94a3b8;
    }

    .budget-summary-box {
      border-top: 1px solid rgba(255, 255, 255, 0.08);
      border-bottom: 1px solid rgba(255, 255, 255, 0.08);
      padding: 0.85rem 0;
      margin-bottom: 0.9rem;
    }
    .budget-item {
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      font-size: 0.88rem;
      margin-bottom: 0.25rem;
    }
    .b-label { color: var(--text-sub); }
    .highlight-1p {
      font-size: 1.2rem;
      font-weight: 900;
      color: var(--accent-cyan);
    }
    .highlight-2p {
      font-size: 0.95rem;
      font-weight: 700;
      color: #e2e8f0;
    }
    .b-desc {
      display: block;
      font-size: 0.74rem;
      color: #64748b;
      text-align: right;
      width: 100%;
      margin-bottom: 0.4rem;
    }
    .visa-tips-box {
      font-size: 0.8rem;
      color: #cbd5e1;
      background: rgba(56, 189, 248, 0.06);
      border-radius: 8px;
      padding: 0.7rem 0.85rem;
      margin-bottom: 1.25rem;
      line-height: 1.5;
    }
    .action-buttons-group {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 0.55rem;
    }
    .act-btn {
      text-align: center;
      padding: 0.65rem 0.7rem;
      border-radius: 10px;
      font-size: 0.82rem;
      font-weight: 700;
      text-decoration: none;
      cursor: pointer;
      transition: all 0.2s;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      border: 1px solid transparent;
      gap: 0.35rem;
    }
    .btn-flight {
      background: rgba(16, 185, 129, 0.15);
      color: #34d399;
      border-color: rgba(52, 211, 153, 0.35);
    }
    .btn-flight:hover {
      background: #10b981;
      color: #fff;
      box-shadow: 0 4px 14px rgba(16, 185, 129, 0.4);
    }
    .btn-hotel {
      background: rgba(56, 189, 248, 0.15);
      color: #38bdf8;
      border-color: rgba(56, 189, 248, 0.35);
    }
    .btn-hotel:hover {
      background: #0284c7;
      color: #fff;
      box-shadow: 0 4px 14px rgba(2, 132, 199, 0.4);
    }
    .btn-ai {
      background: #10a37f;
      color: #fff;
    }
    .btn-ai:hover {
      background: #0d8a6a;
      box-shadow: 0 4px 14px rgba(16, 163, 127, 0.4);
    }
    .btn-prompt {
      background: rgba(255, 255, 255, 0.08);
      color: #e2e8f0;
      border-color: rgba(255, 255, 255, 0.15);
    }
    .btn-prompt:hover {
      background: rgba(255, 255, 255, 0.18);
    }

    /* Quick Filter Bar */
    .filter-bar {
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 0.85rem;
      margin-bottom: 1.25rem;
      padding: 0.5rem 0;
    }
    .filter-label {
      font-size: 0.88rem;
      font-weight: 700;
      color: #94a3b8;
    }
    .filter-buttons {
      display: flex;
      flex-wrap: wrap;
      gap: 0.5rem;
    }
    .filter-btn {
      background: rgba(255, 255, 255, 0.05);
      border: 1px solid rgba(255, 255, 255, 0.12);
      color: #cbd5e1;
      padding: 0.45rem 0.95rem;
      border-radius: 9999px;
      font-size: 0.83rem;
      font-weight: 600;
      cursor: pointer;
      transition: all 0.2s ease;
    }
    .filter-btn:hover {
      background: rgba(255, 255, 255, 0.15);
      color: #fff;
    }
    .filter-btn.active {
      background: #0284c7;
      border-color: #38bdf8;
      color: #fff;
      box-shadow: 0 0 14px rgba(56, 189, 248, 0.35);
    }

    /* Heatmap Legend */
    .heatmap-legend {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 1rem;
      background: rgba(15, 23, 42, 0.6);
      border: 1px solid rgba(255, 255, 255, 0.06);
      border-radius: 14px;
      padding: 0.85rem 1.25rem;
      margin-bottom: 1.25rem;
      font-size: 0.82rem;
    }
    .legend-group {
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 0.6rem;
    }
    .legend-title {
      color: #94a3b8;
      font-weight: 700;
      margin-right: 0.25rem;
    }
    .legend-pill {
      padding: 0.25rem 0.6rem;
      border-radius: 6px;
      font-size: 0.78rem;
      font-weight: 700;
    }
    .pill-super { background: rgba(52, 211, 153, 0.16); color: #34d399; border: 1px solid rgba(52, 211, 153, 0.35); }
    .pill-good { background: rgba(56, 189, 248, 0.16); color: #38bdf8; border: 1px solid rgba(56, 189, 248, 0.35); }
    .pill-normal { background: rgba(255, 255, 255, 0.06); color: #cbd5e1; border: 1px solid rgba(255, 255, 255, 0.12); }
    .pill-peak { background: rgba(251, 191, 36, 0.16); color: #fbbf24; border: 1px solid rgba(251, 191, 36, 0.35); }
    .pill-weather-best { background: rgba(251, 191, 36, 0.15); color: #fbbf24; }
    .pill-weather-caution { background: rgba(244, 63, 94, 0.15); color: #fb7185; }

    /* Dual-View Mode Switcher & Month Tabs */
    .view-mode-bar {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 0.85rem;
      margin-bottom: 1.15rem;
    }
    .view-mode-group {
      display: inline-flex;
      background: rgba(15, 23, 42, 0.85);
      border: 1px solid rgba(56, 189, 248, 0.28);
      border-radius: 14px;
      padding: 0.3rem;
      gap: 0.35rem;
    }
    .view-mode-btn {
      background: transparent;
      border: none;
      color: #94a3b8;
      padding: 0.65rem 1.2rem;
      border-radius: 10px;
      font-size: 0.96rem;
      font-weight: 800;
      cursor: pointer;
      transition: all 0.2s ease;
    }
    .view-mode-btn:hover {
      color: #f8fafc;
      background: rgba(255, 255, 255, 0.06);
    }
    .view-mode-btn.active {
      background: linear-gradient(135deg, #0284c7, #0ea5e9);
      color: #ffffff;
      box-shadow: 0 4px 14px rgba(14, 165, 233, 0.35);
    }
    .month-tabs-bar {
      display: flex;
      flex-wrap: wrap;
      gap: 0.6rem;
      margin-bottom: 1.5rem;
    }
    .month-tab-btn {
      background: rgba(15, 23, 42, 0.78);
      border: 1px solid rgba(255, 255, 255, 0.14);
      color: #e2e8f0;
      padding: 0.7rem 1.2rem;
      border-radius: 12px;
      font-size: 1rem;
      font-weight: 800;
      cursor: pointer;
      transition: all 0.2s ease;
    }
    .month-tab-btn:hover {
      border-color: #38bdf8;
      background: rgba(56, 189, 248, 0.14);
    }
    .month-tab-btn.active {
      background: rgba(16, 185, 129, 0.2);
      border-color: #34d399;
      color: #6ee7b7;
      box-shadow: 0 0 16px rgba(16, 185, 129, 0.28);
    }

    /* Monthly Big-Card Grid (High-Readability Primary View) */
    .monthly-cards-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
      gap: 1.35rem;
      margin-bottom: 3.5rem;
    }
    .monthly-big-card {
      background: rgba(15, 23, 42, 0.88);
      border: 1px solid rgba(255, 255, 255, 0.11);
      border-radius: 18px;
      padding: 1.45rem;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      gap: 1rem;
      box-shadow: 0 10px 28px rgba(0, 0, 0, 0.38);
      transition: transform 0.22s ease, border-color 0.22s ease;
    }
    .monthly-big-card:hover {
      transform: translateY(-4px);
      border-color: rgba(56, 189, 248, 0.5);
    }
    .monthly-big-card.card-super {
      border-color: rgba(52, 211, 153, 0.42);
      background: linear-gradient(180deg, rgba(16, 185, 129, 0.10) 0%, rgba(15, 23, 42, 0.92) 100%);
    }
    .monthly-big-card.card-peak {
      border-color: rgba(251, 191, 36, 0.42);
      background: linear-gradient(180deg, rgba(245, 158, 11, 0.10) 0%, rgba(15, 23, 42, 0.92) 100%);
    }
    .mbc-top {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 0.6rem;
    }
    .mbc-city {
      font-size: 1.35rem;
      font-weight: 800;
      color: #ffffff;
      line-height: 1.3;
    }
    .mbc-sub {
      font-size: 0.92rem;
      color: #94a3b8;
      margin-top: 0.2rem;
    }
    .mbc-price-box {
      padding: 0.85rem 1rem;
      border-radius: 12px;
      background: rgba(255, 255, 255, 0.04);
      border: 1px solid rgba(255, 255, 255, 0.07);
    }
    .mbc-price-val {
      font-size: 1.6rem;
      font-weight: 900;
      letter-spacing: -0.02em;
      color: #38bdf8;
      display: block;
    }
    .card-super .mbc-price-val { color: #34d399; }
    .card-peak .mbc-price-val { color: #fbbf24; }
    .mbc-price-meta {
      font-size: 0.94rem;
      color: #cbd5e1;
      margin-top: 0.25rem;
      display: block;
    }
    .mbc-sched-list {
      display: flex;
      flex-direction: column;
      gap: 0.48rem;
      font-size: 0.98rem;
      line-height: 1.55;
      color: #e2e8f0;
    }
    .mbc-sched-list span.muted {
      color: #94a3b8;
      font-size: 0.92rem;
    }
    .mbc-cta-btn {
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 48px;
      border-radius: 12px;
      background: linear-gradient(135deg, #0284c7, #0369a1);
      color: #ffffff;
      font-size: 1rem;
      font-weight: 800;
      text-decoration: none;
      transition: all 0.2s ease;
      box-shadow: 0 4px 14px rgba(2, 132, 199, 0.32);
    }
    .mbc-cta-btn:hover {
      background: linear-gradient(135deg, #0ea5e9, #0284c7);
      transform: translateY(-1px);
    }

    /* Smart Compact Heatmap Matrix (High-Density Price Board) */
    .table-container {
      overflow-x: auto;
      position: relative;
      border-radius: 16px;
      border: 1px solid var(--card-border);
      background: rgba(15, 23, 42, 0.88);
      backdrop-filter: blur(14px);
      box-shadow: 0 10px 28px rgba(0, 0, 0, 0.35);
      margin-bottom: 3rem;
    }
    table {
      width: 100%;
      border-collapse: separate;
      border-spacing: 0;
      min-width: 760px;
    }
    th {
      background: #0f1930;
      color: #94a3b8;
      font-weight: 700;
      padding: 0.75rem 0.4rem;
      font-size: 0.85rem;
      letter-spacing: 0.02em;
      border-bottom: 2px solid rgba(255, 255, 255, 0.1);
      text-align: center;
    }
    th.dest-col-th, td.dest-col {
      position: sticky;
      left: 0;
      z-index: 10;
      background: #0a1122;
      border-right: 2px solid rgba(56, 189, 248, 0.2);
      box-shadow: 3px 0 10px rgba(0, 0, 0, 0.5);
      text-align: left;
    }
    th.dest-col-th {
      z-index: 15;
      background: #0d172e;
      padding-left: 1rem;
    }
    td.dest-col {
      padding: 0.45rem 0.85rem;
      min-width: 145px;
    }
    .dest-col-main {
      display: flex;
      flex-direction: column;
      gap: 0.15rem;
    }
    .dest-name-row {
      display: flex;
      align-items: center;
      gap: 0.35rem;
    }
    .dest-flag { font-size: 1.15rem; }
    .dest-name { font-size: 0.98rem; font-weight: 800; color: #ffffff; }
    .dest-code { font-size: 0.75rem; color: var(--accent-cyan); font-weight: 700; }
    .dest-sub-info { font-size: 0.72rem; color: var(--text-sub); }
    .dest-baseline-info { font-size: 0.68rem; color: #64748b; font-weight: 600; }

    /* Interactive Smart Compact Matrix Cell */
    .matrix-cell-wrap {
      padding: 0.22rem;
      vertical-align: middle;
      border-bottom: 1px solid rgba(255, 255, 255, 0.05);
    }
    .matrix-cell {
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      gap: 0.2rem;
      padding: 0.45rem 0.35rem;
      min-height: 58px;
      border-radius: 9px;
      text-decoration: none;
      color: var(--text-main);
      transition: all 0.18s cubic-bezier(0.4, 0, 0.2, 1);
      border: 1px solid transparent;
      position: relative;
      background: rgba(255, 255, 255, 0.03);
      cursor: pointer;
    }
    .matrix-cell:hover {
      transform: translateY(-3px) scale(1.02);
      box-shadow: 0 8px 22px rgba(0, 0, 0, 0.5);
      z-index: 5;
    }
    .cell-deal-super {
      background: rgba(16, 185, 129, 0.14);
      border-color: rgba(52, 211, 153, 0.4);
    }
    .cell-deal-super:hover {
      background: rgba(16, 185, 129, 0.24);
      border-color: #34d399;
    }
    .cell-deal-good {
      background: rgba(14, 165, 233, 0.12);
      border-color: rgba(56, 189, 248, 0.35);
    }
    .cell-deal-good:hover {
      background: rgba(14, 165, 233, 0.22);
      border-color: #38bdf8;
    }
    .cell-deal-peak {
      background: rgba(245, 158, 11, 0.1);
      border-color: rgba(251, 191, 36, 0.3);
    }
    .cell-deal-peak:hover {
      background: rgba(245, 158, 11, 0.2);
      border-color: #fbbf24;
    }
    .cell-deal-normal {
      background: rgba(255, 255, 255, 0.03);
      border-color: rgba(255, 255, 255, 0.07);
    }
    .cell-deal-normal:hover {
      background: rgba(255, 255, 255, 0.08);
      border-color: rgba(255, 255, 255, 0.2);
    }

    .cell-header {
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      margin-bottom: 0.35rem;
      gap: 0.25rem;
    }
    .cell-price {
      font-size: 1.26rem;
      font-weight: 900;
      letter-spacing: -0.02em;
    }
    .cell-deal-super .cell-price { color: #34d399; }
    .cell-deal-good .cell-price { color: #38bdf8; }
    .cell-deal-peak .cell-price { color: #fbbf24; }
    .cell-deal-normal .cell-price { color: #f1f5f9; }

    .cell-badge {
      font-size: 0.8rem;
      font-weight: 800;
      padding: 0.2rem 0.5rem;
      border-radius: 9999px;
      white-space: nowrap;
    }
    .badge-super { background: rgba(52, 211, 153, 0.2); color: #34d399; border: 1px solid rgba(52, 211, 153, 0.4); }
    .badge-good { background: rgba(56, 189, 248, 0.2); color: #38bdf8; border: 1px solid rgba(56, 189, 248, 0.4); }
    .badge-peak { background: rgba(251, 191, 36, 0.2); color: #fbbf24; border: 1px solid rgba(251, 191, 36, 0.4); }
    .badge-normal { background: rgba(255, 255, 255, 0.08); color: #cbd5e1; }

    .cell-dates {
      font-size: 0.94rem;
      color: #f1f5f9;
      margin-bottom: 0.38rem;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 0.35rem;
    }
    .nights-tag {
      font-size: 0.78rem;
      color: #cbd5e1;
      background: rgba(255, 255, 255, 0.09);
      padding: 0.14rem 0.42rem;
      border-radius: 5px;
      font-weight: 700;
    }
    .cell-footer {
      display: flex;
      justify-content: space-between;
      align-items: center;
      font-size: 0.86rem;
      color: #cbd5e1;
      margin-bottom: 0.55rem;
    }
    .cell-weather {
      font-size: 0.78rem;
      font-weight: 700;
      padding: 0.18rem 0.45rem;
      border-radius: 5px;
    }
    .weather-best { background: rgba(251, 191, 36, 0.15); color: #fbbf24; }
    .weather-caution { background: rgba(244, 63, 94, 0.15); color: #fb7185; }

    .cell-action-hint {
      font-size: 0.86rem;
      font-weight: 800;
      text-align: center;
      padding: 0.38rem 0.5rem;
      border-radius: 6px;
      background: rgba(255, 255, 255, 0.05);
      color: #38bdf8;
      transition: all 0.2s;
    }
    .matrix-cell:hover .cell-action-hint {
      background: #0284c7;
      color: #ffffff;
      box-shadow: 0 2px 8px rgba(2, 132, 199, 0.4);
    }

    /* 10 Destinations Guide Grid */
    .all-dest-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(290px, 1fr));
      gap: 1.25rem;
      margin-bottom: 3.5rem;
    }
    .dest-card-compact {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 16px;
      padding: 1.35rem;
      transition: all 0.25s ease;
    }
    .dest-card-compact:hover {
      border-color: rgba(56, 189, 248, 0.4);
      transform: translateY(-3px);
      box-shadow: 0 10px 24px rgba(0, 0, 0, 0.4);
    }
    .dest-card-compact h3 {
      font-size: 1.18rem;
      margin-bottom: 0.75rem;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    .dest-detail-line {
      font-size: 0.84rem;
      color: var(--text-sub);
      margin-bottom: 0.4rem;
      line-height: 1.5;
    }
    .dest-detail-line strong {
      color: #e2e8f0;
    }

    footer {
      text-align: center;
      margin-top: 3.5rem;
      padding-top: 2rem;
      border-top: 1px solid rgba(255, 255, 255, 0.08);
      color: #64748b;
      font-size: 0.88rem;
      line-height: 1.6;
    }

    /* Modal */
    .modal {
      display: none;
      position: fixed;
      top: 0; left: 0; right: 0; bottom: 0;
      background: rgba(0,0,0,0.75);
      backdrop-filter: blur(6px);
      z-index: 1000;
      align-items: center;
      justify-content: center;
      padding: 1rem;
    }
    .modal.active { display: flex; }
    .modal-content {
      background: #1e293b;
      border: 1px solid rgba(255,255,255,0.15);
      border-radius: 18px;
      max-width: 650px;
      width: 100%;
      padding: 1.85rem;
      box-shadow: 0 24px 48px rgba(0,0,0,0.6);
    }
    .modal-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 1rem;
    }
    .modal-header h3 { font-size: 1.3rem; color: #fff; font-weight: 800; }
    .modal-close {
      background: none;
      border: none;
      color: var(--text-sub);
      font-size: 1.6rem;
      cursor: pointer;
    }
    .modal-body textarea {
      width: 100%;
      height: 220px;
      background: #0f172a;
      border: 1px solid rgba(255,255,255,0.1);
      border-radius: 10px;
      color: #e2e8f0;
      padding: 0.85rem;
      font-family: monospace;
      font-size: 0.85rem;
      resize: vertical;
      margin-bottom: 1rem;
    }
    .modal-footer {
      display: flex;
      justify-content: flex-end;
      gap: 0.75rem;
    }
    .mobile-scroll-hint {
      display: none;
      text-align: center;
      padding: 0.55rem;
      background: rgba(56, 189, 248, 0.1);
      border: 1px dashed rgba(56, 189, 248, 0.3);
      border-radius: 8px;
      color: #38bdf8;
      font-size: 0.82rem;
      font-weight: 700;
      margin-bottom: 0.85rem;
    }

    /* Mobile Responsive Optimizations */
    @media (max-width: 768px) {
      html, body {
        overflow-x: hidden;
      }
      body {
        padding: 0.85rem 0.6rem;
        font-size: 0.95rem;
      }
      .container {
        width: 100%;
        max-width: 100%;
      }
      header {
        padding: 1.35rem 0.9rem;
        margin-bottom: 1.25rem;
        border-radius: 16px;
      }
      h1 {
        font-size: clamp(1.45rem, 5.5vw, 1.85rem);
        line-height: 1.25;
      }
      .subtitle {
        font-size: 0.88rem;
      }
      .meta-bar {
        display: grid;
        gap: 0.45rem;
        text-align: left;
        font-size: 0.82rem;
      }
      h2 {
        font-size: 1.25rem;
        margin: 1.8rem 0 0.8rem;
      }
      .section-desc {
        font-size: 0.85rem;
        margin-bottom: 1rem;
      }
      .top-card,
      .monthly-big-card,
      .dest-card-compact {
        min-width: 0;
        padding: 1.15rem;
        border-radius: 14px;
      }
      .top-cards-grid,
      .monthly-cards-grid,
      .all-dest-grid {
        grid-template-columns: minmax(0, 1fr);
        gap: 0.9rem;
      }
      .top-card-ribbon {
        top: 10px;
        right: 10px;
        font-size: 0.68rem;
        padding: 0.2rem 0.55rem;
      }
      .dest-main-name {
        font-size: 1.35rem;
      }
      .price-val {
        font-size: 1.5rem;
      }
      .filter-bar {
        display: block;
      }
      .filter-buttons {
        width: 100%;
        flex-wrap: nowrap;
        overflow-x: auto;
        padding-bottom: 0.4rem;
        -webkit-overflow-scrolling: touch;
      }
      .filter-btn {
        flex: 0 0 auto;
        white-space: nowrap;
        min-height: 42px;
        font-size: 0.82rem;
        padding: 0.4rem 0.8rem;
      }
      .view-mode-bar,
      .view-mode-group {
        width: 100%;
      }
      .view-mode-group {
        display: grid;
        grid-template-columns: 1fr 1fr;
      }
      .view-mode-btn {
        min-width: 0;
        padding: 0.55rem 0.3rem;
        font-size: 0.82rem;
        min-height: 44px;
        line-height: 1.25;
      }
      .month-tabs-bar {
        flex-wrap: nowrap;
        overflow-x: auto;
        padding-bottom: 0.4rem;
        -webkit-overflow-scrolling: touch;
      }
      .month-tab-btn {
        flex: 0 0 auto;
        padding: 0.6rem 0.9rem;
        white-space: nowrap;
        min-height: 42px;
        font-size: 0.88rem;
      }
      .mbc-top {
        flex-direction: column;
        align-items: flex-start;
        gap: 0.4rem;
      }
      .mbc-city {
        font-size: 1.25rem;
      }
      .mbc-price-val {
        font-size: 1.5rem;
      }
      .mbc-cta-btn {
        min-height: 46px;
        font-size: 0.95rem;
      }
      .action-buttons-group {
        grid-template-columns: 1fr 1fr;
        gap: 0.45rem;
      }
      .act-btn {
        min-height: 44px;
        font-size: 0.8rem;
        padding: 0.5rem 0.35rem;
      }
      .table-container {
        max-width: 100%;
        overflow-x: auto;
        -webkit-overflow-scrolling: touch;
      }
      table {
        min-width: 660px;
      }
      th {
        padding: 0.75rem 0.45rem;
        font-size: 0.82rem;
      }
      td.dest-col {
        min-width: 140px;
        padding: 0.65rem 0.85rem;
      }
      .dest-name {
        font-size: 0.95rem;
      }
      .matrix-cell {
        padding: 0.65rem 0.5rem;
      }
      .cell-price {
        font-size: 1.05rem;
      }
      .mobile-scroll-hint {
        display: block;
      }
      .modal-content {
        max-height: calc(100dvh - 2rem);
        overflow-y: auto;
        padding: 1.1rem;
      }
      .modal-footer {
        display: grid;
        grid-template-columns: 1fr;
      }
    }
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div class="badge badge-normal">Weekend Travel Intelligence Engine</div>
      <h1>✈️ 주말 해외여행 6개월 특가 & 예산 나침반</h1>
      <p class="subtitle">인천(ICN) 출발 | 목~일 & 금~월 (3박 4일) 직장인 실속 주말 일정 · 네이버 실시간 관측형</p>
      <div class="meta-bar">
        <div class="meta-item"><span>📅 기준 일시:</span> <strong><!-- GENERATED_AT --></strong></div>
        <div class="meta-item"><span>🎯 분석 대상:</span> <strong>인기 10대 근거리 해외 여행지</strong></div>
        <div class="meta-item"><span>🔍 실시간 연동:</span> <strong>네이버 항공권 & 네이버 호텔</strong></div>
        <div class="meta-item"><span>⚠️ 유의사항:</span> <strong>실시간 잔여석에 따라 결제 단계에서 변동 가능</strong></div>
      </div>
    </header>

    <h2>🔥 이번 주 베스트 TOP 3 추천 (초특가 & 여행 최적기)</h2>
    <p class="section-desc">6개월간의 주말 일정 중 항공권 할인율, 현지 날씨 최적기, 가성비 호텔 큐레이션을 종합 평가한 베스트 추천입니다.</p>
    <div class="top-cards-grid">
      <!-- TOP_CARDS_HTML -->
    </div>

    <h2>📊 6개월 주말 왕복 항공권 특가 & 실거래 예상 밴드</h2>
    <p class="section-desc">6개월 가격 흐름은 <strong>컴팩트 히트맵</strong>으로 한눈에 비교하고, 상세 운항·숙소·예산은 <strong>월별 카드</strong>로 확인하세요. 셀 클릭 시 실시간 항공권으로 바로 연결됩니다.</p>

    <!-- View Mode Switcher -->
    <div class="view-mode-bar">
      <div class="view-mode-group">
        <button id="btnViewCards" class="view-mode-btn active" onclick="setViewMode('cards')">🗂️ 월별 큰글씨 카드 보기 (추천)</button>
        <button id="btnViewTable" class="view-mode-btn" onclick="setViewMode('table')">📊 6개월 한눈에 표 보기 (확대형)</button>
      </div>
    </div>

    <!-- Region Filter Bar -->
    <div class="filter-bar">
      <span class="filter-label">🏷️ 지역별 빠른 필터:</span>
      <div class="filter-buttons">
        <button class="filter-btn active" data-filter="all" onclick="setRegionFilter('all')">전체 (10개 도시)</button>
        <button class="filter-btn" data-filter="japan" onclick="setRegionFilter('japan')">🇯🇵 일본 (3)</button>
        <button class="filter-btn" data-filter="vietnam" onclick="setRegionFilter('vietnam')">🇻🇳 베트남 (2)</button>
        <button class="filter-btn" data-filter="asia_city" onclick="setRegionFilter('asia_city')">🏙️ 대만/태국/싱가포르 (3)</button>
        <button class="filter-btn" data-filter="resort" onclick="setRegionFilter('resort')">🏝️ 괌/발리 휴양 (2)</button>
      </div>
    </div>

    <!-- Heatmap Legend -->
    <div class="heatmap-legend">
      <div class="legend-group">
        <span class="legend-title">💰 운임 수준:</span>
        <span class="legend-pill pill-super">🔥 초특가 (기준가 대비 -13% 이하)</span>
        <span class="legend-pill pill-good">✨ 실속할인 (-7% ~ -13%)</span>
        <span class="legend-pill pill-normal">⚪ 평수기 표준</span>
        <span class="legend-pill pill-peak">🚨 연휴·성수기 할증</span>
      </div>
      <div class="legend-group">
        <span class="legend-title">🌤️ 현지 날씨:</span>
        <span class="legend-pill pill-weather-best">☀️ 여행 최적기</span>
        <span class="legend-pill pill-weather-caution">🌧️ 우기/기상주의</span>
      </div>
    </div>

    <!-- Primary High-Readability Monthly Big-Card View -->
    <div id="monthlyCardsSection">
      <div class="month-tabs-bar">
        <!-- MONTH_TABS_HTML -->
      </div>
      <div class="monthly-cards-grid" id="monthlyCardsGrid">
        <!-- MONTHLY_BIG_CARDS_HTML -->
      </div>
    </div>

    <!-- Secondary Enlarged Sticky Heatmap Table -->
    <div id="matrixTableSection" style="display: none;">
      <div class="mobile-scroll-hint">👈 좌우로 스크롤하여 6개월 전체 일정을 확인하세요 👉</div>
      <div class="table-container">
        <table>
          <thead>
            <tr>
              <th class="dest-col-th">도시명 / 비행시간</th>
              <!-- MONTHS_HEADERS_HTML -->
            </tr>
          </thead>
          <tbody>
            <!-- MATRIX_ROWS_HTML -->
          </tbody>
        </table>
      </div>
    </div>

    <h2>🌍 10대 추천 여행지별 비자 규정 및 핵심 여행 팁</h2>
    <p class="section-desc">무비자 체류 가능 일수, 최적의 여행 시즌, 엄선된 추천 숙소 및 현지 실속 여행 팁 요약입니다.</p>
    <div class="all-dest-grid">
      <!-- DEST_CARDS_HTML -->
    </div>

    <footer>
      <p>본 리포트는 자동화된 주말 항공권 트렌드 분석 엔진(kwTravelBrief)에 의해 생성되었습니다.</p>
      <p>데이터 출처: 네이버 항공권 실시간 API · 국토교통부 항공정보포털(Airportal) 3개년 벤치마크 · SafeTelegramDispatcher Enabled</p>
      <p>© 2026 kwDailyMarketReport & kwTravelBrief · Zero Phantom Fares</p>
    </footer>
  </div>

  <!-- Prompt Modal -->
  <div class="modal" id="promptModal">
    <div class="modal-content">
      <div class="modal-header">
        <h3 id="modalTitle">🤖 AI 여행 계획 프롬프트</h3>
        <button class="modal-close" onclick="closeModal()">&times;</button>
      </div>
      <div class="modal-body">
        <p style="font-size: 0.88rem; color: var(--text-sub); margin-bottom: 0.6rem;">
          복사하여 ChatGPT나 Gemini에 붙여넣으면 3박 4일 맞춤 일정과 로컬 맛집을 바로 짜줍니다:
        </p>
        <textarea id="promptText" readonly></textarea>
      </div>
      <div class="modal-footer">
        <button class="btn btn-secondary" onclick="closeModal()" style="background: rgba(255,255,255,0.1); color: #fff; border: none; padding: 0.6rem 1rem; border-radius: 8px; cursor: pointer;">닫기</button>
        <button class="btn btn-primary" onclick="copyPrompt()" style="background: #0284c7; color: #fff; border: none; padding: 0.6rem 1rem; border-radius: 8px; cursor: pointer;">📋 프롬프트 복사</button>
        <a class="btn btn-primary" id="modalChatGptBtn" target="_blank" rel="noopener noreferrer" style="background: #10a37f; color: #fff; text-decoration: none; padding: 0.6rem 1rem; border-radius: 8px; display: inline-flex; align-items: center;">ChatGPT 바로 열기</a>
      </div>
    </div>
  </div>

  <script>
    const promptsData = /* PROMPTS_JSON */ {};

    function openPromptModal(code) {
      const data = promptsData[code];
      if (!data) return;
      document.getElementById('modalTitle').innerText = '🤖 ' + data.dest_name + ' AI 여행 계획 프롬프트';
      document.getElementById('promptText').value = data.prompt;
      document.getElementById('modalChatGptBtn').href = data.chatgpt_link;
      document.getElementById('promptModal').classList.add('active');
    }

    function closeModal() {
      document.getElementById('promptModal').classList.remove('active');
    }

    function copyPrompt() {
      const textarea = document.getElementById('promptText');
      textarea.select();
      navigator.clipboard.writeText(textarea.value).then(() => {
        alert('프롬프트가 클립보드에 복사되었습니다! ChatGPT나 Gemini에 붙여넣으세요.');
      });
    }

    let currentRegion = 'all';
    let currentMonth = 'best';

    function applyCombinedFilters() {
      document.querySelectorAll('.monthly-big-card').forEach(card => {
        const r = card.getAttribute('data-dest-region');
        const m = card.getAttribute('data-month');
        const matchRegion = (currentRegion === 'all' || r === currentRegion);
        const matchMonth = (m === currentMonth);
        card.style.display = (matchRegion && matchMonth) ? '' : 'none';
      });
      document.querySelectorAll('tr[data-dest-region]').forEach(row => {
        const r = row.getAttribute('data-dest-region');
        row.style.display = (currentRegion === 'all' || r === currentRegion) ? '' : 'none';
      });
      document.querySelectorAll('.dest-card-compact').forEach(card => {
        const r = card.getAttribute('data-dest-region');
        card.style.display = (currentRegion === 'all' || r === currentRegion) ? '' : 'none';
      });
    }

    function setViewMode(mode) {
      const cardsSec = document.getElementById('monthlyCardsSection');
      const tableSec = document.getElementById('matrixTableSection');
      document.getElementById('btnViewCards').classList.toggle('active', mode === 'cards');
      document.getElementById('btnViewTable').classList.toggle('active', mode === 'table');
      if (mode === 'table') {
        cardsSec.style.display = 'none';
        tableSec.style.display = 'block';
      } else {
        cardsSec.style.display = 'block';
        tableSec.style.display = 'none';
      }
    }

    function setMonthTab(monthKey) {
      currentMonth = monthKey;
      document.querySelectorAll('.month-tab-btn').forEach(btn => {
        btn.classList.toggle('active', btn.getAttribute('data-month-tab') === monthKey);
      });
      applyCombinedFilters();
    }

    function setRegionFilter(region) {
      currentRegion = region;
      document.querySelectorAll('.filter-btn').forEach(btn => {
        btn.classList.toggle('active', btn.getAttribute('data-filter') === region);
      });
      applyCombinedFilters();
    }

    window.onclick = function(event) {
      const modal = document.getElementById('promptModal');
      if (event.target === modal) {
        closeModal();
      }
    };
  </script>
</body>
</html>
"""


def build_travel_snapshot(
    engine: FlightEngine,
    dest_code: Optional[str] = None,
    max_budget: Optional[int] = None,
    top_n: int = 3,
) -> TravelSnapshot:
    """Builds a single immutable TravelSnapshot shared by Telegram and Web reports."""
    now_dt = datetime.now()
    generated_at = now_dt.strftime("%Y-%m-%d %H:%M")
    version_tag = now_dt.strftime("%Y%m%d%H%M")
    snapshot_id = f"snap_{now_dt.strftime('%Y%m%d_%H%M%S')}"

    all_destinations = engine.destinations
    all_deals = engine.search_deals(dest_code=dest_code, months_ahead=6)
    top_recs = BudgetCalculator.rank_recommendations(
        destinations=all_destinations,
        deals=all_deals,
        max_budget_per_person=max_budget,
        top_n=top_n,
    )
    matrix = engine.get_6month_matrix()
    all_months = sorted(list({m for d in matrix.values() for m in d.keys()}))

    if engine.live_naver and matrix:
        hero_deals = [r.best_deal for r in top_recs]
        hero_deals.extend(min(dm.values(), key=lambda x: x.price_krw) for dm in matrix.values() if dm)
        for m_key in all_months[:6]:
            m_cands = [
                (BudgetCalculator.evaluate_deal(all_destinations[dc], dm[m_key]).recommendation_score, -dm[m_key].price_krw, dm[m_key])
                for dc, dm in matrix.items() if m_key in dm and dc in all_destinations
            ]
            if m_cands:
                m_cands.sort(reverse=True)
                hero_deals.append(m_cands[0][2])
        if any(d.data_source == "LIVE_PG" for d in hero_deals):
            engine.ensure_deals_sse_verified(hero_deals)
            all_deals = engine.search_deals(dest_code=dest_code, months_ahead=6)
            top_recs = BudgetCalculator.rank_recommendations(
                destinations=all_destinations,
                deals=all_deals,
                max_budget_per_person=max_budget,
                top_n=top_n,
            )
            matrix = engine.get_6month_matrix()

    monthly_picks = {}
    for m in all_months[:6]:
        cands = [
            (BudgetCalculator.evaluate_deal(all_destinations[dc], dm[m]).recommendation_score, -dm[m].price_krw, dc, dm[m])
            for dc, dm in matrix.items() if m in dm and dc in all_destinations
        ]
        if cands:
            cands.sort(reverse=True)
            _, _, best_dc, best_deal = cands[0]
            monthly_picks[m] = (all_destinations[best_dc], best_deal)

    return TravelSnapshot(
        snapshot_id=snapshot_id,
        generated_at=generated_at,
        version_tag=version_tag,
        recommendations=top_recs,
        monthly_matrix=matrix,
        monthly_picks=monthly_picks,
    )


class WebReportGenerator:
    def __init__(self, engine: Optional[FlightEngine] = None):
        self.engine = engine or FlightEngine()

    def generate_html_report(self, snapshot: Optional[TravelSnapshot] = None) -> str:
        all_destinations = self.engine.destinations
        if snapshot is None:
            snapshot = build_travel_snapshot(self.engine, top_n=3)

        top_recs = snapshot.recommendations
        matrix = snapshot.monthly_matrix
        now_str = snapshot.generated_at
        all_months = sorted(list({m for d in matrix.values() for m in d.keys()}))

        # Prompts data dictionary for interactive modal
        prompts_dict = {}

        # 1. TOP CARDS HTML
        top_cards_html = []
        for rank, rec in enumerate(top_recs, start=1):
            dest = rec.destination
            deal = rec.best_deal
            budget = rec.budget

            prompt = AIPromptGenerator.build_prompt(dest, deal, budget)
            chatgpt_link = AIPromptGenerator.get_chatgpt_deep_link(prompt)
            prompts_dict[dest.code] = {
                "dest_name": dest.name,
                "prompt": prompt,
                "chatgpt_link": chatgpt_link,
            }

            flag = DEST_FLAGS.get(dest.code, "✈️")
            flight_man, hotel_total_man, meal_man, total_1p_man = reconcile_man_breakdown(
                deal.price_min_krw, budget.hotel_total_krw, budget.meal_transit_total_krw
            )
            total_2p_man = round_man(budget.estimated_for_two_krw)
            share_1p_man = round_man(budget.estimated_for_two_krw / 2)
            hotel_night_man = round_man(dest.hotel_night_avg_krw)

            season_badge = {
                "BEST_SEASON": '<span class="badge badge-best">☀️ 여행 최적기</span>',
                "CAUTION": f'<span class="badge badge-caution">⚠️ 기상 주의 ({dest.caution_note})</span>',
                "NORMAL": '<span class="badge badge-normal">🌤️ 무난한 시즌</span>',
            }.get(rec.season_status, "")

            deal_badge = (
                f'<span class="badge badge-caution">🚨 {html.escape(deal.holiday_name or "연휴 성수기")} {deal.price_diff_pct:+.0f}%</span>'
                if deal.is_peak_holiday and deal.price_diff_pct >= 5.0
                else f'<span class="badge badge-deal">🔥 초특가 {deal.price_diff_pct:+.0f}%</span>'
                if deal.is_super_deal or deal.discount_pct >= 13.0
                else f'<span class="badge badge-normal">✨ 실속할인 {deal.price_diff_pct:+.0f}%</span>'
                if deal.discount_pct >= 7.0
                else ""
            )

            hotel_name = html.escape(dest.recommended_hotel_name or "시내 중심 3~4성급 호텔")
            hotel_grade = html.escape(dest.recommended_hotel_grade or "4성급")
            hotel_features = html.escape(dest.recommended_hotel_features or "가성비 우수")

            card = f"""
      <div class="top-card">
        <div class="top-card-ribbon">BEST #{rank}</div>
        <div>
          <div class="top-card-header">
            <div class="dest-region-tag">{flag} {html.escape(dest.country)} · 직항 {dest.flight_time_hours}시간</div>
            <div class="dest-main-name">{html.escape(dest.name)} ({dest.code})</div>
            <div class="price-highlight-block">
              <span class="price-val">{deal.exact_price_str if deal.is_live else deal.price_band_str}</span>
              <span class="price-sub-tag">{html.escape(deal.airline if "직항" in deal.airline else f"{deal.airline} 직항")} · {deal.live_payment_note if deal.is_live else f"예상 {deal.price_band_str}"} ({deal.price_band_str})</span>
            </div>
            {f'<div style="margin-top:0.45rem; font-size:0.82rem; color:#fbbf24; background:rgba(251,191,36,0.1); border:1px solid rgba(251,191,36,0.28); border-radius:8px; padding:0.4rem 0.65rem;">💡 {html.escape(deal.popular_banner_note)}</div>' if deal.popular_banner_note else ''}
          </div>
          <div class="badges-row">
            {deal_badge} {season_badge}
          </div>
          <div class="schedule-box">
            <div class="schedule-row">
              <span class="sched-label">🛫 출발</span>
              <span class="sched-val"><strong>{deal.departure_date} ({deal.departure_day_of_week})</strong> · {deal.departure_time_slot}</span>
            </div>
            <div class="schedule-row">
              <span class="sched-label">🛬 귀국</span>
              <span class="sched-val"><strong>{deal.return_date} ({deal.return_day_of_week})</strong> · {deal.return_time_slot} [3박 4일]</span>
            </div>
          </div>
          <div class="hotel-curation-box">
            <div class="hotel-title-line">
              <span class="hotel-icon">🏨</span>
              <strong>숙박 예산: {html.escape(dest.hotel_budget_segment)}</strong>
            </div>
            <div class="hotel-feature-line">
              └ 3박 기준예산 약 {hotel_total_man}만 원 (1박 약 {hotel_night_man}만)
            </div>
            <div class="hotel-ref-line" style="margin-top:0.35rem; font-size:0.83rem; color:#94a3b8;">
              📍 추천 참고 호텔: <strong style="color:#f8fafc;">{hotel_name}</strong> <span class="hotel-grade-badge">{hotel_grade}</span> · {hotel_features}
              <div style="font-size:0.76rem; color:#64748b; margin-top:0.15rem;">(※ 추천 호텔의 실제 객실 요금은 [🏨 호텔요금 ↗] 링크에서 실시간 확인)</div>
            </div>
          </div>
          <div class="budget-summary-box">
            <div class="budget-item">
              <span class="b-label">1인 총 예상 경비</span>
              <span class="highlight-1p">약 {total_1p_man}만 원</span>
            </div>
            <span class="b-desc">(항공 {flight_man}만 + 숙소예산 {hotel_total_man}만 + 식비 {meal_man}만)</span>
            <div class="budget-item" style="margin-top: 0.35rem;">
              <span class="b-label">2인 1실 쉐어 총합</span>
              <span class="highlight-2p">약 {total_2p_man}만 원</span>
            </div>
            <span class="b-desc">(호텔 1실 분담 시 1인당 약 {share_1p_man:g}만 원)</span>
          </div>
          <div class="visa-tips-box">
            <strong>📌 비자 & 로컬 팁:</strong> {html.escape(dest.visa_info)} · {html.escape(dest.tips)}
          </div>
        </div>
        <div style="font-size:0.78rem; color:#38bdf8; background:rgba(56,189,248,0.08); border:1px solid rgba(56,189,248,0.22); border-radius:8px; padding:0.4rem 0.65rem; margin-bottom:0.75rem;">
          💡 <strong>팁:</strong> 네이버 항공권 첫 화면 최상단의 스폰서 항공사 배너 아래에 있는 <strong>[1위 최저가]</strong>를 확인하세요.
        </div>
        <div class="action-buttons-group">
          <a class="act-btn btn-flight" href="{html.escape(rec.naver_flight_url, quote=True)}" target="_blank" rel="noopener noreferrer">
            ✈️ 네이버항공권 ↗
          </a>
          <a class="act-btn btn-hotel" href="{html.escape(rec.hotel_search_url, quote=True)}" target="_blank" rel="noopener noreferrer">
            🏨 호텔요금 ↗
          </a>
          <a class="act-btn btn-ai" href="{html.escape(chatgpt_link, quote=True)}" target="_blank" rel="noopener noreferrer">
            🤖 GPT일정짜기 ↗
          </a>
          <button class="act-btn btn-prompt" onclick="openPromptModal('{dest.code}')">
            📋 AI프롬프트실행
          </button>
        </div>
      </div>
"""
            top_cards_html.append(card)

        # 2. MONTHLY TABS, BIG CARDS & MATRIX TABLE HTML
        month_tabs_list = [
            '<button class="month-tab-btn active" data-month-tab="best" onclick="setMonthTab(\'best\')">🌟 6개월 통합 최저가</button>'
        ]
        for m in all_months:
            m_label = f"{m.split('-')[0]}년 {int(m.split('-')[1])}월"
            month_tabs_list.append(
                f'<button class="month-tab-btn" data-month-tab="{m}" onclick="setMonthTab(\'{m}\')">{m_label}</button>'
            )
        month_tabs_html = "\n        ".join(month_tabs_list)

        def _render_big_card(dest: DestinationMeta, deal: FlightDeal, month_bucket: str, hidden: bool) -> str:
            flag = DEST_FLAGS.get(dest.code, "✈️")
            region_tag = DEST_REGION_TAGS.get(dest.code, "asia_city")
            month_int = int(deal.departure_date.split("-")[1])
            bd = BudgetCalculator.calculate_breakdown(dest, deal, nights=deal.nights)
            _, _, _, total_1p_man = reconcile_man_breakdown(
                deal.price_min_krw, bd.hotel_total_krw, bd.meal_transit_total_krw
            )
            total_2p_man = round_man(bd.estimated_for_two_krw)
            share_1p_man = round_man(bd.estimated_for_two_krw / 2)

            if deal.is_peak_holiday and deal.price_diff_pct >= 5.0:
                card_cls = "card-peak"
                badge_html = f'<span class="cell-badge badge-peak">🚨 {html.escape(deal.holiday_name or "연휴 성수기")} {deal.price_diff_pct:+.0f}%</span>'
            elif deal.price_diff_pct >= 10.0:
                card_cls = "card-peak"
                badge_html = f'<span class="cell-badge badge-peak">📈 수요할증 {deal.price_diff_pct:+.0f}%</span>'
            elif deal.is_super_deal or deal.discount_pct >= 13.0:
                card_cls = "card-super"
                badge_html = f'<span class="cell-badge badge-super">🔥 초특가 {deal.price_diff_pct:+.0f}%</span>'
            elif deal.discount_pct >= 7.0:
                card_cls = ""
                badge_html = f'<span class="cell-badge badge-good">✨ 실속 {deal.price_diff_pct:+.0f}%</span>'
            else:
                card_cls = ""
                badge_html = '<span class="cell-badge badge-normal">평수기 표준</span>'

            if dest.is_best_month(month_int):
                w_text = "☀️ 여행 최적기"
            elif dest.is_caution_month(month_int):
                w_text = f"🌧️ {html.escape(dest.caution_note)}"
            else:
                w_text = "🌤️ 무난한 날씨"

            naver_url = FlightEngine.build_naver_flight_url(
                deal.origin_code, deal.destination_code, deal.departure_date, deal.return_date
            )
            style_attr = ' style="display: none;"' if hidden else ""
            return f"""
        <div class="monthly-big-card {card_cls}" data-month="{month_bucket}" data-dest-region="{region_tag}"{style_attr}>
          <div>
            <div class="mbc-top">
              <div>
                <div class="mbc-city">{flag} {html.escape(dest.name)} ({dest.code})</div>
                <div class="mbc-sub">{html.escape(dest.country)} · 직항 {dest.flight_time_hours}시간 · {w_text}</div>
              </div>
              {badge_html}
            </div>
            <div class="mbc-price-box" style="margin-top: 0.9rem;">
              <span class="mbc-price-val">{deal.exact_price_str if deal.is_live else deal.price_band_str}</span>
              <span class="mbc-price-meta">{html.escape(deal.airline if "직항" in deal.airline else f"{deal.airline} 직항")} · {deal.live_payment_note} · 기준가({dest.flight_baseline_krw // 10000}만) 대비 {deal.price_diff_pct:+.0f}%</span>
              {f'<div style="margin-top:0.35rem; font-size:0.8rem; color:#fbbf24;">💡 {html.escape(deal.popular_banner_note)}</div>' if deal.popular_banner_note else ''}
            </div>
            <div class="mbc-sched-list" style="margin-top: 0.9rem;">
              <div>📅 <strong>{deal.departure_date}({deal.departure_day_of_week}) ~ {deal.return_date}({deal.return_day_of_week})</strong> · 3박 4일</div>
              <div>⏰ <span class="muted">{deal.departure_time_slot}</span></div>
              <div>💼 1인 총예산 <strong>약 {total_1p_man}만 원</strong> <span class="muted">· 2인 총 {total_2p_man}만 (1인당 약 {share_1p_man}만)</span></div>
            </div>
          </div>
          <a href="{html.escape(naver_url, quote=True)}" target="_blank" rel="noopener noreferrer" class="mbc-cta-btn">
            ✈️ 네이버 항공권 실시간 좌석·가격 확인 ↗
          </a>
        </div>"""

        monthly_big_cards: list[str] = []
        # 2a. 'best' bucket: single cheapest weekend per destination across 6 months, sorted cheapest first
        best_per_dest = []
        for d_code, d_meta in all_destinations.items():
            d_deals = list(matrix.get(d_code, {}).values())
            if d_deals:
                cheapest_d = min(d_deals, key=lambda x: x.price_krw)
                best_per_dest.append((d_meta, cheapest_d))
        best_per_dest.sort(key=lambda pair: pair[1].price_krw)
        for d_meta, cheapest_d in best_per_dest:
            monthly_big_cards.append(_render_big_card(d_meta, cheapest_d, "best", hidden=False))

        # 2b. Per-month buckets, sorted cheapest first within each month
        for m in all_months:
            month_pairs = []
            for d_code, d_meta in all_destinations.items():
                m_deal = matrix.get(d_code, {}).get(m)
                if m_deal:
                    month_pairs.append((d_meta, m_deal))
            month_pairs.sort(key=lambda pair: pair[1].price_krw)
            for d_meta, m_deal in month_pairs:
                monthly_big_cards.append(_render_big_card(d_meta, m_deal, m, hidden=True))

        months_headers = "".join(f"<th>{m.replace('-', '년 ')}월</th>" for m in all_months)
        matrix_rows = []

        for dest_code, dest in all_destinations.items():
            cells = []
            flag = DEST_FLAGS.get(dest_code, "✈️")
            region_tag = DEST_REGION_TAGS.get(dest_code, "asia_city")

            for m in all_months:
                deal = matrix.get(dest_code, {}).get(m)
                if deal:
                    month_int = int(m.split("-")[1])
                    discount = deal.discount_pct
                    is_peak = (deal.is_peak_holiday and deal.price_diff_pct >= 5.0) or discount <= -10.0
                    is_super = (not is_peak) and (deal.is_super_deal or discount >= 13.0)
                    is_good = (not is_peak) and discount >= 7.0

                    if is_peak:
                        price_cls = "cell-deal-peak"
                        badge_cls = "badge-peak"
                        badge_label = f"🚨 {deal.price_diff_pct:+.0f}%"
                    elif is_super:
                        price_cls = "cell-deal-super"
                        badge_cls = "badge-super"
                        badge_label = f"🔥 {deal.price_diff_pct:+.0f}%"
                    elif is_good:
                        price_cls = "cell-deal-good"
                        badge_cls = "badge-good"
                        badge_label = f"✨ {deal.price_diff_pct:+.0f}%"
                    else:
                        price_cls = "cell-deal-normal"
                        badge_cls = "badge-normal"
                        badge_label = "평수기"

                    is_best = dest.is_best_month(month_int)
                    is_caution = dest.is_caution_month(month_int)
                    if is_best:
                        weather_chip = '<span class="cell-weather weather-best" title="여행 최적기 (최상의 날씨)">☀️ 최적기</span>'
                    elif is_caution:
                        weather_chip = f'<span class="cell-weather weather-caution" title="{dest.caution_note}">🌧️ 주의</span>'
                    else:
                        weather_chip = ""

                    dep_short = deal.departure_date[5:].replace("-", ".")
                    ret_short = deal.return_date[5:].replace("-", ".")
                    naver_url = FlightEngine.build_naver_flight_url(
                        deal.origin_code, deal.destination_code, deal.departure_date, deal.return_date
                    )

                    cell_html = f"""
            <td class="matrix-cell-wrap">
              <a href="{html.escape(naver_url, quote=True)}" target="_blank" rel="noopener noreferrer" class="matrix-cell {price_cls}" title="{dest.name} {deal.departure_date} 네이버 실시간 항공권 조회 (새 창)">
                <span class="cell-price">{deal.exact_price_str if deal.is_live else deal.price_band_compact_str}</span>
                <span class="cell-dates">{dep_short}({deal.departure_day_of_week}) {weather_chip}</span>
                <span class="cell-badge {badge_cls}">{badge_label}</span>
              </a>
            </td>
"""
                    cells.append(cell_html)
                else:
                    cells.append('<td class="matrix-cell-wrap"><div class="matrix-cell cell-deal-normal" style="text-align:center; color:#64748b;">-</div></td>')

            row = f"""
          <tr data-dest-region="{region_tag}">
            <td class="dest-col">
              <div class="dest-col-main">
                <div class="dest-name-row">
                  <span class="dest-flag">{flag}</span>
                  <span class="dest-name">{html.escape(dest.name)}</span>
                  <span class="dest-code">({dest.code})</span>
                </div>
                <div class="dest-sub-info">
                  <span>{html.escape(dest.country)}</span> · <span>직항 {dest.flight_time_hours}시간</span>
                </div>
                <div class="dest-baseline-info">
                  기준운임: {dest.flight_baseline_krw:,}원
                </div>
              </div>
            </td>
            {"".join(cells)}
          </tr>
"""
            matrix_rows.append(row)

        # 3. DESTINATIONS CARDS HTML
        dest_cards = []
        for dest in all_destinations.values():
            flag = DEST_FLAGS.get(dest.code, "✈️")
            region_tag = DEST_REGION_TAGS.get(dest.code, "asia_city")
            best_m_str = ", ".join(f"{m}월" for m in dest.best_months)
            caution_m_str = (
                ", ".join(f"{m}월" for m in dest.caution_months) if dest.caution_months else "없음"
            )
            hotel_night_man = round_man(dest.hotel_night_avg_krw)
            daily_man = dest.daily_meal_transit_krw // 10000
            rec_hotel = html.escape(dest.recommended_hotel_name or "시내 중심 3~4성급 호텔")

            card = f"""
        <div class="dest-card-compact" data-dest-region="{region_tag}">
          <h3>
            <span>{flag} {html.escape(dest.name)} ({dest.code})</span>
            <span style="font-size: 0.82rem; color: var(--accent-cyan);">{html.escape(dest.country)}</span>
          </h3>
          <div class="dest-detail-line"><strong>비자 규정:</strong> {html.escape(dest.visa_info)}</div>
          <div class="dest-detail-line"><strong>☀️ 최적 시즌:</strong> {best_m_str}</div>
          <div class="dest-detail-line"><strong>🌧️ 기상 주의:</strong> {caution_m_str} ({html.escape(dest.caution_note)})</div>
          <div class="dest-detail-line"><strong>🏨 숙소 예산:</strong> {html.escape(dest.hotel_budget_segment)} 1박 약 {hotel_night_man}만 원 · <strong>추천 호텔:</strong> {rec_hotel} (실제 요금 링크 확인)</div>
          <div class="dest-detail-line"><strong>🍽️ 일일 생활경비:</strong> 약 {daily_man}만 원 (식비+대중교통)</div>
          <div class="dest-detail-line" style="margin-top: 0.6rem; color: #cbd5e1; background: rgba(255,255,255,0.03); padding: 0.5rem; border-radius: 6px;">💡 {html.escape(dest.tips)}</div>
        </div>
"""
            dest_cards.append(card)

        # Fill template with clean replace
        rendered = (
            HTML_TEMPLATE.replace("<!-- GENERATED_AT -->", now_str)
            .replace("<!-- TOP_CARDS_HTML -->", "\n".join(top_cards_html))
            .replace("<!-- MONTH_TABS_HTML -->", month_tabs_html)
            .replace("<!-- MONTHLY_BIG_CARDS_HTML -->", "\n".join(monthly_big_cards))
            .replace("<!-- MONTHS_HEADERS_HTML -->", months_headers)
            .replace("<!-- MATRIX_ROWS_HTML -->", "\n".join(matrix_rows))
            .replace("<!-- DEST_CARDS_HTML -->", "\n".join(dest_cards))
            .replace("/* PROMPTS_JSON */ {}", json.dumps(prompts_dict, ensure_ascii=False))
        )
        return rendered

    def save_reports(
        self,
        output_dir: Optional[str | Path] = None,
        snapshot: Optional[TravelSnapshot] = None,
    ) -> tuple[Path, Path]:
        if output_dir is None:
            output_dir = Path(__file__).resolve().parent.parent / "reports"
        out_path = Path(output_dir)
        out_path.mkdir(parents=True, exist_ok=True)

        now_tag = datetime.now().strftime("%Y%m%d_%H%M")
        dated_file = out_path / f"travel_briefing_{now_tag}.html"
        latest_file = out_path / "latest.html"

        content = self.generate_html_report(snapshot=snapshot)
        dated_file.write_text(content, encoding="utf-8")
        latest_file.write_text(content, encoding="utf-8")

        # Also write travel_latest.html in root reports dir if output_dir is reports/travel
        if out_path.name == "travel":
            (out_path.parent / "travel_latest.html").write_text(content, encoding="utf-8")
        else:
            (out_path / "travel_latest.html").write_text(content, encoding="utf-8")

        return dated_file, latest_file
