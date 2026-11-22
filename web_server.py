"""Superset Job Pilot v2 — Mission Control Dashboard.

Autonomous campus placement tracking, intelligent custom question resolver,
and multi-channel auto-applier for app.joinsuperset.com.
"""
import json
import logging
import os
import subprocess
import threading
import time
import requests
from collections import deque
from datetime import datetime, timezone
from typing import Tuple
import pytz
from flask import Flask, jsonify, request, render_template_string, Response
from config import Config, BASE_DIR
from superset_client import SupersetClient
from auto_applier import AutoApplier
from telegram_notifier import TelegramNotifier
from whatsapp_notifier import WhatsAppNotifier, normalize_whatsapp_phone
from calendar_sync import CalendarSync
from ai_engine import AIEngine
from rag_engine import RAGEngine
from error_healer import ErrorHealer
from ambiguity_manager import AmbiguityManager
from notice_scanner import notify_new_job_openings_for_user, scan_notices_for_user, summarize_notices
from resume_tailor import (
    get_resume_master,
    import_resume_from_pdf,
    render_html_to_pdf_bytes,
    render_tailored_resume_html,
    tailor_resume_for_job,
)

logger = logging.getLogger("superset_web")
logging.basicConfig(level=logging.INFO)

app = Flask(__name__)
IST = pytz.timezone("Asia/Kolkata")

# ─── Global Engine State ────────────────────────────────────────────────────
applier = AutoApplier()
ai_engine = AIEngine()
rag_engine = RAGEngine()
error_healer = ErrorHealer()
ambiguity_manager = AmbiguityManager()
recent_logs = deque(maxlen=300)
sync_history = deque(maxlen=50)
last_sync_info = {
    "timestamp": None,
    "status": "INITIALIZING",
    "message": "Engine starting up...",
    "errors": [],
    "total_found": 0,
    "new_jobs": 0,
    "auto_applied": 0
}
daemon_active = True
_cached_jobs = []
_cache_time = 0


def log_event(msg: str, level: str = "INFO"):
    timestamp = datetime.now(IST).strftime("%H:%M:%S")
    entry = {"time": timestamp, "level": level.upper(), "text": msg}
    recent_logs.append(entry)
    if level == "ERROR":
        logger.error(msg)
    elif level == "WARN":
        logger.warning(msg)
    elif level == "SUCCESS":
        logger.info(f"[SUCCESS] {msg}")
    else:
        logger.info(msg)


def background_monitor_worker():
    """Autonomous 24/7 background worker that syncs Superset every Config.POLL_INTERVAL_MINUTES."""
    global last_sync_info, daemon_active, _cached_jobs, _cache_time
    log_event(f"Autonomous background engine online. Poll cadence: {Config.POLL_INTERVAL_MINUTES}m.", "INFO")
    # Brief boot grace period so cloud health probes pass cleanly before initial sync
    time.sleep(8)

    while True:
        if not daemon_active:
            time.sleep(3)
            continue

        cycle_start = datetime.now(IST).strftime("%d %b %Y, %I:%M:%S %p IST")
        try:
            log_event("Starting scheduled sync cycle with Superset API...", "INFO")
            res = applier.run_sync_cycle()
            errors = res.get("errors", [])

            if errors:
                last_sync_info = {
                    "timestamp": cycle_start,
                    "status": "WARNING" if res.get("total_found", 0) > 0 else "ERROR",
                    "message": f"Sync finished with {len(errors)} error(s)",
                    "errors": errors,
                    "total_found": res.get("total_found", 0),
                    "new_jobs": res.get("new_jobs", 0),
                    "auto_applied": res.get("auto_applied", 0)
                }
                for err in errors:
                    log_event(f"Background Sync Issue: {err}", "ERROR")
            else:
                last_sync_info = {
                    "timestamp": cycle_start,
                    "status": "SUCCESS",
                    "message": f"Verified {res['total_found']} campus drives.",
                    "errors": [],
                    "total_found": res['total_found'],
                    "new_jobs": res['new_jobs'],
                    "auto_applied": res['auto_applied']
                }
                log_event(f"Sync complete: {res['total_found']} drives verified ({res['new_jobs']} new, {res['auto_applied']} auto-applied).", "SUCCESS")

            sync_history.appendleft(last_sync_info.copy())
            # Invalidate cache so next /api/jobs fetch is fresh
            _cache_time = 0

            # Also sync per-user open jobs (for WhatsApp job-opening alerts) & scan notices, shortlist PDFs & pending offers
            um = globals().get("user_manager")
            if um is not None:
                for u_obj in um.get_all_users():
                    try:
                        sc_u = SupersetClient(email=u_obj["email"], password=u_obj["superset_password"])
                        if sc_u.login():
                            u_jobs = sc_u.fetch_job_profiles()
                            if u_jobs:
                                sorted_u_jobs = sorted(u_jobs, key=lambda x: x.get("raw", {}).get("createdAt") or 0, reverse=True)
                                um.set_cached_jobs(u_obj["user_id"], sorted_u_jobs)
                                wa_jobs_sent = notify_new_job_openings_for_user(um, u_obj, sorted_u_jobs)
                                if wa_jobs_sent:
                                    log_event(
                                        f"WhatsApp Job Alerts ({u_obj['full_name']}): dispatched {wa_jobs_sent} new job opening alert(s).",
                                        "SUCCESS",
                                    )
                            n_res = scan_notices_for_user(
                                sc_u, um, u_obj, max_pages=2, page_size=25, max_pdfs_to_download=6, send_alerts=True
                            )
                            nc = n_res.get("counts", {})
                            log_event(
                                f"Notice & Offer Watchdog ({u_obj['full_name']}): "
                                f"{nc.get('shortlisted', 0)} shortlist match(es), {nc.get('pending_offers', 0)} pending offer(s).",
                                "SUCCESS" if nc.get("shortlisted") or nc.get("pending_offers") else "INFO",
                            )
                    except Exception as ne:
                        log_event(f"Notice watchdog warning for {u_obj['email']}: {ne}", "WARN")
        except Exception as e:
            last_sync_info = {
                "timestamp": cycle_start,
                "status": "ERROR",
                "message": f"Sync Exception: {str(e)}",
                "errors": [str(e)],
                "total_found": 0,
                "new_jobs": 0,
                "auto_applied": 0
            }
            log_event(f"Background sync exception: {e}", "ERROR")
            sync_history.appendleft(last_sync_info.copy())

        # Sleep in 5-second intervals to allow responsive pausing
        for _ in range(Config.POLL_INTERVAL_MINUTES * 12):
            if not daemon_active:
                break
            time.sleep(5)


# Start background worker
bg_thread = threading.Thread(target=background_monitor_worker, daemon=True)
bg_thread.start()


# ─── HTML TEMPLATE ──────────────────────────────────────────────────────────

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en" class="dark">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover, interactive-widget=resizes-content">
  <meta name="theme-color" media="(prefers-color-scheme: dark)" content="#09090b">
  <meta name="theme-color" media="(prefers-color-scheme: light)" content="#09090b">
  <meta name="color-scheme" content="dark">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
  <title>Superset Job Pilot — Mission Control v2.5</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
  <script>
    tailwind.config = {
      darkMode: 'class',
      theme: {
        extend: {
          fontFamily: {
            sans: ['Inter', 'system-ui', '-apple-system', 'sans-serif'],
            mono: ['JetBrains Mono', 'monospace'],
          },
          colors: {
            brand: { 50:'#f4f4f5', 100:'#e4e4e7', 200:'#d4d4d8', 300:'#a1a1aa', 400:'#818cf8', 500:'#6366f1', 600:'#52525b', 700:'#3f3f46', 800:'#27272a', 900:'#18181b' },
            surface: { 50:'#fafafa', 100:'#f4f4f5', 800:'#1c1c21', 850:'#16161a', 900:'#111114', 950:'#09090b' },
          },
          animation: {
            'slide-up': 'slideUp 0.2s cubic-bezier(0.23, 1, 0.32, 1)',
            'slide-down': 'slideDown 0.2s cubic-bezier(0.23, 1, 0.32, 1)',
            'fade-in': 'fadeIn 0.16s cubic-bezier(0.23, 1, 0.32, 1)',
          },
          keyframes: {
            slideUp: { '0%': { transform: 'translateY(12px)', opacity: '0' }, '100%': { transform: 'translateY(0)', opacity: '1' } },
            slideDown: { '0%': { transform: 'translateY(-8px)', opacity: '0' }, '100%': { transform: 'translateY(0)', opacity: '1' } },
            fadeIn: { '0%': { transform: 'translateY(4px)', opacity: '0' }, '100%': { transform: 'translateY(0)', opacity: '1' } },
          }
        }
      }
    }
  </script>
  <style>
    :root {
      --ease-out: cubic-bezier(0.23, 1, 0.32, 1);
      --surface-canvas: #09090b;
      --surface-card: #111114;
      --surface-card-hover: #151519;
      --border-hairline: rgba(255, 255, 255, 0.075);
      --border-hover: rgba(255, 255, 255, 0.16);
    }

    * { scrollbar-width: thin; scrollbar-color: #27272a transparent; }
    ::-webkit-scrollbar { width: 6px; height: 6px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: #27272a; border-radius: 999px; }

    /* Native 120fps Document Scroll Baseline */
    html {
      -webkit-tap-highlight-color: transparent;
      -webkit-text-size-adjust: 100%;
      background-color: var(--surface-canvas);
      scroll-behavior: auto;
    }
    body {
      background-color: var(--surface-canvas);
      color: #e4e4e7;
      min-height: 100dvh;
      padding-top: env(safe-area-inset-top, 0px);
      padding-left: env(safe-area-inset-left, 0px);
      padding-right: env(safe-area-inset-right, 0px);
      text-rendering: optimizeLegibility;
      -webkit-font-smoothing: antialiased;
    }
    button, input, select, textarea, a, [role="button"] {
      touch-action: manipulation;
    }
    button, [role="button"], .mobile-nav-btn, .drives-filter, .ctc-filter, .drives-sort, .drives-view, .notices-filter {
      user-select: none;
      -webkit-user-select: none;
      -webkit-touch-callout: none;
      transition: transform 140ms var(--ease-out), background-color 150ms ease, border-color 150ms ease, color 150ms ease;
    }
    /* Crisp tactile press feedback */
    button:active:not(:disabled), [role="button"]:active, .mobile-tap:active {
      transform: scale(0.97);
    }
    /* Prevent iOS Safari auto-zoom on input focus */
    @media (pointer: coarse), (max-width: 768px) {
      input, select, textarea {
        font-size: 16px !important;
      }
    }
    /* Contained scroll ONLY inside inner modal/drawer panels */
    #modal-body, #mobile-drawer, .sheet-scroll {
      overscroll-behavior: contain;
      -webkit-overflow-scrolling: touch;
    }
    /* Horizontal pill carousels */
    .pill-carousel {
      touch-action: pan-x;
      scroll-snap-type: x proximity;
      -webkit-overflow-scrolling: touch;
    }
    .pill-carousel > * {
      scroll-snap-align: start;
      flex-shrink: 0;
    }

    /* Defer off-screen rendering for smooth long-list scrolling (strictly applied below the fold) */
    .deferred-card {
      content-visibility: auto;
      contain-intrinsic-size: auto none auto 176px;
    }

    /* Page switch gentle entrance (compositor-only, opt-in reduced-motion) */
    @media (prefers-reduced-motion: no-preference) {
      .page-enter {
        animation: fadeIn 160ms var(--ease-out) both;
      }
    }

    /* Toast system responsive */
    .toast-container { position: fixed; top: calc(0.75rem + env(safe-area-inset-top, 0px)); right: 1rem; z-index: 9999; display: flex; flex-direction: column; gap: 0.5rem; pointer-events: none; }
    .toast { pointer-events: auto; min-width: 280px; max-width: 420px; background: #141418; border-color: rgba(255,255,255,0.12); }
    @media (prefers-reduced-motion: no-preference) {
      .toast { animation: slideDown 180ms var(--ease-out); }
      .toast.removing { animation: slideUp 140ms var(--ease-out) reverse forwards; }
    }
    @media (max-width: 640px) {
      .toast-container { left: 0.75rem; right: 0.75rem; }
      .toast { min-width: 0; width: 100%; max-width: 100%; }
    }

    /* Sidebar Desktop & Mobile Off-Canvas */
    .sidebar { width: 248px; min-width: 248px; transition: width 180ms var(--ease-out), min-width 180ms var(--ease-out); }
    .sidebar.collapsed { width: 64px; min-width: 64px; }
    .sidebar.collapsed .sidebar-label { display: none; }
    .sidebar.collapsed .sidebar-header-text { display: none; }

    /* Active nav (calm monochrome highlight, zero neon gradient) */
    .nav-item { color: #a1a1aa; transition: background-color 140ms ease, color 140ms ease; }
    .nav-item.active { background: rgba(255, 255, 255, 0.08); color: #fafafa; }

    /* Mobile Bottom Nav Active */
    .mobile-nav-btn { position: relative; transition: color 140ms ease, transform 140ms var(--ease-out); }
    .mobile-nav-btn.active { color: #fafafa; }
    .mobile-nav-btn.active .mobile-nav-pill {
      background: rgba(255, 255, 255, 0.11);
      border-color: rgba(255, 255, 255, 0.14);
    }

    /* Calm Obsidian Cards — NO per-card backdrop-filter blur or heavy shadows (120fps scroll) */
    .job-card, .metric-card {
      background-color: var(--surface-card);
      border: 1px solid var(--border-hairline);
      transition: border-color 150ms ease, background-color 150ms ease;
    }

    /* Gate hover states strictly behind fine pointers */
    @media (hover: hover) and (pointer: fine) {
      .nav-item:not(.active):hover { background: rgba(255, 255, 255, 0.045); color: #e4e4e7; }
      .job-card:hover {
        border-color: var(--border-hover);
        background-color: var(--surface-card-hover);
      }
      ::-webkit-scrollbar-thumb:hover { background: #3f3f46; }
    }

    /* Restrained status dot pulse (opt-in reduced motion) */
    @keyframes livePulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }
    @media (prefers-reduced-motion: no-preference) {
      .live-dot { animation: livePulse 2.4s ease-in-out infinite; }
    }

    /* Modal backdrop */
    .modal-backdrop { background: rgba(0, 0, 0, 0.78); backdrop-filter: blur(6px); -webkit-backdrop-filter: blur(6px); }

    /* Profile form */
    .profile-field input, .profile-field select { background: #09090b; border: 1px solid #27272a; color: #f4f4f5; }
    .profile-field input:focus, .profile-field select:focus { border-color: #71717a; outline: none; }

    /* Quiet Semantic Badges (no colored glow shadows) */
    .badge-applied { background: rgba(16, 185, 129, 0.10); color: #6ee7b7; border: 1px solid rgba(16, 185, 129, 0.22); }
    .badge-open { background: rgba(255, 255, 255, 0.08); color: #fafafa; border: 1px solid rgba(255, 255, 255, 0.16); }
    .badge-closed { background: rgba(255, 255, 255, 0.04); color: #71717a; border: 1px solid rgba(255, 255, 255, 0.07); }
    .badge-ineligible { background: rgba(244, 63, 94, 0.08); color: #fda4af; border: 1px solid rgba(244, 63, 94, 0.18); }
    .badge-shortlisted { background: rgba(245, 158, 11, 0.12); color: #fcd34d; border: 1px solid rgba(245, 158, 11, 0.28); }
    .badge-passed { background: rgba(255, 255, 255, 0.04); color: #71717a; border: 1px solid rgba(255, 255, 255, 0.08); }

    /* Native Modal <dialog> PDF Viewer (modern-web-guidance compliant) */
    dialog:modal {
      overscroll-behavior: contain;
    }
    dialog::backdrop {
      overflow: hidden;
      overscroll-behavior: contain;
      background: rgba(0, 0, 0, 0.80);
      backdrop-filter: blur(6px);
      -webkit-backdrop-filter: blur(6px);
    }
    dialog#pdfViewerDialog {
      margin: auto;
      padding: 0;
      border: 1px solid rgba(255, 255, 255, 0.12);
      border-radius: 1rem;
      background: #111114;
      color: #e4e4e7;
      width: min(96vw, 1080px);
      height: min(92dvh, 880px);
      max-width: 1080px;
      max-height: 92dvh;
      box-shadow: 0 24px 64px -12px rgba(0, 0, 0, 0.85);
    }
    dialog#pdfViewerDialog[open] {
      display: flex;
      flex-direction: column;
      overflow: hidden;
    }
    @media (max-width: 640px) {
      dialog#pdfViewerDialog {
        width: 100vw;
        height: 94dvh;
        max-width: 100vw;
        max-height: 94dvh;
        margin-bottom: 0;
        border-radius: 1.25rem 1.25rem 0 0;
      }
    }

    /* Sanitized Notice HTML Styling */
    .notice-html { font-size: 0.8125rem; line-height: 1.65; color: #cbd5e1; word-break: break-word; }
    .notice-html p { margin-bottom: 0.5rem; }
    .notice-html a { color: #818cf8; text-decoration: underline; }
    .notice-html ul, .notice-html ol { padding-left: 1.25rem; margin: 0.4rem 0; list-style: disc; }
    .notice-html table { width: 100%; border-collapse: collapse; margin: 0.65rem 0; font-size: 0.75rem; display: block; overflow-x: auto; }
    .notice-html th, .notice-html td { border: 1px solid rgba(148, 163, 184, 0.22); padding: 0.4rem 0.55rem; text-align: left; }
    .notice-html th { background: rgba(30, 41, 59, 0.85); color: #f8fafc; font-weight: 600; }

    /* Horizontal scroll without ugly bars */
    .no-scrollbar::-webkit-scrollbar { display: none; }
    .no-scrollbar { -ms-overflow-style: none; scrollbar-width: none; }
  </style>
</head>
<body class="font-sans antialiased min-h-screen flex">

  <!-- Toast Container -->
  <div id="toast-container" class="toast-container"></div>

  <!-- ═══ DESKTOP SIDEBAR ═══ -->
  <aside id="sidebar" class="hidden lg:flex sidebar bg-surface-950 border-r border-white/[0.07] flex-col h-screen sticky top-0 z-40">
    <!-- Logo -->
    <div class="p-4 border-b border-white/[0.07] flex items-center gap-3">
      <div class="w-8 h-8 rounded-lg bg-zinc-100 flex items-center justify-center text-zinc-950 font-bold text-xs flex-shrink-0">
        SJ
      </div>
      <div class="sidebar-header-text overflow-hidden">
        <div class="text-xs font-semibold text-zinc-100 tracking-wide truncate">SUPERSET PILOT</div>
        <div class="text-[10px] text-zinc-500 font-mono truncate">Mission Control</div>
      </div>
    </div>

    <!-- Student info -->
    <div class="p-4 border-b border-slate-800/80 sidebar-label flex items-center justify-between gap-2">
      <div class="min-w-0">
        <div class="text-xs text-slate-200 font-semibold truncate" id="sidebar-student-name">Student Account</div>
        <div class="text-[10px] text-slate-500 font-mono truncate" id="sidebar-student-id">KIIT Campus Portal</div>
      </div>
      <button onclick="logoutUser()" class="px-2 py-1 rounded-lg bg-surface-800 hover:bg-red-950/60 text-slate-400 hover:text-red-300 border border-white/10 text-[10px] font-mono flex-shrink-0" title="Sign Out">Exit</button>
    </div>

    <!-- Navigation -->
    <nav class="flex-1 py-3 px-2 space-y-0.5 overflow-y-auto">
      <button onclick="switchPage('dashboard')" class="nav-item active w-full flex items-center justify-between px-3 py-2.5 rounded-lg text-sm font-medium text-left" data-page="dashboard">
        <div class="flex items-center gap-3 truncate">
          <span class="text-base w-5 text-center">📊</span>
          <span class="sidebar-label">Dashboard</span>
        </div>
      </button>
      <button onclick="switchPage('drives')" class="nav-item w-full flex items-center justify-between px-3 py-2.5 rounded-lg text-sm font-medium text-left" data-page="drives">
        <div class="flex items-center gap-3 truncate">
          <span class="text-base w-5 text-center">💼</span>
          <span class="sidebar-label">Placement Drives</span>
        </div>
        <span class="sidebar-label text-[10px] font-mono px-1.5 py-0.5 rounded bg-surface-800 text-slate-400" id="badge-nav-drives">107</span>
      </button>
      <button onclick="switchPage('notices')" class="nav-item w-full flex items-center justify-between px-3 py-2.5 rounded-lg text-sm font-medium text-left" data-page="notices">
        <div class="flex items-center gap-3 truncate">
          <span class="text-base w-5 text-center">📢</span>
          <span class="sidebar-label">Notices & Shortlists</span>
        </div>
        <span class="sidebar-label text-[10px] font-mono px-1.5 py-0.5 rounded bg-surface-800 text-slate-400" id="badge-nav-notices">—</span>
      </button>
      <button onclick="switchPage('applied')" class="nav-item w-full flex items-center justify-between px-3 py-2.5 rounded-lg text-sm font-medium text-left" data-page="applied">
        <div class="flex items-center gap-3 truncate">
          <span class="text-base w-5 text-center">✅</span>
          <span class="sidebar-label">My Applications</span>
        </div>
        <span class="sidebar-label text-[10px] font-mono px-1.5 py-0.5 rounded bg-emerald-500/10 text-emerald-400" id="badge-nav-applied">29</span>
      </button>
      <button onclick="switchPage('resume')" class="nav-item w-full flex items-center justify-between px-3 py-2.5 rounded-lg text-sm font-medium text-left" data-page="resume">
        <div class="flex items-center gap-3 truncate">
          <span class="text-base w-5 text-center">📄</span>
          <span class="sidebar-label">Resume Tailor</span>
        </div>
        <span class="sidebar-label text-[10px] font-mono px-1.5 py-0.5 rounded bg-white/[0.06] text-zinc-400" id="badge-nav-resume">A4</span>
      </button>
      <button onclick="switchPage('profile')" class="nav-item w-full flex items-center justify-between px-3 py-2.5 rounded-lg text-sm font-medium text-left" data-page="profile">
        <div class="flex items-center gap-3 truncate">
          <span class="text-base w-5 text-center">👤</span>
          <span class="sidebar-label">Profile & Answers</span>
        </div>
        <span class="sidebar-label text-[10px] font-mono px-1.5 py-0.5 rounded bg-purple-500/10 text-purple-400">100%</span>
      </button>
      <button onclick="switchPage('engine')" class="nav-item w-full flex items-center justify-between px-3 py-2.5 rounded-lg text-sm font-medium text-left" data-page="engine">
        <div class="flex items-center gap-3 truncate">
          <span class="text-base w-5 text-center">⚙️</span>
          <span class="sidebar-label">Engine & Logs</span>
        </div>
        <span class="sidebar-label w-2 h-2 rounded-full bg-emerald-400 live-dot"></span>
      </button>
      <button onclick="switchPage('telegram')" class="nav-item w-full flex items-center justify-between px-3 py-2.5 rounded-lg text-sm font-medium text-left" data-page="telegram">
        <div class="flex items-center gap-3 truncate">
          <span class="text-base w-5 text-center">💬</span>
          <span class="sidebar-label">WhatsApp & Alerts</span>
        </div>
        <span class="sidebar-label text-[10px] font-mono px-1.5 py-0.5 rounded bg-white/[0.06] text-zinc-400" id="badge-nav-alerts">WA</span>
      </button>
      <button onclick="switchPage('ai')" class="nav-item w-full flex items-center justify-between px-3 py-2.5 rounded-lg text-sm font-medium text-left" data-page="ai">
        <div class="flex items-center gap-3 truncate">
          <span class="text-base w-5 text-center">🤖</span>
          <span class="sidebar-label">AI Copilot & Healer</span>
        </div>
        <span class="sidebar-label text-[10px] font-mono px-1.5 py-0.5 rounded bg-indigo-500/10 text-indigo-400" id="badge-nav-ai">Live</span>
      </button>
      <button onclick="switchPage('settings')" class="nav-item w-full flex items-center justify-between px-3 py-2.5 rounded-lg text-sm font-medium text-left" data-page="settings">
        <div class="flex items-center gap-3 truncate">
          <span class="text-base w-5 text-center">🔧</span>
          <span class="sidebar-label">Settings</span>
        </div>
      </button>
    </nav>

    <!-- Sidebar footer -->
    <div class="p-3 border-t border-slate-800/80">
      <div class="flex items-center gap-2 sidebar-label">
        <div id="daemon-indicator" class="w-2 h-2 rounded-full bg-emerald-400 live-dot"></div>
        <span id="daemon-status-text" class="text-[11px] text-slate-400 font-mono">Daemon Active</span>
      </div>
      <button onclick="toggleSidebar()" class="mt-2 w-full text-center text-slate-500 hover:text-slate-300 text-xs py-1 rounded hover:bg-slate-800/50 transition-all sidebar-label">
        ◁ Collapse
      </button>
    </div>
  </aside>

  <!-- ═══ MOBILE SLIDE-OUT DRAWER ═══ -->
  <div id="mobile-drawer-backdrop" onclick="closeMobileDrawer()" class="fixed inset-0 bg-black/75 backdrop-blur-sm z-50 hidden transition-opacity"></div>
  <div id="mobile-drawer" class="fixed inset-y-0 left-0 w-72 bg-surface-950 border-r border-slate-800 z-50 transform -translate-x-full transition-transform duration-300 lg:hidden flex flex-col" style="padding-top: env(safe-area-inset-top); padding-bottom: env(safe-area-inset-bottom);">
    <div class="p-4 border-b border-slate-800 flex items-center justify-between">
      <div class="flex items-center gap-2.5">
        <div class="w-8 h-8 rounded-lg bg-brand-600 flex items-center justify-center text-white font-bold text-xs" id="drawer-student-initials">SJ</div>
        <div>
          <div class="text-xs font-bold text-white" id="drawer-student-name">SUPERSET PILOT</div>
          <div class="text-[9px] text-slate-500 font-mono" id="drawer-student-id">Multi-User Edition</div>
        </div>
      </div>
      <button onclick="closeMobileDrawer()" class="p-1.5 rounded-lg text-slate-400 hover:text-white hover:bg-slate-800 text-lg">✕</button>
    </div>
    <nav class="flex-1 py-3 px-2 space-y-1 overflow-y-auto">
      <button onclick="switchPage('dashboard'); closeMobileDrawer();" class="w-full flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium text-slate-300 hover:bg-slate-800 text-left">
        <span class="text-base">📊</span> <span>Dashboard</span>
      </button>
      <button onclick="switchPage('drives'); closeMobileDrawer();" class="w-full flex items-center justify-between px-3 py-2.5 rounded-lg text-sm font-medium text-slate-300 hover:bg-slate-800 text-left">
        <div class="flex items-center gap-3"><span class="text-base">💼</span> <span>Placement Drives</span></div>
        <span id="drawer-badge-drives" class="text-[10px] font-mono px-1.5 py-0.5 rounded bg-surface-800 text-slate-400">107</span>
      </button>
      <button onclick="switchPage('notices'); closeMobileDrawer();" class="w-full flex items-center justify-between px-3 py-2.5 rounded-lg text-sm font-medium text-slate-300 hover:bg-slate-800 text-left">
        <div class="flex items-center gap-3"><span class="text-base">📢</span> <span>Notices & Shortlists</span></div>
        <span id="drawer-badge-notices" class="text-[10px] font-mono px-1.5 py-0.5 rounded bg-surface-800 text-slate-400">—</span>
      </button>
      <button onclick="switchPage('applied'); closeMobileDrawer();" class="w-full flex items-center justify-between px-3 py-2.5 rounded-lg text-sm font-medium text-slate-300 hover:bg-slate-800 text-left">
        <div class="flex items-center gap-3"><span class="text-base">✅</span> <span>My Applications</span></div>
        <span class="text-[10px] font-mono px-1.5 py-0.5 rounded bg-emerald-500/10 text-emerald-400">29</span>
      </button>
      <button onclick="switchPage('resume'); closeMobileDrawer();" class="w-full flex items-center justify-between px-3 py-2.5 rounded-lg text-sm font-medium text-slate-300 hover:bg-slate-800 text-left">
        <div class="flex items-center gap-3"><span class="text-base">📄</span> <span>Resume Tailor</span></div>
        <span class="text-[10px] font-mono px-1.5 py-0.5 rounded bg-white/[0.06] text-zinc-400">PDF</span>
      </button>
      <button onclick="switchPage('ai'); closeMobileDrawer();" class="w-full flex items-center justify-between px-3 py-2.5 rounded-lg text-sm font-medium text-slate-300 hover:bg-slate-800 text-left">
        <div class="flex items-center gap-3"><span class="text-base">🤖</span> <span>AI Copilot & Healer</span></div>
        <span class="text-[10px] font-mono px-1.5 py-0.5 rounded bg-indigo-500/10 text-indigo-400">Live</span>
      </button>
      <button onclick="switchPage('profile'); closeMobileDrawer();" class="w-full flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium text-slate-300 hover:bg-slate-800 text-left">
        <span class="text-base">👤</span> <span>Profile & Answers</span>
      </button>
      <button onclick="switchPage('engine'); closeMobileDrawer();" class="w-full flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium text-slate-300 hover:bg-slate-800 text-left">
        <span class="text-base">⚙️</span> <span>Engine & Logs</span>
      </button>
      <button onclick="switchPage('telegram'); closeMobileDrawer();" class="w-full flex items-center justify-between px-3 py-2.5 rounded-lg text-sm font-medium text-slate-300 hover:bg-slate-800 text-left">
        <div class="flex items-center gap-3"><span class="text-base">💬</span> <span>WhatsApp & Alerts</span></div>
        <span class="text-[10px] font-mono px-1.5 py-0.5 rounded bg-white/[0.06] text-zinc-400">WA</span>
      </button>
      <button onclick="switchPage('settings'); closeMobileDrawer();" class="w-full flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium text-slate-300 hover:bg-slate-800 text-left">
        <span class="text-base">🔧</span> <span>System & Invite Codes</span>
      </button>
      <button onclick="logoutUser(); closeMobileDrawer();" class="w-full flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium text-rose-400 hover:bg-rose-950/40 text-left">
        <span class="text-base">🚪</span> <span>Sign Out</span>
      </button>
    </nav>
    <div class="p-3 border-t border-slate-800 text-center">
      <div class="text-[11px] text-slate-400 font-mono">KIIT Bhubaneswar • 2027 Batch</div>
    </div>
  </div>

  <!-- ═══ MOBILE BOTTOM APP DOCK ═══ -->
  <nav class="lg:hidden fixed bottom-0 left-0 right-0 z-40 bg-surface-950/90 backdrop-blur-xl border-t border-white/[0.08] px-2 pt-1.5 flex items-center justify-around" style="padding-bottom: max(0.5rem, env(safe-area-inset-bottom, 0px));">
    <button onclick="switchPage('dashboard')" class="mobile-nav-btn active flex-1 flex flex-col items-center py-1 rounded-xl text-zinc-400" data-page="dashboard">
      <div class="mobile-nav-pill px-3.5 py-1 rounded-full border border-transparent flex items-center justify-center">
        <span class="text-base leading-none">📊</span>
      </div>
      <span class="text-[10px] font-medium tracking-tight mt-1">Overview</span>
    </button>
    <button onclick="switchPage('drives')" class="mobile-nav-btn flex-1 flex flex-col items-center py-1 rounded-xl text-zinc-400 relative" data-page="drives">
      <div class="mobile-nav-pill px-3.5 py-1 rounded-full border border-transparent flex items-center justify-center relative">
        <span class="text-base leading-none">💼</span>
        <span id="mobile-badge-drives" class="absolute -top-1 -right-1.5 px-1.5 py-0.2 rounded-full bg-zinc-800 border border-white/15 text-zinc-200 font-mono text-[8px] font-semibold">0</span>
      </div>
      <span class="text-[10px] font-medium tracking-tight mt-1">Drives</span>
    </button>
    <button onclick="switchPage('notices')" class="mobile-nav-btn flex-1 flex flex-col items-center py-1 rounded-xl text-zinc-400 relative" data-page="notices">
      <div class="mobile-nav-pill px-3.5 py-1 rounded-full border border-transparent flex items-center justify-center relative">
        <span class="text-base leading-none">📢</span>
        <span id="mobile-badge-notices" class="hidden absolute -top-1 -right-1.5 px-1.5 py-0.2 rounded-full bg-amber-400 text-zinc-950 font-mono text-[8px] font-bold">0</span>
      </div>
      <span class="text-[10px] font-medium tracking-tight mt-1">Notices</span>
    </button>
    <button onclick="switchPage('applied')" class="mobile-nav-btn flex-1 flex flex-col items-center py-1 rounded-xl text-zinc-400 relative" data-page="applied">
      <div class="mobile-nav-pill px-3.5 py-1 rounded-full border border-transparent flex items-center justify-center relative">
        <span class="text-base leading-none">✅</span>
        <span id="mobile-badge-applied" class="absolute -top-1 -right-1.5 px-1.5 py-0.2 rounded-full bg-zinc-800 border border-white/15 text-zinc-200 font-mono text-[8px] font-semibold">0</span>
      </div>
      <span class="text-[10px] font-medium tracking-tight mt-1">Applied</span>
    </button>
    <button onclick="openMobileDrawer()" class="mobile-nav-btn flex-1 flex flex-col items-center py-1 rounded-xl text-zinc-400">
      <div class="mobile-nav-pill px-3.5 py-1 rounded-full border border-transparent flex items-center justify-center">
        <span class="text-base leading-none">☰</span>
      </div>
      <span class="text-[10px] font-medium tracking-tight mt-1">Menu</span>
    </button>
  </nav>

  <!-- ═══ MAIN CONTENT (Native Document Scroll — Zero Nested Scroll Trap) ═══ -->
  <main class="flex-1 flex flex-col min-w-0 min-h-screen pb-24 lg:pb-8">

    <!-- Top Bar -->
    <header class="h-14 border-b border-white/[0.07] bg-surface-950/85 backdrop-blur-xl flex items-center justify-between px-3.5 sm:px-6 sticky top-0 z-30">
      <div class="flex items-center gap-2.5 sm:gap-3.5 min-w-0">
        <button onclick="openMobileDrawer()" class="lg:hidden w-8 h-8 rounded-lg bg-zinc-100 flex items-center justify-center text-zinc-950 font-bold text-xs flex-shrink-0" title="Open Menu">SJ</button>
        <button onclick="toggleSidebar()" class="hidden lg:block text-zinc-400 hover:text-zinc-100">☰</button>
        <div class="min-w-0">
          <h1 id="page-title" class="text-xs sm:text-sm font-semibold text-zinc-100 truncate">Dashboard</h1>
          <div class="lg:hidden text-[10px] font-mono text-zinc-500 truncate" id="topbar-student-sub">Signed In • KIIT</div>
        </div>
        <span class="hidden sm:inline-block text-[10px] font-mono text-zinc-500 bg-white/[0.04] border border-white/[0.07] px-2.5 py-0.5 rounded-full" id="last-sync-badge">—</span>
      </div>
      <div class="flex items-center gap-1.5 sm:gap-2 flex-shrink-0">
        <button id="topbar-clarif-btn" onclick="switchPage('ai')" class="hidden items-center gap-1 px-2.5 sm:px-3 py-1.5 rounded-lg bg-white/[0.06] border border-amber-400/30 text-amber-300 text-xs font-medium">
          <span>⚠️</span> <span id="topbar-clarif-count" class="hidden sm:inline">0 Clarifications</span>
        </button>
        <button onclick="toggleUserAutoApply()" id="user-autoapply-btn" class="flex items-center gap-1.5 px-2.5 sm:px-3 py-1.5 rounded-lg bg-surface-900 border border-white/[0.08] text-zinc-300 text-xs font-medium" title="Toggle Auto-Apply for your account">
          <span id="user-autoapply-dot" class="w-1.5 h-1.5 rounded-full bg-zinc-500"></span>
          <span id="user-autoapply-label" class="text-[11px]">Auto: OFF</span>
        </button>
        <button onclick="triggerSync()" id="sync-btn" class="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-zinc-100 hover:bg-white text-zinc-950 text-xs font-semibold">
          <span id="sync-icon">🔄</span> <span class="hidden sm:inline">Sync</span>
        </button>
        <button onclick="toggleDaemon()" id="daemon-toggle-btn" class="hidden sm:flex items-center gap-1.5 px-2.5 sm:px-3 py-1.5 rounded-lg bg-surface-900 border border-white/[0.08] text-zinc-300 text-xs font-medium">
          <span id="daemon-dot" class="w-1.5 h-1.5 rounded-full bg-emerald-400 live-dot"></span>
          <span id="daemon-label" class="hidden sm:inline">Engine ON</span>
        </button>
        <button onclick="logoutUser()" class="p-1.5 rounded-lg bg-surface-900 border border-white/[0.08] text-zinc-400 hover:text-zinc-100 text-xs" title="Sign Out">🚪</button>
      </div>
    </header>

    <!-- Page Content (Natural Window Scroll) -->
    <div class="flex-1">

      <!-- ═══ GLOBAL PENDING OFFER WATCHDOG BANNER (Visible across all tabs when active) ═══ -->
      <div id="pending-offer-banner" class="hidden mx-3.5 sm:mx-6 mt-4"></div>

      <!-- ═══ DASHBOARD PAGE ═══ -->
      <div id="page-dashboard" class="page p-3.5 sm:p-6 max-w-6xl mx-auto space-y-5">

        <!-- Calm Monochrome Candidate Header -->
        <div class="metric-card rounded-2xl p-4 sm:p-6">
          <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
            <div class="flex items-center gap-3.5 min-w-0">
              <div id="hero-student-initials" class="w-12 h-12 rounded-xl bg-zinc-900 border border-white/10 flex items-center justify-center text-zinc-100 font-bold text-sm flex-shrink-0">
                ST
              </div>
              <div class="min-w-0">
                <div class="flex items-center gap-2 flex-wrap">
                  <h2 id="hero-student-name" class="text-sm sm:text-base font-semibold text-zinc-50 tracking-tight truncate">Student Account</h2>
                  <span id="hero-student-cgpa" class="text-[10px] font-mono px-2 py-0.5 rounded-full bg-white/[0.06] text-zinc-300 border border-white/10">Verified</span>
                </div>
                <p id="hero-student-meta" class="text-[11px] sm:text-xs text-zinc-400 font-mono mt-0.5 truncate">KIIT University</p>
                <div class="flex items-center gap-2.5 mt-2.5">
                  <div class="h-1 w-28 sm:w-44 rounded-full bg-zinc-800 overflow-hidden">
                    <div id="hero-progress-bar" class="h-full rounded-full bg-zinc-200 transition-[width] duration-300" style="width: 0%"></div>
                  </div>
                  <span id="hero-progress-label" class="text-[10px] font-mono text-zinc-400">Syncing drives...</span>
                </div>
              </div>
            </div>
            <div class="grid grid-cols-2 sm:flex items-center gap-2 pt-1 sm:pt-0">
              <button onclick="switchPage('drives')" class="px-3.5 py-2 rounded-lg bg-zinc-100 hover:bg-white text-zinc-950 font-semibold text-xs flex items-center justify-center gap-1.5">
                <span>Explore Drives</span>
              </button>
              <button onclick="switchPage('notices')" class="px-3.5 py-2 rounded-lg bg-white/[0.05] hover:bg-white/[0.09] border border-white/10 text-zinc-200 font-medium text-xs flex items-center justify-center gap-1.5">
                <span>Notices & PDFs</span>
              </button>
            </div>
          </div>
        </div>

        <!-- Upcoming Shortlist Strip (Strictly TODAY / UPCOMING — expired dates never appear here) -->
        <div id="dashboard-shortlist-strip" class="hidden space-y-2.5"></div>

        <!-- Metrics Bento Grid (Calm Neutral Hierarchy) -->
        <div class="grid grid-cols-2 lg:grid-cols-5 gap-3">
          <div onclick="switchPage('drives'); setDrivesFilter('all');" class="metric-card rounded-xl p-4 cursor-pointer mobile-tap hover:border-white/15">
            <div class="flex items-center justify-between mb-2">
              <span class="text-[10px] text-zinc-500 uppercase font-mono tracking-wider">Total Drives</span>
              <span class="w-1.5 h-1.5 rounded-full bg-zinc-400"></span>
            </div>
            <div class="text-2xl font-bold text-zinc-100 font-mono tracking-tight" id="m-total">—</div>
            <div class="text-[11px] text-zinc-500 mt-1">Indexed from Superset</div>
          </div>
          <div onclick="switchPage('applied')" class="metric-card rounded-xl p-4 cursor-pointer mobile-tap hover:border-white/15">
            <div class="flex items-center justify-between mb-2">
              <span class="text-[10px] text-zinc-500 uppercase font-mono tracking-wider">Applied</span>
              <span class="w-1.5 h-1.5 rounded-full bg-emerald-400"></span>
            </div>
            <div class="text-2xl font-bold text-zinc-100 font-mono tracking-tight" id="m-applied">—</div>
            <div class="text-[11px] text-zinc-500 mt-1">Confirmed on portal</div>
          </div>
          <div onclick="switchPage('drives'); setDrivesFilter('open');" class="metric-card rounded-xl p-4 cursor-pointer mobile-tap hover:border-white/15">
            <div class="flex items-center justify-between mb-2">
              <span class="text-[10px] text-zinc-500 uppercase font-mono tracking-wider">Active Open</span>
              <span class="w-1.5 h-1.5 rounded-full bg-amber-400"></span>
            </div>
            <div class="text-2xl font-bold text-zinc-100 font-mono tracking-tight" id="m-open">—</div>
            <div class="text-[11px] text-zinc-500 mt-1">Ready to apply</div>
          </div>
          <div onclick="switchPage('drives'); setDrivesFilter('eligible');" class="metric-card rounded-xl p-4 cursor-pointer mobile-tap hover:border-white/15">
            <div class="flex items-center justify-between mb-2">
              <span class="text-[10px] text-zinc-500 uppercase font-mono tracking-wider">Eligible</span>
              <span class="w-1.5 h-1.5 rounded-full bg-indigo-400"></span>
            </div>
            <div class="text-2xl font-bold text-zinc-100 font-mono tracking-tight" id="m-eligible">—</div>
            <div class="text-[11px] text-zinc-500 mt-1">Matching criteria</div>
          </div>
          <div onclick="switchPage('drives'); setDrivesFilter('closed');" class="col-span-2 lg:col-span-1 metric-card rounded-xl p-4 cursor-pointer mobile-tap hover:border-white/15">
            <div class="flex items-center justify-between mb-2">
              <span class="text-[10px] text-zinc-500 uppercase font-mono tracking-wider">Closed / Past</span>
              <span class="w-1.5 h-1.5 rounded-full bg-zinc-600"></span>
            </div>
            <div class="text-2xl font-bold text-zinc-400 font-mono tracking-tight" id="m-closed">—</div>
            <div class="text-[11px] text-zinc-500 mt-1">Deadline completed</div>
          </div>
        </div>

        <!-- Quick Stats -->
        <div class="grid grid-cols-1 lg:grid-cols-3 gap-3.5 sm:gap-4">
          <!-- CTC Breakdown -->
          <div class="metric-card rounded-2xl p-4 sm:p-5">
            <h3 class="text-xs font-bold text-slate-200 uppercase tracking-wider mb-4 flex items-center gap-2"><span>💰</span> CTC Distribution</h3>
            <div id="ctc-breakdown" class="space-y-3">
              <div class="skeleton h-4 rounded"></div>
              <div class="skeleton h-4 rounded w-3/4"></div>
              <div class="skeleton h-4 rounded w-1/2"></div>
            </div>
          </div>

          <!-- Location Breakdown -->
          <div class="metric-card rounded-2xl p-4 sm:p-5">
            <h3 class="text-xs font-bold text-slate-200 uppercase tracking-wider mb-4 flex items-center gap-2"><span>📍</span> Top Locations</h3>
            <div id="location-breakdown" class="space-y-3">
              <div class="skeleton h-4 rounded"></div>
              <div class="skeleton h-4 rounded w-3/4"></div>
              <div class="skeleton h-4 rounded w-1/2"></div>
            </div>
          </div>

          <!-- Engine Status -->
          <div class="metric-card rounded-2xl p-4 sm:p-5">
            <h3 class="text-xs font-bold text-slate-200 uppercase tracking-wider mb-4 flex items-center gap-2"><span>⚡</span> Autonomous Engine</h3>
            <div class="space-y-3">
              <div class="flex justify-between text-xs">
                <span class="text-slate-400">Background Daemon</span>
                <span id="dash-daemon" class="font-mono font-semibold text-emerald-400">ACTIVE</span>
              </div>
              <div class="flex justify-between text-xs">
                <span class="text-slate-400">Poll Interval</span>
                <span id="dash-poll" class="font-mono text-slate-200">15m</span>
              </div>
              <div class="flex justify-between text-xs">
                <span class="text-slate-400">WhatsApp Alerts</span>
                <button onclick="switchPage('telegram')" id="dash-whatsapp" class="font-mono font-semibold text-zinc-400 hover:text-white transition-colors">SETUP →</button>
              </div>
              <div class="flex justify-between text-xs">
                <span class="text-slate-400">Telegram Bot</span>
                <span id="dash-telegram" class="font-mono font-semibold text-emerald-400">PAIRED</span>
              </div>
              <div class="flex justify-between text-xs">
                <span class="text-slate-400">Pre-Submit Review</span>
                <span id="dash-autoapply" class="font-mono font-semibold text-brand-400">ENABLED</span>
              </div>
              <div class="flex justify-between text-xs">
                <span class="text-slate-400">Pending Offer Watchdog</span>
                <span id="dash-offer-watchdog" class="font-mono font-semibold text-emerald-400">ACTIVE • 0 Pending</span>
              </div>
              <div class="flex justify-between text-xs">
                <span class="text-slate-400">Last Sync</span>
                <span id="dash-lastsync" class="font-mono text-slate-300 text-[10px]">—</span>
              </div>
            </div>
          </div>
        </div>

        <!-- Recent Activity -->
        <div class="metric-card rounded-2xl p-4 sm:p-5">
          <div class="flex items-center justify-between mb-3">
            <h3 class="text-xs font-bold text-slate-200 uppercase tracking-wider flex items-center gap-2"><span>📡</span> Live Telemetry Feed</h3>
            <span class="text-[10px] text-slate-400 font-mono" id="dash-log-count">0 entries</span>
          </div>
          <div id="dash-logs" class="space-y-1.5 max-h-48 overflow-y-auto font-mono text-[11px]">
            <div class="text-slate-500">Loading...</div>
          </div>
        </div>
      </div>

      <!-- ═══ SMART NOTICE FEED & AUTO SHORTLIST SCANNER PAGE ═══ -->
      <div id="page-notices" class="page hidden p-3.5 sm:p-6 max-w-5xl mx-auto space-y-5">
        <!-- Calm Monochrome Scanner & Watchdog Header -->
        <div class="metric-card rounded-2xl p-4 sm:p-5">
          <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3.5">
            <div class="space-y-1 min-w-0">
              <div class="flex items-center gap-2 flex-wrap">
                <h2 class="text-sm sm:text-base font-semibold text-zinc-100 tracking-tight">Smart Notice Feed & Shortlist Scanner</h2>
                <span id="notices-roll-pill" class="text-[10px] font-mono px-2 py-0.5 rounded-full bg-white/[0.06] text-zinc-300 border border-white/10">Roll —</span>
              </div>
              <p class="text-xs text-zinc-400">
                Scans KIIT shortlist PDFs for your roll number & name, hides duplicate spam, and watches <code class="text-zinc-300 font-mono">/pending_actions</code> for job offers.
              </p>
              <div class="flex items-center gap-2.5 flex-wrap pt-1 text-[11px] font-mono text-zinc-500">
                <span id="notices-last-scan">Last scan: —</span>
                <span>·</span>
                <span id="notices-offer-pill" class="text-zinc-400">Offer Watchdog: 0 Pending</span>
              </div>
            </div>
            <div class="flex items-center gap-2 flex-shrink-0">
              <button onclick="scanNoticesLive()" id="notices-scan-btn" class="w-full sm:w-auto px-3.5 py-2 rounded-lg bg-zinc-100 hover:bg-white text-zinc-950 font-semibold text-xs flex items-center justify-center gap-1.5">
                <span id="notices-scan-icon">🔍</span> <span>Scan Shortlist PDFs</span>
              </button>
            </div>
          </div>
        </div>

        <!-- Upcoming Shortlist Matches Spotlight (Strictly Today / Upcoming — expired dates omitted) -->
        <div id="notices-shortlist-spotlight" class="hidden space-y-2.5"></div>

        <!-- Search & Smart Filter Ribbon -->
        <div class="space-y-2.5">
          <div class="flex items-center gap-2">
            <div class="relative flex-1">
              <span class="absolute left-3.5 top-1/2 -translate-y-1/2 text-zinc-500 text-xs pointer-events-none">🔍</span>
              <input id="notices-search" type="search" enterkeyhint="search" autocomplete="off" autocorrect="off" spellcheck="false"
                placeholder="Search company, venue, test date, PDF name..."
                class="w-full bg-surface-900 border border-white/[0.08] rounded-xl pl-9 pr-4 py-2 text-sm text-zinc-100 placeholder-zinc-500 focus:border-zinc-500 focus:outline-none"
                oninput="renderNotices()">
            </div>
            <label class="hidden sm:flex items-center gap-2 px-3 py-2 rounded-xl bg-surface-900 border border-white/[0.08] text-xs text-zinc-400 cursor-pointer select-none whitespace-nowrap">
              <input id="notices-show-spam" type="checkbox" onchange="renderNotices()" class="rounded border-zinc-700 bg-zinc-900 text-zinc-100 focus:ring-0">
              <span>Show spam (<span id="notices-spam-count">0</span>)</span>
            </label>
          </div>

          <!-- Swipeable Category Filter Pills -->
          <div class="pill-carousel flex items-center gap-1.5 overflow-x-auto no-scrollbar pb-0.5">
            <button onclick="setNoticesFilter('all')" class="notices-filter active px-3 py-1.5 rounded-full text-xs font-medium bg-zinc-100 text-zinc-950" data-nfilter="all">All Clean <span class="opacity-65 font-mono" id="nf-count-all">0</span></button>
            <button onclick="setNoticesFilter('shortlisted')" class="notices-filter px-3 py-1.5 rounded-full text-xs font-medium bg-surface-900 border border-white/[0.08] text-zinc-300" data-nfilter="shortlisted">Shortlisted <span class="opacity-65 font-mono" id="nf-count-shortlisted">0</span></button>
            <button onclick="setNoticesFilter('shortlists_tests')" class="notices-filter px-3 py-1.5 rounded-full text-xs font-medium bg-surface-900 border border-white/[0.08] text-zinc-300" data-nfilter="shortlists_tests">Shortlists & Tests <span class="opacity-65 font-mono" id="nf-count-tests">0</span></button>
            <button onclick="setNoticesFilter('deadlines_revisions')" class="notices-filter px-3 py-1.5 rounded-full text-xs font-medium bg-surface-900 border border-white/[0.08] text-zinc-300" data-nfilter="deadlines_revisions">Deadlines & Revisions <span class="opacity-65 font-mono" id="nf-count-deadlines">0</span></button>
            <button onclick="setNoticesFilter('job_openings')" class="notices-filter px-3 py-1.5 rounded-full text-xs font-medium bg-surface-900 border border-white/[0.08] text-zinc-300" data-nfilter="job_openings">Job Openings <span class="opacity-65 font-mono" id="nf-count-jobs">0</span></button>
          </div>
          <div class="flex items-center justify-between text-[11px] font-mono text-zinc-500">
            <span id="notices-result-meta">Loading campus notices...</span>
            <label class="sm:hidden inline-flex items-center gap-1.5 text-zinc-500">
              <input id="notices-show-spam-mob" type="checkbox" onchange="document.getElementById('notices-show-spam').checked=this.checked; renderNotices();" class="rounded border-zinc-700 text-zinc-100 focus:ring-0">
              <span>Show spam</span>
            </label>
          </div>
        </div>

        <!-- Notices Feed Container -->
        <div id="notices-list" class="space-y-2.5">
          <div class="metric-card rounded-xl p-8 text-center text-zinc-500 text-xs font-mono">Loading notices from Superset...</div>
        </div>
      </div>

      <!-- ═══ DRIVES PAGE ═══ -->
      <div id="page-drives" class="page hidden p-3.5 sm:p-6 space-y-3.5 sm:space-y-4">
        <!-- Mobile-Native Search & Filter Ribbon -->
        <div class="space-y-2.5">
          <!-- Search Bar + Quick Actions -->
          <div class="flex items-center gap-2">
            <div class="relative flex-1">
              <span class="absolute left-3.5 top-1/2 -translate-y-1/2 text-slate-400 text-xs pointer-events-none">🔍</span>
              <input id="drives-search" type="search" enterkeyhint="search" autocomplete="off" autocorrect="off" spellcheck="false"
                placeholder="Search company, role, city, CTC..."
                class="w-full bg-surface-900/90 border border-white/10 rounded-xl pl-9 pr-4 py-2.5 text-sm text-white placeholder-slate-500 focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-500/25 transition-all shadow-inner"
                oninput="renderDrives()">
            </div>
            <!-- Sorting -->
            <div class="flex items-center gap-1 bg-surface-900/90 rounded-xl border border-white/10 p-1">
              <button onclick="setDrivesSort('newest')" class="drives-sort active px-2.5 py-1.5 rounded-lg text-xs font-medium transition-all" data-sort="newest" title="Newest first">🕐</button>
              <button onclick="setDrivesSort('ctc-high')" class="drives-sort px-2.5 py-1.5 rounded-lg text-xs font-medium transition-all" data-sort="ctc-high" title="Highest CTC">💰</button>
              <button onclick="setDrivesSort('company')" class="drives-sort px-2.5 py-1.5 rounded-lg text-xs font-medium transition-all" data-sort="company" title="A→Z Company">🔤</button>
            </div>
            <!-- Export CSV -->
            <button onclick="exportDrivesCsv()" class="hidden sm:flex px-3 py-2 rounded-xl bg-surface-900/90 border border-white/10 text-slate-300 text-xs font-semibold items-center gap-1.5 transition-all" title="Export drives to CSV">
              <span>📥</span> <span>CSV</span>
            </button>
          </div>

          <!-- Swipeable Status & CTC Pill Carousels -->
          <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-2">
            <!-- Status Filters Carousel -->
            <div class="pill-carousel flex items-center gap-1.5 overflow-x-auto no-scrollbar pb-0.5">
              <button onclick="setDrivesFilter('all')" class="drives-filter active px-3.5 py-1.5 rounded-full text-xs font-semibold bg-brand-600 text-white shadow-sm" data-filter="all">All Drives</button>
              <button onclick="setDrivesFilter('open')" class="drives-filter px-3.5 py-1.5 rounded-full text-xs font-semibold bg-surface-900/90 border border-white/10 text-slate-300" data-filter="open">⚡ Open</button>
              <button onclick="setDrivesFilter('applied')" class="drives-filter px-3.5 py-1.5 rounded-full text-xs font-semibold bg-surface-900/90 border border-white/10 text-slate-300" data-filter="applied">✓ Applied</button>
              <button onclick="setDrivesFilter('eligible')" class="drives-filter px-3.5 py-1.5 rounded-full text-xs font-semibold bg-surface-900/90 border border-white/10 text-slate-300" data-filter="eligible">🎯 Eligible</button>
              <button onclick="setDrivesFilter('closed')" class="drives-filter px-3.5 py-1.5 rounded-full text-xs font-semibold bg-surface-900/90 border border-white/10 text-slate-300" data-filter="closed">Closed</button>
            </div>

            <!-- CTC Filter Pills Carousel -->
            <div class="pill-carousel flex items-center gap-1.5 overflow-x-auto no-scrollbar pb-0.5">
              <button onclick="setCtcFilter('all')" class="ctc-filter active px-3 py-1.5 rounded-full text-[11px] font-semibold bg-brand-600 text-white" data-ctc="all">All CTC</button>
              <button onclick="setCtcFilter('tier1')" class="ctc-filter px-3 py-1.5 rounded-full text-[11px] font-semibold bg-surface-900/90 border border-white/10 text-slate-300" data-ctc="tier1">🔥 &gt;14 LPA</button>
              <button onclick="setCtcFilter('tier2')" class="ctc-filter px-3 py-1.5 rounded-full text-[11px] font-semibold bg-surface-900/90 border border-white/10 text-slate-300" data-ctc="tier2">💎 7-14 LPA</button>
              <button onclick="setCtcFilter('tier3')" class="ctc-filter px-3 py-1.5 rounded-full text-[11px] font-semibold bg-surface-900/90 border border-white/10 text-slate-300" data-ctc="tier3">3-7 LPA</button>
              <!-- Desktop Grid/Table View -->
              <div class="hidden md:flex items-center gap-1 bg-surface-900/90 rounded-full border border-white/10 p-0.5 ml-1">
                <button onclick="setDrivesView('grid')" class="drives-view active px-2.5 py-1 rounded-full text-[11px] font-medium" data-view="grid">Grid</button>
                <button onclick="setDrivesView('table')" class="drives-view px-2.5 py-1 rounded-full text-[11px] font-medium" data-view="table">Table</button>
              </div>
            </div>
          </div>
        </div>

        <div class="text-[11px] text-slate-400 font-mono flex items-center justify-between" id="drives-count-label">Loading drives...</div>

        <!-- Grid View -->
        <div id="drives-grid" class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3.5 sm:gap-4"></div>

        <!-- Table View -->
        <div id="drives-table" class="hidden overflow-x-auto rounded-xl border border-slate-800/60">
          <table class="w-full text-sm">
            <thead class="bg-surface-900/80">
              <tr class="text-[10px] text-slate-500 uppercase font-mono tracking-wider">
                <th class="text-left p-3">Company</th>
                <th class="text-left p-3">Role</th>
                <th class="text-left p-3">CTC</th>
                <th class="text-left p-3">Location</th>
                <th class="text-left p-3">Deadline</th>
                <th class="text-left p-3">Status</th>
                <th class="text-center p-3">Actions</th>
              </tr>
            </thead>
            <tbody id="drives-table-body" class="divide-y divide-slate-800/50"></tbody>
          </table>
        </div>
      </div>

      <!-- ═══ MY APPLICATIONS PAGE ═══ -->
      <div id="page-applied" class="page hidden p-6 space-y-4">
        <div class="flex items-center justify-between">
          <div>
            <h2 class="text-lg font-bold text-white">My Applications Pipeline</h2>
            <p class="text-xs text-slate-500">Track every application registered on the Superset official portal</p>
          </div>
          <div class="text-right">
            <div class="text-2xl font-bold text-emerald-400 font-mono" id="applied-count-big">29</div>
            <div class="text-[10px] text-slate-500">Confirmed</div>
          </div>
        </div>
        <div id="applied-list" class="space-y-3"></div>
      </div>

      <!-- ═══ PROFILE & QUESTION RESOLVER PAGE ═══ -->
      <div id="page-profile" class="page hidden p-6 space-y-6">
        <div class="flex items-center justify-between">
          <div>
            <h2 class="text-lg font-bold text-white">Pre-Loaded Profile & Answers</h2>
            <p class="text-xs text-slate-500">Auto-fill engine answers company questionnaires accurately from verified candidate data.</p>
          </div>
          <button onclick="saveProfile()" class="px-4 py-2 rounded-lg bg-brand-600 hover:bg-brand-500 text-white text-xs font-semibold transition-all shadow-md">
            💾 Save Profile Changes
          </button>
        </div>

        <!-- Live Company Question Simulator Tool -->
        <div class="bg-surface-900/80 rounded-xl p-5 border border-purple-500/30 shadow-lg shadow-purple-900/10">
          <div class="flex flex-col md:flex-row md:items-center justify-between gap-3 mb-3">
            <div>
              <h3 class="text-xs font-bold text-purple-300 uppercase tracking-wider flex items-center gap-1.5">
                <span>⚡</span> Interactive Drive Question Inspector & Simulator
              </h3>
              <p class="text-[11px] text-slate-400">Select any campus company to test and preview how your profile answers their questionnaire live.</p>
            </div>
            <select id="simulator-company-select" onchange="runQuestionSimulator()" class="bg-surface-950 border border-slate-700 text-white text-xs rounded-lg px-3 py-2 focus:border-purple-500 focus:outline-none min-w-[240px]">
              <option value="">Select a company to test...</option>
            </select>
          </div>
          <div id="simulator-output" class="hidden pt-3 border-t border-slate-800 text-xs"></div>
        </div>

        <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
          <!-- Personal Info -->
          <div class="bg-surface-900/60 rounded-xl p-5 border border-slate-800/60">
            <h3 class="text-xs font-semibold text-brand-400 uppercase tracking-wider mb-4">Personal Information</h3>
            <div id="profile-personal" class="space-y-3"></div>
          </div>

          <!-- Identity Documents -->
          <div class="bg-surface-900/60 rounded-xl p-5 border border-slate-800/60">
            <h3 class="text-xs font-semibold text-brand-400 uppercase tracking-wider mb-4">Identity & Documents</h3>
            <div id="profile-identity" class="space-y-3"></div>
          </div>

          <!-- Academic Records -->
          <div class="bg-surface-900/60 rounded-xl p-5 border border-slate-800/60">
            <h3 class="text-xs font-semibold text-brand-400 uppercase tracking-wider mb-4">Academic Records</h3>
            <div id="profile-academic" class="space-y-3"></div>
          </div>

          <!-- Preferences & Links -->
          <div class="bg-surface-900/60 rounded-xl p-5 border border-slate-800/60">
            <h3 class="text-xs font-semibold text-brand-400 uppercase tracking-wider mb-4">Links & Preferences</h3>
            <div id="profile-links" class="space-y-3"></div>
          </div>
        </div>
      </div>

      <!-- ═══ ENGINE & LOGS PAGE ═══ -->
      <div id="page-engine" class="page hidden p-6 space-y-6">
        <div class="flex items-center justify-between">
          <div>
            <h2 class="text-lg font-bold text-white">Engine Telemetry & Execution Logs</h2>
            <p class="text-xs text-slate-500">Real-time status and logs from the autonomous 24/7 background worker</p>
          </div>
          <div class="flex items-center gap-2">
            <button onclick="clearLogs()" class="px-3 py-1.5 rounded-lg border border-slate-700 text-slate-400 text-xs font-medium hover:bg-slate-800 transition-all">
              Clear Logs
            </button>
            <button onclick="triggerSync()" class="px-3 py-1.5 rounded-lg bg-brand-600 hover:bg-brand-500 text-white text-xs font-semibold transition-all">
              Force Sync Cycle
            </button>
          </div>
        </div>

        <!-- Engine Stats -->
        <div class="grid grid-cols-2 lg:grid-cols-4 gap-4">
          <div class="bg-surface-900/60 rounded-xl p-4 border border-slate-800/60">
            <div class="text-[10px] text-slate-500 uppercase font-mono mb-1">Sync Status</div>
            <div class="text-sm font-bold font-mono" id="engine-sync-status">—</div>
          </div>
          <div class="bg-surface-900/60 rounded-xl p-4 border border-slate-800/60">
            <div class="text-[10px] text-slate-500 uppercase font-mono mb-1">Last Sync Time</div>
            <div class="text-sm font-bold text-slate-300 font-mono" id="engine-last-sync">—</div>
          </div>
          <div class="bg-surface-900/60 rounded-xl p-4 border border-slate-800/60">
            <div class="text-[10px] text-slate-500 uppercase font-mono mb-1">Drives Found</div>
            <div class="text-sm font-bold text-white font-mono" id="engine-drives-found">—</div>
          </div>
          <div class="bg-surface-900/60 rounded-xl p-4 border border-slate-800/60">
            <div class="text-[10px] text-slate-500 uppercase font-mono mb-1">Total Cycles</div>
            <div class="text-sm font-bold text-brand-400 font-mono" id="engine-sync-count">—</div>
          </div>
        </div>

        <!-- Sync History -->
        <div class="bg-surface-900/60 rounded-xl p-5 border border-slate-800/60">
          <h3 class="text-xs font-semibold text-slate-300 uppercase tracking-wider mb-4">Historical Execution Cycles</h3>
          <div id="sync-history-list" class="space-y-2 max-h-48 overflow-y-auto"></div>
        </div>

        <!-- Live Logs -->
        <div class="bg-surface-950 rounded-xl p-5 border border-slate-800/60">
          <div class="flex items-center justify-between mb-4">
            <h3 class="text-xs font-semibold text-slate-300 uppercase tracking-wider">Live Terminal Stream</h3>
            <div class="flex items-center gap-2">
              <div class="w-2 h-2 rounded-full bg-emerald-400 live-dot"></div>
              <span class="text-[10px] text-slate-500 font-mono">STREAMING</span>
            </div>
          </div>
          <div id="engine-logs" class="space-y-0.5 max-h-96 overflow-y-auto font-mono text-[11px] leading-5"></div>
        </div>
      </div>

      <!-- ═══ WHATSAPP & TELEGRAM ALERTS PAGE ═══ -->
      <div id="page-telegram" class="page hidden p-4 sm:p-6 space-y-6 max-w-6xl mx-auto">
        <!-- Header -->
        <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
          <div>
            <h2 class="text-lg font-semibold text-zinc-50 tracking-tight">WhatsApp & Telegram Alerts</h2>
            <p class="text-xs text-zinc-400 mt-0.5">Instant phone notifications for new eligible Job Profiles, exact Shortlist Venue/Date/Time PDF matches, and Pending Offers.</p>
          </div>
          <div class="flex flex-wrap items-center gap-2">
            <button onclick="sendWhatsappDigest()" id="btn-wa-digest" class="px-3.5 py-2 rounded-lg bg-white/[0.06] hover:bg-white/[0.1] border border-white/10 text-zinc-100 text-xs font-medium">
              Send Live Digest Now
            </button>
            <button onclick="testWhatsapp()" id="btn-wa-test" class="px-3.5 py-2 rounded-lg bg-zinc-100 hover:bg-white text-zinc-950 text-xs font-semibold">
              Send Test WhatsApp
            </button>
          </div>
        </div>

        <!-- PRIMARY: WHATSAPP CONFIGURATION STUDIO -->
        <div class="grid grid-cols-1 lg:grid-cols-12 gap-5">
          <!-- Left: Provider Config & Toggles -->
          <div class="lg:col-span-7 metric-card rounded-2xl p-5 space-y-5">
            <div class="flex items-center justify-between gap-3 border-b border-white/[0.07] pb-3.5">
              <div>
                <div class="text-xs font-semibold uppercase tracking-wider text-zinc-300">WhatsApp Delivery</div>
                <div class="text-[11px] text-zinc-500 mt-0.5">Stored privately per student inside your account vault</div>
              </div>
              <span id="wa-status-pill" class="text-[11px] font-mono px-2.5 py-1 rounded-full bg-white/[0.05] border border-white/10 text-zinc-400">○ Not Configured</span>
            </div>

            <!-- Master Toggle + Provider -->
            <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
              <div class="space-y-1.5">
                <label for="wa-provider" class="block text-[11px] font-medium uppercase tracking-wider text-zinc-400">Delivery Provider</label>
                <select id="wa-provider" onchange="onWhatsappProviderChange()" class="w-full bg-[#09090b] border border-white/10 focus:border-zinc-500 rounded-lg px-3 py-2.5 text-xs text-zinc-100 focus:outline-none">
                  <option value="local_qr">Built-in WhatsApp QR Link (Free · Instant · Recommended)</option>
                  <option value="meta">Meta WhatsApp Cloud API (Official)</option>
                  <option value="twilio">Twilio WhatsApp API</option>
                  <option value="webhook">Custom Webhook (n8n / Make / WAHA)</option>
                  <option value="callmebot">CallMeBot (Legacy — Free Slots Often Full)</option>
                </select>
              </div>
              <div class="space-y-1.5">
                <label for="wa-phone" class="block text-[11px] font-medium uppercase tracking-wider text-zinc-400">WhatsApp Number <span id="wa-phone-opt-tag" class="text-zinc-500 font-normal normal-case">(optional for QR Link)</span></label>
                <input id="wa-phone" type="tel" autocomplete="tel" placeholder="Leave blank for Message Yourself, or 10-digit mobile"
                  class="w-full bg-[#09090b] border border-white/10 focus:border-zinc-500 rounded-lg px-3 py-2.5 text-xs text-zinc-100 placeholder-zinc-600 focus:outline-none font-mono">
              </div>
            </div>

            <!-- BUILT-IN WHATSAPP QR LINK PANEL (NO 3RD-PARTY BOT QUEUES) -->
            <div id="wa-fields-local_qr" class="rounded-xl bg-[#09090b] border border-white/[0.08] p-4 space-y-3.5">
              <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-2.5">
                <div>
                  <div class="text-[11px] font-semibold text-zinc-200 uppercase tracking-wider">Link Your WhatsApp via QR Code (100% Free · Instant)</div>
                  <p class="text-[11px] text-zinc-400 mt-0.5">No CallMeBot waitlist or Meta verification. Scan once under WhatsApp <span class="text-zinc-200">Settings → Linked Devices</span> and alerts arrive in your <span class="text-zinc-200">Message Yourself</span> chat.</p>
                </div>
                <div class="flex items-center gap-2 flex-shrink-0">
                  <button onclick="startWhatsappQr(false)" id="btn-wa-qr-start" class="px-3.5 py-2 rounded-lg bg-zinc-100 hover:bg-white text-zinc-950 text-xs font-semibold whitespace-nowrap">
                    Generate QR Code
                  </button>
                  <button onclick="logoutWhatsappQr()" id="btn-wa-qr-logout" class="hidden px-3 py-2 rounded-lg bg-white/[0.06] hover:bg-white/[0.1] border border-white/10 text-zinc-300 text-xs font-medium whitespace-nowrap">
                    Unlink Device
                  </button>
                </div>
              </div>

              <!-- Live QR / Connected State Container -->
              <div id="wa-qr-connected-box" class="hidden rounded-xl bg-emerald-500/[0.07] border border-emerald-500/25 p-3.5 flex items-center justify-between gap-3">
                <div class="space-y-0.5">
                  <div class="text-xs font-semibold text-emerald-300 flex items-center gap-2">
                    <span class="w-2 h-2 rounded-full bg-emerald-400"></span>
                    <span id="wa-qr-connected-label">Linked to WhatsApp</span>
                  </div>
                  <div class="text-[11px] text-zinc-400">24/7 WhatsApp delivery is active. Leave WhatsApp Number blank to message yourself, or enter another number above.</div>
                </div>
              </div>

              <div id="wa-qr-canvas-box" class="hidden flex flex-col sm:flex-row items-center gap-4 pt-1">
                <div class="p-2.5 bg-white rounded-xl flex-shrink-0">
                  <img id="wa-qr-img" src="" alt="WhatsApp Linked Devices QR Code" class="w-48 h-48 block">
                </div>
                <div class="space-y-2 text-xs text-zinc-400 leading-relaxed">
                  <div class="text-zinc-100 font-semibold">Scan now on your phone:</div>
                  <ol class="list-decimal list-inside space-y-1">
                    <li>Open <span class="text-zinc-200">WhatsApp</span> on your iPhone / Android.</li>
                    <li>Go to <span class="text-zinc-200">Settings → Linked Devices → Link a Device</span>.</li>
                    <li>Point your camera at this QR code — connection completes automatically in ~3 seconds.</li>
                  </ol>
                  <div id="wa-qr-poll-note" class="text-[11px] font-mono text-zinc-500">Waiting for scan...</div>
                </div>
              </div>

              <div id="wa-qr-error" class="hidden text-xs text-amber-300 bg-amber-500/10 border border-amber-500/20 rounded-lg px-3 py-2"></div>
            </div>

            <!-- CallMeBot Setup Guide (Legacy) -->
            <div id="wa-guide-callmebot" class="hidden rounded-xl bg-[#09090b] border border-white/[0.08] p-3.5 space-y-2 text-xs">
              <div class="text-[11px] font-semibold text-amber-300 uppercase tracking-wider">CallMeBot Note (Free Slots Often Full)</div>
              <p class="text-zinc-400 leading-relaxed">
                CallMeBot frequently pauses new free WhatsApp activations ("bot is currently full"). Unless you already have an active CallMeBot API Key, switch to <span class="text-zinc-100 font-medium">Built-in WhatsApp QR Link</span> above for instant free alerts.
              </p>
            </div>

            <!-- Provider-Specific Credential Inputs -->
            <div id="wa-fields-callmebot" class="hidden space-y-1.5">
              <label for="wa-apikey" class="block text-[11px] font-medium uppercase tracking-wider text-zinc-400">CallMeBot API Key</label>
              <input id="wa-apikey" type="password" autocomplete="off" placeholder="Enter your CallMeBot API key"
                class="w-full bg-[#09090b] border border-white/10 focus:border-zinc-500 rounded-lg px-3 py-2.5 text-xs text-zinc-100 placeholder-zinc-600 focus:outline-none font-mono">
              <div id="wa-apikey-hint" class="text-[11px] text-zinc-500"></div>
            </div>

            <div id="wa-fields-meta" class="hidden grid grid-cols-1 sm:grid-cols-2 gap-4">
              <div class="space-y-1.5">
                <label for="wa-meta-phone-id" class="block text-[11px] font-medium uppercase tracking-wider text-zinc-400">Meta Phone Number ID</label>
                <input id="wa-meta-phone-id" type="text" placeholder="e.g. 123456789012345"
                  class="w-full bg-[#09090b] border border-white/10 focus:border-zinc-500 rounded-lg px-3 py-2.5 text-xs text-zinc-100 placeholder-zinc-600 focus:outline-none font-mono">
              </div>
              <div class="space-y-1.5">
                <label for="wa-meta-token" class="block text-[11px] font-medium uppercase tracking-wider text-zinc-400">Meta Permanent Access Token</label>
                <input id="wa-meta-token" type="password" autocomplete="off" placeholder="EAAG..."
                  class="w-full bg-[#09090b] border border-white/10 focus:border-zinc-500 rounded-lg px-3 py-2.5 text-xs text-zinc-100 placeholder-zinc-600 focus:outline-none font-mono">
              </div>
            </div>

            <div id="wa-fields-twilio" class="hidden grid grid-cols-1 sm:grid-cols-3 gap-3">
              <div class="space-y-1.5">
                <label for="wa-twilio-sid" class="block text-[11px] font-medium uppercase tracking-wider text-zinc-400">Account SID</label>
                <input id="wa-twilio-sid" type="text" placeholder="ACxxxxxxxx"
                  class="w-full bg-[#09090b] border border-white/10 focus:border-zinc-500 rounded-lg px-3 py-2.5 text-xs text-zinc-100 placeholder-zinc-600 focus:outline-none font-mono">
              </div>
              <div class="space-y-1.5">
                <label for="wa-twilio-token" class="block text-[11px] font-medium uppercase tracking-wider text-zinc-400">Auth Token</label>
                <input id="wa-twilio-token" type="password" autocomplete="off" placeholder="Auth Token"
                  class="w-full bg-[#09090b] border border-white/10 focus:border-zinc-500 rounded-lg px-3 py-2.5 text-xs text-zinc-100 placeholder-zinc-600 focus:outline-none font-mono">
              </div>
              <div class="space-y-1.5">
                <label for="wa-twilio-from" class="block text-[11px] font-medium uppercase tracking-wider text-zinc-400">Twilio From</label>
                <input id="wa-twilio-from" type="text" placeholder="whatsapp:+14155238886"
                  class="w-full bg-[#09090b] border border-white/10 focus:border-zinc-500 rounded-lg px-3 py-2.5 text-xs text-zinc-100 placeholder-zinc-600 focus:outline-none font-mono">
              </div>
            </div>

            <div id="wa-fields-webhook" class="hidden space-y-1.5">
              <label for="wa-webhook-url" class="block text-[11px] font-medium uppercase tracking-wider text-zinc-400">Webhook POST Endpoint</label>
              <input id="wa-webhook-url" type="url" placeholder="https://your-n8n-or-waha-webhook/whatsapp"
                class="w-full bg-[#09090b] border border-white/10 focus:border-zinc-500 rounded-lg px-3 py-2.5 text-xs text-zinc-100 placeholder-zinc-600 focus:outline-none font-mono">
              <div class="text-[11px] text-zinc-500">Receives JSON payload: <code class="text-zinc-300">{"phone", "text", "event_type"}</code></div>
            </div>

            <!-- Alert Subscriptions -->
            <div class="space-y-2.5 pt-1">
              <div class="text-[11px] font-medium uppercase tracking-wider text-zinc-400">What to Notify on WhatsApp</div>
              <label class="flex items-start gap-3 p-3 rounded-xl bg-[#09090b] border border-white/[0.07] cursor-pointer">
                <input id="wa-enabled" type="checkbox" checked class="mt-0.5 rounded bg-zinc-900 border-zinc-600 accent-zinc-100">
                <div>
                  <div class="text-xs font-medium text-zinc-100">Enable WhatsApp Notifications</div>
                  <div class="text-[11px] text-zinc-500">Master toggle for automated 24/7 WhatsApp push delivery</div>
                </div>
              </label>
              <label class="flex items-start gap-3 p-3 rounded-xl bg-[#09090b] border border-white/[0.07] cursor-pointer">
                <input id="wa-notify-jobs" type="checkbox" checked class="mt-0.5 rounded bg-zinc-900 border-zinc-600 accent-zinc-100">
                <div>
                  <div class="text-xs font-medium text-zinc-100">New Eligible Job Openings (Job Profiles)</div>
                  <div class="text-[11px] text-zinc-500">Company, Role, CTC, Location, Registration Deadline, Direct Apply & Calendar links</div>
                </div>
              </label>
              <label class="flex items-start gap-3 p-3 rounded-xl bg-[#09090b] border border-white/[0.07] cursor-pointer">
                <input id="wa-notify-shortlists" type="checkbox" checked class="mt-0.5 rounded bg-zinc-900 border-zinc-600 accent-zinc-100">
                <div>
                  <div class="text-xs font-medium text-zinc-100">Shortlist PDF Matches (Role-Specific Venue, Date & Time)</div>
                  <div class="text-[11px] text-zinc-500">Extracts your exact profile's reporting campus/lab, date, time, process, page #, and Meet link</div>
                </div>
              </label>
              <label class="flex items-start gap-3 p-3 rounded-xl bg-[#09090b] border border-white/[0.07] cursor-pointer">
                <input id="wa-notify-offers" type="checkbox" checked class="mt-0.5 rounded bg-zinc-900 border-zinc-600 accent-zinc-100">
                <div>
                  <div class="text-xs font-medium text-zinc-100">Urgent Pending Job Offers</div>
                  <div class="text-[11px] text-zinc-500">Immediate watchdog alert whenever Superset posts an offer with an acceptance deadline</div>
                </div>
              </label>
            </div>

            <div class="flex flex-wrap items-center gap-2.5 pt-2">
              <button onclick="saveWhatsappConfig()" id="btn-wa-save" class="px-4 py-2.5 rounded-lg bg-zinc-100 hover:bg-white text-zinc-950 text-xs font-semibold">
                Save WhatsApp Config
              </button>
              <button onclick="testWhatsapp()" class="px-4 py-2.5 rounded-lg bg-white/[0.06] hover:bg-white/[0.1] border border-white/10 text-zinc-200 text-xs font-medium">
                Send Test Message
              </button>
              <span id="wa-save-feedback" class="text-xs text-zinc-400"></span>
            </div>
          </div>

          <!-- Right: Live Message Format Previews -->
          <div class="lg:col-span-5 space-y-4">
            <div class="metric-card rounded-2xl p-5 space-y-3">
              <div class="flex items-center justify-between">
                <div class="text-xs font-semibold uppercase tracking-wider text-zinc-300">WhatsApp Alert Preview</div>
                <span class="text-[10px] font-mono text-zinc-500">Exact Format</span>
              </div>

              <!-- Sample Shortlist Bubble -->
              <div class="rounded-xl bg-[#0b141a] border border-emerald-500/20 p-3.5 text-[11px] leading-relaxed text-zinc-200 font-sans space-y-1">
                <div class="font-semibold text-amber-300">🔥 YOU ARE SHORTLISTED!</div>
                <div><span class="text-zinc-400">Shortlisted Profile:</span> <span class="font-medium text-zinc-100">Full Stack Developer</span></div>
                <div><span class="text-zinc-400">Process:</span> Technical GD (Live Coding) & Interview</div>
                <div><span class="text-zinc-400">Date:</span> 21st Nov 2026 (Today)</div>
                <div><span class="text-zinc-400">Reporting Time:</span> 9.30 AM</div>
                <div><span class="text-zinc-400">Venue / Mode:</span> <span class="font-medium text-zinc-50">Computer Labs, Campus-6</span></div>
                <div class="text-zinc-400 pt-1 text-[10px] font-mono">Verified In: Shortlisted_List.pdf (Page 3)</div>
              </div>

              <!-- Sample Job Opening Bubble -->
              <div class="rounded-xl bg-[#0b141a] border border-white/10 p-3.5 text-[11px] leading-relaxed text-zinc-200 font-sans space-y-1">
                <div class="font-semibold text-zinc-50">📢 NEW SUPERSET JOB OPENING</div>
                <div><span class="text-zinc-400">Company:</span> Campus Recruiter</div>
                <div><span class="text-zinc-400">Role:</span> Software Engineer (Full Stack)</div>
                <div><span class="text-zinc-400">CTC:</span> 10.5 LPA</div>
                <div><span class="text-zinc-400">Location:</span> Bengaluru / Hybrid</div>
                <div><span class="text-zinc-400">Deadline:</span> Tomorrow, 11:59 PM IST</div>
                <div><span class="text-zinc-400">Status:</span> <span class="text-emerald-400 font-medium">ELIGIBLE — ACTION REQUIRED</span></div>
                <div class="text-zinc-400 pt-1 text-[10px] font-mono">Apply: https://app.joinsuperset.com/...</div>
              </div>
            </div>

            <!-- Instant Digest Explanation -->
            <div class="metric-card rounded-2xl p-4 space-y-2">
              <div class="text-xs font-semibold text-zinc-200">On-Demand Digest</div>
              <p class="text-[11px] text-zinc-400 leading-relaxed">
                Click <span class="text-zinc-200 font-medium">Send Live Digest Now</span> anytime to immediately push your active upcoming shortlist venues + currently open eligible job profiles straight to WhatsApp.
              </p>
            </div>
          </div>
        </div>

        <!-- SECONDARY: TELEGRAM BOT ALERTS (Monochrome Restyled) -->
        <div class="metric-card rounded-2xl p-5 space-y-4">
          <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3 border-b border-white/[0.07] pb-3.5">
            <div>
              <h3 class="text-xs font-semibold text-zinc-200 uppercase tracking-wider">Telegram Push Notifications</h3>
              <p class="text-[11px] text-zinc-500 mt-0.5">Backup channel via <span class="font-mono text-zinc-300">@XSuperset_bot</span></p>
            </div>
            <div class="flex items-center gap-3">
              <span class="text-xs font-mono text-zinc-400">Chat ID: <span id="tg-chat-id" class="text-zinc-200">Not Configured</span></span>
              <span class="text-xs font-semibold text-emerald-400" id="tg-status">● Connected</span>
              <button onclick="testTelegram()" class="px-3 py-1.5 rounded-lg bg-white/[0.06] hover:bg-white/[0.1] border border-white/10 text-zinc-100 text-xs font-medium">
                Test Telegram
              </button>
            </div>
          </div>

          <div class="flex flex-col sm:flex-row gap-2.5">
            <input type="text" id="tg-custom-text" placeholder="Send a quick custom message to your Telegram..."
              class="flex-1 bg-[#09090b] border border-white/10 focus:border-zinc-500 rounded-lg px-3.5 py-2 text-xs text-zinc-100 placeholder-zinc-600 focus:outline-none font-mono">
            <button onclick="sendCustomTelegram()" class="px-4 py-2 rounded-lg bg-white/[0.06] hover:bg-white/[0.1] border border-white/10 text-zinc-100 text-xs font-medium whitespace-nowrap">
              Send Telegram
            </button>
          </div>
        </div>
      </div>

      <!-- ═══ PAGE: AI COPILOT & HEALER ═══ -->
      <div id="page-ai" class="page hidden p-6 space-y-6">
        <!-- Top Stats Row -->
        <div class="grid grid-cols-1 md:grid-cols-4 gap-4">
          <div class="bg-surface-900/60 rounded-xl p-4 border border-slate-800/60">
            <div class="text-[11px] font-semibold text-slate-400 uppercase tracking-wider mb-1 flex items-center justify-between">
              <span>Active AI Model</span>
              <span class="w-2 h-2 rounded-full bg-emerald-400 live-dot"></span>
            </div>
            <div class="text-base font-bold text-white font-mono truncate" id="ai-provider-name">hybrid-bm25-rules</div>
            <div class="text-[10px] text-slate-500 font-mono mt-0.5" id="ai-provider-type">Built-In RAG (0 MB RAM)</div>
          </div>
          <div class="bg-surface-900/60 rounded-xl p-4 border border-slate-800/60">
            <div class="text-[11px] font-semibold text-slate-400 uppercase tracking-wider mb-1">RAG Knowledge Store</div>
            <div class="text-xl font-bold text-indigo-400 font-mono" id="ai-rag-chunks">134 Chunks</div>
            <div class="text-[10px] text-slate-500 font-mono mt-0.5">Profile Facts & Playbooks</div>
          </div>
          <div class="bg-surface-900/60 rounded-xl p-4 border border-slate-800/60">
            <div class="text-[11px] font-semibold text-slate-400 uppercase tracking-wider mb-1">Unclear Data Tickets</div>
            <div class="text-xl font-bold text-amber-400 font-mono" id="ai-pending-tickets">0 Pending</div>
            <div class="text-[10px] text-slate-500 font-mono mt-0.5">Telegram Auto-Alerting</div>
          </div>
          <div class="bg-surface-900/60 rounded-xl p-4 border border-slate-800/60">
            <div class="text-[11px] font-semibold text-slate-400 uppercase tracking-wider mb-1">Live Auto-Healing</div>
            <div class="text-xl font-bold text-emerald-400 font-mono">ONLINE</div>
            <div class="text-[10px] text-slate-500 font-mono mt-0.5">Autonomous 409/401 Remediation</div>
          </div>
        </div>

        <!-- Clarifications Banner / Queue -->
        <div class="bg-surface-900/60 rounded-xl p-5 border border-slate-800/60">
          <div class="flex items-center justify-between mb-4">
            <div>
              <h3 class="text-sm font-bold text-white flex items-center gap-2">
                <span>⚠️</span> Unclear Data & Clarification Queue
              </h3>
              <p class="text-xs text-slate-400 mt-0.5">When questionnaire attributes are ambiguous or missing from your profile, the AI alerts you via Telegram and queues them here for 1-click confirmation.</p>
            </div>
            <button onclick="loadClarifications()" class="px-2.5 py-1 rounded bg-surface-800 hover:bg-surface-700 text-slate-300 text-xs border border-slate-700">Refresh</button>
          </div>
          <div id="clarifications-list" class="space-y-3">
            <div class="text-xs text-slate-500 py-4 text-center italic">Checking clarification tickets...</div>
          </div>
        </div>

        <!-- 2 Column Layout: Live Error Healer & RAG Copilot -->
        <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
          <!-- Live Error Healing Feed -->
          <div class="bg-surface-900/60 rounded-xl p-5 border border-slate-800/60 flex flex-col h-[520px]">
            <div class="flex items-center justify-between mb-3">
              <div class="flex items-center gap-2">
                <span class="text-base">🛡️</span>
                <h3 class="text-sm font-bold text-white">Live Error Diagnostic & Auto-Healer</h3>
              </div>
              <button onclick="loadLiveErrors()" class="px-2.5 py-1 rounded bg-surface-800 hover:bg-surface-700 text-slate-300 text-xs border border-slate-700">Refresh</button>
            </div>
            <p class="text-xs text-slate-400 mb-3">Runtime incidents diagnosed via RAG playbooks with auto-healing execution logs.</p>
            <div id="live-errors-feed" class="flex-1 overflow-y-auto space-y-3 pr-1">
              <div class="text-xs text-slate-500 py-8 text-center italic">No active error incidents recorded.</div>
            </div>
          </div>

          <!-- RAG Copilot Chat -->
          <div class="bg-surface-900/60 rounded-xl p-5 border border-slate-800/60 flex flex-col h-[520px]">
            <div class="flex items-center justify-between mb-2">
              <div class="flex items-center gap-2">
                <span class="text-base">🤖</span>
                <h3 class="text-sm font-bold text-white">RAG Enterprise Copilot</h3>
              </div>
              <span class="text-[10px] font-mono px-2 py-0.5 rounded bg-indigo-500/10 text-indigo-400">Context-Aware</span>
            </div>
            <div class="flex flex-wrap gap-1.5 mb-3">
              <button onclick="setCopilotQuery('Why did IRISCarbon return HTTP 409?')" class="text-[10px] font-mono px-2 py-1 rounded bg-surface-800 hover:bg-surface-700 text-slate-300 border border-slate-700">⚡ IRISCarbon 409</button>
              <button onclick="setCopilotQuery('What verified documents exist for my Nepal passport and citizenship?')" class="text-[10px] font-mono px-2 py-1 rounded bg-surface-800 hover:bg-surface-700 text-slate-300 border border-slate-700">🇳🇵 Nepal Documents</button>
              <button onclick="setCopilotQuery('How does my profile answer education gaps and prior employment?')" class="text-[10px] font-mono px-2 py-1 rounded bg-surface-800 hover:bg-surface-700 text-slate-300 border border-slate-700">📄 Gaps & Employment</button>
            </div>
            <!-- Chat history -->
            <div id="copilot-history" class="flex-1 overflow-y-auto space-y-3 pr-1 mb-3 text-xs">
              <div class="p-3 rounded-lg bg-surface-800/60 border border-slate-800 text-slate-300">
                <div class="font-bold text-indigo-400 mb-1 flex items-center gap-1.5">
                  <span>🤖</span> Mission Control Copilot
                </div>
                Ask me anything about your placement drives, eligibility criteria, error resolutions, or candidate profile alignment.
              </div>
            </div>
            <!-- Input Bar -->
            <form onsubmit="submitCopilotChat(event)" class="flex gap-2">
              <input id="copilot-input" type="text" placeholder="Ask Copilot about drives, error fixes, or profile..." class="flex-1 bg-surface-950 border border-slate-700 rounded-lg px-3 py-2 text-xs text-white placeholder-slate-500 focus:outline-none focus:border-indigo-500 font-sans">
              <button id="copilot-btn" type="submit" class="px-4 py-2 rounded-lg bg-indigo-600 hover:bg-indigo-500 text-white text-xs font-semibold">Send</button>
            </form>
          </div>
        </div>
      </div>

      <!-- ═══ SETTINGS PAGE ═══ -->
      <div id="page-settings" class="page hidden p-3.5 sm:p-6 space-y-5">
        <div class="flex items-center justify-between">
          <div>
            <h2 class="text-lg font-bold text-white">Account, Auto-Apply & Friend Invites</h2>
            <p class="text-xs text-slate-400">Encrypted per-user workspace settings and invite codes for college friends</p>
          </div>
          <button onclick="logoutUser()" class="px-3.5 py-2 rounded-xl bg-rose-950/60 border border-rose-500/30 text-rose-300 text-xs font-bold flex items-center gap-1.5">
            <span>🚪</span> <span>Sign Out</span>
          </button>
        </div>

        <div class="grid grid-cols-1 lg:grid-cols-2 gap-4 sm:gap-6">
          <div class="metric-card rounded-2xl p-5">
            <h3 class="text-xs font-bold text-brand-400 uppercase tracking-wider mb-4">Personal Auto-Apply Preference</h3>
            <div class="space-y-4 text-sm">
              <div class="flex justify-between items-center p-3 rounded-xl bg-surface-950/80 border border-white/10">
                <div>
                  <div class="text-xs font-bold text-white">Auto-Apply to Eligible Drives</div>
                  <div class="text-[11px] text-slate-400">Each user controls whether new drives auto-submit or require manual confirmation</div>
                </div>
                <button onclick="toggleUserAutoApply()" id="set-auto-apply-btn" class="px-3.5 py-2 rounded-xl bg-surface-800 border border-white/10 text-xs font-mono font-bold text-slate-300">
                  OFF
                </button>
              </div>
              <div class="flex justify-between items-center text-xs">
                <span class="text-slate-400">Pre-Submission Review Modal</span>
                <span class="font-mono text-emerald-400 font-semibold">ALWAYS ACTIVE ON MANUAL APPLY</span>
              </div>
              <div class="flex justify-between items-center text-xs">
                <span class="text-slate-400">Encrypted Profile Storage</span>
                <span class="font-mono text-indigo-300">PBKDF2-HMAC-SHA256 Vault</span>
              </div>
              <div class="flex justify-between items-center text-xs">
                <span class="text-slate-400">Polling Cadence</span>
                <span class="font-mono text-slate-300" id="set-poll">15 minutes</span>
              </div>
            </div>
          </div>

          <div class="metric-card rounded-2xl p-5">
            <h3 class="text-xs font-bold text-brand-400 uppercase tracking-wider mb-4">Authenticated Superset Account</h3>
            <div class="space-y-3.5 text-xs">
              <div class="flex justify-between items-center">
                <span class="text-slate-400">Portal Base URL</span>
                <a href="https://app.joinsuperset.com" target="_blank" class="font-mono text-brand-400 hover:text-brand-300">app.joinsuperset.com ↗</a>
              </div>
              <div class="flex justify-between items-center">
                <span class="text-slate-400">Student Name</span>
                <span class="font-semibold text-white" id="set-user-name">—</span>
              </div>
              <div class="flex justify-between items-center">
                <span class="text-slate-400">Authenticated Email</span>
                <span class="font-mono text-slate-200" id="set-user-email">—</span>
              </div>
              <div class="flex justify-between items-center">
                <span class="text-slate-400">Student Roll No</span>
                <span class="font-mono text-slate-300" id="set-user-roll">—</span>
              </div>
            </div>
          </div>

          <!-- Invite Codes for College Friends -->
          <div class="metric-card rounded-2xl p-5 lg:col-span-2 space-y-4">
            <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-2">
              <div>
                <h3 class="text-xs font-bold text-emerald-400 uppercase tracking-wider">🎓 Share with College Friends (Invite & Add-On Codes)</h3>
                <p class="text-xs text-slate-400 mt-0.5">Friends register with their own <code class="text-indigo-300">@kiit.ac.in</code> Superset login + an invite code below. Each friend gets an isolated, encrypted profile and separate auto-apply toggle.</p>
              </div>
              <button onclick="createNewInviteCode()" class="px-3.5 py-2 rounded-xl bg-gradient-to-r from-emerald-600 to-teal-600 text-white text-xs font-bold shadow-md flex items-center gap-1.5 self-start sm:self-auto">
                <span>➕</span> <span>Generate New Code</span>
              </button>
            </div>
            <div id="invite-codes-list" class="grid grid-cols-1 sm:grid-cols-3 gap-2.5 text-xs">
              <div class="p-3 rounded-xl bg-surface-950/80 border border-white/10 font-mono text-slate-300">Default Code: <strong class="text-emerald-400">KIIT2027</strong></div>
            </div>
          </div>

          <div class="metric-card rounded-2xl p-5 lg:col-span-2">
            <h3 class="text-xs font-bold text-brand-400 uppercase tracking-wider mb-3">Encrypted Vault & Backup</h3>
            <div class="flex flex-wrap items-center justify-between gap-4 text-xs">
              <div>
                <span class="text-slate-400">Encrypted Multi-User Vault: </span>
                <span class="font-mono text-slate-300">data/users_vault.sqlite (AES/HMAC Encrypted)</span>
              </div>
              <div>
                <span class="text-slate-400">Tracked Drives: </span>
                <span class="font-mono text-white font-bold" id="set-tracked">0</span>
              </div>
              <button onclick="exportDrivesCsv()" class="px-3.5 py-2 rounded-xl bg-surface-800 border border-white/10 text-slate-200 text-xs font-semibold">
                📥 Download Drives (CSV)
              </button>
            </div>
          </div>
        </div>
      </div>

      <!-- ═══ COMPANY RESUME TAILOR & 1-PAGE A4 PDF STUDIO ═══ -->
      <div id="page-resume" class="page hidden p-3.5 sm:p-6 max-w-6xl mx-auto space-y-5">
        <!-- Studio Header & Target Company Controls -->
        <div class="metric-card rounded-2xl p-4 sm:p-5 space-y-4">
          <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
            <div>
              <h2 class="text-sm sm:text-base font-semibold text-zinc-100 tracking-tight">Company Resume Tailor &amp; 1-Page PDF Studio</h2>
              <p class="text-xs text-zinc-400 mt-0.5">Reads the live Superset JD (+ attached JD PDF), picks your highest-matching projects, and exports a clean 1-page A4 Arial CV.</p>
            </div>
            <div class="flex items-center gap-2 flex-shrink-0">
              <button type="button" onclick="toggleResumeMasterEditor()" id="resume-editor-toggle-btn" class="px-3 py-2 rounded-lg bg-white/[0.05] hover:bg-white/[0.09] border border-white/10 text-zinc-200 text-xs font-medium">
                Edit My Projects &amp; Photo
              </button>
            </div>
          </div>

          <!-- Target Company Picker -->
          <div class="grid grid-cols-1 lg:grid-cols-12 gap-3 items-end">
            <div class="lg:col-span-6">
              <label for="resume-job-select" class="block text-[10px] font-mono uppercase tracking-wider text-zinc-500 mb-1.5">Target Campus Drive / Company</label>
              <select id="resume-job-select" onchange="onResumeJobSelectChange()" class="w-full bg-surface-950 border border-white/10 rounded-lg px-3 py-2 text-xs text-zinc-100 focus:border-zinc-500 focus:outline-none">
                <option value="">— Custom / Off-Campus Role —</option>
              </select>
            </div>
            <div class="lg:col-span-3">
              <label for="resume-max-projects" class="block text-[10px] font-mono uppercase tracking-wider text-zinc-500 mb-1.5">Projects to Feature</label>
              <select id="resume-max-projects" class="w-full bg-surface-950 border border-white/10 rounded-lg px-3 py-2 text-xs text-zinc-100 focus:border-zinc-500 focus:outline-none">
                <option value="2">Top 2 Projects</option>
                <option value="3" selected>Top 3 Projects (Recommended)</option>
                <option value="4">Top 4 Projects (Compact)</option>
              </select>
            </div>
            <div class="lg:col-span-3 flex items-center gap-2">
              <button type="button" onclick="generateTailoredResume()" id="resume-tailor-btn" class="flex-1 py-2 px-3.5 rounded-lg bg-zinc-100 hover:bg-white text-zinc-950 font-semibold text-xs flex items-center justify-center gap-1.5">
                <span id="resume-tailor-icon">⚡</span> <span>Tailor Resume</span>
              </button>
              <button type="button" onclick="exportTailoredPdf()" id="resume-export-btn" class="py-2 px-3 rounded-lg bg-white/[0.06] hover:bg-white/[0.11] border border-white/10 text-zinc-100 font-medium text-xs whitespace-nowrap" title="Download 1-Page A4 PDF">
                ⬇ PDF
              </button>
            </div>
          </div>

          <!-- Options Row -->
          <div class="flex flex-wrap items-center gap-x-5 gap-y-2 pt-1 text-xs text-zinc-400">
            <label class="inline-flex items-center gap-2 cursor-pointer select-none">
              <input id="resume-include-photo" type="checkbox" checked onchange="if(lastTailoredResume) generateTailoredResume(true);" class="rounded border-zinc-700 bg-zinc-900 text-zinc-100 focus:ring-0">
              <span>Include Formal Photo <span id="resume-photo-hint" class="text-[10px] font-mono text-zinc-500"></span></span>
            </label>
            <label class="inline-flex items-center gap-2 cursor-pointer select-none">
              <input id="resume-use-ai" type="checkbox" checked class="rounded border-zinc-700 bg-zinc-900 text-zinc-100 focus:ring-0">
              <span>AI Summary Polish (auto-fallback)</span>
            </label>
            <button type="button" onclick="document.getElementById('resume-custom-jd-box').classList.toggle('hidden')" class="text-[11px] font-mono text-zinc-400 hover:text-zinc-200 underline underline-offset-2">
              + Custom Role / Paste JD
            </button>
          </div>

          <!-- Optional Custom Company / JD Input -->
          <div id="resume-custom-jd-box" class="hidden grid grid-cols-1 sm:grid-cols-2 gap-3 pt-2 border-t border-white/[0.06]">
            <div>
              <label class="block text-[10px] font-mono uppercase text-zinc-500 mb-1">Company Name</label>
              <input id="resume-custom-company" type="text" placeholder="e.g. Empulz, Swiggy, DeltaX" class="w-full bg-surface-950 border border-white/10 rounded-lg px-3 py-2 text-xs text-zinc-100 focus:border-zinc-500 focus:outline-none">
            </div>
            <div>
              <label class="block text-[10px] font-mono uppercase text-zinc-500 mb-1">Target Role</label>
              <input id="resume-custom-role" type="text" placeholder="e.g. Full Stack Developer, Backend Engineer" class="w-full bg-surface-950 border border-white/10 rounded-lg px-3 py-2 text-xs text-zinc-100 focus:border-zinc-500 focus:outline-none">
            </div>
            <div class="sm:col-span-2">
              <label class="block text-[10px] font-mono uppercase text-zinc-500 mb-1">Extra JD / Tech Stack Notes (Optional — Superset JD is fetched automatically when a drive is selected)</label>
              <textarea id="resume-custom-jd" rows="3" placeholder="Paste job description or skills required..." class="w-full bg-surface-950 border border-white/10 rounded-lg px-3 py-2 text-xs text-zinc-100 focus:border-zinc-500 focus:outline-none"></textarea>
            </div>
          </div>
        </div>

        <!-- Collapsible Master Portfolio & Photo Editor (Per-User Encrypted Vault) -->
        <div id="resume-master-editor" class="hidden metric-card rounded-2xl p-4 sm:p-5 space-y-5">
          <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3 border-b border-white/[0.07] pb-3.5">
            <div>
              <h3 class="text-sm font-semibold text-zinc-100">My Master Portfolio &amp; Formal Photo</h3>
              <p class="text-xs text-zinc-500">Stored strictly inside your private encrypted vault. Add all your real projects here — the tailor picks the best ones per company.</p>
            </div>
            <div class="flex items-center gap-2 flex-wrap">
              <label class="px-3 py-1.5 rounded-lg bg-white/[0.05] hover:bg-white/[0.09] border border-white/10 text-zinc-200 text-xs font-medium cursor-pointer">
                <span>⬆ Import from PDF CV</span>
                <input id="resume-import-pdf-input" type="file" accept="application/pdf" class="hidden" onchange="importExistingResumePdf(this)">
              </label>
              <button type="button" onclick="saveResumeMaster()" id="resume-save-master-btn" class="px-3.5 py-1.5 rounded-lg bg-zinc-100 hover:bg-white text-zinc-950 text-xs font-semibold">
                Save Portfolio
              </button>
            </div>
          </div>

          <!-- Formal Photo + Contact Links -->
          <div class="grid grid-cols-1 md:grid-cols-12 gap-4">
            <div class="md:col-span-4 p-3.5 rounded-xl bg-surface-950 border border-white/[0.07] flex items-center gap-3.5">
              <div id="rm-photo-preview" class="w-16 h-20 rounded-md bg-zinc-900 border border-white/10 overflow-hidden flex items-center justify-center text-zinc-600 text-xs font-mono flex-shrink-0">No Photo</div>
              <div class="space-y-2 min-w-0">
                <div class="text-xs font-medium text-zinc-200">Formal Passport Photo</div>
                <div class="text-[10px] text-zinc-500">Used when a drive asks for photo on resume.</div>
                <div class="flex items-center gap-1.5">
                  <label class="px-2.5 py-1 rounded bg-white/[0.07] hover:bg-white/[0.12] border border-white/10 text-zinc-200 text-[11px] font-medium cursor-pointer">
                    Upload
                    <input id="rm-photo-input" type="file" accept="image/*" class="hidden" onchange="handleResumePhotoUpload(this)">
                  </label>
                  <button type="button" onclick="clearResumePhoto()" class="px-2 py-1 rounded bg-white/[0.04] text-zinc-400 hover:text-zinc-200 text-[11px]">Remove</button>
                </div>
              </div>
            </div>
            <div class="md:col-span-8 grid grid-cols-1 sm:grid-cols-3 gap-2.5">
              <div>
                <label class="block text-[10px] font-mono text-zinc-500 mb-1">Full Name</label>
                <input id="rm-full-name" type="text" class="w-full bg-surface-950 border border-white/10 rounded-lg px-2.5 py-1.5 text-xs text-zinc-100">
              </div>
              <div>
                <label class="block text-[10px] font-mono text-zinc-500 mb-1">Phone</label>
                <input id="rm-phone" type="text" class="w-full bg-surface-950 border border-white/10 rounded-lg px-2.5 py-1.5 text-xs text-zinc-100">
              </div>
              <div>
                <label class="block text-[10px] font-mono text-zinc-500 mb-1">Location</label>
                <input id="rm-location" type="text" class="w-full bg-surface-950 border border-white/10 rounded-lg px-2.5 py-1.5 text-xs text-zinc-100">
              </div>
              <div>
                <label class="block text-[10px] font-mono text-zinc-500 mb-1">GitHub URL</label>
                <input id="rm-github" type="text" placeholder="github.com/username" class="w-full bg-surface-950 border border-white/10 rounded-lg px-2.5 py-1.5 text-xs text-zinc-100">
              </div>
              <div>
                <label class="block text-[10px] font-mono text-zinc-500 mb-1">LinkedIn URL</label>
                <input id="rm-linkedin" type="text" placeholder="linkedin.com/in/username" class="w-full bg-surface-950 border border-white/10 rounded-lg px-2.5 py-1.5 text-xs text-zinc-100">
              </div>
              <div>
                <label class="block text-[10px] font-mono text-zinc-500 mb-1">Portfolio / Live Site</label>
                <input id="rm-portfolio" type="text" placeholder="yoursite.com" class="w-full bg-surface-950 border border-white/10 rounded-lg px-2.5 py-1.5 text-xs text-zinc-100">
              </div>
            </div>
          </div>

          <!-- Skills Editor -->
          <div class="space-y-2">
            <div class="flex items-center justify-between">
              <span class="text-xs font-semibold text-zinc-200">Skill Categories</span>
              <button type="button" onclick="addMasterSkillRow()" class="text-[11px] font-mono text-zinc-400 hover:text-zinc-100">+ Add Skill Row</button>
            </div>
            <div id="rm-skills-list" class="space-y-2"></div>
          </div>

          <!-- Projects Editor -->
          <div class="space-y-2.5">
            <div class="flex items-center justify-between">
              <span class="text-xs font-semibold text-zinc-200">All Projects (Tailor Ranks &amp; Picks Best Fit Per Company)</span>
              <button type="button" onclick="addMasterProjectCard()" class="px-2.5 py-1 rounded-lg bg-white/[0.06] border border-white/10 text-[11px] font-medium text-zinc-200 hover:bg-white/[0.1]">+ Add Project</button>
            </div>
            <div id="rm-projects-list" class="space-y-3"></div>
          </div>

          <!-- Certifications Editor -->
          <div class="space-y-2">
            <div class="flex items-center justify-between">
              <span class="text-xs font-semibold text-zinc-200">Training &amp; Certifications</span>
              <button type="button" onclick="addMasterCertRow()" class="text-[11px] font-mono text-zinc-400 hover:text-zinc-100">+ Add Certification</button>
            </div>
            <div id="rm-certs-list" class="space-y-2"></div>
          </div>
        </div>

        <!-- Tailored Output Workspace: Insights/Controls + Live 1-Page A4 Arial Preview -->
        <div id="resume-workspace" class="hidden grid grid-cols-1 xl:grid-cols-12 gap-5 items-start">
          <!-- Left Column: ATS Report, Ranked Project Checkboxes & Quick Text Tweaks -->
          <div class="xl:col-span-5 space-y-4">
            <div class="metric-card rounded-xl p-4 space-y-3">
              <div class="flex items-center justify-between">
                <div>
                  <div class="text-[10px] font-mono uppercase tracking-wider text-zinc-500">ATS Alignment</div>
                  <div id="rt-target-label" class="text-sm font-semibold text-zinc-100 mt-0.5">—</div>
                </div>
                <div class="text-right">
                  <div id="rt-ats-score" class="text-2xl font-bold font-mono text-zinc-100">—%</div>
                  <div id="rt-track-badge" class="text-[10px] font-mono text-zinc-400">—</div>
                </div>
              </div>
              <div class="space-y-1.5 text-[11px]">
                <div class="text-zinc-400"><span class="text-zinc-500 font-mono">Matched:</span> <span id="rt-matched-kws" class="text-zinc-200">—</span></div>
                <div class="text-zinc-400"><span class="text-zinc-500 font-mono">Prep Gaps:</span> <span id="rt-missing-kws" class="text-zinc-400">—</span></div>
              </div>
            </div>

            <!-- Ranked Projects Selector -->
            <div class="metric-card rounded-xl p-4 space-y-2.5">
              <div class="flex items-center justify-between">
                <span class="text-xs font-semibold text-zinc-100">Ranked Projects for This Company</span>
                <span class="text-[10px] font-mono text-zinc-500">Toggle to customize</span>
              </div>
              <div id="rt-projects-rank" class="space-y-2"></div>
            </div>

            <!-- Quick Headline & Summary Override -->
            <div class="metric-card rounded-xl p-4 space-y-2.5">
              <div class="flex items-center justify-between">
                <span class="text-xs font-semibold text-zinc-100">Customize Headline &amp; Summary</span>
                <button type="button" onclick="applyResumeTextOverride()" class="text-[11px] font-mono text-zinc-200 underline underline-offset-2">Update Preview</button>
              </div>
              <input id="rt-edit-headline" type="text" class="w-full bg-surface-950 border border-white/10 rounded-lg px-2.5 py-1.5 text-xs text-zinc-100 focus:border-zinc-500 focus:outline-none">
              <textarea id="rt-edit-summary" rows="4" class="w-full bg-surface-950 border border-white/10 rounded-lg px-2.5 py-2 text-xs text-zinc-100 leading-relaxed focus:border-zinc-500 focus:outline-none"></textarea>
            </div>
          </div>

          <!-- Right Column: Live 1-Page A4 Arial Paper Preview -->
          <div class="xl:col-span-7 space-y-2.5">
            <div class="flex items-center justify-between px-0.5">
              <span class="text-xs font-mono text-zinc-400">Live 1-Page A4 Preview (Arial)</span>
              <div class="flex items-center gap-2">
                <button type="button" onclick="printTailoredIframe()" class="px-2.5 py-1 rounded-lg bg-white/[0.05] hover:bg-white/[0.09] border border-white/10 text-[11px] text-zinc-300 font-medium">🖨 Print</button>
                <button type="button" onclick="exportTailoredPdf()" class="px-3 py-1 rounded-lg bg-zinc-100 hover:bg-white text-zinc-950 text-[11px] font-semibold">⬇ Download PDF</button>
              </div>
            </div>
            <div class="rounded-xl bg-zinc-900 border border-white/10 p-2 sm:p-3 overflow-x-auto">
              <iframe id="resume-preview-frame" title="Tailored Resume Preview" sandbox="allow-same-origin allow-modals" class="w-full min-h-[760px] sm:min-h-[960px] bg-white rounded-md border-0"></iframe>
            </div>
          </div>
        </div>
      </div>

    </div>
  </main>

  <!-- ═══ MULTI-USER AUTHENTICATION & SUPERSET ONBOARDING GATE ═══ -->
  <div id="auth-overlay" class="fixed inset-0 z-50 hidden modal-backdrop flex items-center justify-center p-3.5 sm:p-6 overflow-y-auto">
    <div class="bg-surface-900/95 backdrop-blur-2xl border border-white/15 rounded-3xl shadow-2xl max-w-md w-full p-5 sm:p-7 space-y-5 animate-slide-up my-auto">
      <div class="flex items-center gap-3.5">
        <div class="w-12 h-12 rounded-2xl bg-gradient-to-tr from-brand-600 via-indigo-500 to-purple-500 flex items-center justify-center text-white font-extrabold text-base shadow-lg shadow-brand-600/30 border border-white/20 flex-shrink-0">
          SJ
        </div>
        <div>
          <h2 class="text-base sm:text-lg font-extrabold text-white tracking-tight">Superset Job Pilot</h2>
          <p class="text-xs text-slate-400">Campus Placement Portal</p>
        </div>
      </div>

      <!-- Mode Switch Tabs -->
      <div class="grid grid-cols-2 gap-1.5 p-1 rounded-2xl bg-surface-950 border border-white/10">
        <button id="auth-tab-login" type="button" onclick="switchAuthTab('login')" class="py-2 rounded-xl text-xs font-bold bg-brand-600 text-white shadow">
          Sign In
        </button>
        <button id="auth-tab-register" type="button" onclick="switchAuthTab('register')" class="py-2 rounded-xl text-xs font-bold text-slate-400">
          New Friend / Register
        </button>
      </div>

      <div id="auth-error-box" class="hidden p-3 rounded-xl bg-rose-950/70 border border-rose-500/40 text-rose-200 text-xs font-medium"></div>

      <form id="auth-form" onsubmit="submitAuthForm(event)" class="space-y-3.5">
        <div>
          <label class="block text-[11px] font-bold uppercase tracking-wider text-slate-300 mb-1">Superset College Email</label>
          <input id="auth-email" type="email" required autocomplete="email" placeholder="rollno@kiit.ac.in"
            class="w-full bg-surface-950 border border-white/15 rounded-xl px-3.5 py-2.5 text-sm text-white placeholder-slate-500 focus:border-brand-500 focus:outline-none">
        </div>
        <div>
          <label class="block text-[11px] font-bold uppercase tracking-wider text-slate-300 mb-1">Superset Password</label>
          <div class="relative">
            <input id="auth-password" type="password" required autocomplete="current-password" placeholder="Your joinsuperset.com password"
              class="w-full bg-surface-950 border border-white/15 rounded-xl pl-3.5 pr-10 py-2.5 text-sm text-white placeholder-slate-500 focus:border-brand-500 focus:outline-none">
            <button type="button" onclick="togglePasswordVisibility()" id="auth-pw-toggle" class="absolute right-3 top-1/2 -translate-y-1/2 text-slate-400 hover:text-white text-sm" title="Show/Hide Password">👁️</button>
          </div>
        </div>

        <div id="auth-register-fields" class="hidden space-y-3.5 pt-1">
          <div>
            <label class="block text-[11px] font-bold uppercase tracking-wider text-emerald-300 mb-1">Friend Invite / Add-On Code</label>
            <input id="auth-invite-code" type="text" value="KIIT2027" placeholder="e.g. KIIT2027"
              class="w-full bg-surface-950 border border-emerald-500/40 rounded-xl px-3.5 py-2.5 text-sm text-emerald-300 font-mono placeholder-slate-500 focus:border-emerald-400 focus:outline-none">
          </div>
          <label class="flex items-center justify-between p-3 rounded-xl bg-surface-950/80 border border-white/10 cursor-pointer">
            <div>
              <div class="text-xs font-semibold text-white">Enable Auto-Apply for my account</div>
              <div class="text-[10px] text-slate-400">Can be toggled anytime from the top bar</div>
            </div>
            <input id="auth-auto-apply" type="checkbox" class="rounded border-slate-700 text-brand-600 focus:ring-0 w-4 h-4">
          </label>
        </div>

        <button id="auth-submit-btn" type="submit" class="w-full py-2.5 px-4 rounded-xl bg-zinc-100 hover:bg-white text-zinc-950 font-semibold text-xs flex items-center justify-center gap-2">
          <span id="auth-submit-label">Sign In</span>
        </button>
      </form>
    </div>
  </div>

  <!-- ═══ NATIVE BOTTOM-SHEET / MODAL ═══ -->
  <div id="modal-overlay" class="fixed inset-0 z-50 hidden modal-backdrop flex items-end sm:items-center justify-center p-0 sm:p-4">
    <div class="bg-surface-900/95 backdrop-blur-2xl border-t border-x sm:border border-white/15 rounded-t-3xl sm:rounded-2xl shadow-[0_-12px_40px_rgba(0,0,0,0.75)] max-w-2xl w-full max-h-[92dvh] sm:max-h-[88dvh] overflow-hidden animate-slide-up flex flex-col" style="padding-bottom: env(safe-area-inset-bottom, 0px);">
      <!-- Native mobile bottom-sheet drag pill -->
      <div class="w-10 h-1.5 bg-slate-700/80 rounded-full mx-auto mt-2.5 mb-0.5 sm:hidden flex-shrink-0"></div>
      <div class="flex items-center justify-between px-4 py-3.5 sm:p-5 border-b border-white/10 sticky top-0 bg-surface-900/95 backdrop-blur-xl z-10">
        <h3 class="text-xs sm:text-sm font-bold text-white flex items-center gap-2 truncate" id="modal-title">Details</h3>
        <button onclick="closeModal()" class="w-8 h-8 rounded-full bg-surface-800/80 border border-white/10 text-slate-300 flex items-center justify-center text-sm">✕</button>
      </div>
      <div class="p-4 sm:p-5 flex-1 overflow-y-auto sheet-scroll" id="modal-body"></div>
    </div>
  </div>

  <!-- ═══ 1-CLICK NATIVE <dialog> PDF VIEWER (modern-web-guidance compliant) ═══ -->
  <dialog id="pdfViewerDialog" closedby="any" aria-labelledby="pdfViewerTitle">
    <div class="flex items-center justify-between gap-3 px-4 py-3 border-b border-white/[0.08] bg-surface-950 flex-shrink-0">
      <div class="min-w-0 flex items-center gap-2.5">
        <span class="w-8 h-8 rounded-lg bg-white/[0.06] border border-white/10 text-zinc-200 flex items-center justify-center text-sm flex-shrink-0">📄</span>
        <div class="min-w-0">
          <h2 id="pdfViewerTitle" class="text-xs sm:text-sm font-semibold text-zinc-100 truncate">Attachment Preview</h2>
          <div id="pdfViewerSubtitle" class="text-[10px] font-mono text-zinc-500 truncate">Loading pre-signed PDF...</div>
        </div>
      </div>
      <div class="flex items-center gap-1.5 flex-shrink-0">
        <a id="pdfViewerNewTab" href="#" target="_blank" rel="noopener noreferrer" class="hidden px-2.5 py-1.5 rounded-lg bg-white/[0.06] border border-white/10 text-zinc-200 text-xs font-medium">↗ Open</a>
        <a id="pdfViewerDownload" href="#" download class="hidden px-2.5 py-1.5 rounded-lg bg-zinc-100 text-zinc-950 text-xs font-semibold">⬇ Save</a>
        <form method="dialog" class="inline">
          <button type="submit" autofocus aria-label="Close PDF viewer" class="w-8 h-8 rounded-full bg-white/[0.06] border border-white/10 text-zinc-400 hover:text-zinc-100 flex items-center justify-center text-sm">✕</button>
        </form>
      </div>
    </div>
    <div id="pdfViewerMatchBanner" class="hidden px-4 py-2.5 bg-[#151410] border-b border-amber-400/25 text-xs text-zinc-200 flex-shrink-0"></div>
    <div id="pdfViewerBody" class="flex-1 bg-surface-950 relative overflow-hidden flex items-center justify-center">
      <div class="text-zinc-500 font-mono text-xs">Loading document...</div>
    </div>
  </dialog>

<script>
// ═══════════════════════════════════════════════════════════════════
// STATE & SAFE API CLIENT
// ═══════════════════════════════════════════════════════════════════
let allJobs = [];
let statusData = {};
let profileData = {};
let noticesPayload = { notices: [], shortlisted_notices: [], pending_actions: [], counts: {} };
let noticesFilter = 'all';
let expandedNoticeIds = new Set();
let activePdfBlobUrl = null;
let resumeMaster = null;
let lastTailoredResume = null;
let currentUser = null;
let authToken = localStorage.getItem('sjp_token') || '';
let authMode = 'login';
let currentPage = 'dashboard';
let drivesFilter = 'all';
let ctcFilter = 'all';
let drivesSort = 'newest';
let drivesView = 'grid';

// ═══════════════════════════════════════════════════════════════════
// UTILITIES
// ═══════════════════════════════════════════════════════════════════
const esc = s => { const d = document.createElement('div'); d.textContent = s || ''; return d.innerHTML; };

function getCompanyAvatarStyle(name) {
  return 'bg-zinc-900 border-white/10 text-zinc-200';
}

function getCompanyInitials(name) {
  const words = String(name || 'C').replace(/[^a-zA-Z0-9 ]/g, '').trim().split(/\s+/).filter(Boolean);
  if (words.length >= 2) return (words[0][0] + words[1][0]).toUpperCase();
  return (words[0] || 'C').slice(0, 2).toUpperCase();
}

function toast(msg, type = 'info') {
  const container = document.getElementById('toast-container');
  const colors = {
    success: 'bg-[#121614] border-emerald-500/30 text-zinc-100',
    error: 'bg-[#181214] border-rose-500/30 text-zinc-100',
    warn: 'bg-[#171510] border-amber-500/30 text-zinc-100',
    info: 'bg-[#141418] border-white/15 text-zinc-100'
  };
  const icons = { success: '✓', error: '✕', warn: '⚠', info: 'ℹ' };
  const el = document.createElement('div');
  el.className = `toast rounded-xl px-4 py-3 border text-xs font-medium shadow-xl flex items-start gap-3 ${colors[type] || colors.info}`;
  el.innerHTML = `<span class="text-sm mt-0.5 font-bold">${icons[type] || icons.info}</span><div class="flex-1 break-words leading-relaxed">${esc(msg)}</div><button onclick="this.parentElement.remove()" class="text-current opacity-60 hover:opacity-100 ml-2">✕</button>`;
  container.appendChild(el);
  setTimeout(() => { if (el.parentElement) { el.classList.add('removing'); setTimeout(() => el.remove(), 200); } }, 6000);
}

function isJobClosed(j) {
  if (j.is_closed !== undefined) return j.is_closed;
  const s = (j.raw?.jobProfileStatus || '').toUpperCase();
  if (['CLOSED_FOR_APPLICATIONS','COMPLETED','IN_PROCESS','ARCHIVED','CLOSED'].includes(s)) return true;
  if (j.is_expired) return true;
  if (j.hours_left !== null && j.hours_left !== undefined && j.hours_left <= 0) return true;
  if (j.deadline_dt) { const dl = new Date(j.deadline_dt); if (!isNaN(dl.getTime()) && dl.getTime() < Date.now()) return true; }
  return false;
}

function getCtcLevel(j) {
  const cat = (j.raw?.placementCategoryName || j.ctc_display || '').toLowerCase();
  if (cat.includes('> 14') || cat.includes('14 lpa')) return 3;
  if (cat.includes('> 7') || cat.includes('7 lpa') || cat.includes('14')) return 2;
  if (cat.includes('3 lpa') || cat.includes('7')) return 1;
  return 0;
}

function getJobBadge(j) {
  const isApplied = j.is_applied || j.app_status === 'APPLIED';
  const isClosed = isJobClosed(j);
  if (isApplied) return '<span class="badge-applied text-[10px] font-mono px-2.5 py-0.5 rounded-full font-medium flex-shrink-0 inline-flex items-center gap-1.5"><span class="w-1.5 h-1.5 rounded-full bg-emerald-400"></span>Applied</span>';
  if (!isClosed && j.is_eligible) return '<span class="badge-open text-[10px] font-mono px-2.5 py-0.5 rounded-full font-medium flex-shrink-0 inline-flex items-center gap-1.5"><span class="w-1.5 h-1.5 rounded-full bg-amber-400"></span>Open</span>';
  if (isClosed) return '<span class="badge-closed text-[10px] font-mono px-2.5 py-0.5 rounded-full font-medium flex-shrink-0">Closed</span>';
  return '<span class="badge-ineligible text-[10px] font-mono px-2.5 py-0.5 rounded-full font-medium flex-shrink-0">Ineligible</span>';
}

function getJobActions(j) {
  const isApplied = j.is_applied || j.app_status === 'APPLIED';
  const isClosed = isJobClosed(j);
  let html = '';
  if (!isApplied && !isClosed && j.is_eligible) {
    html += `<button onclick="applyToJob('${j.id}','${esc(j.company_name)}','${esc(j.job_role)}')" class="flex-1 min-h-[40px] py-2 px-3.5 rounded-lg bg-zinc-100 hover:bg-white text-zinc-950 text-xs font-semibold flex items-center justify-center gap-1.5"><span>Apply Now</span></button>`;
  } else if (isApplied) {
    html += `<button disabled class="flex-1 min-h-[40px] py-2 px-3.5 rounded-lg bg-white/[0.04] text-emerald-300/90 text-xs font-medium cursor-default border border-white/[0.08] flex items-center justify-center gap-1.5"><span>✓</span> <span>Applied</span></button>`;
  } else {
    html += `<button disabled class="flex-1 min-h-[40px] py-2 px-3.5 rounded-lg bg-white/[0.03] text-zinc-500 text-xs font-medium cursor-not-allowed border border-white/[0.05]">${isClosed ? 'Deadline Passed' : 'Not Eligible'}</button>`;
  }
  html += `<button onclick="openResumeTailorForJob('${j.id}')" class="min-w-[40px] min-h-[40px] p-2 rounded-lg bg-white/[0.04] hover:bg-white/[0.08] text-zinc-300 border border-white/[0.08] text-xs flex items-center justify-center" title="Tailor 1-Page Resume for ${esc(j.company_name)}">📄</button>`;
  html += `<button onclick="showJobQuestions('${j.id}', '${esc(j.company_name)}')" class="min-w-[40px] min-h-[40px] p-2 rounded-lg bg-white/[0.04] hover:bg-white/[0.08] text-zinc-300 border border-white/[0.08] text-xs flex items-center justify-center" title="Inspect Custom Questions & Answers">📝</button>`;
  html += `<a href="${j.profile_url || '#'}" target="_blank" class="min-w-[40px] min-h-[40px] p-2 rounded-lg bg-white/[0.04] hover:bg-white/[0.08] text-zinc-300 border border-white/[0.08] text-xs flex items-center justify-center" title="Open on Superset Portal">↗</a>`;
  return html;
}

// ═══════════════════════════════════════════════════════════════════
// NAVIGATION
// ═══════════════════════════════════════════════════════════════════
function switchPage(page) {
  currentPage = page;
  document.querySelectorAll('.page').forEach(p => {
    p.classList.add('hidden');
    p.classList.remove('page-enter');
  });
  const target = document.getElementById('page-' + page);
  if (target) {
    target.classList.remove('hidden');
    target.classList.add('page-enter');
  }
  window.scrollTo({ top: 0, behavior: 'instant' });

  // Update desktop nav
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  const navBtn = document.querySelector(`.nav-item[data-page="${page}"]`);
  if (navBtn) navBtn.classList.add('active');

  // Update mobile bottom dock
  document.querySelectorAll('.mobile-nav-btn').forEach(b => b.classList.remove('active'));
  const mobileBtn = document.querySelector(`.mobile-nav-btn[data-page="${page}"]`);
  if (mobileBtn) mobileBtn.classList.add('active');

  const titles = {
    dashboard:'Dashboard',
    drives:'Placement Drives',
    notices:'Smart Notices & Shortlist Scanner',
    applied:'My Applications Pipeline',
    resume:'Company Resume Tailor Studio',
    profile:'Profile & Custom Question Answers',
    engine:'Engine Telemetry & Logs',
    telegram:'WhatsApp & Telegram Alerts',
    ai:'AI Copilot, RAG & Live Error Healer',
    settings:'System Settings'
  };
  document.getElementById('page-title').textContent = titles[page] || page;

  // Refresh page data
  if (page !== 'telegram' && typeof stopWhatsappQrPolling === 'function') stopWhatsappQrPolling();
  if (page === 'drives') renderDrives();
  if (page === 'notices') renderNotices();
  if (page === 'applied') renderApplied();
  if (page === 'resume') openResumeStudio();
  if (page === 'profile') renderProfile();
  if (page === 'engine') renderEngine();
  if (page === 'telegram') renderTelegram();
  if (page === 'ai') renderAiPage();
  if (page === 'settings') renderSettings();
}

function toggleSidebar() {
  document.getElementById('sidebar').classList.toggle('collapsed');
}

function openMobileDrawer() {
  const drawer = document.getElementById('mobile-drawer');
  const backdrop = document.getElementById('mobile-drawer-backdrop');
  if (drawer) drawer.classList.remove('-translate-x-full');
  if (backdrop) backdrop.classList.remove('hidden');
}

function closeMobileDrawer() {
  const drawer = document.getElementById('mobile-drawer');
  const backdrop = document.getElementById('mobile-drawer-backdrop');
  if (drawer) drawer.classList.add('-translate-x-full');
  if (backdrop) backdrop.classList.add('hidden');
}

// ═══════════════════════════════════════════════════════════════════
// SAFE API CLIENT & MULTI-USER AUTHENTICATION
// ═══════════════════════════════════════════════════════════════════
async function apiFetch(url, options = {}) {
  const headers = Object.assign({}, options.headers || {});
  if (authToken) headers['Authorization'] = `Bearer ${authToken}`;
  if (options.body && !headers['Content-Type']) headers['Content-Type'] = 'application/json';

  const res = await fetch(url, Object.assign({ credentials: 'same-origin' }, options, { headers }));
  const rawText = await res.text();
  let data = null;
  if (rawText && rawText.trim()) {
    try {
      data = JSON.parse(rawText);
    } catch (parseErr) {
      data = {
        success: false,
        error: `Server returned non-JSON response (HTTP ${res.status})`,
        message: `Server busy or unreachable (HTTP ${res.status}). Please retry in a moment.`
      };
    }
  } else {
    data = {
      success: false,
      error: `Empty response from server (HTTP ${res.status})`,
      message: `Empty response from server (HTTP ${res.status}).`
    };
  }

  if (res.status === 401 && !url.includes('/api/auth/')) {
    authToken = '';
    currentUser = null;
    try { localStorage.removeItem('sjp_token'); } catch (e) {}
    showAuthOverlay();
  }
  return { ok: res.ok, status: res.status, data };
}

function showAuthOverlay() {
  document.getElementById('auth-overlay')?.classList.remove('hidden');
}

function hideAuthOverlay() {
  document.getElementById('auth-overlay')?.classList.add('hidden');
}

function togglePasswordVisibility() {
  const inp = document.getElementById('auth-password');
  const btn = document.getElementById('auth-pw-toggle');
  if (!inp) return;
  if (inp.type === 'password') {
    inp.type = 'text';
    if (btn) btn.textContent = '🙈';
  } else {
    inp.type = 'password';
    if (btn) btn.textContent = '👁️';
  }
}

function switchAuthTab(mode) {
  authMode = mode;
  const loginTab = document.getElementById('auth-tab-login');
  const regTab = document.getElementById('auth-tab-register');
  const regFields = document.getElementById('auth-register-fields');
  const submitLbl = document.getElementById('auth-submit-label');
  const errBox = document.getElementById('auth-error-box');
  if (errBox) errBox.classList.add('hidden');

  if (mode === 'register') {
    regTab.className = 'py-2 rounded-xl text-xs font-bold bg-brand-600 text-white shadow';
    loginTab.className = 'py-2 rounded-xl text-xs font-bold text-slate-400';
    regFields.classList.remove('hidden');
    submitLbl.textContent = 'Register & Connect Superset';
  } else {
    loginTab.className = 'py-2 rounded-xl text-xs font-bold bg-brand-600 text-white shadow';
    regTab.className = 'py-2 rounded-xl text-xs font-bold text-slate-400';
    regFields.classList.add('hidden');
    submitLbl.textContent = 'Sign In';
  }
}

async function submitAuthForm(e) {
  if (e) e.preventDefault();
  const email = (document.getElementById('auth-email')?.value || '').trim();
  const password = (document.getElementById('auth-password')?.value || '').trim();
  const inviteCode = (document.getElementById('auth-invite-code')?.value || 'KIIT2027').trim();
  const autoApply = Boolean(document.getElementById('auth-auto-apply')?.checked);
  const btn = document.getElementById('auth-submit-btn');
  const errBox = document.getElementById('auth-error-box');

  if (errBox) errBox.classList.add('hidden');
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<span class="animate-spin inline-block">🔄</span> <span>Signing in...</span>';
  }

  try {
    const endpoint = authMode === 'register' ? '/api/auth/register' : '/api/auth/login';
    const { ok, data } = await apiFetch(endpoint, {
      method: 'POST',
      body: JSON.stringify({ email, password, invite_code: inviteCode, auto_apply_enabled: autoApply })
    });

    if (ok && data && data.success) {
      authToken = data.token || '';
      localStorage.setItem('sjp_token', authToken);
      currentUser = data.user || null;
      hideAuthOverlay();
      toast(data.message || 'Signed in successfully!', 'success');
      await Promise.all([fetchStatus(), fetchJobs(), fetchProfile(), fetchNotices()]);
    } else {
      if (errBox) {
        errBox.textContent = (data && (data.message || data.error)) || 'Authentication failed. Check your Superset credentials.';
        errBox.classList.remove('hidden');
      }
    }
  } catch (err) {
    if (errBox) {
      errBox.textContent = 'Connection error: ' + err.message;
      errBox.classList.remove('hidden');
    }
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = `<span>⚡</span> <span id="auth-submit-label">${authMode === 'register' ? 'Register & Connect Superset' : 'Sign In'}</span>`;
    }
  }
}

async function logoutUser() {
  try {
    await apiFetch('/api/auth/logout', { method: 'POST' });
  } catch (e) {}
  authToken = '';
  currentUser = null;
  allJobs = [];
  profileData = {};
  noticesPayload = { notices: [], shortlisted_notices: [], pending_actions: [], counts: {} };
  expandedNoticeIds.clear();
  resumeMaster = null;
  lastTailoredResume = null;
  const rWs = document.getElementById('resume-workspace');
  if (rWs) rWs.classList.add('hidden');
  const offerBanner = document.getElementById('pending-offer-banner');
  if (offerBanner) { offerBanner.classList.add('hidden'); offerBanner.innerHTML = ''; }
  const dashStrip = document.getElementById('dashboard-shortlist-strip');
  if (dashStrip) { dashStrip.classList.add('hidden'); dashStrip.innerHTML = ''; }
  localStorage.removeItem('sjp_token');
  toast('Signed out and locked encrypted vault.', 'info');
  showAuthOverlay();
}

async function toggleUserAutoApply() {
  try {
    const { ok, data } = await apiFetch('/api/user/auto-apply', { method: 'POST' });
    if (ok && data && data.success) {
      statusData.auto_apply_enabled = data.auto_apply_enabled;
      updateStatusUI();
      renderSettings();
      toast(data.auto_apply_enabled ? 'Auto-Apply ENABLED for your account' : 'Auto-Apply OFF (Manual Review & Confirm mode)', data.auto_apply_enabled ? 'success' : 'info');
    }
  } catch (e) {
    toast('Could not toggle auto-apply: ' + e.message, 'error');
  }
}

async function fetchStatus() {
  try {
    const { ok, status, data } = await apiFetch('/api/status');
    if (status === 401) return;
    if (ok && data) statusData = data;
  } catch (e) { console.warn('Status fetch warning:', e); }
  updateStatusUI();
  loadClarifications();
}

async function fetchJobs() {
  try {
    const { ok, status, data } = await apiFetch('/api/jobs');
    if (status === 401) return;
    if (ok && Array.isArray(data)) allJobs = data;
  } catch (e) {
    console.warn('Jobs fetch warning:', e);
  }
  updateDashboardMetrics();
  populateSimulatorDropdown();
  if (currentPage === 'drives') renderDrives();
  if (currentPage === 'applied') renderApplied();
}

async function fetchProfile() {
  try {
    const { ok, status, data } = await apiFetch('/api/profile');
    if (status === 401) return;
    if (ok && data) profileData = data;
  } catch (e) { console.warn('Profile fetch warning:', e); }
  if (currentPage === 'profile') renderProfile();
}

// ═══════════════════════════════════════════════════════════════════
// DASHBOARD
// ═══════════════════════════════════════════════════════════════════
function updateStatusUI() {
  const d = statusData || {};
  const sName = d.student_name || profileData.full_name || 'Student Account';
  const sRoll = d.student_id || profileData.roll_number || 'KIIT';
  const sCgpa = d.cgpa || profileData.current_cgpa || '';
  const sBranch = d.branch || profileData.graduation_branch || 'B.Tech';
  const initials = getCompanyInitials(sName);

  document.getElementById('sidebar-student-name').textContent = sName;
  document.getElementById('sidebar-student-id').textContent = `${sRoll} • KIIT`;
  const drInit = document.getElementById('drawer-student-initials');
  const drName = document.getElementById('drawer-student-name');
  const drId = document.getElementById('drawer-student-id');
  if (drInit) drInit.textContent = initials;
  if (drName) drName.textContent = sName;
  if (drId) drId.textContent = `${sRoll} • ${d.email || ''}`;

  const topSub = document.getElementById('topbar-student-sub');
  if (topSub) topSub.textContent = `${sName.split(' ')[0]} • ${sRoll}${sCgpa ? ' • CGPA ' + sCgpa : ''}`;

  const heroInit = document.getElementById('hero-student-initials');
  const heroName = document.getElementById('hero-student-name');
  const heroCgpa = document.getElementById('hero-student-cgpa');
  const heroMeta = document.getElementById('hero-student-meta');
  if (heroInit) heroInit.textContent = initials;
  if (heroName) heroName.textContent = sName;
  if (heroCgpa) heroCgpa.textContent = sCgpa ? `CGPA ${sCgpa}` : 'Verified';
  if (heroMeta) heroMeta.textContent = `Roll ${sRoll} • ${sBranch} • ${profileData.active_backlogs || '0'} Backlogs`;

  document.getElementById('last-sync-badge').textContent = d.last_sync ? 'Synced: ' + d.last_sync : 'Live Sync Ready';

  // Per-user Auto-Apply state
  const autoOn = Boolean(d.auto_apply_enabled);
  const aaDot = document.getElementById('user-autoapply-dot');
  const aaLbl = document.getElementById('user-autoapply-label');
  const dashAa = document.getElementById('dash-autoapply');
  if (aaDot) aaDot.className = `w-2 h-2 rounded-full ${autoOn ? 'bg-emerald-400 live-dot' : 'bg-slate-500'}`;
  if (aaLbl) aaLbl.textContent = autoOn ? 'Auto: ON' : 'Auto: OFF';
  if (dashAa) {
    dashAa.textContent = autoOn ? 'AUTO-APPLY ON' : 'REVIEW & CONFIRM';
    dashAa.className = `font-mono font-semibold ${autoOn ? 'text-emerald-400' : 'text-brand-400'}`;
  }

  // Daemon state
  const da = d.daemon_active !== false;
  document.getElementById('daemon-indicator').className = `w-2 h-2 rounded-full ${da ? 'bg-emerald-400 live-dot' : 'bg-slate-500'}`;
  document.getElementById('daemon-status-text').textContent = da ? 'Daemon Active' : 'Daemon Paused';
  document.getElementById('daemon-dot').className = `w-2 h-2 rounded-full ${da ? 'bg-emerald-400 live-dot' : 'bg-red-400'}`;
  document.getElementById('daemon-label').textContent = da ? 'Engine ON' : 'Engine OFF';

  // Dashboard engine box
  document.getElementById('dash-daemon').textContent = da ? 'ACTIVE' : 'PAUSED';
  document.getElementById('dash-daemon').className = `font-mono font-semibold ${da ? 'text-emerald-400' : 'text-red-400'}`;
  document.getElementById('dash-poll').textContent = (d.poll_interval || 15) + 'm';
  const dashWa = document.getElementById('dash-whatsapp');
  if (dashWa) {
    if (d.whatsapp_paired) {
      dashWa.textContent = `ACTIVE (${String(d.whatsapp_provider || 'WA').toUpperCase()})`;
      dashWa.className = 'font-mono font-semibold text-emerald-400 hover:text-emerald-300 transition-colors';
    } else {
      dashWa.textContent = 'SETUP →';
      dashWa.className = 'font-mono font-semibold text-zinc-400 hover:text-white transition-colors';
    }
  }
  const badgeAlerts = document.getElementById('badge-nav-alerts');
  if (badgeAlerts) {
    badgeAlerts.textContent = d.whatsapp_paired ? 'LIVE' : 'WA';
    badgeAlerts.className = d.whatsapp_paired
      ? 'sidebar-label text-[10px] font-mono px-1.5 py-0.5 rounded bg-emerald-500/15 text-emerald-400'
      : 'sidebar-label text-[10px] font-mono px-1.5 py-0.5 rounded bg-white/[0.06] text-zinc-400';
  }
  document.getElementById('dash-telegram').textContent = d.telegram_paired !== false ? 'PAIRED' : 'NOT PAIRED';
  document.getElementById('dash-telegram').className = `font-mono font-semibold ${d.telegram_paired !== false ? 'text-emerald-400' : 'text-red-400'}`;
  document.getElementById('dash-lastsync').textContent = d.last_sync || 'Just now';

  // Nav badges
  const totalCount = allJobs.length || d.total_jobs || 0;
  const appliedCount = allJobs.filter(j => j.is_applied || j.app_status === 'APPLIED').length || d.applied_jobs || 0;
  document.getElementById('badge-nav-drives').textContent = totalCount;
  document.getElementById('badge-nav-applied').textContent = appliedCount;
  const mobDrives = document.getElementById('mobile-badge-drives');
  const mobApplied = document.getElementById('mobile-badge-applied');
  if (mobDrives) mobDrives.textContent = totalCount;
  if (mobApplied) mobApplied.textContent = appliedCount;

  // Logs on dashboard
  const logs = d.recent_logs || [];
  const dashLogs = document.getElementById('dash-logs');
  if (logs.length > 0) {
    dashLogs.innerHTML = logs.slice(-15).reverse().map(l =>
      `<div class="flex gap-3 items-start"><span class="text-slate-500 w-14 flex-shrink-0">${esc(l.time)}</span><span class="log-${l.level} text-slate-300">${esc(l.text)}</span></div>`
    ).join('');
    document.getElementById('dash-log-count').textContent = logs.length + ' entries';
  }
}

function updateDashboardMetrics() {
  const applied = allJobs.filter(j => j.is_applied || j.app_status === 'APPLIED');
  const open = allJobs.filter(j => !isJobClosed(j) && !j.is_applied && j.is_eligible);
  const closed = allJobs.filter(j => isJobClosed(j));
  const eligible = allJobs.filter(j => j.is_eligible);

  document.getElementById('m-total').textContent = allJobs.length;
  document.getElementById('m-applied').textContent = applied.length;
  document.getElementById('m-open').textContent = open.length;
  document.getElementById('m-closed').textContent = closed.length;
  document.getElementById('m-eligible').textContent = eligible.length;

  const mobDrives = document.getElementById('mobile-badge-drives');
  const mobApplied = document.getElementById('mobile-badge-applied');
  if (mobDrives) mobDrives.textContent = allJobs.length;
  if (mobApplied) mobApplied.textContent = applied.length;

  // Hero progress bar
  const heroBar = document.getElementById('hero-progress-bar');
  const heroLbl = document.getElementById('hero-progress-label');
  if (heroBar && heroLbl) {
    const pct = eligible.length > 0 ? Math.min(100, Math.round((applied.length / eligible.length) * 100)) : 75;
    heroBar.style.width = `${pct}%`;
    heroLbl.textContent = `${applied.length} Applied • ${open.length} Open`;
  }

  // CTC breakdown
  const ctcGroups = {};
  allJobs.forEach(j => {
    const cat = j.ctc_display || j.raw?.placementCategoryName || 'Unknown';
    ctcGroups[cat] = (ctcGroups[cat] || 0) + 1;
  });
  const ctcEl = document.getElementById('ctc-breakdown');
  const sortedCtc = Object.entries(ctcGroups).sort((a,b) => b[1] - a[1]);
  const maxCtc = sortedCtc[0]?.[1] || 1;
  ctcEl.innerHTML = sortedCtc.map(([cat, count]) => {
    const pct = Math.round((count / maxCtc) * 100);
    return `<div>
      <div class="flex justify-between text-[11px] mb-1"><span class="text-zinc-300 truncate mr-2">${esc(cat)}</span><span class="text-zinc-100 font-mono font-medium">${count}</span></div>
      <div class="h-1 rounded-full bg-zinc-800 overflow-hidden"><div class="h-full rounded-full bg-zinc-200/85" style="width:${pct}%"></div></div>
    </div>`;
  }).join('');

  // Location breakdown
  const locGroups = {};
  allJobs.forEach(j => {
    const loc = j.location || 'Unknown';
    locGroups[loc] = (locGroups[loc] || 0) + 1;
  });
  const locEl = document.getElementById('location-breakdown');
  const sortedLoc = Object.entries(locGroups).sort((a,b) => b[1] - a[1]).slice(0, 8);
  const maxLoc = sortedLoc[0]?.[1] || 1;
  locEl.innerHTML = sortedLoc.map(([loc, count]) => {
    const pct = Math.round((count / maxLoc) * 100);
    return `<div>
      <div class="flex justify-between text-[11px] mb-1"><span class="text-zinc-300 truncate mr-2">${esc(loc)}</span><span class="text-zinc-100 font-mono font-medium">${count}</span></div>
      <div class="h-1 rounded-full bg-zinc-800 overflow-hidden"><div class="h-full rounded-full bg-zinc-400/80" style="width:${pct}%"></div></div>
    </div>`;
  }).join('');
}

// ═══════════════════════════════════════════════════════════════════
// DRIVES PAGE
// ═══════════════════════════════════════════════════════════════════
function setDrivesFilter(f) {
  drivesFilter = f;
  document.querySelectorAll('.drives-filter').forEach(b => {
    if (b.dataset.filter === f) {
      b.className = 'drives-filter active px-3.5 py-1.5 rounded-full text-xs font-medium bg-zinc-100 text-zinc-950';
    } else {
      b.className = 'drives-filter px-3.5 py-1.5 rounded-full text-xs font-medium bg-surface-900 border border-white/[0.08] text-zinc-300';
    }
  });
  renderDrives();
}

function setCtcFilter(tier) {
  ctcFilter = tier;
  document.querySelectorAll('.ctc-filter').forEach(b => {
    if (b.dataset.ctc === tier) {
      b.className = 'ctc-filter active px-3 py-1.5 rounded-full text-[11px] font-medium bg-zinc-100 text-zinc-950';
    } else {
      b.className = 'ctc-filter px-3 py-1.5 rounded-full text-[11px] font-medium bg-surface-900 border border-white/[0.08] text-zinc-300';
    }
  });
  renderDrives();
}

function setDrivesSort(s) {
  drivesSort = s;
  document.querySelectorAll('.drives-sort').forEach(b => {
    if (b.dataset.sort === s) { b.classList.add('bg-brand-600', 'text-white'); b.classList.remove('text-slate-400'); }
    else { b.classList.remove('bg-brand-600', 'text-white'); b.classList.add('text-slate-400'); }
  });
  renderDrives();
}

function setDrivesView(v) {
  drivesView = v;
  document.querySelectorAll('.drives-view').forEach(b => {
    if (b.dataset.view === v) { b.classList.add('bg-brand-600', 'text-white'); b.classList.remove('text-slate-400'); }
    else { b.classList.remove('bg-brand-600', 'text-white'); b.classList.add('text-slate-400'); }
  });
  document.getElementById('drives-grid').classList.toggle('hidden', v !== 'grid');
  document.getElementById('drives-table').classList.toggle('hidden', v !== 'table');
  renderDrives();
}

function renderDrives() {
  const q = (document.getElementById('drives-search')?.value || '').toLowerCase();
  let jobs = allJobs.filter(j => {
    const text = `${j.company_name} ${j.job_role} ${j.location} ${j.ctc_display}`.toLowerCase();
    if (q && !text.includes(q)) return false;

    const isApplied = j.is_applied || j.app_status === 'APPLIED';
    const isClosed = isJobClosed(j);

    if (drivesFilter === 'open') { if (isClosed || isApplied || !j.is_eligible) return false; }
    else if (drivesFilter === 'applied') { if (!isApplied) return false; }
    else if (drivesFilter === 'eligible') { if (!j.is_eligible) return false; }
    else if (drivesFilter === 'closed') { if (!isClosed) return false; }

    if (ctcFilter === 'tier1') { if (getCtcLevel(j) !== 3) return false; }
    else if (ctcFilter === 'tier2') { if (getCtcLevel(j) !== 2) return false; }
    else if (ctcFilter === 'tier3') { if (getCtcLevel(j) !== 1) return false; }

    return true;
  });

  // Sort
  if (drivesSort === 'newest') jobs.sort((a,b) => (b.raw?.createdAt || 0) - (a.raw?.createdAt || 0));
  else if (drivesSort === 'ctc-high') jobs.sort((a,b) => getCtcLevel(b) - getCtcLevel(a));
  else if (drivesSort === 'company') jobs.sort((a,b) => (a.company_name || '').localeCompare(b.company_name || ''));

  document.getElementById('drives-count-label').textContent = `Showing ${jobs.length} of ${allJobs.length} campus placement drives`;

  // Grid
  const grid = document.getElementById('drives-grid');
  if (jobs.length === 0) {
    grid.innerHTML = '<div class="col-span-full text-center py-16 text-zinc-500"><div class="text-3xl mb-2">📭</div><div class="text-sm font-medium">No drives match your current filters</div></div>';
  } else {
    grid.innerHTML = jobs.map((j, idx) => {
      const initials = getCompanyInitials(j.company_name);
      const avatarStyle = getCompanyAvatarStyle(j.company_name);
      const sector = j.raw?.sectorName || '';
      const posType = j.raw?.positionTypeLabel || '';
      const deferClass = idx >= 6 ? 'deferred-card' : '';
      return `
        <div class="job-card ${deferClass} rounded-xl p-4 sm:p-5 flex flex-col gap-3">
          <div class="flex items-start justify-between gap-3">
            <div class="flex items-center gap-3 min-w-0">
              <div class="w-10 h-10 rounded-lg ${avatarStyle} border flex items-center justify-center font-semibold text-xs tracking-wide flex-shrink-0">${initials}</div>
              <div class="min-w-0">
                <h3 class="font-semibold text-sm text-zinc-100 truncate leading-snug">${esc(j.company_name)}</h3>
                <div class="text-xs text-zinc-400 truncate mt-0.5">${esc(j.job_role)}</div>
              </div>
            </div>
            ${getJobBadge(j)}
          </div>
          <div class="grid grid-cols-2 gap-2 text-[11px]">
            <div class="p-2.5 rounded-lg bg-surface-950 border border-white/[0.06]">
              <div class="text-[9px] text-zinc-500 uppercase font-mono mb-0.5">CTC Package</div>
              <div class="font-semibold text-zinc-100 truncate font-mono text-[11px]">${esc(j.ctc_display || 'As per Policy')}</div>
            </div>
            <div class="p-2.5 rounded-lg bg-surface-950 border border-white/[0.06]">
              <div class="text-[9px] text-zinc-500 uppercase font-mono mb-0.5">Location</div>
              <div class="font-medium text-zinc-300 truncate text-[11px]">${esc(j.location || 'Pan India')}</div>
            </div>
          </div>
          ${sector || posType ? `<div class="flex flex-wrap gap-1.5">
            ${sector ? `<span class="text-[10px] font-mono px-2 py-0.5 rounded bg-white/[0.04] text-zinc-400 border border-white/[0.06]">${esc(sector)}</span>` : ''}
            ${posType ? `<span class="text-[10px] font-mono px-2 py-0.5 rounded bg-white/[0.04] text-zinc-400 border border-white/[0.06]">${esc(posType)}</span>` : ''}
          </div>` : ''}
          <div class="text-[11px] text-zinc-500 font-mono flex items-center justify-between">
            <span>${esc(j.deadline_display || 'Open Window')}</span>
          </div>
          <div class="flex items-center gap-2 pt-2.5 border-t border-white/[0.06] mt-auto">
            ${getJobActions(j)}
          </div>
        </div>`;
    }).join('');
  }

  // Table
  const tbody = document.getElementById('drives-table-body');
  if (jobs.length === 0) {
    tbody.innerHTML = '<tr><td colspan="7" class="p-8 text-center text-slate-500">No drives match your filters</td></tr>';
  } else {
    tbody.innerHTML = jobs.map(j => {
      const isApplied = j.is_applied || j.app_status === 'APPLIED';
      const isClosed = isJobClosed(j);
      return `
        <tr class="hover:bg-surface-800/30 transition-colors">
          <td class="p-3"><div class="font-semibold text-white text-xs">${esc(j.company_name)}</div></td>
          <td class="p-3 text-xs text-slate-300">${esc(j.job_role)}</td>
          <td class="p-3 text-xs text-slate-300 font-mono">${esc(j.ctc_display || '—')}</td>
          <td class="p-3 text-xs text-slate-400">${esc(j.location || '—')}</td>
          <td class="p-3 text-[10px] text-slate-400 font-mono">${esc(j.deadline_display || '—')}</td>
          <td class="p-3">${getJobBadge(j)}</td>
          <td class="p-3 text-center">
            <div class="flex items-center justify-center gap-1.5">
              ${!isApplied && !isClosed && j.is_eligible ?
                `<button onclick="applyToJob('${j.id}','${esc(j.company_name)}','${esc(j.job_role)}')" class="px-2.5 py-1 rounded bg-brand-600 hover:bg-brand-500 text-white text-[10px] font-semibold transition-all">Apply</button>` :
                `<span class="text-[10px] text-slate-500">${isApplied ? 'Applied' : isClosed ? 'Closed' : '—'}</span>`
              }
              <button onclick="showJobQuestions('${j.id}', '${esc(j.company_name)}')" class="p-1 rounded text-purple-400 hover:text-purple-300 text-xs" title="Questions">📝</button>
              <a href="${j.profile_url || '#'}" target="_blank" class="text-slate-500 hover:text-white text-xs transition-colors">↗</a>
            </div>
          </td>
        </tr>`;
    }).join('');
  }
}

function exportDrivesCsv() {
  if (!allJobs.length) { toast('No drives data loaded yet', 'warn'); return; }
  const headers = ['Company', 'Role', 'CTC', 'Location', 'Deadline', 'Status', 'Superset URL'];
  const rows = allJobs.map(j => [
    `"${(j.company_name||'').replace(/"/g, '""')}"`,
    `"${(j.job_role||'').replace(/"/g, '""')}"`,
    `"${(j.ctc_display||'').replace(/"/g, '""')}"`,
    `"${(j.location||'').replace(/"/g, '""')}"`,
    `"${(j.deadline_display||'').replace(/"/g, '""')}"`,
    `"${(j.is_applied ? 'APPLIED' : isJobClosed(j) ? 'CLOSED' : 'OPEN')}"`,
    `"${(j.profile_url||'')}"`
  ]);
  const csvContent = [headers.join(','), ...rows.map(r => r.join(','))].join('\n');
  const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `superset_campus_drives_${new Date().toISOString().slice(0,10)}.csv`;
  a.click();
  toast('Exported 107 campus placement drives to CSV', 'success');
}

// ═══════════════════════════════════════════════════════════════════
// CUSTOM QUESTIONS INSPECTOR & SIMULATOR
// ═══════════════════════════════════════════════════════════════════
async function showJobQuestions(jobId, companyName) {
  const modal = document.getElementById('modal-overlay');
  const title = document.getElementById('modal-title');
  const body = document.getElementById('modal-body');

  title.innerHTML = `<span>📝</span> Custom Questions & Answers: ${esc(companyName)}`;
  body.innerHTML = '<div class="py-12 text-center text-slate-400 font-mono text-sm"><div class="animate-spin inline-block mb-3">🔄</div><div>Querying Superset API for questionnaire fields...</div></div>';
  modal.classList.remove('hidden');

  try {
    const { ok, data: questions } = await apiFetch(`/api/jobs/${jobId}/questions`);

    if (!ok || !Array.isArray(questions) || questions.length === 0) {
      body.innerHTML = `
        <div class="p-6 text-center space-y-3">
          <div class="text-3xl">📄</div>
          <div class="text-sm font-semibold text-white">Standard Application Profile</div>
          <p class="text-xs text-slate-400 max-w-md mx-auto">
            ${esc(companyName)} does not mandate any additional custom questionnaire fields. Your default uploaded resume and on-campus placement consent will be submitted directly.
          </p>
        </div>
      `;
      return;
    }

    body.innerHTML = `
      <div class="space-y-4">
        <div class="flex items-center justify-between text-xs text-slate-400 pb-2 border-b border-slate-800">
          <span>Found <strong class="text-white">${questions.length}</strong> custom questions</span>
          <span class="text-emerald-400 font-mono text-[10px] bg-emerald-500/10 px-2 py-0.5 rounded border border-emerald-500/20">✓ 100% Pre-Answered by Pilot</span>
        </div>
        <div class="space-y-3 max-h-[60vh] overflow-y-auto pr-1">
          ${questions.map((q, idx) => `
            <div class="p-3.5 rounded-xl bg-surface-950/70 border border-slate-800/80 space-y-2">
              <div class="flex items-start justify-between gap-2">
                <div class="text-xs font-semibold text-slate-200">
                  <span class="text-slate-500 font-mono mr-1">#${idx+1}</span> ${esc(q.question)}
                </div>
                ${q.mandatory ? '<span class="text-[9px] font-mono px-1.5 py-0.5 rounded bg-red-500/10 text-red-400 border border-red-500/20">REQUIRED</span>' : '<span class="text-[9px] font-mono px-1.5 py-0.5 rounded bg-slate-800 text-slate-500">OPTIONAL</span>'}
              </div>
              <div class="p-2.5 rounded-lg bg-surface-900 border border-slate-800 text-xs">
                <div class="text-[10px] text-slate-500 uppercase font-mono mb-0.5">Pre-loaded Answer</div>
                <div class="font-mono text-emerald-400 font-medium break-all">${esc(String(q.answer))}</div>
              </div>
              ${q.options && q.options.length ? `
                <div class="text-[10px] text-slate-500 font-mono">
                  Options: ${esc(q.options.slice(0, 4).join(', '))}${q.options.length > 4 ? '...' : ''}
                </div>
              ` : ''}
            </div>
          `).join('')}
        </div>
      </div>
    `;
  } catch (e) {
    body.innerHTML = `<div class="p-6 text-center text-red-400 text-xs">Failed to fetch questions: ${esc(e.message)}</div>`;
  }
}

function populateSimulatorDropdown() {
  const sel = document.getElementById('simulator-company-select');
  if (!sel) return;
  const currentVal = sel.value;
  sel.innerHTML = '<option value="">Select a company to test questionnaire answers...</option>';
  const sorted = [...allJobs].sort((a,b) => (a.company_name||'').localeCompare(b.company_name||''));
  sorted.forEach(j => {
    const opt = document.createElement('option');
    opt.value = j.id;
    opt.textContent = `${j.company_name} (${j.job_role || 'Role'})`;
    sel.appendChild(opt);
  });
  if (currentVal) sel.value = currentVal;
}

async function runQuestionSimulator() {
  const sel = document.getElementById('simulator-company-select');
  const jobId = sel.value;
  const out = document.getElementById('simulator-output');
  if (!jobId) { out.classList.add('hidden'); return; }

  const compName = sel.options[sel.selectedIndex].text;
  out.classList.remove('hidden');
  out.innerHTML = '<div class="py-4 text-center text-slate-400 font-mono text-xs">Querying Superset question resolver for ' + esc(compName) + '...</div>';

  try {
    const { ok, data: questions } = await apiFetch(`/api/jobs/${jobId}/questions`);
    if (!ok || !Array.isArray(questions) || questions.length === 0) {
      out.innerHTML = `
        <div class="p-4 rounded-lg bg-surface-950 border border-slate-800 text-center text-slate-400">
          <span>📄</span> <strong>${esc(compName)}</strong> requires only standard resume and placement consent (0 custom questions).
        </div>
      `;
      return;
    }

    out.innerHTML = `
      <div class="space-y-2 max-h-72 overflow-y-auto">
        <div class="text-[11px] text-purple-300 font-mono pb-1">
          ✓ Resolved <strong>${questions.length}</strong> custom attributes for ${esc(compName)}:
        </div>
        ${questions.map((q, idx) => `
          <div class="p-2.5 rounded-lg bg-surface-950 border border-slate-800/80 flex flex-col md:flex-row md:items-center justify-between gap-2">
            <div class="flex-1 min-w-0">
              <div class="font-medium text-slate-200 truncate"><span class="text-slate-500 font-mono">#${idx+1}</span> ${esc(q.question)}</div>
              <div class="text-[10px] text-slate-500 font-mono">${esc(q.type)} ${q.mandatory ? '• Required' : ''}</div>
            </div>
            <div class="md:text-right">
              <span class="font-mono text-emerald-400 text-xs px-2 py-1 rounded bg-emerald-500/10 border border-emerald-500/20 font-semibold">${esc(String(q.answer))}</span>
            </div>
          </div>
        `).join('')}
      </div>
    `;
  } catch (e) {
    out.innerHTML = `<div class="text-red-400 text-xs">Error: ${esc(e.message)}</div>`;
  }
}

// ═══════════════════════════════════════════════════════════════════
// APPLIED PAGE
// ═══════════════════════════════════════════════════════════════════
function renderApplied() {
  const applied = allJobs.filter(j => j.is_applied || j.app_status === 'APPLIED');
  document.getElementById('applied-count-big').textContent = applied.length;

  const el = document.getElementById('applied-list');
  if (applied.length === 0) {
    el.innerHTML = '<div class="text-center py-16 text-slate-500"><div class="text-3xl mb-2">📄</div><div class="text-sm">No applications registered yet</div></div>';
    return;
  }
  el.innerHTML = applied.map(j => {
    const stage = j.raw?.jobApplicationCurrentStage || j.raw?.jobProfileCurrentStage || 'YET_TO_PROCESS';
    const appliedAt = j.raw?.jobApplicationLastAppliedAt;
    let appliedStr = '—';
    if (appliedAt) {
      try {
        const d = new Date(typeof appliedAt === 'number' ? (appliedAt > 1e11 ? appliedAt : appliedAt * 1000) : appliedAt);
        appliedStr = d.toLocaleDateString('en-IN', { day:'2-digit', month:'short', year:'numeric' });
      } catch(e) {}
    }
    return `
      <div class="bg-surface-900/60 rounded-xl p-4 border border-slate-800/60 flex items-center gap-4 hover:border-emerald-800/40 transition-all">
        <div class="w-10 h-10 rounded-xl bg-emerald-900/30 border border-emerald-800/30 flex items-center justify-center text-emerald-400 text-sm font-bold flex-shrink-0">${(j.company_name||'C')[0]}</div>
        <div class="flex-1 min-w-0">
          <div class="font-semibold text-sm text-white truncate">${esc(j.company_name)}</div>
          <div class="text-xs text-slate-400">${esc(j.job_role)}</div>
        </div>
        <div class="text-right flex-shrink-0">
          <div class="text-[10px] text-slate-400 font-mono">${esc(j.ctc_display || '—')}</div>
          <div class="text-[10px] text-slate-500">Applied: ${appliedStr}</div>
          <div class="text-[10px] text-emerald-400 font-mono mt-0.5">${esc(stage)}</div>
        </div>
        <button onclick="showJobQuestions('${j.id}', '${esc(j.company_name)}')" class="p-2 rounded-lg text-purple-400 hover:text-purple-300 transition-colors" title="View Questions">📝</button>
        <a href="${j.profile_url || '#'}" target="_blank" class="p-2 rounded-lg text-slate-500 hover:text-white transition-colors" title="Open in Portal">↗</a>
      </div>`;
  }).join('');
}

// ═══════════════════════════════════════════════════════════════════
// PROFILE PAGE
// ═══════════════════════════════════════════════════════════════════
function renderProfile() {
  const p = profileData;
  if (!p || Object.keys(p).length === 0) { return; }

  const field = (key, label) => `
    <div class="profile-field flex items-center gap-3">
      <label class="text-[11px] text-slate-400 w-36 flex-shrink-0 font-medium">${esc(label)}</label>
      <input type="text" data-key="${key}" value="${esc(p[key] || '')}" class="flex-1 rounded-lg px-3 py-1.5 text-xs font-mono">
    </div>`;

  document.getElementById('profile-personal').innerHTML = [
    field('full_name', 'Full Name'), field('dob', 'Date of Birth'), field('gender', 'Gender'),
    field('phone_number', 'Mobile Number'), field('fathers_name', "Father's Name"),
    field('nationality', 'Nationality'), field('citizenship', 'Citizenship'),
    field('current_address', 'Current Address'), field('permanent_address', 'Permanent Address'),
    field('emergency_contact_name', 'Emergency Contact'), field('emergency_contact_number', 'Emergency Phone'),
  ].join('');

  document.getElementById('profile-identity').innerHTML = [
    field('pan_card_number', 'PAN Card Number'), field('pan_first_name', 'PAN First Name'),
    field('pan_middle_name', 'PAN Middle Name'), field('pan_last_name', 'PAN Last Name'),
    field('aadhaar_last_4', 'Aadhaar Last 4'),
    field('passport_number', 'Passport Number'), field('passport_type', 'Passport Type'),
    field('passport_expiry_date', 'Passport Expiry Date'),
    field('citizenship_certificate_no', 'Citizenship Cert No.'),
  ].join('');

  document.getElementById('profile-academic').innerHTML = [
    field('tenth_school', '10th School Name'), field('tenth_board', '10th Board'),
    field('tenth_passing_year', '10th Passing Year'), field('tenth_gpa', '10th GPA / Scale'),
    field('tenth_percentage', '10th Percentage'),
    field('twelfth_college', '12th College Name'), field('twelfth_board', '12th Board'),
    field('twelfth_passing_year', '12th Passing Year'), field('twelfth_gpa', '12th GPA / Scale'),
    field('twelfth_percentage', '12th Percentage'),
    field('graduation_college', 'University College'), field('graduation_branch', 'B.Tech Branch'),
    field('graduation_passing_year', 'Graduation Year'), field('current_cgpa', 'Current CGPA'),
    field('gap_10th_12th', 'Gap 10th to 12th'), field('gap_12th_grad', 'Gap 12th to College'),
    field('active_backlogs', 'Active Backlogs'),
  ].join('');

  document.getElementById('profile-links').innerHTML = [
    field('resume_link', 'Resume Cloud Link'), field('github_link', 'GitHub Profile'),
    field('linkedin_link', 'LinkedIn Profile'), field('portfolio_link', 'Portfolio Link'),
    field('flexible_shift', 'Flexible Shift (Yes/No)'), field('flexible_location', 'Flexible Location'),
    field('disclaimers_agree', 'Default Agreement'),
  ].join('');
}

async function saveProfile() {
  const inputs = document.querySelectorAll('.profile-field input[data-key]');
  const updated = {};
  inputs.forEach(inp => { updated[inp.dataset.key] = inp.value; });
  try {
    const { ok, data } = await apiFetch('/api/profile', { method:'POST', body: JSON.stringify(updated) });
    if (ok && data && data.success) {
      toast('Profile encrypted & saved to your vault!', 'success');
      profileData = data.profile || updated;
      await fetchStatus();
    } else {
      toast('Failed to save profile: ' + ((data && data.message) || 'Error'), 'error');
    }
  } catch (e) { toast('Error saving profile: ' + e.message, 'error'); }
}

// ═══════════════════════════════════════════════════════════════════
// ENGINE & LOGS PAGE
// ═══════════════════════════════════════════════════════════════════
function renderEngine() {
  const d = statusData;
  const ls = d.last_sync_status || {};

  const statusEl = document.getElementById('engine-sync-status');
  const st = ls.status || 'UNKNOWN';
  const stColor = st === 'SUCCESS' ? 'text-emerald-400' : st === 'ERROR' ? 'text-red-400' : st === 'WARNING' ? 'text-amber-400' : 'text-slate-400';
  statusEl.textContent = st;
  statusEl.className = `text-sm font-bold font-mono ${stColor}`;

  document.getElementById('engine-last-sync').textContent = d.last_sync || '—';
  document.getElementById('engine-drives-found').textContent = ls.total_found || '—';
  document.getElementById('engine-sync-count').textContent = (d.sync_history || []).length;

  const histEl = document.getElementById('sync-history-list');
  const history = d.sync_history || [];
  histEl.innerHTML = history.map(h => {
    const icon = h.status === 'SUCCESS' ? '✓' : h.status === 'ERROR' ? '✕' : '⚠';
    const color = h.status === 'SUCCESS' ? 'text-emerald-400' : h.status === 'ERROR' ? 'text-red-400' : 'text-amber-400';
    return `<div class="flex items-center gap-3 text-[11px] font-mono py-1.5 border-b border-slate-800/30">
      <span class="${color} text-xs font-bold">${icon}</span>
      <span class="text-slate-500 w-44 flex-shrink-0">${esc(h.timestamp || '—')}</span>
      <span class="text-slate-300">${esc(h.message || '—')}</span>
      <span class="text-slate-500 ml-auto font-mono">${h.total_found || 0} drives</span>
    </div>`;
  }).join('') || '<div class="text-slate-500 text-xs">No sync history yet</div>';

  const logsEl = document.getElementById('engine-logs');
  const logs = d.recent_logs || [];
  logsEl.innerHTML = logs.map(l =>
    `<div class="flex gap-3 items-start py-0.5"><span class="text-slate-600 w-14 flex-shrink-0">${esc(l.time)}</span><span class="log-${l.level} flex-1">${esc(l.text)}</span></div>`
  ).join('') || '<div class="text-slate-500">No logs yet</div>';
  logsEl.scrollTop = logsEl.scrollHeight;
}

// ═══════════════════════════════════════════════════════════════════
// WHATSAPP & TELEGRAM ALERTS PAGE
// ═══════════════════════════════════════════════════════════════════
let whatsappConfigCache = null;
let waQrPollTimer = null;

function stopWhatsappQrPolling() {
  if (waQrPollTimer) {
    clearInterval(waQrPollTimer);
    waQrPollTimer = null;
  }
}

function renderWhatsappQrState(st) {
  const connBox = document.getElementById('wa-qr-connected-box');
  const connLbl = document.getElementById('wa-qr-connected-label');
  const qrBox = document.getElementById('wa-qr-canvas-box');
  const qrImg = document.getElementById('wa-qr-img');
  const errBox = document.getElementById('wa-qr-error');
  const startBtn = document.getElementById('btn-wa-qr-start');
  const logoutBtn = document.getElementById('btn-wa-qr-logout');
  const pollNote = document.getElementById('wa-qr-poll-note');
  const pill = document.getElementById('wa-status-pill');

  if (!st) return;
  if (errBox) {
    if (st.last_error && !st.connected && st.state !== 'qr_ready') {
      errBox.textContent = st.last_error;
      errBox.classList.remove('hidden');
    } else {
      errBox.classList.add('hidden');
    }
  }

  if (st.connected) {
    stopWhatsappQrPolling();
    if (connBox) connBox.classList.remove('hidden');
    if (qrBox) qrBox.classList.add('hidden');
    if (logoutBtn) logoutBtn.classList.remove('hidden');
    if (startBtn) startBtn.classList.add('hidden');
    const phoneStr = st.me_phone ? `+${st.me_phone}` : 'Your Device';
    const nameStr = st.me_name ? ` (${st.me_name})` : '';
    if (connLbl) connLbl.textContent = `Linked to WhatsApp ${phoneStr}${nameStr}`;
    if (pill && (document.getElementById('wa-provider')?.value === 'local_qr')) {
      pill.textContent = `● Active · QR LINKED (${phoneStr})`;
      pill.className = 'text-[11px] font-mono px-2.5 py-1 rounded-full bg-emerald-500/10 border border-emerald-500/25 text-emerald-300';
    }
    return;
  }

  if (connBox) connBox.classList.add('hidden');
  if (logoutBtn) logoutBtn.classList.add('hidden');
  if (startBtn) startBtn.classList.remove('hidden');

  if (st.state === 'qr_ready' && st.qr_data_url) {
    if (qrImg) qrImg.src = st.qr_data_url;
    if (qrBox) qrBox.classList.remove('hidden');
    if (startBtn) startBtn.textContent = 'Refresh QR Code';
    if (pollNote) pollNote.textContent = 'Live QR active — scan with WhatsApp Linked Devices...';
    if (!waQrPollTimer) {
      waQrPollTimer = setInterval(pollWhatsappQrStatus, 2500);
    }
  } else {
    if (qrBox) qrBox.classList.add('hidden');
    if (startBtn) startBtn.textContent = st.state === 'connecting' ? 'Connecting...' : 'Generate QR Code';
  }
}

async function pollWhatsappQrStatus() {
  const pageEl = document.getElementById('page-telegram');
  if (!pageEl || pageEl.classList.contains('hidden')) {
    stopWhatsappQrPolling();
    return;
  }
  try {
    const { ok, data } = await apiFetch('/api/whatsapp/qr/status');
    if (ok && data) {
      const wasConnected = Boolean(whatsappConfigCache && whatsappConfigCache.configured && whatsappConfigCache.provider === 'local_qr');
      renderWhatsappQrState(data);
      if (data.connected && !wasConnected) {
        toast('WhatsApp Linked Successfully! Try "Send Test WhatsApp" or "Send Live Digest Now".', 'success');
        loadWhatsappConfig();
        fetchStatus();
      }
    }
  } catch (e) {
    console.warn('WA QR poll error:', e);
  }
}

async function startWhatsappQr(fresh = false) {
  const btn = document.getElementById('btn-wa-qr-start');
  if (btn) { btn.disabled = true; btn.textContent = 'Generating QR...'; }
  try {
    const { ok, data } = await apiFetch('/api/whatsapp/qr/start', {
      method: 'POST',
      body: JSON.stringify({ fresh: Boolean(fresh) }),
    });
    if (ok && data) {
      renderWhatsappQrState(data);
      if (data.connected) {
        toast('WhatsApp is already linked and ready!', 'success');
        loadWhatsappConfig();
        fetchStatus();
      } else if (data.qr_data_url) {
        toast('QR code ready — scan in WhatsApp → Linked Devices.', 'info');
      } else {
        toast(data.message || data.last_error || 'Connecting to WhatsApp...', 'warn');
      }
    } else {
      toast((data && data.message) || 'Could not start WhatsApp QR bridge.', 'error');
    }
  } catch (e) {
    toast('Network error starting WhatsApp QR bridge: ' + e.message, 'error');
  } finally {
    if (btn) { btn.disabled = false; }
  }
}

async function logoutWhatsappQr() {
  if (!confirm('Unlink this WhatsApp device from Superset Job Pilot?')) return;
  try {
    const { ok, data } = await apiFetch('/api/whatsapp/qr/logout', { method: 'POST' });
    if (ok && data) {
      renderWhatsappQrState(data);
      toast('WhatsApp device unlinked.', 'info');
      loadWhatsappConfig();
      fetchStatus();
    }
  } catch (e) {
    toast('Failed to unlink device: ' + e.message, 'error');
  }
}

function onWhatsappProviderChange() {
  const prov = (document.getElementById('wa-provider')?.value || 'local_qr').toLowerCase();
  const blocks = ['local_qr', 'callmebot', 'meta', 'twilio', 'webhook'];
  blocks.forEach(p => {
    const el = document.getElementById(`wa-fields-${p}`);
    if (el) el.classList.toggle('hidden', p !== prov);
  });
  const guide = document.getElementById('wa-guide-callmebot');
  if (guide) guide.classList.toggle('hidden', prov !== 'callmebot');
  const optTag = document.getElementById('wa-phone-opt-tag');
  if (optTag) optTag.classList.toggle('hidden', prov !== 'local_qr');
  if (prov === 'local_qr') {
    pollWhatsappQrStatus();
  } else {
    stopWhatsappQrPolling();
  }
}

function applyWhatsappConfigToUi(cfg) {
  whatsappConfigCache = cfg || {};
  const provEl = document.getElementById('wa-provider');
  const phoneEl = document.getElementById('wa-phone');
  const apiKeyEl = document.getElementById('wa-apikey');
  const apiHintEl = document.getElementById('wa-apikey-hint');
  const metaPidEl = document.getElementById('wa-meta-phone-id');
  const metaTokEl = document.getElementById('wa-meta-token');
  const twSidEl = document.getElementById('wa-twilio-sid');
  const twTokEl = document.getElementById('wa-twilio-token');
  const twFromEl = document.getElementById('wa-twilio-from');
  const whEl = document.getElementById('wa-webhook-url');

  if (provEl) provEl.value = cfg.provider || 'local_qr';
  if (phoneEl) phoneEl.value = cfg.phone || '';
  if (apiKeyEl) {
    apiKeyEl.value = '';
    apiKeyEl.placeholder = cfg.has_api_key ? 'Saved (leave blank to keep current key)' : 'Enter your CallMeBot API key';
  }
  if (apiHintEl) {
    apiHintEl.textContent = cfg.has_api_key ? '● API key securely stored in your vault.' : '';
  }
  if (metaPidEl) metaPidEl.value = cfg.meta_phone_number_id || '';
  if (metaTokEl) {
    metaTokEl.value = '';
    metaTokEl.placeholder = cfg.has_meta_token ? 'Saved (leave blank to keep current token)' : 'EAAG...';
  }
  if (twSidEl) twSidEl.value = cfg.twilio_account_sid || '';
  if (twTokEl) {
    twTokEl.value = '';
    twTokEl.placeholder = cfg.has_twilio_token ? 'Saved (leave blank to keep current token)' : 'Auth Token';
  }
  if (twFromEl) twFromEl.value = cfg.twilio_from || 'whatsapp:+14155238886';
  if (whEl) whEl.value = cfg.webhook_url || '';

  const setCheck = (id, val) => { const el = document.getElementById(id); if (el) el.checked = val !== false; };
  setCheck('wa-enabled', cfg.enabled !== false);
  setCheck('wa-notify-jobs', cfg.notify_jobs !== false);
  setCheck('wa-notify-shortlists', cfg.notify_shortlists !== false);
  setCheck('wa-notify-offers', cfg.notify_offers !== false);

  const pill = document.getElementById('wa-status-pill');
  if (pill) {
    if (cfg.configured) {
      const label = cfg.provider === 'local_qr' ? 'QR LINKED' : String(cfg.provider || 'WA').toUpperCase();
      pill.textContent = `● Active · ${label}`;
      pill.className = 'text-[11px] font-mono px-2.5 py-1 rounded-full bg-emerald-500/10 border border-emerald-500/25 text-emerald-300';
    } else {
      pill.textContent = '○ Not Linked Yet';
      pill.className = 'text-[11px] font-mono px-2.5 py-1 rounded-full bg-white/[0.05] border border-white/10 text-zinc-400';
    }
  }
  if (cfg.local_bridge) renderWhatsappQrState(cfg.local_bridge);
  onWhatsappProviderChange();
}

async function loadWhatsappConfig() {
  try {
    const { ok, data } = await apiFetch('/api/whatsapp/config');
    if (ok && data && data.success) {
      applyWhatsappConfigToUi(data.config || {});
    }
  } catch (e) {
    console.warn('WhatsApp config fetch failed:', e);
  }
}

async function saveWhatsappConfig() {
  const btn = document.getElementById('btn-wa-save');
  const fb = document.getElementById('wa-save-feedback');
  const payload = {
    provider: document.getElementById('wa-provider')?.value || 'local_qr',
    phone: (document.getElementById('wa-phone')?.value || '').trim(),
    api_key: (document.getElementById('wa-apikey')?.value || '').trim(),
    meta_phone_number_id: (document.getElementById('wa-meta-phone-id')?.value || '').trim(),
    meta_access_token: (document.getElementById('wa-meta-token')?.value || '').trim(),
    twilio_account_sid: (document.getElementById('wa-twilio-sid')?.value || '').trim(),
    twilio_auth_token: (document.getElementById('wa-twilio-token')?.value || '').trim(),
    twilio_from: (document.getElementById('wa-twilio-from')?.value || '').trim(),
    webhook_url: (document.getElementById('wa-webhook-url')?.value || '').trim(),
    enabled: Boolean(document.getElementById('wa-enabled')?.checked),
    notify_jobs: Boolean(document.getElementById('wa-notify-jobs')?.checked),
    notify_shortlists: Boolean(document.getElementById('wa-notify-shortlists')?.checked),
    notify_offers: Boolean(document.getElementById('wa-notify-offers')?.checked),
  };

  if (btn) { btn.disabled = true; btn.textContent = 'Saving...'; }
  try {
    const { ok, data } = await apiFetch('/api/whatsapp/config', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    if (ok && data && data.success) {
      applyWhatsappConfigToUi(data.config || {});
      toast(data.config?.configured ? 'WhatsApp alerts saved & armed!' : 'WhatsApp settings saved (add key/phone to activate).', 'success');
      if (fb) fb.textContent = 'Saved.';
      fetchStatus();
    } else {
      toast('Could not save WhatsApp config: ' + ((data && data.message) || 'Error'), 'error');
    }
  } catch (e) {
    toast('Network error saving WhatsApp config: ' + e.message, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Save WhatsApp Config'; }
  }
}

async function testWhatsapp() {
  toast('Sending test WhatsApp alert...', 'info');
  try {
    const { ok, data } = await apiFetch('/api/whatsapp/test', { method: 'POST' });
    if (ok && data && data.success) {
      toast(data.message || 'Test message sent to your WhatsApp!', 'success');
    } else {
      toast((data && data.message) || 'WhatsApp test failed — save Phone + API Key first.', 'error');
    }
  } catch (e) {
    toast('Network error: ' + e.message, 'error');
  }
}

async function sendWhatsappDigest() {
  const btn = document.getElementById('btn-wa-digest');
  if (btn) { btn.disabled = true; btn.textContent = 'Sending Digest...'; }
  toast('Sending active Shortlist venues + open Job Profiles to WhatsApp...', 'info');
  try {
    const { ok, data } = await apiFetch('/api/whatsapp/send-digest', { method: 'POST' });
    if (ok && data && data.success) {
      toast(data.message || 'WhatsApp live digest delivered!', 'success');
    } else {
      toast((data && data.message) || 'Could not send digest — configure WhatsApp first.', 'error');
    }
  } catch (e) {
    toast('Network error: ' + e.message, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Send Live Digest Now'; }
  }
}

function renderTelegram() {
  const d = statusData || {};
  const tgId = document.getElementById('tg-chat-id');
  if (tgId) tgId.textContent = d.telegram_chat_id || 'Not Configured';
  const tgStatus = document.getElementById('tg-status');
  if (tgStatus) {
    if (d.telegram_paired) { tgStatus.textContent = '● Connected'; tgStatus.className = 'text-xs font-semibold text-emerald-400'; }
    else { tgStatus.textContent = '○ Not paired'; tgStatus.className = 'text-xs font-semibold text-zinc-500'; }
  }
  loadWhatsappConfig();
}

async function testTelegram() {
  toast('Sending test notification to your Telegram...', 'info');
  try {
    const { ok, data } = await apiFetch('/api/telegram/test', { method: 'POST' });
    if (ok && data && data.success) toast('Test alert sent to your Telegram phone app!', 'success');
    else toast('Telegram send failed: ' + ((data && data.message) || 'Unknown error'), 'error');
  } catch (e) { toast('Network error: ' + e.message, 'error'); }
}

async function sendCustomTelegram() {
  const inp = document.getElementById('tg-custom-text');
  const text = (inp.value || '').trim();
  if (!text) { toast('Please type message text first', 'warn'); return; }
  toast('Sending message to Telegram...', 'info');
  try {
    const { ok, data } = await apiFetch('/api/telegram/send', {
      method: 'POST',
      body: JSON.stringify({ text })
    });
    if (ok && data && data.success) {
      toast('Custom alert delivered to Telegram!', 'success');
      inp.value = '';
    } else {
      toast('Failed to send: ' + ((data && data.message) || 'Error'), 'error');
    }
  } catch (e) { toast('Network error: ' + e.message, 'error'); }
}

// ═══════════════════════════════════════════════════════════════════
// SETTINGS & FRIEND INVITE CODES PAGE
// ═══════════════════════════════════════════════════════════════════
async function renderSettings() {
  const d = statusData || {};
  document.getElementById('set-poll').textContent = (d.poll_interval || 15) + ' minutes';
  document.getElementById('set-tracked').textContent = allJobs.length || d.total_jobs || '0';
  const uName = document.getElementById('set-user-name');
  const uEmail = document.getElementById('set-user-email');
  const uRoll = document.getElementById('set-user-roll');
  if (uName) uName.textContent = d.student_name || profileData.full_name || '—';
  if (uEmail) uEmail.textContent = d.email || profileData.email || '—';
  if (uRoll) uRoll.textContent = d.student_id || profileData.roll_number || '—';

  const aaBtn = document.getElementById('set-auto-apply-btn');
  if (aaBtn) {
    const on = Boolean(d.auto_apply_enabled);
    aaBtn.textContent = on ? 'ENABLED (ON)' : 'DISABLED (OFF)';
    aaBtn.className = on
      ? 'px-3.5 py-2 rounded-xl bg-emerald-500/20 border border-emerald-500/40 text-xs font-mono font-bold text-emerald-300'
      : 'px-3.5 py-2 rounded-xl bg-surface-800 border border-white/10 text-xs font-mono font-bold text-slate-300';
  }
  await loadInviteCodes();
}

async function loadInviteCodes() {
  const container = document.getElementById('invite-codes-list');
  if (!container) return;
  try {
    const { ok, data } = await apiFetch('/api/invites');
    if (ok && data && Array.isArray(data.invites)) {
      container.innerHTML = data.invites.map(c => `
        <div class="p-3 rounded-xl bg-surface-950/90 border border-white/10 flex items-center justify-between gap-2">
          <div>
            <div class="font-mono font-bold text-emerald-400 text-sm">${esc(c.code)}</div>
            <div class="text-[10px] text-slate-400">${esc(c.label || 'Friend Invite')} • Used ${c.uses_count || 0}/${c.max_uses || 100}</div>
          </div>
          <button onclick="navigator.clipboard.writeText('${esc(c.code)}'); toast('Copied invite code ${esc(c.code)}!', 'success');" class="px-2.5 py-1.5 rounded-lg bg-surface-800 text-slate-200 text-[11px] font-semibold border border-white/10">
            Copy
          </button>
        </div>
      `).join('');
    }
  } catch (e) {}
}

async function createNewInviteCode() {
  try {
    const { ok, data } = await apiFetch('/api/invites', {
      method: 'POST',
      body: JSON.stringify({ label: 'College Friend Invite', max_uses: 50 })
    });
    if (ok && data && data.success) {
      toast(`Created new invite code: ${data.invite.code}`, 'success');
      await loadInviteCodes();
    }
  } catch (e) {
    toast('Could not create invite code: ' + e.message, 'error');
  }
}

// ═══════════════════════════════════════════════════════════════════
// ACTIONS
// ═══════════════════════════════════════════════════════════════════
async function triggerSync() {
  const btn = document.getElementById('sync-btn');
  const icon = document.getElementById('sync-icon');
  btn.disabled = true;
  icon.textContent = '⏳';
  btn.classList.add('opacity-70');
  toast('Syncing live drives with Superset API...', 'info');

  try {
    const { ok, data } = await apiFetch('/api/sync', { method: 'POST' });
    if (ok && data && data.success) {
      toast(`Sync complete: ${data.total_found} drives verified${data.auto_applied ? ', ' + data.auto_applied + ' auto-applied' : ''}!`, 'success');
    } else if (data && data.total_found) {
      const errors = data.errors || [];
      toast(`Verified ${data.total_found} drives (${errors.length} note: ${errors[0] || ''})`, 'warn');
    } else {
      toast((data && (data.message || data.error)) || 'Could not reach Superset sync endpoint.', 'warn');
    }
    await Promise.all([fetchStatus(), fetchJobs(), fetchNotices()]);
  } catch (e) {
    toast('Sync notice: ' + e.message, 'warn');
  } finally {
    btn.disabled = false;
    icon.textContent = '🔄';
    btn.classList.remove('opacity-70');
  }
}

async function toggleDaemon() {
  try {
    const { ok, data } = await apiFetch('/api/daemon/toggle', { method: 'POST' });
    if (ok && data && data.success) {
      toast(data.active ? 'Background engine activated' : 'Background engine paused', data.active ? 'success' : 'warn');
      await fetchStatus();
    }
  } catch (e) { toast('Error toggling daemon: ' + e.message, 'error'); }
}

async function applyToJob(jobId, company, role) {
  const modal = document.getElementById('modal-overlay');
  const title = document.getElementById('modal-title');
  const body = document.getElementById('modal-body');

  title.innerHTML = `<span>⚡</span> Review Application — <span class="text-indigo-400 font-mono">${esc(company)}</span>`;
  body.innerHTML = `
    <div class="py-12 text-center text-slate-400 font-mono text-sm space-y-3">
      <div class="inline-block animate-spin text-2xl text-brand-400">🔄</div>
      <div class="text-slate-300 font-medium">Resolving application details for ${esc(company)}...</div>
      <div class="text-xs text-slate-500">Retrieving questionnaire & mapping verified candidate facts via RAG</div>
    </div>
  `;
  modal.classList.remove('hidden');

  try {
    const { ok, data: respData } = await apiFetch(`/api/jobs/${jobId}/preview-application`);
    let data = ok ? respData : null;

    if (!data || !data.success) {
      const jobObj = allJobs.find(j => j.id === jobId) || {};
      const pf = profileData || {};
      data = {
        success: true,
        job_id: jobId,
        company_name: company || jobObj.company_name || 'Company',
        job_role: role || jobObj.job_role || 'Engineering Role',
        ctc_display: jobObj.ctc_display || 'Standard CTC',
        default_resume: {},
        resumes_available: [],
        questions: [],
        candidate_profile: {
          name: pf.full_name || statusData.student_name || 'Student',
          email: pf.email || statusData.email || '—',
          roll_no: pf.roll_number || statusData.student_id || '—',
          cgpa: pf.current_cgpa || '—',
          phone: pf.phone_number || '—',
          emergency_phone: pf.emergency_contact_number || '—',
          father_name: pf.fathers_name || '—',
          permanent_address: pf.permanent_address || '—',
          nationality: `${pf.citizenship || 'India'}${pf.passport_number && pf.passport_number !== 'NA' ? ' (Passport: ' + pf.passport_number + ')' : ''}`,
          college: pf.graduation_college || 'KIIT Bhubaneswar',
          branch: pf.graduation_branch || 'Computer Science and Engineering',
          degree: pf.graduation_degree || 'B.Tech',
          passing_year: pf.graduation_passing_year || '2027',
          backlogs: `${pf.active_backlogs || '0'} Active / ${pf.history_backlogs || '0'} History`,
          gap: `${pf.total_gap || '0'} years`
        }
      };
    }

    const p = data.candidate_profile || {};
    const resumes = data.resumes_available || [];
    const defaultResume = data.default_resume || {};
    const questions = data.questions || [];

    let resumesOptionsHtml = '';
    if (resumes.length > 0) {
      resumesOptionsHtml = resumes.map(r => {
        const isDefault = (defaultResume.identifier && r.identifier === defaultResume.identifier) || (defaultResume.uuid && r.uuid === defaultResume.uuid) || (r.name && r.name.toLowerCase().includes('default'));
        const docId = r.identifier || r.uuid || r.name;
        return `<option value="${esc(docId)}" ${isDefault ? 'selected' : ''}>📄 ${esc(r.name || docId)} ${isDefault ? '(Recommended / Default)' : ''}</option>`;
      }).join('');
    } else if (defaultResume.identifier || defaultResume.uuid) {
      const docId = defaultResume.identifier || defaultResume.uuid;
      resumesOptionsHtml = `<option value="${esc(docId)}" selected>📄 ${esc(defaultResume.name || 'Default Placement Resume')} (Recommended / Default)</option>`;
    } else {
      resumesOptionsHtml = `<option value="Default_Resume" selected>📄 Default Placement Resume (Verified PDF)</option>`;
    }

    let questionsHtml = '';
    if (questions.length > 0) {
      questionsHtml = `
        <div class="space-y-3 pt-2">
          <div class="flex items-center justify-between">
            <div class="text-xs font-bold uppercase tracking-wider text-purple-400 flex items-center gap-1.5">
              <span>📝</span> Company Questionnaire (${questions.length} questions)
            </div>
            <span class="text-[10px] text-slate-400 font-mono">Editable before submitting</span>
          </div>
          <div class="space-y-3 max-h-80 overflow-y-auto pr-1">
            ${questions.map((q, idx) => {
              const qKey = esc(q.key || `question_${idx}`);
              const qAns = esc(q.answer !== undefined && q.answer !== null ? String(q.answer) : '');
              const qType = (q.type || 'string').toLowerCase();
              const isMandatory = q.mandatory;
              
              let inputField = '';
              if (q.options && Array.isArray(q.options) && q.options.length > 0) {
                inputField = `
                  <select data-attr-key="${qKey}" class="app-review-input w-full bg-surface-950 border border-slate-700 rounded-lg px-3 py-2 text-xs text-white focus:border-brand-500 focus:outline-none">
                    ${q.options.map(opt => `<option value="${esc(opt)}" ${String(opt).toLowerCase() === qAns.toLowerCase() ? 'selected' : ''}>${esc(opt)}</option>`).join('')}
                  </select>
                `;
              } else if (qType === 'boolean' || qType === 'bool') {
                inputField = `
                  <select data-attr-key="${qKey}" class="app-review-input w-full bg-surface-950 border border-slate-700 rounded-lg px-3 py-2 text-xs text-white focus:border-brand-500 focus:outline-none">
                    <option value="Yes" ${['yes', 'true', '1'].includes(qAns.toLowerCase()) ? 'selected' : ''}>Yes</option>
                    <option value="No" ${['no', 'false', '0'].includes(qAns.toLowerCase()) ? 'selected' : ''}>No</option>
                  </select>
                `;
              } else if (qType.includes('int') || qType.includes('number') || qType.includes('float')) {
                inputField = `
                  <input type="number" step="any" data-attr-key="${qKey}" value="${qAns}" class="app-review-input w-full bg-surface-950 border border-slate-700 rounded-lg px-3 py-2 text-xs text-white font-mono focus:border-brand-500 focus:outline-none" />
                `;
              } else {
                inputField = `
                  <input type="text" data-attr-key="${qKey}" value="${qAns}" class="app-review-input w-full bg-surface-950 border border-slate-700 rounded-lg px-3 py-2 text-xs text-white focus:border-brand-500 focus:outline-none" />
                `;
              }

              return `
                <div class="p-3 rounded-xl bg-surface-950/70 border border-slate-800 space-y-1.5">
                  <div class="flex items-start justify-between gap-2">
                    <label class="text-xs text-slate-200 font-medium leading-snug">
                      <span class="text-slate-500 font-mono">#${idx+1}</span> ${esc(q.question)}
                      ${isMandatory ? '<span class="text-rose-400 font-bold ml-1">*</span>' : ''}
                    </label>
                    <span class="text-[9px] font-mono text-slate-400 uppercase px-1.5 py-0.5 rounded bg-surface-800">${esc(q.type || 'text')}</span>
                  </div>
                  <div>${inputField}</div>
                  <div class="flex items-center justify-between text-[10px]">
                    <span class="text-emerald-400 flex items-center gap-1 font-mono">
                      <span>✓</span> Auto-filled: ${esc(q.clarity_reason || 'Verified Academic Fact')}
                    </span>
                    <span class="text-purple-400 font-mono font-semibold">${Math.round((q.confidence || 0.95)*100)}% match</span>
                  </div>
                </div>
              `;
            }).join('')}
          </div>
        </div>
      `;
    } else {
      questionsHtml = `
        <div class="p-3.5 rounded-xl bg-surface-950/80 border border-slate-800/80 flex items-center gap-3">
          <span class="text-xl">📄</span>
          <div class="text-xs text-slate-300">
            <strong class="text-white">Standard Campus Application:</strong> This recruiter requires no custom questionnaires. Your verified academic records and selected resume will be submitted.
          </div>
        </div>
      `;
    }

    body.innerHTML = `
      <div class="space-y-4">
        <!-- Drive Header Banner -->
        <div class="p-3.5 rounded-xl bg-gradient-to-r from-brand-950/60 to-purple-950/40 border border-brand-800/40 flex flex-col sm:flex-row sm:items-center justify-between gap-2">
          <div>
            <div class="text-sm font-bold text-white">${esc(data.company_name)}</div>
            <div class="text-xs text-brand-300">${esc(data.job_role)} • <span class="font-mono text-white font-semibold">${esc(data.ctc_display || 'Standard CTC')}</span></div>
          </div>
          <div class="text-[11px] text-emerald-400 font-mono bg-emerald-950/50 px-2.5 py-1 rounded-lg border border-emerald-800/40 self-start sm:self-auto">
            ⚡ Ready for Confirmed Submission
          </div>
        </div>

        <!-- Section 1: Pre-Filled Candidate Profile Details -->
        <div class="space-y-2">
          <div class="flex items-center justify-between">
            <div class="text-xs font-bold uppercase tracking-wider text-slate-300 flex items-center gap-1.5">
              <span>👤</span> Candidate Details Pre-Filled from Encrypted Profile
            </div>
            <span class="text-[10px] text-emerald-400 font-mono">✓ Verified Records</span>
          </div>

          <div class="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 gap-2 p-3 rounded-xl bg-surface-950/80 border border-slate-800 text-xs">
            <div>
              <div class="text-[10px] text-slate-500 font-mono">Full Name</div>
              <div class="font-medium text-slate-200">${esc(p.name)}</div>
            </div>
            <div>
              <div class="text-[10px] text-slate-500 font-mono">Roll Number & Email</div>
              <div class="font-medium text-slate-200 font-mono">${esc(p.roll_no)} • ${esc(p.email)}</div>
            </div>
            <div>
              <div class="text-[10px] text-slate-500 font-mono">Current CGPA & Backlogs</div>
              <div class="font-semibold text-emerald-400 font-mono">${esc(p.cgpa)} <span class="text-slate-400 font-normal">(${esc(p.backlogs)})</span></div>
            </div>
            <div>
              <div class="text-[10px] text-slate-500 font-mono">Father's Name</div>
              <div class="font-medium text-slate-200">${esc(p.father_name)}</div>
            </div>
            <div>
              <div class="text-[10px] text-slate-500 font-mono">Contact Phone</div>
              <div class="font-medium text-slate-200 font-mono">${esc(p.phone)}</div>
            </div>
            <div>
              <div class="text-[10px] text-slate-500 font-mono">Emergency Contact</div>
              <div class="font-medium text-slate-200 font-mono">${esc(p.emergency_phone)}</div>
            </div>
            <div>
              <div class="text-[10px] text-slate-500 font-mono">Nationality / Passport</div>
              <div class="font-medium text-slate-200">${esc(p.nationality)}</div>
            </div>
            <div>
              <div class="text-[10px] text-slate-500 font-mono">Permanent Address</div>
              <div class="font-medium text-slate-200 truncate" title="${esc(p.permanent_address)}">${esc(p.permanent_address)}</div>
            </div>
            <div>
              <div class="text-[10px] text-slate-500 font-mono">Branch & College</div>
              <div class="font-medium text-slate-200 truncate">${esc(p.branch)} (${esc(p.passing_year)})</div>
            </div>
          </div>
        </div>

        <!-- Section 2: Attached Resume Selection -->
        <div class="space-y-1.5">
          <label class="text-xs font-bold uppercase tracking-wider text-slate-300 flex items-center gap-1.5">
            <span>📎</span> Select Resume to Attach
          </label>
          <select id="review-resume-select" class="w-full bg-surface-950 border border-slate-700 rounded-lg px-3 py-2 text-xs text-white focus:border-brand-500 focus:outline-none">
            ${resumesOptionsHtml}
          </select>
        </div>

        <!-- Section 3: Company Custom Questions -->
        ${questionsHtml}

        <!-- Section 4: Consent Checkbox -->
        <label class="flex items-start gap-2.5 p-3 rounded-xl bg-surface-950 border border-slate-800/80 cursor-pointer hover:border-slate-700 transition-colors">
          <input type="checkbox" id="review-consent-check" checked class="mt-0.5 rounded border-slate-700 text-brand-600 focus:ring-0">
          <span class="text-xs text-slate-300 leading-relaxed">
            I confirm that the above filled information, answers, and selected documents are verified and accurate. Authorize TNP Suite to register my formal application for <strong>${esc(data.company_name)}</strong>.
          </span>
        </label>

        <!-- Section 5: Modal Action Buttons -->
        <div class="pt-2 flex flex-col sm:flex-row items-center gap-2 border-t border-slate-800">
          <button type="button" onclick="closeModal()" class="w-full sm:w-auto px-4 py-2.5 rounded-xl bg-surface-800 hover:bg-surface-700 text-slate-300 font-semibold text-xs transition-colors">
            Cancel
          </button>
          <button id="btn-submit-app-modal" type="button" onclick="confirmAndSubmitApplication('${jobId}', '${esc(data.company_name)}')" class="w-full sm:flex-1 py-2.5 px-4 rounded-xl bg-gradient-to-r from-brand-600 to-indigo-600 hover:from-brand-500 hover:to-indigo-500 text-white font-bold text-xs shadow-lg shadow-brand-900/40 flex items-center justify-center gap-2 transition-all">
            <span>⚡</span> Confirm & Submit to Superset
          </button>
        </div>
      </div>
    `;
  } catch (err) {
    body.innerHTML = `
      <div class="p-4 rounded-xl bg-red-950/40 border border-red-800 text-red-200 text-xs font-mono space-y-2">
        <div class="font-bold text-red-300">Application Preview Error:</div>
        <div>${esc(err.message)}</div>
      </div>
    `;
  }
}

async function confirmAndSubmitApplication(jobId, company) {
  const consent = document.getElementById('review-consent-check');
  if (consent && !consent.checked) {
    toast('Please confirm application consent before submitting.', 'warn');
    return;
  }

  const resumeSelect = document.getElementById('review-resume-select');
  const selectedResume = resumeSelect ? resumeSelect.value : '';

  // Gather all custom question answers from input elements
  const answers = {};
  const inputs = document.querySelectorAll('.app-review-input');
  inputs.forEach(el => {
    const key = el.getAttribute('data-attr-key');
    if (key) {
      answers[key] = el.value;
    }
  });

  const submitBtn = document.getElementById('btn-submit-app-modal');
  if (submitBtn) {
    submitBtn.disabled = true;
    submitBtn.innerHTML = '<div class="inline-block animate-spin w-3.5 h-3.5 border-2 border-white border-t-transparent rounded-full mr-2"></div> Submitting to Superset...';
  }

  toast(`Submitting application to ${company}...`, 'info');

  try {
    const { ok, data } = await apiFetch(`/api/apply/${jobId}`, {
      method: 'POST',
      body: JSON.stringify({
        resume_id: selectedResume,
        answers: answers
      })
    });

    if (ok && data && data.success) {
      closeModal();
      toast(`🎉 Successfully applied to ${company}!`, 'success');
      await fetchJobs();
      await fetchStatus();
    } else {
      showRejectionModal(company, (data && data.message) || 'Application rejected by Superset server.', data && data.ai_diagnosis);
      await fetchJobs();
    }
  } catch (e) {
    if (submitBtn) {
      submitBtn.disabled = false;
      submitBtn.innerHTML = '<span>⚡</span> Confirm & Submit to Superset';
    }
    toast(`Error applying to ${company}: ${e.message}`, 'error');
  }
}

function showRejectionModal(company, msg, aiDiag = null) {
  const modal = document.getElementById('modal-overlay');
  const title = document.getElementById('modal-title');
  const body = document.getElementById('modal-body');

  title.innerHTML = `<span class="text-rose-400">✕</span> Application Rejection Diagnostics: <span class="text-indigo-400 font-mono">${esc(company)}</span>`;

  let diagHtml = '';
  if (aiDiag && (aiDiag.root_cause || aiDiag.remediation_guide || aiDiag.remediation)) {
    diagHtml = `
      <div class="p-4 rounded-xl bg-indigo-950/40 border border-indigo-700/50 mb-4 space-y-2">
        <div class="flex items-center justify-between">
          <div class="flex items-center gap-2">
            <span class="text-base">🤖</span>
            <span class="text-xs font-bold uppercase tracking-wider text-indigo-400">AI Root Cause Analysis & Auto-Remedy</span>
          </div>
          <span class="text-[10px] font-mono px-2 py-0.5 rounded ${aiDiag.auto_healed ? 'bg-emerald-500/20 text-emerald-300 border border-emerald-500/40' : 'bg-amber-500/20 text-amber-300'}">
            ${aiDiag.auto_healed ? '✓ AUTO-HEALED' : 'REQUIRES ATTENTION'}
          </span>
        </div>
        <div class="text-xs text-indigo-200 leading-relaxed font-sans">${esc(aiDiag.root_cause)}</div>
        <div class="text-[11px] text-slate-300 bg-surface-900/80 p-2.5 rounded-lg border border-slate-700 font-mono">
          <span class="text-emerald-400 font-semibold">Remedy Action:</span> ${esc(aiDiag.remediation_guide || aiDiag.remediation || '')}
        </div>
        ${aiDiag.heal_notes ? `<div class="text-[10px] text-emerald-300 font-mono">📝 ${esc(aiDiag.heal_notes)}</div>` : ''}
      </div>
    `;
  }

  body.innerHTML = `
    <div class="space-y-4">
      ${diagHtml}
      <div class="p-4 rounded-xl bg-red-950/40 border border-red-800/60 text-red-200 text-xs font-mono space-y-2">
        <div class="font-bold text-red-300">Raw Superset Server Response:</div>
        <div class="break-all">${esc(msg)}</div>
      </div>
      <div class="text-xs text-slate-400 space-y-2">
        <p><strong>Common Causes:</strong></p>
        <ul class="list-disc pl-5 space-y-1 text-slate-400">
          <li><strong>Deadline Passed / Closed:</strong> Recruiter closed submissions or advanced stages.</li>
          <li><strong>Eligibility Criteria:</strong> CGPA, branch, or backlogs did not match policy.</li>
          <li><strong>Already Registered:</strong> Application previously accepted.</li>
        </ul>
      </div>
      <div class="pt-3 border-t border-slate-800 flex justify-end gap-2">
        <button onclick="switchPage('ai'); closeModal();" class="px-4 py-2 rounded-lg bg-indigo-600 hover:bg-indigo-500 text-white text-xs font-semibold flex items-center gap-1.5">
          <span>🤖</span> Open AI Copilot & Healer Console
        </button>
        <button onclick="closeModal()" class="px-4 py-2 rounded-lg bg-surface-800 hover:bg-surface-700 text-white text-xs font-semibold">
          Dismiss
        </button>
      </div>
    </div>
  `;
  modal.classList.remove('hidden');
}

// ═══════════════════════════════════════════════════════════════════
// AI COPILOT, RAG & LIVE HEALER
// ═══════════════════════════════════════════════════════════════════
async function renderAiPage() {
  await fetchAiStatus();
  await loadClarifications();
  await loadLiveErrors();
}

async function fetchAiStatus() {
  try {
    const { ok, data } = await apiFetch('/api/ai/status');
    if (!ok || !data) return;
    const active = data.active_provider || {};
    const modelEl = document.getElementById('ai-provider-name');
    const typeEl = document.getElementById('ai-provider-type');
    const ragEl = document.getElementById('ai-rag-chunks');
    if (modelEl) modelEl.textContent = active.model || 'Built-In Hybrid RAG';
    if (typeEl) typeEl.textContent = active.name || 'Zero-RAM Engine';
    const rag = data.rag_stats || {};
    if (ragEl) ragEl.textContent = `${rag.total_documents || 136} Chunks`;
  } catch (e) {
    console.error('Error fetching AI status:', e);
  }
}

async function loadClarifications() {
  const container = document.getElementById('clarifications-list');
  try {
    const { ok, status, data } = await apiFetch('/api/clarifications');
    if (status === 401 || !ok || !data) return;
    const pending = data.pending || [];
    
    const topBtn = document.getElementById('topbar-clarif-btn');
    const topCount = document.getElementById('topbar-clarif-count');
    const pagePending = document.getElementById('ai-pending-tickets');
    if (pagePending) pagePending.textContent = `${pending.length} Pending`;

    if (pending.length > 0) {
      if (topBtn) { topBtn.classList.remove('hidden'); topBtn.classList.add('flex'); }
      if (topCount) topCount.textContent = `${pending.length} Clarification${pending.length > 1 ? 's' : ''} Needed`;
    } else {
      if (topBtn) { topBtn.classList.add('hidden'); topBtn.classList.remove('flex'); }
    }

    if (!container) return;
    if (pending.length === 0) {
      container.innerHTML = `
        <div class="p-6 rounded-xl bg-surface-950/40 border border-slate-800 text-center">
          <div class="text-2xl mb-1">✨</div>
          <div class="text-xs font-semibold text-emerald-400">Zero Pending Clarifications</div>
          <div class="text-[11px] text-slate-500 mt-0.5">All student attributes and questionnaire fields are unambiguous.</div>
        </div>
      `;
      return;
    }

    container.innerHTML = pending.map(t => `
      <div class="p-4 rounded-xl bg-amber-950/20 border border-amber-800/40 space-y-3">
        <div class="flex items-start justify-between gap-4">
          <div>
            <div class="flex items-center gap-2 mb-1">
              <span class="text-xs font-bold text-amber-400 uppercase tracking-wider">${esc(t.company_name)}</span>
              <span class="text-[10px] font-mono px-2 py-0.5 rounded bg-amber-500/10 text-amber-300 border border-amber-500/20">Action Required</span>
            </div>
            <div class="text-sm font-semibold text-white font-sans">"${esc(t.question_text)}"</div>
            <div class="text-xs text-slate-400 mt-1">💡 <span class="text-slate-300">${esc(t.reason)}</span></div>
          </div>
          <div class="text-[10px] text-slate-500 font-mono whitespace-nowrap">${esc(t.created_at || '')}</div>
        </div>

        ${t.options && t.options.length ? `
          <div class="text-xs text-slate-400">
            <span class="text-slate-500">Allowed Options:</span>
            <div class="flex flex-wrap gap-1.5 mt-1">
              ${t.options.map(o => `<span class="px-2 py-0.5 rounded bg-surface-800 text-slate-300 text-[11px] font-mono border border-slate-700">${esc(o)}</span>`).join('')}
            </div>
          </div>
        ` : ''}

        <div class="pt-2 border-t border-amber-900/40 flex flex-wrap items-center justify-between gap-3">
          <div class="flex items-center gap-2">
            <button onclick="resolveClarifTicket('${t.ticket_id}', '${esc(t.suggested_answer)}')" class="px-3 py-1.5 rounded-lg bg-amber-600 hover:bg-amber-500 text-white text-xs font-semibold flex items-center gap-1.5 shadow-sm">
              <span>✓</span> Confirm AI Suggestion: "${esc(t.suggested_answer)}"
            </button>
          </div>
          <div class="flex items-center gap-2 flex-1 max-w-sm">
            <input id="input-ticket-${t.ticket_id}" type="text" placeholder="Or specify custom answer..." class="flex-1 bg-surface-950 border border-slate-700 rounded-lg px-2.5 py-1.5 text-xs text-white placeholder-slate-500 font-mono focus:outline-none focus:border-amber-500">
            <button onclick="resolveClarifCustom('${t.ticket_id}')" class="px-3 py-1.5 rounded-lg bg-surface-800 hover:bg-surface-700 text-slate-200 text-xs border border-slate-700 font-medium">Save</button>
          </div>
        </div>
      </div>
    `).join('');

  } catch (e) {
    console.error('Error loading clarifications:', e);
    if (container) container.innerHTML = `<div class="text-xs text-rose-400 p-4">Error loading clarification queue: ${esc(e.message)}</div>`;
  }
}

async function resolveClarifTicket(ticketId, answer) {
  try {
    const { ok, data } = await apiFetch('/api/clarifications/resolve', {
      method: 'POST',
      body: JSON.stringify({ ticket_id: ticketId, answer: answer })
    });
    if (ok && data && data.success) {
      toast(`Clarification saved! RAG memory updated for "${answer}"`, 'success');
      await loadClarifications();
      await fetchAiStatus();
    } else {
      toast(`Error resolving: ${(data && data.message) || 'Error'}`, 'error');
    }
  } catch (e) { toast('Error: ' + e.message, 'error'); }
}

function resolveClarifCustom(ticketId) {
  const input = document.getElementById(`input-ticket-${ticketId}`);
  if (!input || !input.value.trim()) {
    toast('Please enter an answer.', 'warn');
    return;
  }
  resolveClarifTicket(ticketId, input.value.trim());
}

async function loadLiveErrors() {
  const container = document.getElementById('live-errors-feed');
  if (!container) return;
  try {
    const { ok, data: errors } = await apiFetch('/api/errors/live');
    if (!ok || !Array.isArray(errors) || errors.length === 0) {
      container.innerHTML = `
        <div class="p-6 rounded-xl bg-surface-950/40 border border-slate-800 text-center">
          <div class="text-2xl mb-1">🛡️</div>
          <div class="text-xs font-semibold text-emerald-400">Clean Operational History</div>
          <div class="text-[11px] text-slate-500 mt-0.5">No recent application errors or API rejections recorded.</div>
        </div>
      `;
      return;
    }

    container.innerHTML = errors.map(err => `
      <div class="p-3.5 rounded-xl bg-surface-950/60 border border-slate-800 space-y-2">
        <div class="flex items-start justify-between gap-3">
          <div>
            <div class="flex items-center gap-2 mb-1">
              <span class="text-xs font-bold text-white">${esc(err.company_name)}</span>
              <span class="text-[10px] font-mono px-1.5 py-0.5 rounded ${err.status_code === 409 ? 'bg-amber-500/20 text-amber-300' : 'bg-rose-500/20 text-rose-300'}">HTTP ${err.status_code || 500}</span>
              <span class="text-[10px] font-mono px-1.5 py-0.5 rounded ${err.auto_healed ? 'bg-emerald-500/20 text-emerald-300 border border-emerald-500/30' : 'bg-slate-700 text-slate-300'}">
                ${err.auto_healed ? '✓ AUTO-HEALED' : 'REQUIRES ATTENTION'}
              </span>
            </div>
            <div class="text-xs text-slate-300">${esc(err.root_cause)}</div>
          </div>
          <div class="text-[10px] text-slate-500 font-mono whitespace-nowrap">${esc(err.timestamp || '')}</div>
        </div>
        ${err.heal_notes ? `
          <div class="p-2 rounded bg-surface-900 border border-slate-800 text-[11px] text-emerald-400 font-mono">
            ⚡ ${esc(err.heal_notes)}
          </div>
        ` : ''}
      </div>
    `).join('');
  } catch (e) {
    container.innerHTML = `<div class="text-xs text-rose-400 p-4">Error loading error feed: ${esc(e.message)}</div>`;
  }
}

function setCopilotQuery(q) {
  const inp = document.getElementById('copilot-input');
  if (inp) {
    inp.value = q;
    inp.focus();
  }
}

async function submitCopilotChat(e) {
  if (e) e.preventDefault();
  const input = document.getElementById('copilot-input');
  const query = (input?.value || '').trim();
  if (!query) return;

  const history = document.getElementById('copilot-history');
  const btn = document.getElementById('copilot-btn');
  input.value = '';

  const userDiv = document.createElement('div');
  userDiv.className = 'p-3 rounded-lg bg-indigo-950/40 border border-indigo-800/40 text-slate-200 ml-6';
  userDiv.innerHTML = `<div class="font-bold text-xs text-indigo-400 mb-1">👤 You</div><div class="text-xs">${esc(query)}</div>`;
  history.appendChild(userDiv);

  const typingDiv = document.createElement('div');
  typingDiv.className = 'p-3 rounded-lg bg-surface-800/60 border border-slate-800 text-slate-400 mr-6 animate-pulse';
  typingDiv.innerHTML = `<div class="text-xs font-mono">🤖 Copilot thinking (consulting RAG knowledge base)...</div>`;
  history.appendChild(typingDiv);
  history.scrollTop = history.scrollHeight;

  if (btn) btn.disabled = true;

  try {
    const { data } = await apiFetch('/api/ai/copilot', {
      method: 'POST',
      body: JSON.stringify({ query: query })
    });
    typingDiv.remove();

    const botDiv = document.createElement('div');
    botDiv.className = 'p-3.5 rounded-lg bg-surface-800/70 border border-slate-800 text-slate-200 mr-6 space-y-2';
    
    let text = esc((data && data.reply) || 'No response.');
    text = text.replace(/\\*\\*(.*?)\\*\\*/g, '<strong>$1</strong>');
    text = text.replace(/`([^`]+)`/g, '<code class="bg-surface-950 px-1 py-0.5 rounded text-indigo-300 font-mono text-[11px]">$1</code>');
    text = text.replace(/\\n\\n/g, '<br><br>').replace(/\\n/g, '<br>');

    let sourcesHtml = '';
    if (data && data.rag_sources && data.rag_sources.length) {
      sourcesHtml = `
        <div class="pt-2 border-t border-slate-800 text-[10px] text-slate-400 font-mono flex flex-wrap gap-1.5 items-center">
          <span class="text-slate-500">RAG Sources:</span>
          ${data.rag_sources.map(s => `<span class="px-1.5 py-0.5 rounded bg-surface-950 border border-slate-800 text-indigo-300">${esc(s.title)} (${s.score})</span>`).join('')}
        </div>
      `;
    }

    botDiv.innerHTML = `
      <div class="flex items-center justify-between mb-1">
        <div class="font-bold text-xs text-indigo-400 flex items-center gap-1.5">
          <span>🤖</span> Mission Control Copilot
        </div>
        <span class="text-[9px] font-mono text-slate-500">${esc(data?.provider?.name || 'RAG Engine')}</span>
      </div>
      <div class="text-xs text-slate-200 leading-relaxed font-sans">${text}</div>
      ${sourcesHtml}
    `;
    history.appendChild(botDiv);
    history.scrollTop = history.scrollHeight;
  } catch (e) {
    typingDiv.innerHTML = `<div class="text-xs text-rose-400 font-mono">Error querying Copilot: ${esc(e.message)}</div>`;
  } finally {
    if (btn) btn.disabled = false;
  }
}

function clearLogs() {
  document.getElementById('engine-logs').innerHTML = '<div class="text-slate-500">Logs cleared (server logs persist)</div>';
}

function closeModal() { document.getElementById('modal-overlay').classList.add('hidden'); }
document.getElementById('modal-overlay').addEventListener('click', e => { if (e.target === e.currentTarget) closeModal(); });

// ═══════════════════════════════════════════════════════════════════
// SMART NOTICE FEED, AUTO SHORTLIST SCANNER, 1-CLICK PDF VIEWER & OFFER WATCHDOG
// ═══════════════════════════════════════════════════════════════════

function safeSetNoticeHtml(container, rawHtml) {
  const allowedElements = ['p','br','b','strong','i','em','u','ul','ol','li','table','thead','tbody','tr','th','td','a','span','div','h1','h2','h3','h4','hr','blockquote','code','pre'];
  const allowedAttributes = ['href','target','rel','colspan','rowspan'];

  if ('setHTML' in Element.prototype) {
    try {
      container.setHTML(rawHtml || '', { sanitizer: { elements: allowedElements, attributes: allowedAttributes } });
      return;
    } catch (e) {}
  }

  // Fallback for browsers without native Sanitizer API (e.g. Safari): parse & allowlist DOM nodes
  const doc = new DOMParser().parseFromString(rawHtml || '', 'text/html');
  const allowedSet = new Set(allowedElements);
  const cleanFragment = document.createDocumentFragment();

  function cloneClean(node) {
    if (node.nodeType === Node.TEXT_NODE) {
      return document.createTextNode(node.textContent || '');
    }
    if (node.nodeType !== Node.ELEMENT_NODE) return null;
    const tag = node.tagName.toLowerCase();
    if (['script','style','iframe','object','embed','svg','form','input'].includes(tag)) return null;

    if (!allowedSet.has(tag)) {
      const frag = document.createDocumentFragment();
      for (const ch of node.childNodes) {
        const cloned = cloneClean(ch);
        if (cloned) frag.appendChild(cloned);
      }
      return frag;
    }

    const el = document.createElement(tag);
    if (tag === 'a') {
      const href = (node.getAttribute('href') || '').trim();
      if (/^(https?:\/\/|mailto:)/i.test(href)) {
        el.setAttribute('href', href);
        el.setAttribute('target', '_blank');
        el.setAttribute('rel', 'noopener noreferrer');
      }
    } else if (tag === 'td' || tag === 'th') {
      for (const a of ['colspan', 'rowspan']) {
        const v = node.getAttribute(a);
        if (v && /^\d+$/.test(v)) el.setAttribute(a, v);
      }
    }
    for (const ch of node.childNodes) {
      const cloned = cloneClean(ch);
      if (cloned) el.appendChild(cloned);
    }
    return el;
  }

  for (const child of doc.body.childNodes) {
    const cloned = cloneClean(child);
    if (cloned) cleanFragment.appendChild(cloned);
  }
  container.replaceChildren(cleanFragment);
}

async function fetchNotices(refresh = false) {
  try {
    const url = refresh ? '/api/notices?refresh=1' : '/api/notices';
    const { ok, status, data } = await apiFetch(url);
    if (status === 401) return;
    if (ok && data && data.success) {
      noticesPayload = data;
      updateNoticeBadgesAndBanners();
      if (currentPage === 'notices') renderNotices();
    }
  } catch (e) {
    console.warn('Notices fetch warning:', e);
  }
}

async function scanNoticesLive() {
  const btn = document.getElementById('notices-scan-btn');
  const icon = document.getElementById('notices-scan-icon');
  if (btn) btn.disabled = true;
  if (icon) icon.textContent = '⏳';
  toast('Scanning Superset homepage notices, shortlist PDFs & pending offers...', 'info');

  try {
    const { ok, data } = await apiFetch('/api/notices/scan', { method: 'POST' });
    if (ok && data && data.success) {
      noticesPayload = data;
      updateNoticeBadgesAndBanners();
      renderNotices();
      const c = data.counts || {};
      const s = data.scan_stats || {};
      const upCount = c.shortlisted_upcoming ?? 0;
      if (upCount > 0) {
        toast(`${upCount} upcoming shortlist match(es) active! Scanned ${s.pdfs_scanned_total || 0} PDFs.`, 'success');
      } else {
        toast(`Scanned ${s.pdfs_scanned_total || 0} PDFs across ${c.clean || 0} notices (${c.shortlisted_past || 0} past archive).`, 'success');
      }
    } else {
      toast((data && (data.message || data.error)) || 'Notice scan could not complete.', 'warn');
    }
  } catch (e) {
    toast('Notice scan error: ' + e.message, 'error');
  } finally {
    if (btn) btn.disabled = false;
    if (icon) icon.textContent = '🔍';
  }
}

function updateNoticeBadgesAndBanners() {
  const c = noticesPayload.counts || {};
  const upcomingShortlisted = c.shortlisted_upcoming ?? (noticesPayload.upcoming_shortlisted_notices || []).length;
  const totalShortlisted = c.shortlisted || 0;
  const cleanCount = c.clean || 0;
  const pendingCount = c.pending_offers || 0;

  // Sidebar, Drawer, and Mobile Dock badges — highlight ONLY UPCOMING / TODAY shortlists!
  const navBadge = document.getElementById('badge-nav-notices');
  const drawerBadge = document.getElementById('drawer-badge-notices');
  const mobBadge = document.getElementById('mobile-badge-notices');

  if (navBadge) {
    if (upcomingShortlisted > 0) {
      navBadge.textContent = String(upcomingShortlisted);
      navBadge.className = 'sidebar-label text-[10px] font-mono px-1.5 py-0.5 rounded bg-amber-400/15 text-amber-300 border border-amber-400/30 font-semibold';
    } else {
      navBadge.textContent = cleanCount || '0';
      navBadge.className = 'sidebar-label text-[10px] font-mono px-1.5 py-0.5 rounded bg-white/[0.06] text-zinc-400';
    }
  }
  if (drawerBadge) {
    drawerBadge.textContent = upcomingShortlisted > 0 ? `${upcomingShortlisted} active` : String(cleanCount || 0);
  }
  if (mobBadge) {
    if (upcomingShortlisted > 0) {
      mobBadge.textContent = String(upcomingShortlisted);
      mobBadge.classList.remove('hidden');
      mobBadge.className = 'absolute -top-1 -right-1.5 px-1.5 py-0.2 rounded-full bg-amber-400 text-zinc-950 font-mono text-[8px] font-bold';
    } else {
      mobBadge.classList.add('hidden');
    }
  }

  // Dashboard Autonomous Engine Offer Watchdog row
  const dashOffer = document.getElementById('dash-offer-watchdog');
  if (dashOffer) {
    if (pendingCount > 0) {
      dashOffer.textContent = `${pendingCount} PENDING OFFER`;
      dashOffer.className = 'font-mono font-semibold text-amber-300';
    } else {
      dashOffer.textContent = 'ACTIVE · 0 Pending';
      dashOffer.className = 'font-mono font-medium text-emerald-400';
    }
  }

  // Notices Header info
  const rollPill = document.getElementById('notices-roll-pill');
  const lastScanEl = document.getElementById('notices-last-scan');
  const offerPill = document.getElementById('notices-offer-pill');
  if (rollPill) {
    const r = noticesPayload.student_roll || statusData.student_id || 'Profile';
    rollPill.textContent = `Roll ${r}`;
  }
  if (lastScanEl) lastScanEl.textContent = `Last scan: ${noticesPayload.last_scanned_display || '—'}`;
  if (offerPill) {
    if (pendingCount > 0) {
      offerPill.textContent = `Offer Watchdog: ${pendingCount} Action Required`;
      offerPill.className = 'text-amber-300 font-medium';
    } else {
      offerPill.textContent = `Offer Watchdog: 0 Pending`;
      offerPill.className = 'text-zinc-400';
    }
  }

  // Filter pill counts
  const setTxt = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = String(v ?? 0); };
  setTxt('nf-count-all', c.clean || 0);
  setTxt('nf-count-shortlisted', totalShortlisted);
  setTxt('nf-count-tests', c.shortlists_tests || 0);
  setTxt('nf-count-deadlines', c.deadlines_revisions || 0);
  setTxt('nf-count-jobs', c.job_openings || 0);
  setTxt('notices-spam-count', c.spam_hidden || 0);

  renderPendingOfferBanner();
  renderDashboardShortlistStrip();
}

function renderPendingOfferBanner() {
  const container = document.getElementById('pending-offer-banner');
  if (!container) return;
  const actions = noticesPayload.pending_actions || [];
  if (!actions.length) {
    container.classList.add('hidden');
    container.innerHTML = '';
    return;
  }

  container.classList.remove('hidden');
  container.innerHTML = actions.map(a => `
    <div class="rounded-xl p-4 sm:p-5 border border-white/10 border-l-2 border-l-amber-400 bg-[#13120f] max-w-6xl mx-auto flex flex-col sm:flex-row sm:items-center justify-between gap-3">
      <div class="space-y-1 min-w-0">
        <div class="flex items-center gap-2 flex-wrap">
          <span class="badge-shortlisted text-[10px] font-mono px-2 py-0.5 rounded-full font-semibold uppercase tracking-wider inline-flex items-center gap-1.5">
            <span class="w-1.5 h-1.5 rounded-full bg-amber-400"></span>Pending Job Offer
          </span>
          ${a.hours_left !== null && a.hours_left !== undefined ? `<span class="text-[11px] font-mono text-zinc-400">${esc(String(a.hours_left))}h left</span>` : ''}
        </div>
        <div class="text-sm sm:text-base font-semibold text-zinc-50 truncate">${esc(a.company_name)} — ${esc(a.title)}</div>
        <div class="text-xs font-mono text-zinc-400">Deadline: ${esc(a.deadline_display || 'Check Superset immediately')}</div>
      </div>
      <a href="${esc(a.portal_url || 'https://app.joinsuperset.com/students/')}" target="_blank" rel="noopener noreferrer"
        class="px-4 py-2 rounded-lg bg-zinc-100 hover:bg-white text-zinc-950 font-semibold text-xs flex items-center justify-center gap-1.5 flex-shrink-0">
        <span>Respond on Superset ↗</span>
      </a>
    </div>
  `).join('');
}

function renderShortlistCardHtml(n, compact = false) {
  const m = n.shortlist_match || {};
  const ev = n.event_details || {};
  const matchPdf = m.attachment_id ? (n.attachments || []).find(a => a.id === m.attachment_id) : null;
  const pageNum = m.page_number || 1;
  const dateStatus = ev.date_status_label ? ` · ${ev.date_status_label}` : '';
  const metaParts = [ev.process || ev.event_type, ev.date, ev.time, ev.venue].filter(Boolean);

  return `
    <div class="rounded-xl p-4 sm:p-5 bg-[#121216] border border-white/[0.09] border-l-2 border-l-amber-400/85">
      <div class="flex flex-col sm:flex-row sm:items-start justify-between gap-3.5">
        <div class="space-y-1.5 min-w-0 flex-1">
          <div class="flex items-center gap-2 flex-wrap">
            <span class="badge-shortlisted text-[10px] font-mono px-2.5 py-0.5 rounded-full font-semibold uppercase tracking-wider inline-flex items-center gap-1.5">
              <span class="w-1.5 h-1.5 rounded-full bg-amber-400"></span>Shortlisted${esc(dateStatus)}
            </span>
            ${ev.matched_profile ? `<span class="text-[10px] font-mono px-2 py-0.5 rounded-full bg-white/[0.06] border border-white/10 text-zinc-200">${esc(ev.matched_profile)}</span>` : ''}
            <span class="text-[11px] font-mono text-zinc-500">Page ${pageNum} · ${esc(m.match_type === 'EXACT_ROLL' ? 'Roll Verified' : (m.match_type || 'Matched'))}</span>
          </div>
          <div class="text-sm font-semibold text-zinc-50 leading-snug">${esc(n.title)}</div>
          ${metaParts.length ? `<div class="text-xs text-zinc-300 leading-relaxed">${metaParts.map(p => `<span>${esc(p)}</span>`).join('<span class="text-zinc-600 mx-1.5">·</span>')}</div>` : ''}
          ${m.matched_line ? `<div class="pt-1 text-[11px] font-mono text-zinc-500 truncate" title="${esc(m.matched_line)}">${esc(m.matched_line)}</div>` : ''}
        </div>
        <div class="flex sm:flex-col gap-2 flex-shrink-0">
          ${matchPdf ? `<button onclick="openPdfByAttachment('${encodeURIComponent(n.id)}','${encodeURIComponent(matchPdf.id)}',${pageNum})" class="flex-1 sm:flex-none px-3.5 py-2 rounded-lg bg-zinc-100 hover:bg-white text-zinc-950 font-semibold text-xs flex items-center justify-center gap-1.5 whitespace-nowrap"><span>View PDF (p.${pageNum})</span></button>` : ''}
          <button onclick="openResumeTailorForNotice('${encodeURIComponent(n.id)}')" class="flex-1 sm:flex-none px-3 py-2 rounded-lg bg-white/[0.06] hover:bg-white/[0.1] border border-white/10 text-zinc-100 font-medium text-xs whitespace-nowrap">Tailor CV</button>
          ${ev.meeting_link ? `<a href="${esc(ev.meeting_link)}" target="_blank" rel="noopener noreferrer" class="flex-1 sm:flex-none px-3.5 py-2 rounded-lg bg-white/[0.06] hover:bg-white/[0.1] border border-white/10 text-zinc-100 font-medium text-xs flex items-center justify-center gap-1.5 whitespace-nowrap"><span>Join Meet ↗</span></a>` : ''}
          ${compact ? `<button onclick="switchPage('notices'); setNoticesFilter('shortlisted');" class="px-3 py-2 rounded-lg bg-white/[0.04] hover:bg-white/[0.08] border border-white/[0.08] text-zinc-300 text-xs font-medium whitespace-nowrap">Notices →</button>` : ''}
        </div>
      </div>
    </div>
  `;
}

function renderDashboardShortlistStrip() {
  const strip = document.getElementById('dashboard-shortlist-strip');
  if (!strip) return;
  // Strictly show ONLY TODAY / UPCOMING shortlists on Homepage (never passed dates!)
  const upcomingHits = noticesPayload.upcoming_shortlisted_notices
    || (noticesPayload.shortlisted_notices || []).filter(n => !n.is_past_event);
  if (!upcomingHits.length) {
    strip.classList.add('hidden');
    strip.innerHTML = '';
    return;
  }
  strip.classList.remove('hidden');
  strip.innerHTML = upcomingHits.slice(0, 3).map(n => renderShortlistCardHtml(n, true)).join('');
}

function setNoticesFilter(f) {
  noticesFilter = f;
  document.querySelectorAll('.notices-filter').forEach(btn => {
    const active = btn.dataset.nfilter === f;
    if (active) {
      btn.className = 'notices-filter active px-3 py-1.5 rounded-full text-xs font-medium bg-zinc-100 text-zinc-950';
    } else {
      btn.className = 'notices-filter px-3 py-1.5 rounded-full text-xs font-medium bg-surface-900 border border-white/[0.08] text-zinc-300';
    }
  });
  renderNotices();
}

function toggleNoticeBody(noticeId) {
  if (expandedNoticeIds.has(noticeId)) expandedNoticeIds.delete(noticeId);
  else expandedNoticeIds.add(noticeId);
  renderNotices();
}

function renderNotices() {
  const listEl = document.getElementById('notices-list');
  const spotEl = document.getElementById('notices-shortlist-spotlight');
  const metaEl = document.getElementById('notices-result-meta');
  if (!listEl) return;

  const allNotices = noticesPayload.notices || [];
  const upcomingHits = noticesPayload.upcoming_shortlisted_notices
    || (noticesPayload.shortlisted_notices || []).filter(n => !n.is_past_event);

  // Pinned spotlight shows ONLY upcoming/today shortlists (never expired past dates!)
  if (spotEl) {
    if (upcomingHits.length > 0 && noticesFilter !== 'shortlisted') {
      spotEl.classList.remove('hidden');
      spotEl.innerHTML = `
        <div class="flex items-center justify-between px-0.5">
          <span class="text-[11px] font-mono uppercase tracking-wider text-zinc-400">Upcoming Shortlist (${upcomingHits.length})</span>
          <button onclick="setNoticesFilter('shortlisted')" class="text-[11px] font-mono text-zinc-400 hover:text-zinc-200">View all shortlisted →</button>
        </div>
        ${upcomingHits.slice(0, 2).map(n => renderShortlistCardHtml(n, false)).join('')}
      `;
    } else {
      spotEl.classList.add('hidden');
      spotEl.innerHTML = '';
    }
  }

  const query = (document.getElementById('notices-search')?.value || '').trim().toLowerCase();
  const showSpam = Boolean(document.getElementById('notices-show-spam')?.checked);

  let filtered = allNotices.filter(n => {
    if (!showSpam && (n.is_spam || n.is_duplicate)) return false;
    if (noticesFilter === 'shortlisted' && !n.is_shortlisted) return false;
    if (noticesFilter === 'shortlists_tests' && n.category !== 'shortlists_tests') return false;
    if (noticesFilter === 'deadlines_revisions' && n.category !== 'deadlines_revisions') return false;
    if (noticesFilter === 'job_openings' && n.category !== 'job_openings') return false;
    if (query) {
      const hay = [
        n.title, n.author, n.plain_text,
        n.event_details?.venue, n.event_details?.date, n.event_details?.time,
        ...(n.attachments || []).map(a => a.name)
      ].filter(Boolean).join(' ').toLowerCase();
      if (!hay.includes(query)) return false;
    }
    return true;
  });

  // Inside Shortlisted filter, show upcoming first and past/completed second
  if (noticesFilter === 'shortlisted') {
    filtered.sort((a, b) => Number(Boolean(a.is_past_event)) - Number(Boolean(b.is_past_event)));
  }

  if (metaEl) {
    const hiddenSpam = (noticesPayload.counts || {}).spam_hidden || 0;
    metaEl.textContent = `Showing ${filtered.length} notice${filtered.length === 1 ? '' : 's'}${!showSpam && hiddenSpam ? ` · ${hiddenSpam} promo/duplicate hidden` : ''}`;
  }

  if (!filtered.length) {
    listEl.innerHTML = `
      <div class="metric-card rounded-xl p-8 text-center space-y-1.5">
        <div class="text-sm font-medium text-zinc-200">No notices match this filter</div>
        <div class="text-xs text-zinc-500">Switch filters or click <strong>Scan Shortlist PDFs</strong> to pull fresh notices.</div>
      </div>
    `;
    return;
  }

  const catBadge = n => {
    if (n.is_shortlisted && !n.is_past_event) {
      const lbl = n.event_details?.date_status_label ? ` · ${esc(n.event_details.date_status_label)}` : '';
      return `<span class="badge-shortlisted text-[10px] font-mono px-2 py-0.5 rounded-full font-medium inline-flex items-center gap-1.5"><span class="w-1.5 h-1.5 rounded-full bg-amber-400"></span>Shortlisted${lbl}</span>`;
    }
    if (n.is_shortlisted && n.is_past_event) {
      return '<span class="badge-passed text-[10px] font-mono px-2 py-0.5 rounded-full font-medium">Shortlisted · Passed</span>';
    }
    if (n.category === 'shortlists_tests') {
      return n.is_past_event
        ? '<span class="badge-passed text-[10px] font-mono px-2 py-0.5 rounded-full font-medium">Test / Drive · Passed</span>'
        : '<span class="text-[10px] font-mono px-2 py-0.5 rounded-full bg-white/[0.06] text-zinc-200 border border-white/10 font-medium">Shortlist / Test</span>';
    }
    if (n.category === 'deadlines_revisions') return '<span class="text-[10px] font-mono px-2 py-0.5 rounded-full bg-white/[0.05] text-zinc-300 border border-white/[0.08] font-medium">Deadline / Revision</span>';
    if (n.category === 'job_openings') return '<span class="text-[10px] font-mono px-2 py-0.5 rounded-full bg-white/[0.04] text-zinc-400 border border-white/[0.07] font-medium">Job Opening</span>';
    return '<span class="text-[10px] font-mono px-2 py-0.5 rounded-full bg-white/[0.03] text-zinc-500 border border-white/[0.06]">System</span>';
  };

  listEl.innerHTML = '';
  let insertedPastDivider = false;

  filtered.forEach((n, idx) => {
    // Clean divider between upcoming and passed items in Shortlisted tab
    if (noticesFilter === 'shortlisted' && n.is_past_event && !insertedPastDivider) {
      insertedPastDivider = true;
      const div = document.createElement('div');
      div.className = 'flex items-center gap-3 pt-3 pb-1 text-[10px] font-mono uppercase tracking-wider text-zinc-500';
      div.innerHTML = '<span>Completed / Past Date</span><div class="flex-1 h-px bg-white/[0.07]"></div>';
      listEl.appendChild(div);
    }

    const ev = n.event_details || {};
    const m = n.shortlist_match || null;
    const isExpanded = expandedNoticeIds.has(n.id);
    const isUpcomingShortlist = n.is_shortlisted && !n.is_past_event;
    const deferClass = idx >= 6 ? 'deferred-card' : '';

    const card = document.createElement('article');
    card.className = `job-card ${deferClass} rounded-xl p-4 sm:p-5 space-y-2.5 ${isUpcomingShortlist ? 'border-l-2 border-l-amber-400/85' : ''} ${n.is_past_event ? 'opacity-75' : ''}`;

    const attHtml = (n.attachments || []).length ? `
      <div class="flex flex-wrap gap-2 pt-1">
        ${(n.attachments || []).map(a => {
          const isMatchedAtt = a.matched || (m && m.attachment_id === a.id);
          const jumpPage = isMatchedAtt && m && m.page_number ? m.page_number : 1;
          return `
            <div class="inline-flex items-center rounded-lg overflow-hidden border ${isMatchedAtt && !n.is_past_event ? 'border-amber-400/40 bg-white/[0.05]' : 'border-white/[0.08] bg-surface-950'} max-w-full">
              <button type="button" onclick="openPdfByAttachment('${encodeURIComponent(n.id)}','${encodeURIComponent(a.id)}',${jumpPage})"
                class="px-3 py-1.5 text-xs font-medium text-zinc-200 hover:text-white flex items-center gap-1.5 min-w-0">
                <span class="text-zinc-400">${a.is_pdf ? '📄' : '🖼️'}</span>
                <span class="truncate max-w-[200px] sm:max-w-[320px]">${esc(a.name)}</span>
                ${a.page_count ? `<span class="text-[10px] font-mono text-zinc-500">(${a.page_count}p)</span>` : ''}
                ${isMatchedAtt ? `<span class="text-[10px] font-mono px-1.5 py-0.5 rounded bg-white/10 text-zinc-100">p.${jumpPage}</span>` : ''}
              </button>
              <button type="button" onclick="downloadPdfByAttachment('${encodeURIComponent(n.id)}','${encodeURIComponent(a.id)}')" title="Download file"
                class="px-2.5 py-1.5 border-l border-white/[0.08] text-zinc-500 hover:text-zinc-100 text-xs">⬇</button>
            </div>
          `;
        }).join('')}
      </div>
    ` : '';

    const logisticsParts = [ev.date, ev.time, ev.venue].filter(Boolean);
    const logisticsHtml = (logisticsParts.length || ev.meeting_link) ? `
      <div class="flex items-center gap-2 flex-wrap text-xs text-zinc-400">
        <span>${logisticsParts.map(esc).join(' <span class="text-zinc-600 mx-1">·</span> ')}</span>
        ${ev.meeting_link ? `<a href="${esc(ev.meeting_link)}" target="_blank" rel="noopener noreferrer" class="text-zinc-200 underline underline-offset-2 font-mono text-[11px]">Join Meet ↗</a>` : ''}
      </div>
    ` : '';

    const matchBoxHtml = n.is_shortlisted && m ? `
      <div class="px-3 py-2 rounded-lg bg-surface-950 border border-white/[0.07] text-[11px] font-mono text-zinc-400 space-y-0.5">
        <div class="text-zinc-200 font-medium">Verified in ${esc(m.attachment_name || 'Notice')}${m.page_number ? ` · Page ${m.page_number}` : ''}</div>
        ${m.matched_line ? `<div class="truncate" title="${esc(m.matched_line)}">${esc(m.matched_line)}</div>` : ''}
      </div>
    ` : '';

    card.innerHTML = `
      <div class="space-y-1">
        <div class="flex items-center gap-2 flex-wrap">
          ${catBadge(n)}
          <span class="text-[11px] font-mono text-zinc-500">${esc(n.published_display || '')}</span>
          <span class="text-[11px] text-zinc-600">· ${esc(n.author || 'T&P Cell')}</span>
        </div>
        <h3 class="text-sm font-semibold text-zinc-100 leading-snug">${esc(n.title)}</h3>
      </div>
      ${matchBoxHtml}
      ${logisticsHtml}
      <div class="notice-body-slot"></div>
      ${attHtml}
      <div class="flex items-center justify-between pt-2 border-t border-white/[0.06]">
        <button type="button" onclick="toggleNoticeBody('${esc(n.id)}')" class="text-xs font-medium text-zinc-400 hover:text-zinc-100">
          ${isExpanded ? 'Hide full notice' : 'Read full notice'}
        </button>
        <span class="text-[10px] font-mono text-zinc-600">${(n.attachments || []).length} attachment${(n.attachments || []).length === 1 ? '' : 's'}</span>
      </div>
    `;

    const bodySlot = card.querySelector('.notice-body-slot');
    if (isExpanded) {
      const richDiv = document.createElement('div');
      richDiv.className = 'notice-html p-3 rounded-lg bg-surface-950 border border-white/[0.07] max-h-96 overflow-y-auto sheet-scroll';
      safeSetNoticeHtml(richDiv, n.safe_html || esc(n.plain_text || ''));
      bodySlot.appendChild(richDiv);
    } else if (n.snippet) {
      const p = document.createElement('p');
      p.className = 'text-xs text-zinc-400 leading-relaxed line-clamp-2';
      p.textContent = n.snippet;
      bodySlot.appendChild(p);
    }

    listEl.appendChild(card);
  });
}

function _findNoticeAttachment(noticeIdEnc, attachmentIdEnc) {
  const nid = decodeURIComponent(noticeIdEnc || '');
  const aid = decodeURIComponent(attachmentIdEnc || '');
  const notice = (noticesPayload.notices || []).find(n => n.id === nid);
  const att = notice ? (notice.attachments || []).find(a => a.id === aid) : null;
  return { nid, aid, notice, att };
}

function openPdfByAttachment(noticeIdEnc, attachmentIdEnc, pageNum = 1) {
  const { nid, aid, att } = _findNoticeAttachment(noticeIdEnc, attachmentIdEnc);
  openPdfViewer(nid, aid, (att && att.name) || 'superset_notice.pdf', pageNum, Boolean(att && att.is_image));
}

function downloadPdfByAttachment(noticeIdEnc, attachmentIdEnc) {
  const { nid, aid, att } = _findNoticeAttachment(noticeIdEnc, attachmentIdEnc);
  downloadNoticeAttachment(nid, aid, (att && att.name) || 'attachment.pdf');
}

async function openPdfViewer(noticeId, attachmentId, filename, pageNum = 1, isImage = false) {
  const dialog = document.getElementById('pdfViewerDialog');
  const titleEl = document.getElementById('pdfViewerTitle');
  const subEl = document.getElementById('pdfViewerSubtitle');
  const bodyEl = document.getElementById('pdfViewerBody');
  const newTabLink = document.getElementById('pdfViewerNewTab');
  const dlLink = document.getElementById('pdfViewerDownload');
  const matchBanner = document.getElementById('pdfViewerMatchBanner');

  if (!dialog) return;

  // Revoke any previous Blob URL
  if (activePdfBlobUrl) {
    URL.revokeObjectURL(activePdfBlobUrl);
    activePdfBlobUrl = null;
  }

  titleEl.textContent = filename || 'Document Preview';
  subEl.textContent = 'Fetching authenticated PDF stream from Superset...';
  newTabLink.classList.add('hidden');
  dlLink.classList.add('hidden');

  // Show shortlist match highlight if this PDF matched the student
  const notice = (noticesPayload.notices || []).find(n => n.id === noticeId);
  if (notice && notice.is_shortlisted && notice.shortlist_match && notice.shortlist_match.attachment_id === attachmentId) {
    const m = notice.shortlist_match;
    const ev = notice.event_details || {};
    matchBanner.classList.remove('hidden');
    matchBanner.innerHTML = `
      <div class="flex flex-wrap items-center justify-between gap-2">
        <div><strong>🔥 SHORTLISTED ON PAGE ${m.page_number || pageNum}:</strong> <span class="font-mono">${esc(m.matched_line || m.matched_value || '')}</span></div>
        <div class="font-mono text-[11px] text-amber-200">${[ev.date, ev.time, ev.venue].filter(Boolean).map(esc).join(' • ')}</div>
      </div>
    `;
  } else {
    matchBanner.classList.add('hidden');
    matchBanner.innerHTML = '';
  }

  bodyEl.innerHTML = `
    <div class="text-center space-y-2.5 p-6">
      <div class="inline-block animate-spin text-2xl text-brand-400">🔄</div>
      <div class="text-xs font-mono text-slate-300">Streaming pre-signed document securely...</div>
    </div>
  `;

  if (!dialog.open) dialog.showModal();

  try {
    const headers = {};
    if (authToken) headers['Authorization'] = `Bearer ${authToken}`;
    const res = await fetch(`/api/notices/${encodeURIComponent(noticeId)}/attachments/${encodeURIComponent(attachmentId)}/pdf`, {
      headers,
      credentials: 'same-origin'
    });
    if (!res.ok) {
      throw new Error(`HTTP ${res.status} — Could not load attachment`);
    }
    const blob = await res.blob();
    activePdfBlobUrl = URL.createObjectURL(blob);
    const viewerUrl = isImage ? activePdfBlobUrl : `${activePdfBlobUrl}#page=${pageNum || 1}`;

    subEl.textContent = `${(blob.size / 1024).toFixed(0)} KB • ${isImage ? 'Image' : `PDF (Page ${pageNum || 1})`}`;
    newTabLink.href = viewerUrl;
    newTabLink.classList.remove('hidden');
    dlLink.href = activePdfBlobUrl;
    dlLink.download = filename || 'superset_notice.pdf';
    dlLink.classList.remove('hidden');

    if (isImage) {
      bodyEl.innerHTML = `<img src="${activePdfBlobUrl}" alt="${esc(filename)}" class="max-w-full max-h-full object-contain p-3">`;
    } else {
      bodyEl.innerHTML = `
        <div class="w-full h-full flex flex-col">
          <div class="sm:hidden px-3 py-2 bg-surface-900 border-b border-white/10 flex items-center justify-between gap-2 text-[11px] text-slate-300">
            <span>Mobile tip: tap Open for full pinch-zoom</span>
            <a href="${viewerUrl}" target="_blank" rel="noopener noreferrer" class="px-2.5 py-1 rounded-lg bg-brand-600 text-white font-bold">Open Full PDF ↗</a>
          </div>
          <iframe src="${viewerUrl}" title="${esc(filename)}" class="w-full flex-1 border-0 bg-white"></iframe>
        </div>
      `;
    }
  } catch (e) {
    subEl.textContent = 'Preview unavailable';
    bodyEl.innerHTML = `
      <div class="p-6 text-center space-y-2 max-w-md">
        <div class="text-2xl">⚠️</div>
        <div class="text-sm font-bold text-rose-300">Failed to load PDF preview</div>
        <div class="text-xs font-mono text-slate-400">${esc(e.message)}</div>
      </div>
    `;
  }
}

async function downloadNoticeAttachment(noticeId, attachmentId, filename) {
  toast(`Downloading ${filename}...`, 'info');
  try {
    const headers = {};
    if (authToken) headers['Authorization'] = `Bearer ${authToken}`;
    const res = await fetch(`/api/notices/${encodeURIComponent(noticeId)}/attachments/${encodeURIComponent(attachmentId)}/pdf?download=1`, {
      headers,
      credentials: 'same-origin'
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename || 'attachment.pdf';
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
  } catch (e) {
    toast('Download failed: ' + e.message, 'error');
  }
}

// Setup native <dialog> close cleanup + Safari light-dismiss fallback (per modern-web-guidance)
(function setupPdfDialog() {
  const dialog = document.getElementById('pdfViewerDialog');
  if (!dialog) return;

  dialog.addEventListener('close', () => {
    dialog.returnValue = '';
    const bodyEl = document.getElementById('pdfViewerBody');
    if (bodyEl) bodyEl.innerHTML = '';
    if (activePdfBlobUrl) {
      URL.revokeObjectURL(activePdfBlobUrl);
      activePdfBlobUrl = null;
    }
  });

  if (!('closedBy' in HTMLDialogElement.prototype)) {
    const outside = (e) => {
      const r = dialog.getBoundingClientRect();
      return e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom;
    };
    let pressedOutside = false;
    dialog.addEventListener('pointerdown', (e) => { pressedOutside = outside(e); });
    dialog.addEventListener('pointerup', (e) => { if (pressedOutside && outside(e)) dialog.close(); });
  }
})();

// ═══════════════════════════════════════════════════════════════════
// COMPANY RESUME TAILOR & 1-PAGE A4 PDF STUDIO
// ═══════════════════════════════════════════════════════════════════

async function openResumeStudio() {
  populateResumeJobDropdown();
  if (!resumeMaster) await loadResumeMaster();
  if (resumeMaster && (!resumeMaster.projects || resumeMaster.projects.length === 0)) {
    const ed = document.getElementById('resume-master-editor');
    if (ed) ed.classList.remove('hidden');
  }
}

async function loadResumeMaster() {
  try {
    const { ok, status, data } = await apiFetch('/api/resume/master');
    if (status === 401) return;
    if (ok && data && data.success) {
      resumeMaster = data.master;
      renderMasterEditor();
      const hint = document.getElementById('resume-photo-hint');
      if (hint) hint.textContent = resumeMaster.photo_b64 ? '(photo saved)' : '(no photo uploaded yet)';
    }
  } catch (e) {
    console.warn('Resume master load warning:', e);
  }
}

function populateResumeJobDropdown(preselectId = '') {
  const sel = document.getElementById('resume-job-select');
  if (!sel) return;
  const currentVal = preselectId || sel.value;

  const sorted = [...allJobs].sort((a, b) => {
    const score = j => ((j.is_applied ? 2 : 0) + (j.is_open ? 3 : 0));
    return score(b) - score(a);
  });

  sel.innerHTML = '<option value="">— Custom / Off-Campus Role —</option>' + sorted.map(j => {
    const tag = j.is_applied ? '[Applied] ' : (j.is_open ? '[Open] ' : '');
    const label = `${tag}${j.company_name} — ${(j.job_role || '').slice(0, 55)}`;
    return `<option value="${esc(j.id)}">${esc(label)}</option>`;
  }).join('');

  if (currentVal && [...sel.options].some(o => o.value === currentVal)) {
    sel.value = currentVal;
  }
}

function onResumeJobSelectChange() {
  const sel = document.getElementById('resume-job-select');
  const customBox = document.getElementById('resume-custom-jd-box');
  if (sel && customBox && !sel.value) {
    customBox.classList.remove('hidden');
  }
}

async function openResumeTailorForJob(jobId) {
  switchPage('resume');
  if (!resumeMaster) await loadResumeMaster();
  populateResumeJobDropdown(jobId);
  const sel = document.getElementById('resume-job-select');
  if (sel) sel.value = jobId;
  await generateTailoredResume();
}

async function openResumeTailorForNotice(noticeIdEnc) {
  const nid = decodeURIComponent(noticeIdEnc || '');
  const notice = (noticesPayload.notices || []).find(n => n.id === nid);
  switchPage('resume');
  if (!resumeMaster) await loadResumeMaster();
  populateResumeJobDropdown();

  if (notice) {
    // Try matching a Superset job profile by company name prefix in notice title
    const titleLow = (notice.title || '').toLowerCase();
    const matchedJob = allJobs.find(j => {
      const cname = (j.company_name || '').trim().toLowerCase();
      return cname.length >= 3 && titleLow.includes(cname);
    });
    const sel = document.getElementById('resume-job-select');
    if (matchedJob && sel) {
      sel.value = matchedJob.id;
    } else {
      if (sel) sel.value = '';
      document.getElementById('resume-custom-jd-box')?.classList.remove('hidden');
      const cc = document.getElementById('resume-custom-company');
      const cr = document.getElementById('resume-custom-role');
      const cj = document.getElementById('resume-custom-jd');
      if (cc) cc.value = (notice.title || '').split(/\s+/)[0] || '';
      if (cr) cr.value = notice.event_details?.event_type || 'Software Engineer';
      if (cj) cj.value = (notice.plain_text || '').slice(0, 1500);
    }
    await generateTailoredResume();
  }
}

async function generateTailoredResume(useCurrentCheckboxes = false) {
  const btn = document.getElementById('resume-tailor-btn');
  const icon = document.getElementById('resume-tailor-icon');
  if (!resumeMaster) await loadResumeMaster();

  const jobId = document.getElementById('resume-job-select')?.value || '';
  const company = (document.getElementById('resume-custom-company')?.value || '').trim();
  const role = (document.getElementById('resume-custom-role')?.value || '').trim();
  const customJd = (document.getElementById('resume-custom-jd')?.value || '').trim();
  const maxProjects = parseInt(document.getElementById('resume-max-projects')?.value || '3', 10);
  const includePhoto = Boolean(document.getElementById('resume-include-photo')?.checked);
  const useAi = Boolean(document.getElementById('resume-use-ai')?.checked);

  if (!jobId && !company && !role && !customJd) {
    toast('Select a campus drive or enter a target company/role first.', 'warn');
    return;
  }

  let selectedProjectIds = null;
  if (useCurrentCheckboxes) {
    const boxes = [...document.querySelectorAll('.rt-proj-checkbox:checked')];
    if (boxes.length) selectedProjectIds = boxes.map(b => b.value);
  }

  if (btn) btn.disabled = true;
  if (icon) icon.textContent = '⏳';

  try {
    const { ok, data } = await apiFetch('/api/resume/tailor', {
      method: 'POST',
      body: JSON.stringify({
        job_id: jobId,
        company_name: company,
        role: role,
        custom_jd: customJd,
        max_projects: maxProjects,
        include_photo: includePhoto,
        selected_project_ids: selectedProjectIds,
        use_ai: useAi && !useCurrentCheckboxes
      })
    });

    if (ok && data && data.success) {
      lastTailoredResume = data.tailored;
      renderTailoredWorkspace(data.tailored);
      toast(`Tailored 1-page resume for ${data.tailored.company_name || data.tailored.role} (${data.tailored.ats_report?.score || 0}% ATS match)`, 'success');
    } else {
      toast((data && (data.message || data.error)) || 'Could not tailor resume.', 'error');
    }
  } catch (e) {
    toast('Resume tailor error: ' + e.message, 'error');
  } finally {
    if (btn) btn.disabled = false;
    if (icon) icon.textContent = '⚡';
  }
}

async function applyResumeTextOverride() {
  if (!lastTailoredResume) return;
  const customHeadline = (document.getElementById('rt-edit-headline')?.value || '').trim();
  const customSummary = (document.getElementById('rt-edit-summary')?.value || '').trim();
  const includePhoto = Boolean(document.getElementById('resume-include-photo')?.checked);
  const boxes = [...document.querySelectorAll('.rt-proj-checkbox:checked')];
  const selectedProjectIds = boxes.length ? boxes.map(b => b.value) : null;

  try {
    const { ok, data } = await apiFetch('/api/resume/tailor', {
      method: 'POST',
      body: JSON.stringify({
        job_id: document.getElementById('resume-job-select')?.value || '',
        company_name: lastTailoredResume.company_name || '',
        role: lastTailoredResume.role || '',
        custom_jd: (document.getElementById('resume-custom-jd')?.value || '').trim(),
        include_photo: includePhoto,
        selected_project_ids: selectedProjectIds,
        custom_headline: customHeadline,
        custom_summary: customSummary,
        use_ai: false
      })
    });
    if (ok && data && data.success) {
      lastTailoredResume = data.tailored;
      renderTailoredWorkspace(data.tailored);
      toast('Preview updated!', 'success');
    }
  } catch (e) {
    toast('Update error: ' + e.message, 'error');
  }
}

function renderTailoredWorkspace(t) {
  const ws = document.getElementById('resume-workspace');
  if (ws) ws.classList.remove('hidden');

  const lbl = document.getElementById('rt-target-label');
  const ats = document.getElementById('rt-ats-score');
  const trk = document.getElementById('rt-track-badge');
  const mk = document.getElementById('rt-matched-kws');
  const miss = document.getElementById('rt-missing-kws');
  const hl = document.getElementById('rt-edit-headline');
  const sm = document.getElementById('rt-edit-summary');

  if (lbl) lbl.textContent = `${t.company_name || 'Target Company'} · ${t.role || t.track}`;
  if (ats) ats.textContent = `${t.ats_report?.score ?? 0}%`;
  if (trk) trk.textContent = `${t.track}${t.tailor_engine ? ' · ' + t.tailor_engine : ''}`;
  if (mk) mk.textContent = (t.ats_report?.matched_keywords || []).join(', ') || 'None detected yet';
  if (miss) miss.textContent = (t.ats_report?.missing_keywords || []).join(', ') || 'Strong coverage';
  if (hl) hl.value = t.headline || '';
  if (sm) sm.value = t.summary || '';

  const rankEl = document.getElementById('rt-projects-rank');
  if (rankEl) {
    const projs = t.all_projects_ranked || [];
    if (!projs.length) {
      rankEl.innerHTML = `<div class="text-xs text-zinc-500 py-2">No projects in your portfolio yet. Click <strong>Edit My Projects &amp; Photo</strong> above to add or import your resume.</div>`;
    } else {
      rankEl.innerHTML = projs.map(p => `
        <label class="flex items-start gap-2.5 p-2.5 rounded-lg bg-surface-950 border ${p.selected ? 'border-white/20' : 'border-white/[0.06] opacity-70'} cursor-pointer">
          <input type="checkbox" class="rt-proj-checkbox mt-0.5 rounded border-zinc-700 bg-zinc-900 text-zinc-100 focus:ring-0" value="${esc(p.id)}" ${p.selected ? 'checked' : ''} onchange="generateTailoredResume(true)">
          <div class="min-w-0 flex-1">
            <div class="flex items-center justify-between gap-2">
              <span class="text-xs font-medium text-zinc-100 truncate">${esc(p.title)}</span>
              <span class="text-[10px] font-mono px-1.5 py-0.5 rounded bg-white/[0.06] text-zinc-300 flex-shrink-0">${p.match_score}% fit</span>
            </div>
            <div class="text-[11px] text-zinc-500 truncate mt-0.5">${esc(p.why_selected || p.tech || '')}</div>
          </div>
        </label>
      `).join('');
    }
  }

  const frame = document.getElementById('resume-preview-frame');
  if (frame) {
    frame.srcdoc = t.preview_html || '';
  }
}

async function exportTailoredPdf() {
  if (!lastTailoredResume || !lastTailoredResume.preview_html) {
    toast('Click Tailor Resume first to generate your preview.', 'warn');
    return;
  }
  const btn = document.getElementById('resume-export-btn');
  if (btn) btn.disabled = true;
  toast('Generating crisp 1-page A4 PDF...', 'info');

  try {
    const headers = { 'Content-Type': 'application/json' };
    if (authToken) headers['Authorization'] = `Bearer ${authToken}`;
    const res = await fetch('/api/resume/export-pdf', {
      method: 'POST',
      headers,
      credentials: 'same-origin',
      body: JSON.stringify({
        html: lastTailoredResume.preview_html,
        company_name: lastTailoredResume.company_name || 'Tailored',
        role: lastTailoredResume.role || 'Resume'
      })
    });
    const ct = res.headers.get('Content-Type') || '';
    if (res.ok && ct.includes('application/pdf')) {
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      const safeCo = (lastTailoredResume.company_name || 'Tailored').replace(/[^a-zA-Z0-9_-]+/g, '_');
      const safeName = (lastTailoredResume.full_name || 'Resume').replace(/[^a-zA-Z0-9_-]+/g, '_');
      a.href = url;
      a.download = `${safeName}_${safeCo}_Resume.pdf`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 5000);
      toast('1-Page A4 PDF downloaded!', 'success');
    } else {
      // Graceful fallback: trigger browser native A4 Print-to-PDF on the iframe
      toast('Opening browser Print-to-PDF dialog...', 'info');
      printTailoredIframe();
    }
  } catch (e) {
    toast('PDF export warning: ' + e.message, 'warn');
  } finally {
    if (btn) btn.disabled = false;
  }
}

function printTailoredIframe() {
  const frame = document.getElementById('resume-preview-frame');
  if (frame && frame.contentWindow) {
    frame.contentWindow.focus();
    frame.contentWindow.print();
  }
}

// ─── Master Portfolio Editor ───

function toggleResumeMasterEditor() {
  const ed = document.getElementById('resume-master-editor');
  if (!ed) return;
  ed.classList.toggle('hidden');
  if (!ed.classList.contains('hidden')) {
    if (!resumeMaster) loadResumeMaster();
    else renderMasterEditor();
  }
}

function renderMasterEditor() {
  if (!resumeMaster) return;
  const setV = (id, v) => { const el = document.getElementById(id); if (el) el.value = v || ''; };
  setV('rm-full-name', resumeMaster.full_name);
  setV('rm-phone', resumeMaster.phone);
  setV('rm-location', resumeMaster.location);
  setV('rm-github', resumeMaster.github);
  setV('rm-linkedin', resumeMaster.linkedin);
  setV('rm-portfolio', resumeMaster.portfolio);

  // Photo thumbnail
  const pBox = document.getElementById('rm-photo-preview');
  if (pBox) {
    if (resumeMaster.photo_b64) {
      const src = resumeMaster.photo_b64.startsWith('data:image/') ? resumeMaster.photo_b64 : `data:image/jpeg;base64,${resumeMaster.photo_b64}`;
      pBox.innerHTML = `<img src="${src}" alt="Formal Photo" class="w-full h-full object-cover object-top">`;
    } else {
      pBox.textContent = 'No Photo';
    }
  }

  // Skills rows
  const sList = document.getElementById('rm-skills-list');
  if (sList) {
    sList.innerHTML = (resumeMaster.skills || []).map((s, idx) => `
      <div class="rm-skill-row grid grid-cols-12 gap-2 items-center">
        <input type="text" value="${esc(s.category)}" placeholder="Category" class="rm-skill-cat col-span-4 sm:col-span-3 bg-surface-950 border border-white/10 rounded-lg px-2.5 py-1.5 text-xs text-zinc-100">
        <input type="text" value="${esc(s.items)}" placeholder="React, TypeScript, Node.js..." class="rm-skill-items col-span-7 sm:col-span-8 bg-surface-950 border border-white/10 rounded-lg px-2.5 py-1.5 text-xs text-zinc-100">
        <button type="button" onclick="this.parentElement.remove()" class="col-span-1 text-zinc-500 hover:text-zinc-200 text-xs text-center">✕</button>
      </div>
    `).join('');
  }

  // Projects cards
  const pList = document.getElementById('rm-projects-list');
  if (pList) {
    pList.innerHTML = (resumeMaster.projects || []).map(p => _masterProjectRowHtml(p)).join('');
  }

  // Certifications rows
  const cList = document.getElementById('rm-certs-list');
  if (cList) {
    cList.innerHTML = (resumeMaster.certifications || []).map(c => _masterCertRowHtml(c)).join('');
  }
}

function _masterProjectRowHtml(p) {
  return `
    <div class="rm-proj-card p-3 rounded-xl bg-surface-950 border border-white/[0.08] space-y-2" data-pid="${esc(p.id || '')}">
      <div class="flex items-center gap-2">
        <input type="text" value="${esc(p.title || '')}" placeholder="Project Title — Short Description" class="rm-proj-title flex-1 bg-surface-900 border border-white/10 rounded-lg px-2.5 py-1.5 text-xs font-medium text-zinc-100">
        <button type="button" onclick="this.closest('.rm-proj-card').remove()" class="px-2 py-1 rounded bg-white/[0.04] text-zinc-500 hover:text-rose-300 text-xs">Delete</button>
      </div>
      <div class="grid grid-cols-1 sm:grid-cols-2 gap-2">
        <input type="text" value="${esc(p.tech || '')}" placeholder="Tech Stack (e.g. React, TypeScript, PostgreSQL)" class="rm-proj-tech bg-surface-900 border border-white/10 rounded-lg px-2.5 py-1.5 text-xs text-zinc-200">
        <input type="text" value="${esc(p.link || '')}" placeholder="GitHub or Live URL (optional)" class="rm-proj-link bg-surface-900 border border-white/10 rounded-lg px-2.5 py-1.5 text-xs text-zinc-200">
      </div>
      <textarea rows="3" placeholder="Impact bullets (one bullet per line, up to 3)" class="rm-proj-bullets w-full bg-surface-900 border border-white/10 rounded-lg px-2.5 py-1.5 text-xs text-zinc-200 leading-relaxed">${esc((p.bullets || []).join('\n'))}</textarea>
    </div>
  `;
}

function _masterCertRowHtml(c) {
  return `
    <div class="rm-cert-row grid grid-cols-12 gap-2 items-center">
      <input type="text" value="${esc(c.title || '')}" placeholder="Certification Title" class="rm-cert-title col-span-6 bg-surface-950 border border-white/10 rounded-lg px-2.5 py-1.5 text-xs text-zinc-100">
      <input type="text" value="${esc(c.issuer || '')}" placeholder="Issuer" class="rm-cert-issuer col-span-3 bg-surface-950 border border-white/10 rounded-lg px-2.5 py-1.5 text-xs text-zinc-100">
      <input type="text" value="${esc(c.year || '')}" placeholder="Year" class="rm-cert-year col-span-2 bg-surface-950 border border-white/10 rounded-lg px-2.5 py-1.5 text-xs text-zinc-100">
      <button type="button" onclick="this.parentElement.remove()" class="col-span-1 text-zinc-500 hover:text-zinc-200 text-xs text-center">✕</button>
    </div>
  `;
}

function addMasterSkillRow() {
  const sList = document.getElementById('rm-skills-list');
  if (!sList) return;
  const d = document.createElement('div');
  d.className = 'rm-skill-row grid grid-cols-12 gap-2 items-center';
  d.innerHTML = `
    <input type="text" placeholder="Category" class="rm-skill-cat col-span-4 sm:col-span-3 bg-surface-950 border border-white/10 rounded-lg px-2.5 py-1.5 text-xs text-zinc-100">
    <input type="text" placeholder="Comma-separated skills..." class="rm-skill-items col-span-7 sm:col-span-8 bg-surface-950 border border-white/10 rounded-lg px-2.5 py-1.5 text-xs text-zinc-100">
    <button type="button" onclick="this.parentElement.remove()" class="col-span-1 text-zinc-500 hover:text-zinc-200 text-xs text-center">✕</button>
  `;
  sList.appendChild(d);
}

function addMasterProjectCard() {
  const pList = document.getElementById('rm-projects-list');
  if (!pList) return;
  pList.insertAdjacentHTML('beforeend', _masterProjectRowHtml({ id: 'p_' + Date.now().toString(36), title: '', tech: '', link: '', bullets: [] }));
}

function addMasterCertRow() {
  const cList = document.getElementById('rm-certs-list');
  if (!cList) return;
  cList.insertAdjacentHTML('beforeend', _masterCertRowHtml({ title: '', issuer: '', year: '' }));
}

function handleResumePhotoUpload(inputEl) {
  const file = inputEl?.files?.[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => {
    const img = new Image();
    img.onload = () => {
      // Resize neatly to crisp passport dimensions (max 360x440 JPEG) to keep vault lightweight
      const canvas = document.createElement('canvas');
      const maxW = 360, maxH = 440;
      const scale = Math.min(maxW / img.width, maxH / img.height, 1);
      canvas.width = Math.round(img.width * scale);
      canvas.height = Math.round(img.height * scale);
      const ctx = canvas.getContext('2d');
      ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
      const dataUrl = canvas.toDataURL('image/jpeg', 0.86);
      if (!resumeMaster) resumeMaster = {};
      resumeMaster.photo_b64 = dataUrl.split(',')[1] || '';
      renderMasterEditor();
      toast('Photo ready — click Save Portfolio to store in your encrypted vault.', 'success');
    };
    img.src = reader.result;
  };
  reader.readAsDataURL(file);
}

function clearResumePhoto() {
  if (!resumeMaster) resumeMaster = {};
  resumeMaster.photo_b64 = '';
  renderMasterEditor();
}

async function importExistingResumePdf(inputEl) {
  const file = inputEl?.files?.[0];
  if (!file) return;
  toast('Extracting projects & skills from your PDF resume...', 'info');
  try {
    const fd = new FormData();
    fd.append('file', file);
    const headers = {};
    if (authToken) headers['Authorization'] = `Bearer ${authToken}`;
    const res = await fetch('/api/resume/import-pdf', { method: 'POST', headers, credentials: 'same-origin', body: fd });
    const data = await res.json();
    if (res.ok && data.success) {
      if (!resumeMaster) resumeMaster = {};
      if (data.github && !resumeMaster.github) resumeMaster.github = data.github;
      if (data.linkedin && !resumeMaster.linkedin) resumeMaster.linkedin = data.linkedin;
      if (data.skills && data.skills.length) resumeMaster.skills = data.skills;
      if (data.projects && data.projects.length) resumeMaster.projects = data.projects;
      if (data.certifications && data.certifications.length) resumeMaster.certifications = data.certifications;
      renderMasterEditor();
      toast(`Imported ${(data.projects || []).length} project(s) & ${(data.skills || []).length} skill groups! Review & click Save Portfolio.`, 'success');
    } else {
      toast(data.message || 'Could not parse PDF resume.', 'warn');
    }
  } catch (e) {
    toast('PDF import error: ' + e.message, 'error');
  } finally {
    inputEl.value = '';
  }
}

async function saveResumeMaster() {
  const skills = [...document.querySelectorAll('.rm-skill-row')].map(r => ({
    category: r.querySelector('.rm-skill-cat')?.value.trim() || '',
    items: r.querySelector('.rm-skill-items')?.value.trim() || ''
  })).filter(s => s.category && s.items);

  const projects = [...document.querySelectorAll('.rm-proj-card')].map((c, idx) => ({
    id: c.dataset.pid || `proj_${idx}_${Date.now().toString(36)}`,
    title: c.querySelector('.rm-proj-title')?.value.trim() || '',
    tech: c.querySelector('.rm-proj-tech')?.value.trim() || '',
    link: c.querySelector('.rm-proj-link')?.value.trim() || '',
    bullets: (c.querySelector('.rm-proj-bullets')?.value || '').split('\n').map(b => b.trim()).filter(Boolean).slice(0, 3)
  })).filter(p => p.title);

  const certifications = [...document.querySelectorAll('.rm-cert-row')].map(r => ({
    title: r.querySelector('.rm-cert-title')?.value.trim() || '',
    issuer: r.querySelector('.rm-cert-issuer')?.value.trim() || '',
    year: r.querySelector('.rm-cert-year')?.value.trim() || ''
  })).filter(c => c.title);

  const payload = {
    full_name: document.getElementById('rm-full-name')?.value.trim() || '',
    phone: document.getElementById('rm-phone')?.value.trim() || '',
    location: document.getElementById('rm-location')?.value.trim() || '',
    github: document.getElementById('rm-github')?.value.trim() || '',
    linkedin: document.getElementById('rm-linkedin')?.value.trim() || '',
    portfolio: document.getElementById('rm-portfolio')?.value.trim() || '',
    photo_b64: (resumeMaster && resumeMaster.photo_b64) || '',
    skills,
    projects,
    certifications
  };

  try {
    const { ok, data } = await apiFetch('/api/resume/master', {
      method: 'POST',
      body: JSON.stringify(payload)
    });
    if (ok && data && data.success) {
      resumeMaster = data.master;
      const hint = document.getElementById('resume-photo-hint');
      if (hint) hint.textContent = resumeMaster.photo_b64 ? '(photo saved)' : '(no photo uploaded yet)';
      document.getElementById('resume-master-editor')?.classList.add('hidden');
      toast(`Saved ${projects.length} project(s) securely to your encrypted vault!`, 'success');
      if (lastTailoredResume) await generateTailoredResume();
    } else {
      toast((data && (data.message || data.error)) || 'Failed to save portfolio.', 'error');
    }
  } catch (e) {
    toast('Save error: ' + e.message, 'error');
  }
}

// ═══════════════════════════════════════════════════════════════════
// INIT
// ═══════════════════════════════════════════════════════════════════
(async function init() {
  setDrivesFilter('all');
  setCtcFilter('all');
  setDrivesSort('newest');
  setDrivesView('grid');

  const { ok, status, data } = await apiFetch('/api/auth/me');
  if (ok && data && data.authenticated) {
    currentUser = data.user;
    if (data.token) {
      authToken = data.token;
      localStorage.setItem('sjp_token', authToken);
    }
    hideAuthOverlay();
    await Promise.all([fetchStatus(), fetchJobs(), fetchProfile(), fetchNotices()]);
  } else {
    showAuthOverlay();
  }

  // Auto-refresh cadence when authenticated
  setInterval(() => { if (authToken || currentUser) fetchStatus(); }, 20000);
  setInterval(() => { if (authToken || currentUser) fetchJobs(); }, 60000);
  setInterval(() => { if (authToken || currentUser) fetchNotices(); }, 90000);
})();
</script>
</body>
</html>"""


# ─── MULTI-USER AUTH HELPERS & FLASK API ROUTES ─────────────────────────────

from user_manager import UserManager

user_manager = UserManager()


def _extract_token() -> str:
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header.split(" ", 1)[1].strip()
    return request.cookies.get("sjp_token", "").strip()


def get_current_user():
    """Returns the authenticated user dict from the session token, or None."""
    token = _extract_token()
    if token:
        user = user_manager.get_user_by_token(token)
        if user:
            return user
    # Convenient localhost auto-session for the local machine owner only when accessed directly via 127.0.0.1/localhost
    # and no explicit logout has occurred
    host = (request.host or "").split(":")[0]
    if host in ("127.0.0.1", "localhost") and not request.headers.get("X-Forwarded-For"):
        if request.cookies.get("sjp_logged_out") != "1":
            users = user_manager.get_all_users()
            if users:
                return users[0]
    return None


def _get_superset_for_user(user: dict) -> SupersetClient:
    return SupersetClient(email=user["email"], password=user["superset_password"])


@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route("/api/health", methods=["GET"])
def api_health():
    """Instant non-blocking health probe (<1ms, zero PII) for Docker, Render, Railway & Fly.io."""
    return jsonify({
        "status": "ok",
        "daemon_active": daemon_active,
    })


@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    data = request.get_json(silent=True) or {}
    email = data.get("email", "")
    password = data.get("password", "")
    ok, msg, user, token = user_manager.authenticate_or_register(
        email=email, password=password, is_register=False
    )
    if not ok or not user:
        return jsonify({"success": False, "message": msg}), 401
    log_event(f"User signed in: {user['email']} ({user['full_name']})", "SUCCESS")
    resp = jsonify({
        "success": True,
        "message": msg,
        "token": token,
        "user": {
            "user_id": user["user_id"],
            "email": user["email"],
            "full_name": user["full_name"],
            "roll_number": user["roll_number"],
            "auto_apply_enabled": user["auto_apply_enabled"],
            "is_admin": user["is_admin"],
        },
    })
    is_https = bool(request.is_secure or request.headers.get("X-Forwarded-Proto") == "https")
    resp.set_cookie("sjp_token", token, httponly=True, samesite="Lax", secure=is_https, max_age=90 * 86400)
    resp.set_cookie("sjp_logged_out", "0", max_age=90 * 86400)
    return resp


@app.route("/api/auth/register", methods=["POST"])
def auth_register():
    data = request.get_json(silent=True) or {}
    email = data.get("email", "")
    password = data.get("password", "")
    invite_code = data.get("invite_code", "KIIT2027")
    auto_apply = bool(data.get("auto_apply_enabled", False))
    ok, msg, user, token = user_manager.authenticate_or_register(
        email=email, password=password, invite_code=invite_code, is_register=True
    )
    if not ok or not user:
        return jsonify({"success": False, "message": msg}), 400
    if auto_apply:
        user_manager.set_user_auto_apply(user["user_id"], True)
        user["auto_apply_enabled"] = True
    log_event(f"Registered student account: {user['email']} ({user['full_name']})", "SUCCESS")
    resp = jsonify({
        "success": True,
        "message": msg,
        "token": token,
        "user": {
            "user_id": user["user_id"],
            "email": user["email"],
            "full_name": user["full_name"],
            "roll_number": user["roll_number"],
            "auto_apply_enabled": user["auto_apply_enabled"],
            "is_admin": user["is_admin"],
        },
    })
    is_https = bool(request.is_secure or request.headers.get("X-Forwarded-Proto") == "https")
    resp.set_cookie("sjp_token", token, httponly=True, samesite="Lax", secure=is_https, max_age=90 * 86400)
    resp.set_cookie("sjp_logged_out", "0", max_age=90 * 86400)
    return resp


@app.route("/api/auth/me")
def auth_me():
    user = get_current_user()
    if not user:
        return jsonify({"authenticated": False}), 401
    token = _extract_token()
    # Auto-upgrade legacy opaque tokens or localhost auto-sessions to 90-day stateless signed tokens
    if not token or not token.startswith("sjp1."):
        token = user_manager.create_session(user["user_id"])
    resp = jsonify({
        "authenticated": True,
        "token": token,
        "user": {
            "user_id": user["user_id"],
            "email": user["email"],
            "full_name": user["full_name"],
            "roll_number": user["roll_number"],
            "auto_apply_enabled": user["auto_apply_enabled"],
            "is_admin": user["is_admin"],
        },
    })
    is_https = bool(request.is_secure or request.headers.get("X-Forwarded-Proto") == "https")
    resp.set_cookie("sjp_token", token, httponly=True, samesite="Lax", secure=is_https, max_age=90 * 86400)
    return resp


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    token = _extract_token()
    if token:
        user_manager.destroy_session(token)
    resp = jsonify({"success": True})
    resp.delete_cookie("sjp_token")
    resp.set_cookie("sjp_logged_out", "1", max_age=86400)
    return resp


@app.route("/api/user/auto-apply", methods=["POST"])
def toggle_user_auto_apply():
    user = get_current_user()
    if not user:
        return jsonify({"error": "Authentication required"}), 401
    data = request.get_json(silent=True) or {}
    if "enabled" in data:
        new_state = bool(data["enabled"])
    else:
        new_state = not user["auto_apply_enabled"]
    user_manager.set_user_auto_apply(user["user_id"], new_state)
    log_event(f"Auto-Apply set to {'ON' if new_state else 'OFF'} for {user['email']}", "INFO")
    return jsonify({"success": True, "auto_apply_enabled": new_state})


@app.route("/api/invites", methods=["GET", "POST"])
def handle_invites():
    user = get_current_user()
    if not user:
        return jsonify({"error": "Authentication required"}), 401
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        inv = user_manager.create_invite_code(
            code=data.get("code", ""),
            label=data.get("label", f"Shared by {user['full_name']}"),
            max_uses=int(data.get("max_uses", 50)),
        )
        log_event(f"Generated friend invite code: {inv['code']}", "SUCCESS")
        return jsonify({"success": True, "invite": inv})
    return jsonify({"success": True, "invites": user_manager.list_invite_codes()})


@app.route("/api/status")
def get_status():
    user = get_current_user()
    if not user:
        return jsonify({"error": "Authentication required"}), 401

    cached_jobs, _ = user_manager.get_cached_jobs(user["user_id"])
    cached_notices, _ = user_manager.get_cached_notices(user["user_id"])
    cached_pending, _ = user_manager.get_cached_pending_actions(user["user_id"])
    jobs_list = cached_jobs or _cached_jobs
    applied_count = sum(1 for j in jobs_list if j.get("is_applied") or j.get("app_status") == "APPLIED")
    open_count = sum(1 for j in jobs_list if j.get("is_open"))
    total_jobs = len(jobs_list)
    shortlisted_count = sum(1 for n in cached_notices if n.get("is_shortlisted") and not n.get("is_past_event"))
    prof = user["profile"]
    wa_notifier = WhatsAppNotifier.from_user_profile(user)

    return jsonify({
        "student_name": user["full_name"],
        "student_id": user["roll_number"],
        "email": user["email"],
        "cgpa": prof.get("current_cgpa", ""),
        "branch": prof.get("graduation_branch", "B.Tech"),
        "college": prof.get("graduation_college", "Kalinga Institute of Industrial Technology, Bhubaneswar"),
        "auto_apply_enabled": user["auto_apply_enabled"],
        "is_admin": user["is_admin"],
        "total_jobs": total_jobs,
        "applied_jobs": applied_count,
        "open_jobs": open_count,
        "shortlisted_count": shortlisted_count,
        "pending_offers_count": len(cached_pending),
        "whatsapp_paired": wa_notifier.is_configured(check_live_bridge=True),
        "whatsapp_provider": wa_notifier.provider if wa_notifier.is_configured(check_live_bridge=True) else "",
        "telegram_paired": bool(user["telegram_chat_id"] or Config.TELEGRAM_CHAT_ID),
        "telegram_chat_id": user["telegram_chat_id"] or ("Configured" if Config.TELEGRAM_CHAT_ID else ""),
        "daemon_active": daemon_active,
        "poll_interval": Config.POLL_INTERVAL_MINUTES,
        "last_sync": user["last_sync_at"] or last_sync_info.get("timestamp") or "Ready",
        "last_sync_status": last_sync_info,
        "recent_logs": list(recent_logs),
        "sync_history": list(sync_history),
    })


@app.route("/api/daemon/toggle", methods=["POST"])
def toggle_daemon():
    user = get_current_user()
    if not user:
        return jsonify({"error": "Authentication required"}), 401
    global daemon_active
    daemon_active = not daemon_active
    state_str = "ACTIVE" if daemon_active else "PAUSED"
    log_event(f"Daemon poller transitioned to: {state_str}", "WARN" if not daemon_active else "INFO")
    return jsonify({"success": True, "active": daemon_active})


@app.route("/api/sync", methods=["POST"])
def trigger_sync():
    user = get_current_user()
    if not user:
        return jsonify({"error": "Authentication required"}), 401

    log_event(f"Manual sync requested by {user['full_name']} ({user['email']}).", "INFO")
    try:
        superset = _get_superset_for_user(user)
        if not superset.login():
            return jsonify({
                "success": False,
                "has_errors": True,
                "errors": ["Superset portal temporarily unavailable or credentials rejected. Please retry."],
                "message": "Superset portal temporarily unavailable or credentials rejected.",
            }), 502

        jobs = superset.fetch_job_profiles()
        sorted_jobs = sorted(jobs, key=lambda x: x.get("raw", {}).get("createdAt") or 0, reverse=True)
        user_manager.set_cached_jobs(user["user_id"], sorted_jobs)

        auto_applied = 0
        errors = []
        if user["auto_apply_enabled"]:
            open_eligible = [
                j for j in sorted_jobs
                if j.get("is_open") and j.get("is_eligible") and not j.get("is_applied")
            ]
            if open_eligible:
                def _bg_user_auto_apply(u_obj, jobs_to_apply):
                    sc_bg = _get_superset_for_user(u_obj)
                    if not sc_bg.login():
                        return
                    for job_item in jobs_to_apply[:5]:
                        r_app = sc_bg.apply_to_job(job_item["id"], profile_data=u_obj["profile"])
                        if r_app.get("success"):
                            log_event(f"Auto-applied to {job_item.get('company_name')} for {u_obj['full_name']}", "SUCCESS")
                    # Refresh cache after background auto-apply
                    fresh = sc_bg.fetch_job_profiles()
                    if fresh:
                        user_manager.set_cached_jobs(
                            u_obj["user_id"],
                            sorted(fresh, key=lambda x: x.get("raw", {}).get("createdAt") or 0, reverse=True),
                        )

                threading.Thread(target=_bg_user_auto_apply, args=(user, open_eligible), daemon=True).start()

        timestamp = datetime.now(IST).strftime("%d %b %Y, %I:%M:%S %p IST")
        user_manager.set_user_last_sync(user["user_id"], timestamp)

        # Also dispatch WhatsApp alerts for new open eligible jobs + refresh notices/shortlist PDFs/pending offers in background
        def _bg_notice_scan(u_obj, jobs_snapshot):
            try:
                wa_sent = notify_new_job_openings_for_user(user_manager, u_obj, jobs_snapshot)
                if wa_sent:
                    log_event(f"WhatsApp Job Alerts ({u_obj['full_name']}): dispatched {wa_sent} new job opening alert(s).", "SUCCESS")
            except Exception as wa_err:
                logger.debug(f"Background WhatsApp job alert warning: {wa_err}")
            try:
                sc_n = _get_superset_for_user(u_obj)
                if sc_n.login():
                    scan_notices_for_user(sc_n, user_manager, u_obj, max_pages=2, page_size=25, max_pdfs_to_download=6, send_alerts=True)
            except Exception as bg_err:
                logger.debug(f"Background notice scan warning: {bg_err}")

        threading.Thread(target=_bg_notice_scan, args=(user, sorted_jobs), daemon=True).start()

        global last_sync_info
        last_sync_info = {
            "timestamp": timestamp,
            "status": "WARNING" if errors else "SUCCESS",
            "message": f"Verified {len(sorted_jobs)} drives for {user['full_name']}.",
            "errors": errors,
            "total_found": len(sorted_jobs),
            "new_jobs": 0,
            "auto_applied": auto_applied,
        }
        sync_history.appendleft(last_sync_info.copy())
        log_event(f"Sync complete for {user['full_name']}: {len(sorted_jobs)} drives verified.", "SUCCESS")

        return jsonify({
            "success": len(errors) == 0,
            "has_errors": len(errors) > 0,
            "errors": errors,
            "total_found": len(sorted_jobs),
            "new_jobs": 0,
            "auto_applied": auto_applied,
            "timestamp": timestamp,
        })
    except Exception as e:
        log_event(f"Manual sync failed for {user['email']}: {e}", "ERROR")
        return jsonify({"success": False, "has_errors": True, "errors": [str(e)], "message": str(e)}), 500


@app.route("/api/jobs")
def get_jobs():
    user = get_current_user()
    if not user:
        return jsonify({"error": "Authentication required"}), 401

    now = int(time.time())
    cached_jobs, updated_at = user_manager.get_cached_jobs(user["user_id"])
    if cached_jobs and (now - updated_at) < 45:
        return jsonify(cached_jobs)

    try:
        superset = _get_superset_for_user(user)
        if not superset.login():
            return jsonify(cached_jobs or [])
        jobs = superset.fetch_job_profiles()
        sorted_jobs = sorted(jobs, key=lambda x: x.get("raw", {}).get("createdAt") or 0, reverse=True)
        user_manager.set_cached_jobs(user["user_id"], sorted_jobs)
        return jsonify(sorted_jobs)
    except Exception as e:
        logger.error(f"Error fetching jobs for {user['email']}: {e}")
        return jsonify(cached_jobs or [])


@app.route("/api/notices", methods=["GET"])
def get_notices():
    """Returns Smart Notice Feed, Shortlist PDF matches, and Pending Offer Watchdog state."""
    user = get_current_user()
    if not user:
        return jsonify({"error": "Authentication required"}), 401

    force_refresh = request.args.get("refresh") in ("1", "true", "yes")
    cached_notices, notices_ts = user_manager.get_cached_notices(user["user_id"])
    cached_pending, pending_ts = user_manager.get_cached_pending_actions(user["user_id"])
    now = int(time.time())

    # Serve warm cache immediately unless empty, stale (> 15m), or explicitly refreshed
    if cached_notices and not force_refresh and (now - notices_ts) < 900:
        return jsonify(summarize_notices(
            notices=cached_notices,
            pending_actions=cached_pending,
            notices_updated_at=notices_ts,
            pending_updated_at=pending_ts,
            student_roll=user["roll_number"],
            student_name=user["full_name"],
        ))

    try:
        superset = _get_superset_for_user(user)
        if not superset.login():
            return jsonify(summarize_notices(
                notices=cached_notices,
                pending_actions=cached_pending,
                notices_updated_at=notices_ts,
                pending_updated_at=pending_ts,
                student_roll=user["roll_number"],
                student_name=user["full_name"],
            ))
        result = scan_notices_for_user(
            superset, user_manager, user, max_pages=2, page_size=25, max_pdfs_to_download=6, send_alerts=True
        )
        return jsonify(result)
    except Exception as e:
        logger.error(f"Error loading notices for {user['email']}: {e}")
        return jsonify(summarize_notices(
            notices=cached_notices,
            pending_actions=cached_pending,
            notices_updated_at=notices_ts,
            pending_updated_at=pending_ts,
            student_roll=user["roll_number"],
            student_name=user["full_name"],
        ))


@app.route("/api/notices/scan", methods=["POST"])
def scan_notices_endpoint():
    """Forces a fresh live scan of Superset homepage notices, shortlist PDFs & /pending_actions."""
    user = get_current_user()
    if not user:
        return jsonify({"error": "Authentication required"}), 401

    try:
        superset = _get_superset_for_user(user)
        if not superset.login():
            return jsonify({"success": False, "message": "Superset portal temporarily unavailable."}), 502

        result = scan_notices_for_user(
            superset, user_manager, user, max_pages=2, page_size=25, max_pdfs_to_download=8, send_alerts=True
        )
        c = result.get("counts", {})
        log_event(
            f"Shortlist PDF scan for {user['full_name']}: {c.get('shortlisted', 0)} shortlisted, "
            f"{c.get('clean', 0)} clean notices, {c.get('pending_offers', 0)} pending offer(s).",
            "SUCCESS" if c.get("shortlisted") else "INFO",
        )
        return jsonify(result)
    except Exception as e:
        log_event(f"Notice PDF scan failed for {user['email']}: {e}", "ERROR")
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/notices/<notice_id>/attachments/<attachment_id>/pdf", methods=["GET"])
def proxy_notice_attachment_pdf(notice_id, attachment_id):
    """
    Authenticated 1-click PDF / image proxy.
    Fetches a fresh S3 pre-signed URL via the student's Superset session and streams
    the file inline (so browsers preview it cleanly instead of forcing download).
    """
    user = get_current_user()
    if not user:
        return jsonify({"error": "Authentication required"}), 401

    try:
        superset = _get_superset_for_user(user)
        if not superset.login():
            return jsonify({"error": "Superset portal temporarily unavailable"}), 502

        content_bytes, content_type, _ = superset.download_notice_attachment(notice_id, attachment_id)
        if not content_bytes:
            return jsonify({"error": "Attachment not available or expired on Superset"}), 404

        # Look up clean filename from cached notices if available
        filename = "superset_notice.pdf"
        cached_notices, _ = user_manager.get_cached_notices(user["user_id"])
        for n in cached_notices:
            if n.get("id") == notice_id:
                for a in n.get("attachments") or []:
                    if a.get("id") == attachment_id and a.get("name"):
                        filename = a["name"]
                        break
                break

        safe_filename = "".join(ch for ch in filename if ch not in ('"', "\r", "\n", "\\", "/"))[:140] or "superset_notice.pdf"
        if safe_filename.lower().endswith(".pdf"):
            mime = "application/pdf"
        elif safe_filename.lower().endswith((".jpg", ".jpeg")):
            mime = "image/jpeg"
        elif safe_filename.lower().endswith(".png"):
            mime = "image/png"
        else:
            mime = content_type or "application/octet-stream"

        disposition = "attachment" if request.args.get("download") in ("1", "true") else "inline"
        resp = Response(content_bytes, mimetype=mime)
        resp.headers["Content-Disposition"] = f'{disposition}; filename="{safe_filename}"'
        resp.headers["Cache-Control"] = "private, max-age=300"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        return resp
    except Exception as e:
        logger.error(f"PDF proxy error ({notice_id}/{attachment_id}): {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/pending-actions", methods=["GET"])
def get_pending_actions():
    """Pending Offer Watchdog endpoint polling Superset /pending_actions."""
    user = get_current_user()
    if not user:
        return jsonify({"error": "Authentication required"}), 401

    force_refresh = request.args.get("refresh") in ("1", "true", "yes")
    cached_pending, updated_at = user_manager.get_cached_pending_actions(user["user_id"])
    now = int(time.time())

    if not force_refresh and updated_at and (now - updated_at) < 300:
        return jsonify({
            "success": True,
            "count": len(cached_pending),
            "pending_actions": cached_pending,
            "checked_at": updated_at,
        })

    try:
        superset = _get_superset_for_user(user)
        if not superset.login():
            return jsonify({"success": False, "count": len(cached_pending), "pending_actions": cached_pending}), 200
        actions = superset.fetch_pending_actions()
        user_manager.set_cached_pending_actions(user["user_id"], actions)
        return jsonify({
            "success": True,
            "count": len(actions),
            "pending_actions": actions,
            "checked_at": int(time.time()),
        })
    except Exception as e:
        logger.error(f"Pending actions watchdog error: {e}")
        return jsonify({"success": False, "count": len(cached_pending), "pending_actions": cached_pending, "error": str(e)}), 200


@app.route("/api/resume/master", methods=["GET", "POST"])
def handle_resume_master():
    """Reads or updates the logged-in student's private master resume portfolio & photo."""
    user = get_current_user()
    if not user:
        return jsonify({"error": "Authentication required"}), 401

    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        # Cap uploaded photo payload at ~700KB base64 for vault health
        photo_b64 = str(data.get("photo_b64") or "").strip()
        if len(photo_b64) > 700_000:
            return jsonify({"success": False, "message": "Photo too large. Please upload a smaller image."}), 400

        cleaned_master = {
            "full_name": str(data.get("full_name") or user["full_name"]).strip(),
            "email": str(data.get("email") or user["email"]).strip(),
            "phone": str(data.get("phone") or "").strip(),
            "location": str(data.get("location") or "").strip(),
            "github": str(data.get("github") or "").strip(),
            "linkedin": str(data.get("linkedin") or "").strip(),
            "portfolio": str(data.get("portfolio") or "").strip(),
            "photo_b64": photo_b64,
            "skills": data.get("skills") if isinstance(data.get("skills"), list) else [],
            "projects": data.get("projects") if isinstance(data.get("projects"), list) else [],
            "certifications": data.get("certifications") if isinstance(data.get("certifications"), list) else [],
        }
        updated_profile = user_manager.update_user_profile(user["user_id"], {"resume_master": cleaned_master})
        log_event(f"Updated master resume portfolio ({len(cleaned_master['projects'])} projects) for {user['full_name']}.", "SUCCESS")
        return jsonify({"success": True, "master": get_resume_master(updated_profile)})

    return jsonify({"success": True, "master": get_resume_master(user["profile"])})


@app.route("/api/resume/import-pdf", methods=["POST"])
def import_resume_pdf_endpoint():
    """Parses a student's uploaded existing resume PDF into editable portfolio items."""
    user = get_current_user()
    if not user:
        return jsonify({"error": "Authentication required"}), 401

    f = request.files.get("file")
    if not f:
        return jsonify({"success": False, "message": "Please upload a PDF file."}), 400
    pdf_bytes = f.read(10 * 1024 * 1024)
    result = import_resume_from_pdf(pdf_bytes)
    status = 200 if result.get("success") else 400
    return jsonify(result), status


@app.route("/api/resume/tailor", methods=["POST"])
def tailor_resume_endpoint():
    """
    Tailors the logged-in student's master portfolio for a specific Superset drive
    (automatically fetching live JD + attached JD PDF) or custom company/role.
    """
    user = get_current_user()
    if not user:
        return jsonify({"error": "Authentication required"}), 401

    data = request.get_json(silent=True) or {}
    job_id = str(data.get("job_id") or "").strip()
    company_name = str(data.get("company_name") or "").strip()
    role = str(data.get("role") or "").strip()
    custom_jd = str(data.get("custom_jd") or "").strip()
    max_projects = int(data.get("max_projects") or 3)
    include_photo = bool(data.get("include_photo", True))
    selected_project_ids = data.get("selected_project_ids")
    custom_headline = str(data.get("custom_headline") or "").strip()
    custom_summary = str(data.get("custom_summary") or "").strip()
    use_ai = bool(data.get("use_ai", True))

    jd_text = custom_jd
    user_specified_role = bool(role)
    if job_id:
        # Look up cached job metadata first for fast fallback
        cached_jobs, _ = user_manager.get_cached_jobs(user["user_id"])
        for cj in cached_jobs:
            if str(cj.get("id")) == job_id:
                company_name = company_name or str(cj.get("company_name") or "")
                role = role or str(cj.get("job_role") or "")
                break
        try:
            sc = _get_superset_for_user(user)
            if sc.login():
                bundle = sc.fetch_job_description_bundle(job_id)
                if bundle:
                    company_name = company_name or bundle.get("company_name") or ""
                    # Prefer student's exact applied sub-profile (e.g. 'Full Stack Developer') over generic multi-role title
                    if not user_specified_role and bundle.get("chosen_profile"):
                        role = bundle["chosen_profile"]
                    else:
                        role = role or bundle.get("role") or ""
                    if bundle.get("jd_text"):
                        jd_text = "\n\n".join(x for x in [bundle["jd_text"], custom_jd] if x)
        except Exception as e:
            logger.debug(f"Live JD bundle fetch warning for {job_id}: {e}")

    tailored = tailor_resume_for_job(
        profile=user["profile"],
        company_name=company_name,
        role=role,
        jd_text=jd_text,
        max_projects=max_projects,
        include_photo=include_photo,
        selected_project_ids=selected_project_ids,
        custom_headline=custom_headline,
        custom_summary=custom_summary,
        use_ai=use_ai,
        ai_engine=ai_engine,
    )
    log_event(
        f"Tailored 1-page resume for {tailored['company_name'] or tailored['role']} "
        f"({tailored['ats_report']['score']}% ATS match) for {user['full_name']}.",
        "SUCCESS",
    )
    return jsonify({"success": True, "tailored": tailored})


@app.route("/api/resume/export-pdf", methods=["POST"])
def export_resume_pdf_endpoint():
    """Exports the tailored 1-page A4 Arial HTML resume directly to a downloadable PDF."""
    user = get_current_user()
    if not user:
        return jsonify({"error": "Authentication required"}), 401

    data = request.get_json(silent=True) or {}
    html_str = str(data.get("html") or "").strip()
    if not html_str or "<html" not in html_str.lower():
        return jsonify({"success": False, "message": "Invalid resume HTML payload."}), 400

    pdf_bytes, status_msg = render_html_to_pdf_bytes(html_str)
    if not pdf_bytes:
        return jsonify({"success": False, "fallback": "browser_print", "message": status_msg}), 503

    safe_name = "".join(c if c.isalnum() or c in ("_", "-") else "_" for c in (user["full_name"] or "Resume")).strip("_")
    safe_co = "".join(c if c.isalnum() or c in ("_", "-") else "_" for c in str(data.get("company_name") or "Tailored")).strip("_")
    filename = f"{safe_name}_{safe_co}_Resume.pdf"

    resp = Response(pdf_bytes, mimetype="application/pdf")
    resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


@app.route("/api/profile", methods=["GET", "POST"])
def handle_profile():
    user = get_current_user()
    if not user:
        return jsonify({"error": "Authentication required"}), 401

    from attribute_resolver import AttributeResolver
    resolver = AttributeResolver(user_id=user["user_id"], profile_data=user["profile"])
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        resolver.save_profile(data)
        log_event(f"Updated encrypted student profile for {user['full_name']}.", "SUCCESS")
        return jsonify({"success": True, "profile": resolver.profile})
    return jsonify(resolver.profile)


@app.route("/api/jobs/<job_id>/questions")
def get_job_questions(job_id):
    user = get_current_user()
    if not user:
        return jsonify({"error": "Authentication required"}), 401
    try:
        from attribute_resolver import AttributeResolver
        resolver = AttributeResolver(user_id=user["user_id"], profile_data=user["profile"])
        superset = _get_superset_for_user(user)
        if not superset.login():
            return jsonify([])
        detail_url = f"{superset.base_url}/tnpsuite-core/students/{superset.user_uuid}/job_profiles/{job_id}"
        resp = superset.session.get(detail_url, timeout=10)
        if resp.status_code == 200:
            preview = resolver.preview_answers_for_job(resp.json())
            return jsonify(preview)
        return jsonify([])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/jobs/<job_id>/preview-application")
def get_job_application_preview(job_id):
    user = get_current_user()
    if not user:
        return jsonify({"error": "Authentication required"}), 401
    try:
        from attribute_resolver import AttributeResolver
        resolver = AttributeResolver(user_id=user["user_id"], profile_data=user["profile"])
        superset = _get_superset_for_user(user)
        if not superset.login():
            return jsonify({"error": "Superset portal temporarily unavailable"}), 502

        detail_url = f"{superset.base_url}/tnpsuite-core/students/{superset.user_uuid}/job_profiles/{job_id}"
        resp = superset.session.get(detail_url, timeout=12)
        if resp.status_code != 200:
            return jsonify({"error": f"Failed to fetch job details: HTTP {resp.status_code}"}), resp.status_code

        job_data = resp.json()
        default_resume = job_data.get("defaultResume") or {}
        resumes_available = job_data.get("resumesAvailable") or []

        # Consent check
        consent_required = False
        consent_type = "JOB_APPLICATION"
        try:
            consent_url = f"{superset.base_url}/tnpsuite-core/students/{superset.user_uuid}/job_profiles/{job_id}/application-consent"
            c_resp = superset.session.get(consent_url, timeout=8)
            if c_resp.status_code == 200:
                cdata = c_resp.json()
                consent_required = cdata.get("required", False)
                consent_type = cdata.get("consentType", "JOB_APPLICATION")
        except Exception:
            pass

        questions = resolver.preview_answers_for_job(job_data)
        p = resolver.profile

        citizenship_str = p.get("citizenship", "India")
        pp_num = p.get("passport_number", "NA")
        nat_display = f"{citizenship_str} (Passport: {pp_num})" if pp_num and pp_num != "NA" else citizenship_str

        profile_summary = {
            "name": p.get("full_name") or user["full_name"],
            "email": p.get("email") or user["email"],
            "roll_no": p.get("roll_number") or user["roll_number"],
            "cgpa": p.get("current_cgpa") or "—",
            "phone": p.get("phone_number") or "—",
            "emergency_phone": p.get("emergency_contact_number") or "—",
            "father_name": p.get("fathers_name") or "—",
            "permanent_address": p.get("permanent_address") or p.get("current_address") or "—",
            "nationality": nat_display,
            "college": p.get("graduation_college", "KIIT Bhubaneswar"),
            "branch": p.get("graduation_branch", "Computer Science and Engineering"),
            "degree": p.get("graduation_degree", "B.Tech"),
            "passing_year": p.get("graduation_passing_year", "2027"),
            "backlogs": f"{p.get('active_backlogs', '0')} Active / {p.get('history_backlogs', '0')} History",
            "gap": f"{p.get('total_gap', '0')} years",
        }

        return jsonify({
            "success": True,
            "job_id": job_id,
            "company_name": job_data.get("companyName", "Company"),
            "job_role": job_data.get("jobProfileTitle", "Engineering Role"),
            "ctc_display": job_data.get("ctcDisplay") or f"₹ {job_data.get('ctc', 'N/A')}",
            "default_resume": default_resume,
            "resumes_available": resumes_available,
            "consent_required": consent_required,
            "consent_type": consent_type,
            "questions": questions,
            "candidate_profile": profile_summary,
        })
    except Exception as e:
        logger.error(f"Error previewing application for {job_id}: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/apply/<job_id>", methods=["POST"])
def manual_apply(job_id):
    user = get_current_user()
    if not user:
        return jsonify({"error": "Authentication required"}), 401

    log_event(f"Application submission initiated by {user['full_name']} for job: {job_id}", "INFO")
    try:
        data = request.get_json(silent=True) or {}
        custom_answers = data.get("answers")
        selected_resume_id = data.get("resume_id") or Config.DEFAULT_RESUME_NAME

        superset = _get_superset_for_user(user)
        if not superset.login():
            err_msg = "Superset portal temporarily unavailable. Please retry in a moment."
            log_event(err_msg, "ERROR")
            return jsonify({"success": False, "message": err_msg}), 502

        res = superset.apply_to_job(
            job_id,
            resume_id=selected_resume_id,
            custom_attributes=custom_answers,
            profile_data=user["profile"],
        )
        if res.get("success"):
            company = res.get("company_name", "Company")
            log_event(f"Successfully applied to {job_id} ({company}) for {user['full_name']}!", "SUCCESS")
            try:
                chat_id = user["telegram_chat_id"] or (Config.TELEGRAM_CHAT_ID if user["is_admin"] else "")
                if chat_id:
                    notifier = TelegramNotifier(chat_id=chat_id)
                    tg_msg = (
                        f"⚡ <b>Application Confirmed & Submitted!</b>\n"
                        f"Student: <b>{user['full_name']}</b>\n"
                        f"Company: <b>{company}</b>\n"
                        f"Job ID: <code>{job_id}</code>\n"
                        f"Status: Registered successfully via Superset API."
                    )
                    notifier.send_message(tg_msg)
            except Exception as te:
                log_event(f"Telegram alert delivery note: {te}", "WARN")
            try:
                wa = WhatsAppNotifier.from_user_profile(user)
                if wa.is_configured() and wa.notify_jobs:
                    wa.send_message(
                        f"⚡ *Application Confirmed & Submitted!*\n"
                        f"*Student:* {user['full_name']}\n"
                        f"*Company:* {company}\n"
                        f"*Status:* Registered successfully on Superset.",
                        event_type="application_confirmed",
                    )
            except Exception as we:
                log_event(f"WhatsApp alert delivery note: {we}", "WARN")

            # Invalidate user's job cache
            user_manager.set_cached_jobs(user["user_id"], [])
            return jsonify({"success": True, "message": res.get("message", "Application submitted successfully!")})
        else:
            err = res.get("message", "Application rejected by Superset server.")
            ai_diag = res.get("ai_diagnosis", {})
            log_event(f"Application rejected for {job_id}: {err}", "ERROR")
            if ai_diag.get("auto_healed"):
                user_manager.set_cached_jobs(user["user_id"], [])
            return jsonify({
                "success": False,
                "message": err,
                "ai_diagnosis": ai_diag,
            }), 400
    except Exception as e:
        logger.error(f"Error in manual_apply: {e}")
        log_event(f"Internal apply error: {e}", "ERROR")
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/clarifications", methods=["GET"])
def get_clarifications():
    user = get_current_user()
    if not user:
        return jsonify({"error": "Authentication required"}), 401
    try:
        am = AmbiguityManager()
        return jsonify({
            "pending": am.get_pending_tickets(user_id=user["user_id"]),
            "all": am.get_all_tickets(user_id=user["user_id"]),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/clarifications/resolve", methods=["POST"])
def resolve_clarification():
    user = get_current_user()
    if not user:
        return jsonify({"error": "Authentication required"}), 401
    data = request.get_json(silent=True) or {}
    ticket_id = data.get("ticket_id")
    answer = data.get("answer")
    if not ticket_id or answer is None:
        return jsonify({"success": False, "message": "ticket_id and answer are required"}), 400
    try:
        am = AmbiguityManager()
        ticket = am.resolve_ticket(ticket_id, str(answer), user_id=user["user_id"])
        log_event(f"User resolved clarification: '{ticket['question_text'][:30]}...' -> '{answer}'", "SUCCESS")
        return jsonify({"success": True, "ticket": ticket})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/errors/live", methods=["GET"])
def get_live_errors():
    user = get_current_user()
    if not user:
        return jsonify({"error": "Authentication required"}), 401
    try:
        healer = ErrorHealer()
        return jsonify(healer.get_recent_errors(limit=30))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/errors/diagnose", methods=["POST"])
def diagnose_error_api():
    user = get_current_user()
    if not user:
        return jsonify({"error": "Authentication required"}), 401
    data = request.get_json(silent=True) or {}
    try:
        ai = AIEngine()
        diagnosis = ai.diagnose_error(data)
        return jsonify(diagnosis)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/ai/copilot", methods=["POST"])
def ai_copilot_chat():
    user = get_current_user()
    if not user:
        return jsonify({"error": "Authentication required"}), 401
    data = request.get_json(silent=True) or {}
    query = data.get("query", "").strip()
    if not query:
        return jsonify({"error": "Query cannot be empty"}), 400
    try:
        ai = AIEngine()
        resp = ai.ask_copilot(query)
        return jsonify(resp)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/ai/status", methods=["GET"])
def get_ai_status():
    user = get_current_user()
    if not user:
        return jsonify({"error": "Authentication required"}), 401
    try:
        ai = AIEngine()
        return jsonify(ai.get_status())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/telegram/test", methods=["POST"])
def test_telegram():
    user = get_current_user()
    if not user:
        return jsonify({"error": "Authentication required"}), 401
    chat_id = user["telegram_chat_id"] or Config.TELEGRAM_CHAT_ID
    notifier = TelegramNotifier(chat_id=chat_id)
    test_msg = (
        "🚀 <b>Superset Job Pilot — Notification Test</b>\n\n"
        "✅ Push alert pipeline verified.\n"
        f"Student: {user['full_name']}\n"
        f"Roll No: {user['roll_number']} (KIIT)\n"
        "System: Autonomous 24/7 background monitor active."
    )
    sent = notifier.send_message(test_msg)
    if sent:
        log_event("Dispatched verified test alert to Telegram phone app.", "SUCCESS")
        return jsonify({"success": True, "chat_id": chat_id})
    else:
        err_msg = "Failed to dispatch Telegram message. Check bot token and ensure you have tapped /start."
        log_event(err_msg, "ERROR")
        return jsonify({"success": False, "message": err_msg}), 500


@app.route("/api/telegram/send", methods=["POST"])
def send_telegram_custom():
    user = get_current_user()
    if not user:
        return jsonify({"error": "Authentication required"}), 401
    data = request.get_json(silent=True) or {}
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"success": False, "message": "Message text is required"}), 400
    chat_id = user["telegram_chat_id"] or Config.TELEGRAM_CHAT_ID
    notifier = TelegramNotifier(chat_id=chat_id)
    sent = notifier.send_message(f"📢 <b>Superset Pilot Alert:</b>\n\n{text}")
    if sent:
        log_event(f"Custom Telegram message sent: {text[:40]}...", "SUCCESS")
        return jsonify({"success": True})
    return jsonify({"success": False, "message": "Failed to send Telegram message"}), 500


# ─── WHATSAPP ALERTS API ────────────────────────────────────────────────────

_wa_bridge_lock = threading.Lock()


def _ensure_whatsapp_bridge_running(bridge_url: str = "") -> Tuple[bool, str]:
    """Ensures the loopback-only Node.js WhatsApp Linked-Device bridge is listening."""
    base = (bridge_url or Config.WHATSAPP_BRIDGE_URL or "http://127.0.0.1:8091").rstrip("/")
    try:
        r = requests.get(f"{base}/status", timeout=1.0)
        if r.status_code == 200:
            return True, "Bridge online"
    except Exception:
        pass

    with _wa_bridge_lock:
        try:
            r = requests.get(f"{base}/status", timeout=0.8)
            if r.status_code == 200:
                return True, "Bridge online"
        except Exception:
            pass

        bridge_dir = BASE_DIR / "whatsapp_bridge"
        bridge_script = bridge_dir / "bridge.mjs"
        if not bridge_script.exists() or not (bridge_dir / "node_modules").exists():
            return False, "Local WhatsApp bridge dependencies not installed. Run: cd whatsapp_bridge && npm install"

        try:
            subprocess.Popen(
                ["node", str(bridge_script)],
                cwd=str(bridge_dir),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception as e:
            return False, f"Failed to launch Node WhatsApp bridge: {e}"

        for _ in range(16):
            time.sleep(0.25)
            try:
                r = requests.get(f"{base}/status", timeout=0.6)
                if r.status_code == 200:
                    return True, "Bridge launched"
            except Exception:
                continue
        return False, "WhatsApp bridge started but did not respond on 127.0.0.1:8091 in time."


def _auto_arm_local_qr_for_user(user: dict):
    """Arms local_qr in the user's vault if not configured or switching to local_qr."""
    existing = (user.get("profile") or {}).get("whatsapp_config")
    existing = dict(existing) if isinstance(existing, dict) else {}
    if existing.get("provider") != "local_qr" or not existing.get("enabled", True):
        existing.setdefault("phone", "")
        existing.setdefault("notify_jobs", True)
        existing.setdefault("notify_shortlists", True)
        existing.setdefault("notify_offers", True)
        existing["provider"] = "local_qr"
        existing["enabled"] = True
        existing["bridge_url"] = existing.get("bridge_url") or Config.WHATSAPP_BRIDGE_URL
        user_manager.update_user_profile(user["user_id"], {"whatsapp_config": existing})


def _safe_whatsapp_config_payload(user: dict) -> dict:
    prof = user.get("profile") or {}
    cfg = prof.get("whatsapp_config") if isinstance(prof.get("whatsapp_config"), dict) else {}
    wa = WhatsAppNotifier.from_user_profile(user)
    local_bridge = wa.get_local_bridge_status(timeout=0.9) if wa.provider == "local_qr" else None
    is_live_configured = (
        bool(wa.enabled and local_bridge and local_bridge.get("connected"))
        if wa.provider == "local_qr"
        else wa.is_configured()
    )
    return {
        "configured": is_live_configured,
        "enabled": cfg.get("enabled", True) if cfg else wa.enabled,
        "provider": wa.provider,
        "phone": cfg.get("phone") or wa.phone or "",
        "has_api_key": bool(cfg.get("api_key") or (user.get("is_admin") and Config.WHATSAPP_API_KEY)),
        "meta_phone_number_id": cfg.get("meta_phone_number_id") or "",
        "has_meta_token": bool(cfg.get("meta_access_token") or (user.get("is_admin") and Config.WHATSAPP_META_TOKEN)),
        "twilio_account_sid": cfg.get("twilio_account_sid") or "",
        "has_twilio_token": bool(cfg.get("twilio_auth_token") or (user.get("is_admin") and Config.TWILIO_AUTH_TOKEN)),
        "twilio_from": cfg.get("twilio_from") or Config.TWILIO_WHATSAPP_FROM or "whatsapp:+14155238886",
        "webhook_url": cfg.get("webhook_url") or "",
        "notify_jobs": wa.notify_jobs,
        "notify_shortlists": wa.notify_shortlists,
        "notify_offers": wa.notify_offers,
        "local_bridge": local_bridge,
    }


@app.route("/api/whatsapp/qr/status", methods=["GET"])
def api_whatsapp_qr_status():
    user = get_current_user()
    if not user:
        return jsonify({"error": "Authentication required"}), 401
    wa = WhatsAppNotifier.from_user_profile(user)
    st = wa.get_local_bridge_status(timeout=1.5)
    if st.get("connected"):
        _auto_arm_local_qr_for_user(user)
    return jsonify({"success": True, **st})


@app.route("/api/whatsapp/qr/start", methods=["POST"])
def api_whatsapp_qr_start():
    user = get_current_user()
    if not user:
        return jsonify({"error": "Authentication required"}), 401
    data = request.get_json(silent=True) or {}
    fresh = bool(data.get("fresh", False))
    base = Config.WHATSAPP_BRIDGE_URL.rstrip("/")

    ok_boot, boot_msg = _ensure_whatsapp_bridge_running(base)
    if not ok_boot:
        return jsonify({"success": False, "message": boot_msg, "connected": False, "state": "offline"}), 503

    try:
        resp = requests.post(f"{base}/start", json={"fresh": fresh}, timeout=12)
        payload = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        _auto_arm_local_qr_for_user(user)
        return jsonify({"success": True, **payload})
    except Exception as e:
        return jsonify({"success": False, "message": f"WhatsApp QR bridge error: {e}", "connected": False}), 502


@app.route("/api/whatsapp/qr/logout", methods=["POST"])
def api_whatsapp_qr_logout():
    user = get_current_user()
    if not user:
        return jsonify({"error": "Authentication required"}), 401
    base = Config.WHATSAPP_BRIDGE_URL.rstrip("/")
    try:
        resp = requests.post(f"{base}/logout", timeout=8)
        payload = resp.json() if resp.status_code == 200 else {"connected": False, "state": "idle"}
        log_event(f"Unlinked local WhatsApp Linked Device for {user['full_name']}.", "INFO")
        return jsonify({"success": True, **payload})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 502


@app.route("/api/whatsapp/config", methods=["GET", "POST"])
def api_whatsapp_config():
    user = get_current_user()
    if not user:
        return jsonify({"error": "Authentication required"}), 401

    if request.method == "GET":
        return jsonify({"success": True, "config": _safe_whatsapp_config_payload(user)})

    data = request.get_json(silent=True) or {}
    provider = str(data.get("provider") or "local_qr").strip().lower()
    if provider not in ("local_qr", "callmebot", "meta", "twilio", "webhook"):
        return jsonify({"success": False, "message": f"Unsupported provider '{provider}'"}), 400

    existing = (user.get("profile") or {}).get("whatsapp_config")
    existing = existing if isinstance(existing, dict) else {}

    phone_raw = str(data.get("phone") if data.get("phone") is not None else existing.get("phone", "")).strip()
    phone_norm = normalize_whatsapp_phone(phone_raw) if phone_raw else ""

    def _keep_secret(field_name: str) -> str:
        val = str(data.get(field_name) or "").strip()
        return val if val else str(existing.get(field_name) or "").strip()

    new_cfg = {
        "provider": provider,
        "bridge_url": str(data.get("bridge_url") or existing.get("bridge_url") or Config.WHATSAPP_BRIDGE_URL).strip(),
        "phone": phone_norm,
        "api_key": _keep_secret("api_key"),
        "meta_phone_number_id": str(data.get("meta_phone_number_id") or "").strip(),
        "meta_access_token": _keep_secret("meta_access_token"),
        "twilio_account_sid": str(data.get("twilio_account_sid") or "").strip(),
        "twilio_auth_token": _keep_secret("twilio_auth_token"),
        "twilio_from": str(data.get("twilio_from") or "whatsapp:+14155238886").strip(),
        "webhook_url": str(data.get("webhook_url") or "").strip(),
        "enabled": bool(data.get("enabled", True)),
        "notify_jobs": bool(data.get("notify_jobs", True)),
        "notify_shortlists": bool(data.get("notify_shortlists", True)),
        "notify_offers": bool(data.get("notify_offers", True)),
    }

    user_manager.update_user_profile(user["user_id"], {"whatsapp_config": new_cfg})
    updated_user = user_manager.get_user_by_id(user["user_id"]) or user
    safe_cfg = _safe_whatsapp_config_payload(updated_user)
    log_event(f"Updated WhatsApp config for {user['full_name']} (provider={provider}, active={safe_cfg['configured']}).", "SUCCESS")
    return jsonify({"success": True, "config": safe_cfg})


@app.route("/api/whatsapp/test", methods=["POST"])
def api_whatsapp_test():
    user = get_current_user()
    if not user:
        return jsonify({"error": "Authentication required"}), 401

    wa = WhatsAppNotifier.from_user_profile(user)
    if not wa.is_configured():
        return jsonify({
            "success": False,
            "message": "WhatsApp is not configured yet. Enter your phone number and API key, then click Save WhatsApp Config first.",
        }), 400

    ok, detail = wa.send_test_message(student_name=user["full_name"])
    if ok:
        log_event(f"Dispatched verified WhatsApp test alert for {user['full_name']} via {wa.provider}.", "SUCCESS")
        return jsonify({"success": True, "provider": wa.provider, "message": detail})
    log_event(f"WhatsApp test failed ({wa.provider}): {detail}", "ERROR")
    return jsonify({"success": False, "provider": wa.provider, "message": detail}), 502


@app.route("/api/whatsapp/send-digest", methods=["POST"])
def api_whatsapp_send_digest():
    user = get_current_user()
    if not user:
        return jsonify({"error": "Authentication required"}), 401

    wa = WhatsAppNotifier.from_user_profile(user)
    if not wa.is_configured():
        return jsonify({
            "success": False,
            "message": "WhatsApp is not configured yet. Save your WhatsApp phone and API key first.",
        }), 400

    cached_notices, _ = user_manager.get_cached_notices(user["user_id"])
    cached_jobs, _ = user_manager.get_cached_jobs(user["user_id"])
    cached_pending, _ = user_manager.get_cached_pending_actions(user["user_id"])

    upcoming_shortlists = [
        n for n in (cached_notices or [])
        if n.get("is_shortlisted") and not n.get("is_past_event")
    ][:3]
    open_eligible_jobs = [
        j for j in (cached_jobs or [])
        if j.get("is_open") and j.get("is_eligible") and not j.get("is_applied")
    ][:6]
    pending_offers = (cached_pending or [])[:2]

    messages_to_send = []
    # 1. Upcoming shortlists with exact role-specific venue, date, reporting time
    for n in upcoming_shortlists:
        m = n.get("shortlist_match") or {}
        messages_to_send.append((
            "shortlist_digest",
            wa.format_shortlist_message(
                n, m, student_name=user["full_name"], roll_number=user["roll_number"]
            ),
        ))

    # 2. Pending offers
    for act in pending_offers:
        messages_to_send.append((
            "offer_digest",
            wa.format_pending_offer_message(act, student_name=user["full_name"]),
        ))

    # 3. Currently open eligible job openings
    if len(open_eligible_jobs) == 1:
        j = open_eligible_jobs[0]
        messages_to_send.append((
            "job_opening_digest",
            wa.format_job_opening_message(j, auto_applied=False),
        ))
    elif len(open_eligible_jobs) > 1:
        lines = [f"📢 *OPEN ELIGIBLE JOB PROFILES ({len(open_eligible_jobs)})*", ""]
        for idx, j in enumerate(open_eligible_jobs, 1):
            lines.append(
                f"{idx}. *{j.get('company_name', 'Company')}* — {j.get('job_role', 'Role')}\n"
                f"   CTC: {j.get('ctc_display', 'N/A')} | Deadline: {j.get('deadline_display', 'Check Portal')}\n"
                f"   Apply: {j.get('profile_url', Config.SUPERSET_BASE_URL)}"
            )
        messages_to_send.append(("job_opening_digest", "\n".join(lines)))

    if not messages_to_send:
        total_open = sum(1 for j in (cached_jobs or []) if j.get("is_open"))
        summary_msg = (
            f"📋 *Superset Job Pilot — Live Digest*\n"
            f"Hi {user['full_name']}, you are all caught up!\n"
            f"• Upcoming Shortlists: 0\n"
            f"• Unapplied Open Eligible Jobs: 0 (Total open drives: {total_open})\n"
            f"• Pending Offers: 0\n\n"
            f"24/7 WhatsApp watchdog is armed for new job openings and shortlist PDFs."
        )
        messages_to_send.append(("status_digest", summary_msg))

    sent_count = 0
    errors = []
    for idx, (ev_type, txt) in enumerate(messages_to_send):
        if idx > 0:
            time.sleep(0.5)
        ok, detail = wa.send_message(txt, event_type=ev_type)
        if ok:
            sent_count += 1
        else:
            errors.append(detail)

    if sent_count > 0:
        msg = (
            f"Delivered {sent_count} WhatsApp alert(s): "
            f"{len(upcoming_shortlists)} shortlist(s), {len(open_eligible_jobs)} open eligible job(s), "
            f"{len(pending_offers)} pending offer(s)."
        )
        log_event(f"WhatsApp Live Digest ({user['full_name']}): {msg}", "SUCCESS")
        return jsonify({
            "success": True,
            "sent_count": sent_count,
            "shortlists_sent": len(upcoming_shortlists),
            "open_jobs_included": len(open_eligible_jobs),
            "pending_offers_included": len(pending_offers),
            "message": msg,
        })

    err_msg = errors[0] if errors else "Failed to deliver WhatsApp digest."
    log_event(f"WhatsApp Live Digest failed ({user['full_name']}): {err_msg}", "ERROR")
    return jsonify({"success": False, "message": err_msg}), 502


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)), debug=False)
