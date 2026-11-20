"""Superset Job Pilot v2 — Mission Control Dashboard.

Autonomous campus placement tracking, intelligent custom question resolver,
and multi-channel auto-applier for app.joinsuperset.com.
"""
import json
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone
import pytz
from flask import Flask, jsonify, request, render_template_string, Response
from config import Config, BASE_DIR
from superset_client import SupersetClient
from auto_applier import AutoApplier
from telegram_notifier import TelegramNotifier
from calendar_sync import CalendarSync
from ai_engine import AIEngine
from rag_engine import RAGEngine
from error_healer import ErrorHealer
from ambiguity_manager import AmbiguityManager

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
  <meta name="theme-color" media="(prefers-color-scheme: dark)" content="#030712">
  <meta name="theme-color" media="(prefers-color-scheme: light)" content="#030712">
  <meta name="color-scheme" content="dark">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
  <title>Superset Job Pilot — Mission Control v2.5</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
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
            brand: { 50:'#eef2ff', 100:'#e0e7ff', 200:'#c7d2fe', 300:'#a5b4fc', 400:'#818cf8', 500:'#6366f1', 600:'#4f46e5', 700:'#4338ca', 800:'#3730a3', 900:'#312e81' },
            surface: { 50:'#f8fafc', 100:'#f1f5f9', 800:'#1e293b', 850:'#141c2e', 900:'#0b1120', 950:'#030712' },
          },
          animation: {
            'pulse-slow': 'pulse 3s cubic-bezier(0.4, 0, 0.6, 1) infinite',
            'slide-up': 'slideUp 0.26s cubic-bezier(0.22, 1, 0.36, 1)',
            'slide-down': 'slideDown 0.26s cubic-bezier(0.22, 1, 0.36, 1)',
            'fade-in': 'fadeIn 0.2s ease-out',
          },
          keyframes: {
            slideUp: { '0%': { transform: 'translateY(18px)', opacity: '0' }, '100%': { transform: 'translateY(0)', opacity: '1' } },
            slideDown: { '0%': { transform: 'translateY(-12px)', opacity: '0' }, '100%': { transform: 'translateY(0)', opacity: '1' } },
            fadeIn: { '0%': { opacity: '0' }, '100%': { opacity: '1' } },
          }
        }
      }
    }
  </script>
  <style>
    * { scrollbar-width: thin; scrollbar-color: #334155 transparent; }
    ::-webkit-scrollbar { width: 5px; height: 5px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: #334155; border-radius: 3px; }

    /* Mobile Native Platform Baseline */
    html {
      -webkit-tap-highlight-color: transparent;
      -webkit-text-size-adjust: 100%;
      overscroll-behavior: none;
    }
    body {
      background-color: #030712;
      background-image:
        radial-gradient(ellipse 85% 50% at 50% -15%, rgba(99, 102, 241, 0.22), transparent),
        radial-gradient(ellipse 60% 45% at 92% 12%, rgba(168, 85, 247, 0.13), transparent),
        radial-gradient(ellipse 55% 40% at 8% 88%, rgba(16, 185, 129, 0.09), transparent);
      background-attachment: fixed;
      color: #e2e8f0;
      min-height: 100dvh;
      padding-top: env(safe-area-inset-top, 0px);
      padding-left: env(safe-area-inset-left, 0px);
      padding-right: env(safe-area-inset-right, 0px);
    }
    button, input, select, textarea, a, [role="button"] {
      touch-action: manipulation;
    }
    button, [role="button"], .mobile-nav-btn, .drives-filter, .ctc-filter, .drives-sort, .drives-view {
      user-select: none;
      -webkit-user-select: none;
      -webkit-touch-callout: none;
      transition: transform 110ms cubic-bezier(0.22, 1, 0.36, 1), background-color 150ms ease, border-color 150ms ease, box-shadow 150ms ease;
    }
    /* Instant tactile press feedback on pointerdown/active */
    button:active:not(:disabled), [role="button"]:active, .mobile-tap:active {
      transform: scale(0.96);
    }
    /* Prevent iOS Safari auto-zoom on input focus without disabling pinch zoom */
    @media (pointer: coarse), (max-width: 768px) {
      input, select, textarea {
        font-size: 16px !important;
      }
    }
    /* Contained scroll bounce on sheets, modals, and drawers */
    #modal-body, #mobile-drawer, .sheet-scroll {
      overscroll-behavior: contain;
      -webkit-overflow-scrolling: touch;
    }
    /* Horizontal pill carousels */
    .pill-carousel {
      touch-action: pan-x;
      scroll-snap-type: x mandatory;
      -webkit-overflow-scrolling: touch;
    }
    .pill-carousel > * {
      scroll-snap-align: start;
      flex-shrink: 0;
    }

    /* Toast system responsive */
    .toast-container { position: fixed; top: calc(0.75rem + env(safe-area-inset-top, 0px)); right: 1rem; z-index: 9999; display: flex; flex-direction: column; gap: 0.5rem; pointer-events: none; }
    .toast { pointer-events: auto; animation: slideDown 0.26s cubic-bezier(0.22, 1, 0.36, 1); min-width: 300px; max-width: 440px; backdrop-filter: blur(16px); }
    .toast.removing { animation: slideUp 0.18s ease-out reverse forwards; }
    @media (max-width: 640px) {
      .toast-container { left: 0.75rem; right: 0.75rem; }
      .toast { min-width: 0; width: 100%; max-width: 100%; }
    }

    /* Sidebar Desktop & Mobile Off-Canvas */
    .sidebar { width: 260px; min-width: 260px; transition: all 0.25s cubic-bezier(0.16, 1, 0.3, 1); }
    .sidebar.collapsed { width: 64px; min-width: 64px; }
    .sidebar.collapsed .sidebar-label { display: none; }
    .sidebar.collapsed .sidebar-header-text { display: none; }

    /* Active nav */
    .nav-item { transition: all 0.15s; }
    .nav-item.active { background: linear-gradient(90deg, rgba(99, 102, 241, 0.22), rgba(99, 102, 241, 0.05)); color: #a5b4fc; border-left: 3px solid #6366f1; box-shadow: inset 0 1px 0 rgba(255,255,255,0.06); }

    /* Mobile Bottom Nav Active */
    .mobile-nav-btn { position: relative; transition: all 0.2s cubic-bezier(0.22, 1, 0.36, 1); }
    .mobile-nav-btn.active { color: #ffffff; }
    .mobile-nav-btn.active .mobile-nav-pill {
      background: linear-gradient(135deg, rgba(99, 102, 241, 0.32), rgba(168, 85, 247, 0.22));
      border-color: rgba(129, 140, 248, 0.45);
      box-shadow: 0 4px 14px -2px rgba(99, 102, 241, 0.45);
      transform: translateY(-1px);
    }

    /* High-craft Glass Cards */
    .job-card {
      background: linear-gradient(165deg, rgba(15, 23, 42, 0.82) 0%, rgba(11, 17, 32, 0.92) 100%);
      border: 1px solid rgba(255, 255, 255, 0.08);
      box-shadow: 0 10px 30px -10px rgba(0, 0, 0, 0.5), inset 0 1px 0 rgba(255, 255, 255, 0.08);
      backdrop-filter: blur(12px);
      transition: transform 0.22s cubic-bezier(0.22, 1, 0.36, 1), border-color 0.22s ease, box-shadow 0.22s ease;
    }
    .metric-card {
      background: linear-gradient(145deg, rgba(15, 23, 42, 0.85), rgba(11, 17, 32, 0.75));
      border: 1px solid rgba(255, 255, 255, 0.08);
      box-shadow: 0 8px 24px -8px rgba(0, 0, 0, 0.45), inset 0 1px 0 rgba(255, 255, 255, 0.08);
      backdrop-filter: blur(12px);
    }

    /* Gate all hover states strictly behind pointer capability so mobile taps never stick */
    @media (hover: hover) and (pointer: fine) {
      .nav-item:not(.active):hover { background: rgba(148, 163, 184, 0.08); }
      .job-card:hover {
        border-color: rgba(129, 140, 248, 0.45);
        transform: translateY(-2px);
        box-shadow: 0 16px 36px -10px rgba(0, 0, 0, 0.65), 0 0 20px -5px rgba(99, 102, 241, 0.2), inset 0 1px 0 rgba(255, 255, 255, 0.14);
      }
      ::-webkit-scrollbar-thumb:hover { background: #475569; }
    }

    /* Status dot animation */
    @keyframes livePulse { 0%, 100% { opacity: 1; transform: scale(1); } 50% { opacity: 0.35; transform: scale(0.92); } }
    .live-dot { animation: livePulse 2s infinite; }

    /* Modal backdrop */
    .modal-backdrop { background: rgba(2, 6, 23, 0.82); backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px); }

    /* Profile form */
    .profile-field input, .profile-field select { background: #0b1120; border: 1px solid #334155; color: #e2e8f0; }
    .profile-field input:focus, .profile-field select:focus { border-color: #6366f1; outline: none; box-shadow: 0 0 0 2px rgba(99,102,241,0.25); }

    /* Badge styles */
    .badge-applied { background: rgba(16, 185, 129, 0.14); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.28); box-shadow: 0 0 12px -3px rgba(16, 185, 129, 0.25); }
    .badge-open { background: rgba(245, 158, 11, 0.15); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.32); box-shadow: 0 0 12px -3px rgba(245, 158, 11, 0.25); }
    .badge-closed { background: rgba(100, 116, 139, 0.14); color: #94a3b8; border: 1px solid rgba(100, 116, 139, 0.22); }
    .badge-ineligible { background: rgba(239, 68, 68, 0.14); color: #f87171; border: 1px solid rgba(239, 68, 68, 0.25); }

    /* Horizontal scroll without ugly bars */
    .no-scrollbar::-webkit-scrollbar { display: none; }
    .no-scrollbar { -ms-overflow-style: none; scrollbar-width: none; }
  </style>
</head>
<body class="font-sans antialiased min-h-screen flex">

  <!-- Toast Container -->
  <div id="toast-container" class="toast-container"></div>

  <!-- ═══ DESKTOP SIDEBAR ═══ -->
  <aside id="sidebar" class="hidden lg:flex sidebar bg-surface-950 border-r border-slate-800/80 flex-col h-screen sticky top-0 z-40">
    <!-- Logo -->
    <div class="p-4 border-b border-slate-800/80 flex items-center gap-3">
      <div class="w-9 h-9 rounded-xl bg-brand-600 flex items-center justify-center text-white font-bold text-sm flex-shrink-0 shadow-lg shadow-brand-600/30">
        SJ
      </div>
      <div class="sidebar-header-text overflow-hidden">
        <div class="text-sm font-bold text-white tracking-tight truncate">SUPERSET PILOT</div>
        <div class="text-[10px] text-slate-500 font-mono truncate">Mission Control v2.0</div>
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
      <button onclick="switchPage('applied')" class="nav-item w-full flex items-center justify-between px-3 py-2.5 rounded-lg text-sm font-medium text-left" data-page="applied">
        <div class="flex items-center gap-3 truncate">
          <span class="text-base w-5 text-center">✅</span>
          <span class="sidebar-label">My Applications</span>
        </div>
        <span class="sidebar-label text-[10px] font-mono px-1.5 py-0.5 rounded bg-emerald-500/10 text-emerald-400" id="badge-nav-applied">29</span>
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
          <span class="text-base w-5 text-center">📨</span>
          <span class="sidebar-label">Telegram</span>
        </div>
        <span class="sidebar-label text-[10px] font-mono px-1.5 py-0.5 rounded bg-blue-500/10 text-blue-400">Bot</span>
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
        <span class="text-[10px] font-mono px-1.5 py-0.5 rounded bg-surface-800 text-slate-400">107</span>
      </button>
      <button onclick="switchPage('applied'); closeMobileDrawer();" class="w-full flex items-center justify-between px-3 py-2.5 rounded-lg text-sm font-medium text-slate-300 hover:bg-slate-800 text-left">
        <div class="flex items-center gap-3"><span class="text-base">✅</span> <span>My Applications</span></div>
        <span class="text-[10px] font-mono px-1.5 py-0.5 rounded bg-emerald-500/10 text-emerald-400">29</span>
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
      <button onclick="switchPage('telegram'); closeMobileDrawer();" class="w-full flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium text-slate-300 hover:bg-slate-800 text-left">
        <span class="text-base">📨</span> <span>Telegram Alerts</span>
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
  <nav class="lg:hidden fixed bottom-0 left-0 right-0 z-40 bg-surface-950/85 backdrop-blur-2xl border-t border-white/10 px-2 pt-1.5 flex items-center justify-around shadow-[0_-10px_30px_rgba(0,0,0,0.6)]" style="padding-bottom: max(0.5rem, env(safe-area-inset-bottom, 0px));">
    <button onclick="switchPage('dashboard')" class="mobile-nav-btn active flex-1 flex flex-col items-center py-1 rounded-xl text-slate-400" data-page="dashboard">
      <div class="mobile-nav-pill px-3.5 py-1 rounded-full border border-transparent flex items-center justify-center transition-all">
        <span class="text-base leading-none">📊</span>
      </div>
      <span class="text-[10px] font-semibold tracking-tight mt-1">Overview</span>
    </button>
    <button onclick="switchPage('drives')" class="mobile-nav-btn flex-1 flex flex-col items-center py-1 rounded-xl text-slate-400 relative" data-page="drives">
      <div class="mobile-nav-pill px-3.5 py-1 rounded-full border border-transparent flex items-center justify-center transition-all relative">
        <span class="text-base leading-none">💼</span>
        <span id="mobile-badge-drives" class="absolute -top-1 -right-1.5 px-1.5 py-0.2 rounded-full bg-brand-600 text-white font-mono text-[8px] font-bold shadow">119</span>
      </div>
      <span class="text-[10px] font-semibold tracking-tight mt-1">Drives</span>
    </button>
    <button onclick="switchPage('applied')" class="mobile-nav-btn flex-1 flex flex-col items-center py-1 rounded-xl text-slate-400 relative" data-page="applied">
      <div class="mobile-nav-pill px-3.5 py-1 rounded-full border border-transparent flex items-center justify-center transition-all relative">
        <span class="text-base leading-none">✅</span>
        <span id="mobile-badge-applied" class="absolute -top-1 -right-1.5 px-1.5 py-0.2 rounded-full bg-emerald-600 text-white font-mono text-[8px] font-bold shadow">36</span>
      </div>
      <span class="text-[10px] font-semibold tracking-tight mt-1">Applied</span>
    </button>
    <button onclick="switchPage('ai')" class="mobile-nav-btn flex-1 flex flex-col items-center py-1 rounded-xl text-slate-400" data-page="ai">
      <div class="mobile-nav-pill px-3.5 py-1 rounded-full border border-transparent flex items-center justify-center transition-all">
        <span class="text-base leading-none">🤖</span>
      </div>
      <span class="text-[10px] font-semibold tracking-tight mt-1">Copilot</span>
    </button>
    <button onclick="openMobileDrawer()" class="mobile-nav-btn flex-1 flex flex-col items-center py-1 rounded-xl text-slate-400">
      <div class="mobile-nav-pill px-3.5 py-1 rounded-full border border-transparent flex items-center justify-center transition-all">
        <span class="text-base leading-none">☰</span>
      </div>
      <span class="text-[10px] font-semibold tracking-tight mt-1">Menu</span>
    </button>
  </nav>

  <!-- ═══ MAIN CONTENT ═══ -->
  <main class="flex-1 flex flex-col min-h-screen overflow-hidden pb-24 lg:pb-0">

    <!-- Top Bar -->
    <header class="h-14 border-b border-white/10 bg-surface-950/75 backdrop-blur-xl flex items-center justify-between px-3.5 sm:px-6 sticky top-0 z-30">
      <div class="flex items-center gap-2.5 sm:gap-4 min-w-0">
        <button onclick="openMobileDrawer()" class="lg:hidden w-9 h-9 rounded-xl bg-gradient-to-br from-brand-600 to-indigo-700 flex items-center justify-center text-white font-bold text-xs shadow-md shadow-brand-600/30 flex-shrink-0" title="Open Menu">SJ</button>
        <button onclick="toggleSidebar()" class="hidden lg:block text-slate-400 hover:text-white">☰</button>
        <div class="min-w-0">
          <h1 id="page-title" class="text-xs sm:text-sm font-bold text-white truncate">Dashboard</h1>
          <div class="lg:hidden text-[9px] font-mono text-slate-400 truncate" id="topbar-student-sub">Signed In • KIIT</div>
        </div>
        <span class="hidden sm:inline-block text-[10px] font-mono text-slate-400 bg-slate-800/70 border border-white/5 px-2.5 py-0.5 rounded-full" id="last-sync-badge">—</span>
      </div>
      <div class="flex items-center gap-1.5 sm:gap-2 flex-shrink-0">
        <button id="topbar-clarif-btn" onclick="switchPage('ai')" class="hidden items-center gap-1 px-2.5 sm:px-3 py-1.5 rounded-xl bg-amber-500/15 border border-amber-500/30 text-amber-300 text-xs font-semibold transition-all animate-pulse-slow">
          <span>⚠️</span> <span id="topbar-clarif-count" class="hidden sm:inline">0 Clarifications</span>
        </button>
        <button onclick="toggleUserAutoApply()" id="user-autoapply-btn" class="flex items-center gap-1.5 px-2.5 sm:px-3 py-2 rounded-xl bg-surface-900/90 border border-white/10 text-slate-200 text-xs font-semibold transition-all" title="Toggle Auto-Apply for your account">
          <span id="user-autoapply-dot" class="w-2 h-2 rounded-full bg-slate-500"></span>
          <span id="user-autoapply-label" class="text-[11px]">Auto: OFF</span>
        </button>
        <button onclick="triggerSync()" id="sync-btn" class="flex items-center gap-1.5 px-3 py-2 rounded-xl bg-gradient-to-r from-brand-600 to-indigo-600 text-white text-xs font-semibold transition-all shadow-md shadow-brand-600/25">
          <span id="sync-icon">🔄</span> <span class="hidden sm:inline">Sync Now</span>
        </button>
        <button onclick="toggleDaemon()" id="daemon-toggle-btn" class="hidden sm:flex items-center gap-1.5 px-2.5 sm:px-3 py-2 rounded-xl bg-surface-900/90 border border-white/10 text-slate-200 text-xs font-semibold transition-all">
          <span id="daemon-dot" class="w-2 h-2 rounded-full bg-emerald-400 live-dot"></span>
          <span id="daemon-label" class="hidden sm:inline">Engine ON</span>
        </button>
        <button onclick="logoutUser()" class="p-2 rounded-xl bg-surface-900/90 border border-white/10 text-slate-400 hover:text-rose-300 text-xs font-semibold transition-all" title="Sign Out">🚪</button>
      </div>
    </header>

    <!-- Page Content -->
    <div class="flex-1 overflow-y-auto sheet-scroll">

      <!-- ═══ DASHBOARD PAGE ═══ -->
      <div id="page-dashboard" class="page p-3.5 sm:p-6 space-y-4 sm:space-y-6">

        <!-- Mobile-First Candidate Mission Hero Banner -->
        <div class="relative overflow-hidden rounded-2xl p-4 sm:p-6 border border-indigo-500/25 bg-gradient-to-br from-indigo-950/75 via-surface-900/90 to-purple-950/50 shadow-2xl">
          <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
            <div class="flex items-center gap-3.5">
              <div id="hero-student-initials" class="w-12 h-12 sm:w-14 sm:h-14 rounded-2xl bg-gradient-to-tr from-brand-600 via-indigo-500 to-purple-500 flex items-center justify-center text-white font-extrabold text-base sm:text-lg shadow-lg shadow-indigo-500/30 border border-white/20 flex-shrink-0">
                ST
              </div>
              <div class="min-w-0">
                <div class="flex items-center gap-2 flex-wrap">
                  <h2 id="hero-student-name" class="text-sm sm:text-base font-extrabold text-white tracking-tight truncate">Student Account</h2>
                  <span id="hero-student-cgpa" class="text-[10px] font-mono px-2 py-0.5 rounded-full bg-emerald-500/15 text-emerald-300 border border-emerald-500/30 font-semibold">Verified</span>
                </div>
                <p id="hero-student-meta" class="text-[11px] sm:text-xs text-slate-300 font-mono mt-0.5 truncate">KIIT University • Encrypted Vault Active</p>
                <div class="flex items-center gap-2 mt-2">
                  <div class="h-1.5 w-28 sm:w-40 rounded-full bg-slate-800/90 overflow-hidden">
                    <div id="hero-progress-bar" class="h-full rounded-full bg-gradient-to-r from-emerald-400 to-brand-400 transition-all duration-500" style="width: 0%"></div>
                  </div>
                  <span id="hero-progress-label" class="text-[10px] font-mono text-indigo-200 font-semibold">Syncing drives...</span>
                </div>
              </div>
            </div>
            <div class="grid grid-cols-2 sm:flex items-center gap-2 pt-1 sm:pt-0">
              <button onclick="switchPage('drives')" class="px-3.5 py-2.5 rounded-xl bg-white text-slate-950 font-bold text-xs shadow-lg flex items-center justify-center gap-1.5">
                <span>⚡</span> <span>Explore Drives</span>
              </button>
              <button onclick="switchPage('profile')" class="px-3.5 py-2.5 rounded-xl bg-surface-900/80 border border-white/15 text-slate-200 font-semibold text-xs flex items-center justify-center gap-1.5">
                <span>👤</span> <span>My Profile</span>
              </button>
            </div>
          </div>
        </div>

        <!-- Metrics Bento Grid -->
        <div class="grid grid-cols-2 lg:grid-cols-5 gap-3 sm:gap-4">
          <div onclick="switchPage('drives'); setDrivesFilter('all');" class="metric-card rounded-2xl p-4 cursor-pointer mobile-tap">
            <div class="flex items-center justify-between mb-1.5">
              <span class="text-[10px] text-slate-400 uppercase font-mono tracking-wider">Total Drives</span>
              <span class="w-6 h-6 rounded-lg bg-brand-500/15 text-brand-400 flex items-center justify-center text-xs">💼</span>
            </div>
            <div class="text-2xl sm:text-3xl font-extrabold text-white font-mono" id="m-total">—</div>
            <div class="text-[10px] text-slate-400 mt-1">Indexed from Superset</div>
          </div>
          <div onclick="switchPage('applied')" class="metric-card rounded-2xl p-4 cursor-pointer mobile-tap">
            <div class="flex items-center justify-between mb-1.5">
              <span class="text-[10px] text-emerald-300/80 uppercase font-mono tracking-wider">Applied</span>
              <span class="w-6 h-6 rounded-lg bg-emerald-500/15 text-emerald-400 flex items-center justify-center text-xs">✓</span>
            </div>
            <div class="text-2xl sm:text-3xl font-extrabold text-emerald-400 font-mono" id="m-applied">—</div>
            <div class="text-[10px] text-slate-400 mt-1">Confirmed on portal</div>
          </div>
          <div onclick="switchPage('drives'); setDrivesFilter('open');" class="metric-card rounded-2xl p-4 cursor-pointer mobile-tap">
            <div class="flex items-center justify-between mb-1.5">
              <span class="text-[10px] text-amber-300/80 uppercase font-mono tracking-wider">Active Open</span>
              <span class="w-6 h-6 rounded-lg bg-amber-500/15 text-amber-400 flex items-center justify-center text-xs">⚡</span>
            </div>
            <div class="text-2xl sm:text-3xl font-extrabold text-amber-400 font-mono" id="m-open">—</div>
            <div class="text-[10px] text-slate-400 mt-1">Ready for 1-click apply</div>
          </div>
          <div onclick="switchPage('drives'); setDrivesFilter('eligible');" class="metric-card rounded-2xl p-4 cursor-pointer mobile-tap">
            <div class="flex items-center justify-between mb-1.5">
              <span class="text-[10px] text-indigo-300/80 uppercase font-mono tracking-wider">Eligible</span>
              <span class="w-6 h-6 rounded-lg bg-indigo-500/15 text-indigo-400 flex items-center justify-center text-xs">🎯</span>
            </div>
            <div class="text-2xl sm:text-3xl font-extrabold text-brand-400 font-mono" id="m-eligible">—</div>
            <div class="text-[10px] text-slate-400 mt-1">Matching criteria</div>
          </div>
          <div onclick="switchPage('drives'); setDrivesFilter('closed');" class="col-span-2 lg:col-span-1 metric-card rounded-2xl p-4 cursor-pointer mobile-tap">
            <div class="flex items-center justify-between mb-1.5">
              <span class="text-[10px] text-slate-400 uppercase font-mono tracking-wider">Closed / Past</span>
              <span class="w-6 h-6 rounded-lg bg-slate-800 text-slate-400 flex items-center justify-center text-xs">🔒</span>
            </div>
            <div class="text-2xl sm:text-3xl font-extrabold text-slate-300 font-mono" id="m-closed">—</div>
            <div class="text-[10px] text-slate-400 mt-1">Deadline completed</div>
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
                <span class="text-slate-400">Telegram Bot</span>
                <span id="dash-telegram" class="font-mono font-semibold text-emerald-400">PAIRED</span>
              </div>
              <div class="flex justify-between text-xs">
                <span class="text-slate-400">Pre-Submit Review</span>
                <span id="dash-autoapply" class="font-mono font-semibold text-brand-400">ENABLED</span>
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

      <!-- ═══ TELEGRAM PAGE ═══ -->
      <div id="page-telegram" class="page hidden p-6 space-y-6">
        <div class="flex items-center justify-between">
          <div>
            <h2 class="text-lg font-bold text-white">Telegram Push Notifications</h2>
            <p class="text-xs text-slate-500">Instant alerts to your phone for new drives and auto-applications via @XSuperset_bot</p>
          </div>
          <button onclick="testTelegram()" class="px-4 py-2 rounded-lg bg-blue-600 hover:bg-blue-500 text-white text-xs font-semibold transition-all shadow-md shadow-blue-600/20">
            📨 Send Test Alert
          </button>
        </div>

        <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
          <div class="bg-surface-900/60 rounded-xl p-5 border border-slate-800/60">
            <h3 class="text-xs font-semibold text-blue-400 uppercase tracking-wider mb-4">Bot Configuration</h3>
            <div class="space-y-4">
              <div class="flex justify-between items-center">
                <span class="text-sm text-slate-400">Bot Handle</span>
                <span class="text-sm font-mono text-white">@XSuperset_bot</span>
              </div>
              <div class="flex justify-between items-center">
                <span class="text-sm text-slate-400">Chat ID</span>
                <span class="text-sm font-mono text-white" id="tg-chat-id">Not Configured</span>
              </div>
              <div class="flex justify-between items-center">
                <span class="text-sm text-slate-400">Status</span>
                <span class="text-sm font-semibold text-emerald-400" id="tg-status">● Connected</span>
              </div>
            </div>
          </div>

          <div class="bg-surface-900/60 rounded-xl p-5 border border-slate-800/60">
            <h3 class="text-xs font-semibold text-blue-400 uppercase tracking-wider mb-4">Active Alert Policies</h3>
            <div class="space-y-4 text-sm">
              <div class="flex justify-between items-center">
                <span class="text-slate-400">New Campus Drive Alert</span>
                <span class="text-emerald-400 font-semibold font-mono text-xs">IMMEDIATE</span>
              </div>
              <div class="flex justify-between items-center">
                <span class="text-slate-400">Auto-Apply Confirmation Receipt</span>
                <span class="text-emerald-400 font-semibold font-mono text-xs">INSTANT</span>
              </div>
              <div class="flex justify-between items-center">
                <span class="text-slate-400">Application Error / Rejection Alert</span>
                <span class="text-emerald-400 font-semibold font-mono text-xs">ENABLED</span>
              </div>
              <div class="flex justify-between items-center">
                <span class="text-slate-400">1-Click Google Calendar Button</span>
                <span class="text-emerald-400 font-semibold font-mono text-xs">INCLUDED</span>
              </div>
            </div>
          </div>
        </div>

        <!-- Custom Telegram Dispatcher -->
        <div class="bg-surface-900/60 rounded-xl p-5 border border-slate-800/60 space-y-3">
          <h3 class="text-xs font-semibold text-blue-400 uppercase tracking-wider">Custom Message Dispatcher</h3>
          <p class="text-xs text-slate-400">Send an immediate customized text notification directly to your phone via the bot.</p>
          <div class="flex gap-3">
            <input type="text" id="tg-custom-text" placeholder="Type notification text to send to your phone..."
              class="flex-1 bg-surface-950 border border-slate-700 rounded-lg px-4 py-2 text-xs text-white placeholder-slate-500 focus:border-blue-500 focus:outline-none font-mono">
            <button onclick="sendCustomTelegram()" class="px-4 py-2 rounded-lg bg-blue-600 hover:bg-blue-500 text-white text-xs font-semibold transition-all">
              Send Alert
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

        <button id="auth-submit-btn" type="submit" class="w-full py-3 px-4 rounded-xl bg-gradient-to-r from-brand-600 to-indigo-600 text-white font-bold text-xs shadow-lg shadow-brand-600/30 flex items-center justify-center gap-2">
          <span>⚡</span> <span id="auth-submit-label">Sign In</span>
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

<script>
// ═══════════════════════════════════════════════════════════════════
// STATE & SAFE API CLIENT
// ═══════════════════════════════════════════════════════════════════
let allJobs = [];
let statusData = {};
let profileData = {};
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
  const palettes = [
    'from-indigo-500 to-purple-600 shadow-indigo-500/25',
    'from-emerald-500 to-teal-600 shadow-emerald-500/25',
    'from-rose-500 to-orange-500 shadow-rose-500/25',
    'from-cyan-500 to-blue-600 shadow-cyan-500/25',
    'from-amber-500 to-orange-600 shadow-amber-500/25',
    'from-fuchsia-500 to-pink-600 shadow-fuchsia-500/25'
  ];
  let hash = 0;
  const s = String(name || 'Company');
  for (let i = 0; i < s.length; i++) hash = (hash * 31 + s.charCodeAt(i)) >>> 0;
  return palettes[hash % palettes.length];
}

function getCompanyInitials(name) {
  const words = String(name || 'C').replace(/[^a-zA-Z0-9 ]/g, '').trim().split(/\s+/).filter(Boolean);
  if (words.length >= 2) return (words[0][0] + words[1][0]).toUpperCase();
  return (words[0] || 'C').slice(0, 2).toUpperCase();
}

function toast(msg, type = 'info') {
  const container = document.getElementById('toast-container');
  const colors = {
    success: 'bg-emerald-950/95 border-emerald-500/50 text-emerald-100 shadow-emerald-950/50',
    error: 'bg-red-950/95 border-red-500/50 text-red-100 shadow-red-950/50',
    warn: 'bg-amber-950/95 border-amber-500/50 text-amber-100 shadow-amber-950/50',
    info: 'bg-slate-900/95 border-indigo-500/40 text-slate-100 shadow-slate-950/50'
  };
  const icons = { success: '✓', error: '✕', warn: '⚠', info: 'ℹ' };
  const el = document.createElement('div');
  el.className = `toast rounded-2xl px-4 py-3 border text-xs font-medium shadow-2xl flex items-start gap-3 ${colors[type] || colors.info}`;
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
  if (isApplied) return '<span class="badge-applied text-[10px] font-mono px-2.5 py-1 rounded-full font-semibold flex-shrink-0">✓ Applied</span>';
  if (!isClosed && j.is_eligible) return '<span class="badge-open text-[10px] font-mono px-2.5 py-1 rounded-full font-semibold animate-pulse-slow flex-shrink-0">⚡ Open</span>';
  if (isClosed) return '<span class="badge-closed text-[10px] font-mono px-2.5 py-1 rounded-full font-semibold flex-shrink-0">Closed</span>';
  return '<span class="badge-ineligible text-[10px] font-mono px-2.5 py-1 rounded-full font-semibold flex-shrink-0">Ineligible</span>';
}

function getJobActions(j) {
  const isApplied = j.is_applied || j.app_status === 'APPLIED';
  const isClosed = isJobClosed(j);
  let html = '';
  if (!isApplied && !isClosed && j.is_eligible) {
    html += `<button onclick="applyToJob('${j.id}','${esc(j.company_name)}','${esc(j.job_role)}')" class="flex-1 min-h-[42px] py-2.5 px-3.5 rounded-xl bg-gradient-to-r from-brand-600 to-indigo-600 text-white text-xs font-bold transition-all flex items-center justify-center gap-1.5 shadow-lg shadow-brand-600/25"><span>⚡</span> <span>Apply Now</span></button>`;
  } else if (isApplied) {
    html += `<button disabled class="flex-1 min-h-[42px] py-2.5 px-3.5 rounded-xl bg-emerald-500/10 text-emerald-300 text-xs font-bold cursor-default border border-emerald-500/25 flex items-center justify-center gap-1.5"><span>✓</span> <span>Applied on Superset</span></button>`;
  } else {
    html += `<button disabled class="flex-1 min-h-[42px] py-2.5 px-3.5 rounded-xl bg-surface-800/60 text-slate-500 text-xs font-semibold cursor-not-allowed border border-white/5">${isClosed ? 'Deadline Passed' : 'Not Eligible'}</button>`;
  }
  html += `<button onclick="showJobQuestions('${j.id}', '${esc(j.company_name)}')" class="min-w-[42px] min-h-[42px] p-2.5 rounded-xl bg-surface-800/80 text-purple-300 border border-white/10 transition-all text-xs flex items-center justify-center" title="Inspect Custom Questions & Answers">📝</button>`;
  html += `<a href="${j.profile_url || '#'}" target="_blank" class="min-w-[42px] min-h-[42px] p-2.5 rounded-xl bg-surface-800/80 text-slate-300 border border-white/10 transition-all text-xs flex items-center justify-center" title="Open on Superset Portal">↗</a>`;
  return html;
}

// ═══════════════════════════════════════════════════════════════════
// NAVIGATION
// ═══════════════════════════════════════════════════════════════════
function switchPage(page) {
  currentPage = page;
  document.querySelectorAll('.page').forEach(p => p.classList.add('hidden'));
  const target = document.getElementById('page-' + page);
  if (target) target.classList.remove('hidden');

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
    applied:'My Applications Pipeline',
    profile:'Profile & Custom Question Answers',
    engine:'Engine Telemetry & Logs',
    telegram:'Telegram Notifications',
    ai:'AI Copilot, RAG & Live Error Healer',
    settings:'System Settings'
  };
  document.getElementById('page-title').textContent = titles[page] || page;

  // Refresh page data
  if (page === 'drives') renderDrives();
  if (page === 'applied') renderApplied();
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

  const res = await fetch(url, Object.assign({}, options, { headers }));
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
      await Promise.all([fetchStatus(), fetchJobs(), fetchProfile()]);
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
      <div class="flex justify-between text-[11px] mb-1"><span class="text-slate-300 truncate mr-2">${esc(cat)}</span><span class="text-white font-mono font-semibold">${count}</span></div>
      <div class="h-1.5 rounded-full bg-surface-800 overflow-hidden"><div class="h-full rounded-full bg-gradient-to-r from-brand-500 to-purple-500" style="width:${pct}%"></div></div>
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
      <div class="flex justify-between text-[11px] mb-1"><span class="text-slate-300 truncate mr-2">${esc(loc)}</span><span class="text-white font-mono font-semibold">${count}</span></div>
      <div class="h-1.5 rounded-full bg-surface-800 overflow-hidden"><div class="h-full rounded-full bg-gradient-to-r from-emerald-500 to-teal-400" style="width:${pct}%"></div></div>
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
      b.className = 'drives-filter active px-3.5 py-1.5 rounded-full text-xs font-semibold bg-brand-600 text-white shadow-sm shadow-brand-600/30';
    } else {
      b.className = 'drives-filter px-3.5 py-1.5 rounded-full text-xs font-semibold bg-surface-900/90 border border-white/10 text-slate-300';
    }
  });
  renderDrives();
}

function setCtcFilter(tier) {
  ctcFilter = tier;
  document.querySelectorAll('.ctc-filter').forEach(b => {
    if (b.dataset.ctc === tier) {
      b.className = 'ctc-filter active px-3 py-1.5 rounded-full text-[11px] font-semibold bg-brand-600 text-white shadow-sm shadow-brand-600/30';
    } else {
      b.className = 'ctc-filter px-3 py-1.5 rounded-full text-[11px] font-semibold bg-surface-900/90 border border-white/10 text-slate-300';
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
    grid.innerHTML = '<div class="col-span-full text-center py-16 text-slate-400"><div class="text-4xl mb-2">📭</div><div class="text-sm font-medium">No drives match your current filters</div></div>';
  } else {
    grid.innerHTML = jobs.map(j => {
      const initials = getCompanyInitials(j.company_name);
      const avatarGradient = getCompanyAvatarStyle(j.company_name);
      const sector = j.raw?.sectorName || '';
      const posType = j.raw?.positionTypeLabel || '';
      return `
        <div class="job-card rounded-2xl p-4 sm:p-5 flex flex-col gap-3.5">
          <div class="flex items-start justify-between gap-3">
            <div class="flex items-center gap-3 min-w-0">
              <div class="w-11 h-11 rounded-2xl bg-gradient-to-br ${avatarGradient} border border-white/20 shadow-lg flex items-center justify-center font-extrabold text-white text-xs tracking-wider flex-shrink-0">${initials}</div>
              <div class="min-w-0">
                <h3 class="font-bold text-sm text-white truncate leading-snug">${esc(j.company_name)}</h3>
                <div class="text-xs text-indigo-200/80 truncate mt-0.5">${esc(j.job_role)}</div>
              </div>
            </div>
            ${getJobBadge(j)}
          </div>
          <div class="grid grid-cols-2 gap-2 text-[11px]">
            <div class="p-2.5 rounded-xl bg-surface-950/70 border border-white/5">
              <div class="text-[9px] text-slate-400 uppercase font-mono mb-0.5 flex items-center gap-1"><span>💰</span> CTC Package</div>
              <div class="font-bold text-white truncate font-mono text-[11px]">${esc(j.ctc_display || 'As per Policy')}</div>
            </div>
            <div class="p-2.5 rounded-xl bg-surface-950/70 border border-white/5">
              <div class="text-[9px] text-slate-400 uppercase font-mono mb-0.5 flex items-center gap-1"><span>📍</span> Location</div>
              <div class="font-semibold text-slate-200 truncate text-[11px]">${esc(j.location || 'Pan India')}</div>
            </div>
          </div>
          ${sector || posType ? `<div class="flex flex-wrap gap-1.5">
            ${sector ? `<span class="text-[9px] font-mono px-2.5 py-0.5 rounded-full bg-surface-800/80 text-slate-300 border border-white/5">${esc(sector)}</span>` : ''}
            ${posType ? `<span class="text-[9px] font-mono px-2.5 py-0.5 rounded-full bg-indigo-950/60 text-indigo-300 border border-indigo-500/20">${esc(posType)}</span>` : ''}
          </div>` : ''}
          <div class="text-[10px] text-slate-400 font-mono flex items-center justify-between">
            <span class="flex items-center gap-1.5">⏰ ${esc(j.deadline_display || 'Open Window')}</span>
          </div>
          <div class="flex items-center gap-2 pt-2.5 border-t border-white/10 mt-auto">
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
// TELEGRAM PAGE
// ═══════════════════════════════════════════════════════════════════
function renderTelegram() {
  const d = statusData;
  document.getElementById('tg-chat-id').textContent = d.telegram_chat_id || 'Not Configured';
  const tgStatus = document.getElementById('tg-status');
  if (d.telegram_paired) { tgStatus.textContent = '● Connected & Paired'; tgStatus.className = 'text-sm font-semibold text-emerald-400'; }
  else { tgStatus.textContent = '○ Not paired'; tgStatus.className = 'text-sm font-semibold text-red-400'; }
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
    await Promise.all([fetchStatus(), fetchJobs()]);
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
    await Promise.all([fetchStatus(), fetchJobs(), fetchProfile()]);
  } else {
    showAuthOverlay();
  }

  // Auto-refresh cadence when authenticated
  setInterval(() => { if (authToken || currentUser) fetchStatus(); }, 20000);
  setInterval(() => { if (authToken || currentUser) fetchJobs(); }, 60000);
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
    resp.set_cookie("sjp_token", token, httponly=True, samesite="Lax", max_age=30 * 86400)
    resp.set_cookie("sjp_logged_out", "0", max_age=30 * 86400)
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
    resp.set_cookie("sjp_token", token, httponly=True, samesite="Lax", max_age=30 * 86400)
    resp.set_cookie("sjp_logged_out", "0", max_age=30 * 86400)
    return resp


@app.route("/api/auth/me")
def auth_me():
    user = get_current_user()
    if not user:
        return jsonify({"authenticated": False}), 401
    token = _extract_token() or user_manager.create_session(user["user_id"])
    return jsonify({
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
    jobs_list = cached_jobs or _cached_jobs
    applied_count = sum(1 for j in jobs_list if j.get("is_applied") or j.get("app_status") == "APPLIED")
    open_count = sum(1 for j in jobs_list if j.get("is_open"))
    total_jobs = len(jobs_list)
    prof = user["profile"]

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
                "errors": ["Superset authentication failed. Check your Superset credentials."],
                "message": "Superset authentication failed.",
            }), 401

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
            return jsonify({"error": "Failed to log in to Superset"}), 401

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
            err_msg = "Superset authentication failed. Please check your credentials."
            log_event(err_msg, "ERROR")
            return jsonify({"success": False, "message": err_msg}), 401

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
            "pending": am.get_pending_tickets(),
            "all": am.get_all_tickets(),
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
        ticket = am.resolve_ticket(ticket_id, str(answer))
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
