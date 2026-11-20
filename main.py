"""CLI entry point for Superset Job Pilot."""
import argparse
import logging
import sys
import time
from datetime import datetime, timedelta
import pytz
from config import Config
from auto_applier import AutoApplier
from calendar_sync import CalendarSync
from telegram_notifier import TelegramNotifier
from superset_client import SupersetClient
from user_manager import UserManager
from notice_scanner import scan_notices_for_user

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("superset_pilot")
IST = pytz.timezone("Asia/Kolkata")


def run_test():
    """Runs a simulated test using realistic campus placement job data."""
    logger.info("--- Starting Superset Job Pilot Simulation Test ---")
    
    ts = int(time.time())
    now_ist = datetime.now(IST)
    deadline_cognizant = now_ist + timedelta(hours=14, minutes=30)
    deadline_hexaware = now_ist + timedelta(hours=6, minutes=45)
    
    mock_jobs = [
        {
            "id": f"mock-cognizant-genc-{ts}",
            "company_name": "Cognizant Technology Solutions",
            "job_role": "Analyst Trainee (GenC Next)",
            "ctc_display": "₹8.50 LPA",
            "location": "Bengaluru / Hyderabad / Pune / Chennai",
            "deadline_dt": deadline_cognizant,
            "deadline_display": deadline_cognizant.strftime("%d %b %Y, %I:%M %p IST"),
            "hours_left": 14.5,
            "is_eligible": True,
            "is_applied": False,
            "profile_url": "https://app.joinsuperset.com",
        },
        {
            "id": f"mock-hexaware-get-{ts}",
            "company_name": "Hexaware Technologies",
            "job_role": "Graduate Engineer Trainee (GET)",
            "ctc_display": "₹5.00 LPA",
            "location": "Chennai / Mumbai / Pune",
            "deadline_dt": deadline_hexaware,
            "deadline_display": deadline_hexaware.strftime("%d %b %Y, %I:%M %p IST"),
            "hours_left": 6.75,
            "is_eligible": True,
            "is_applied": False,
            "profile_url": "https://app.joinsuperset.com",
        }
    ]

    applier = AutoApplier()
    res = applier.run_sync_cycle(mock_jobs=mock_jobs)
    
    print("\n" + "="*50)
    print("TEST EXECUTION COMPLETED")
    print(f"Total Jobs Processed : {res['total_found']}")
    print(f"New Jobs Tracked     : {res['new_jobs']}")
    print(f"Auto-Applied Count   : {res['auto_applied']}")
    print(f"Telegram Alerts Sent : {res['notified']}")
    print("="*50 + "\n")
    
    # Also preview sample Google Calendar link
    sample_cal = CalendarSync.generate_google_calendar_url(
        title="Cognizant GenC Next Deadline",
        deadline_dt=deadline_cognizant,
        description="Superset Campus Placement Application Deadline"
    )
    print("Sample 1-Click Google Calendar Link generated:")
    print(sample_cal)
    print("\nTest completed successfully!")


def run_notices_scan(send_alerts: bool = True):
    """Scans Superset homepage notices, shortlist PDFs, and pending offers for all vault users."""
    um = UserManager()
    users = um.get_all_users()
    if not users:
        logger.warning("No users in encrypted vault to scan notices for.")
        return

    for u in users:
        sc = SupersetClient(email=u["email"], password=u["superset_password"])
        if not sc.login():
            logger.warning(f"Superset login skipped/failed for {u['email']}.")
            continue
        res = scan_notices_for_user(sc, um, u, max_pages=2, page_size=25, send_alerts=send_alerts)
        counts = res.get("counts", {})
        stats = res.get("scan_stats", {})
        logger.info(
            f"Notices scan for {u['full_name']}: {counts.get('clean', 0)} clean notices, "
            f"{counts.get('shortlisted', 0)} shortlisted hits, "
            f"{counts.get('pending_offers', 0)} pending offers "
            f"(PDFs: {stats.get('pdfs_downloaded_now', 0)} new / {stats.get('pdfs_from_cache', 0)} cached)."
        )


def run_daemon():
    """Runs the monitor continuously on a schedule."""
    logger.info(f"Starting Superset Job Pilot daemon (poll every {Config.POLL_INTERVAL_MINUTES} min)...")
    applier = AutoApplier()
    
    while True:
        try:
            logger.info("Executing scheduled sync cycle...")
            res = applier.run_sync_cycle()
            logger.info(f"Cycle completed: {res['new_jobs']} new, {res['auto_applied']} auto-applied.")
            run_notices_scan(send_alerts=True)
        except Exception as e:
            logger.error(f"Error during daemon execution: {e}")
        
        sleep_secs = Config.POLL_INTERVAL_MINUTES * 60
        logger.info(f"Sleeping for {Config.POLL_INTERVAL_MINUTES} minutes...")
        time.sleep(sleep_secs)


def show_status():
    """Displays summary of all tracked jobs from SQLite."""
    applier = AutoApplier()
    jobs = applier.get_tracked_summary()
    
    print("\n" + "="*70)
    print(f"SUPERSET TRACKED JOBS ({len(jobs)} total)")
    print("="*70)
    for j in jobs:
        status_symbol = "🟢" if "APPLIED" in j["applied_status"] else "🟡"
        print(f"{status_symbol} [{j['company_name']}] - {j['job_role']}")
        print(f"   CTC: {j['ctc']} | Status: {j['applied_status']} | Deadline: {j['deadline']}")
        print(f"   Notified: {j['notified_at']}")
        print("-" * 70)


def setup_telegram():
    """Detects and pairs the user's Telegram Chat ID with the bot."""
    print("\n" + "="*60)
    print("TELEGRAM BOT SETUP & PAIRING")
    print("="*60)
    notifier = TelegramNotifier()
    info = notifier.test_connection()
    if not info.get("ok"):
        print(f"❌ Failed to reach Telegram API: {info.get('error')}")
        return

    bot_username = info.get("result", {}).get("username", "XSuperset_bot")
    print(f"✅ Bot connected: @{bot_username}")
    print(f"👉 Please open: https://t.me/{bot_username}")
    print("👉 Send /start or any greeting message to the bot now.")
    print("Checking for messages...")

    chat_id = notifier.detect_chat_id()
    if not chat_id:
        print("\n⏳ Waiting up to 15 seconds for incoming /start message...")
        for _ in range(15):
            time.sleep(1)
            chat_id = notifier.detect_chat_id()
            if chat_id:
                break

    if chat_id:
        print(f"\n🎉 Successfully captured Chat ID: {chat_id}")
        env_path = Config.BASE_DIR / ".env"
        if env_path.exists():
            content = env_path.read_text()
            if "TELEGRAM_CHAT_ID=" in content:
                import re
                content = re.sub(r"TELEGRAM_CHAT_ID=.*", f"TELEGRAM_CHAT_ID={chat_id}", content)
            else:
                content += f"\nTELEGRAM_CHAT_ID={chat_id}\n"
            env_path.write_text(content)
            print("💾 Saved TELEGRAM_CHAT_ID into .env")

        # Send test message
        test_notifier = TelegramNotifier(chat_id=chat_id)
        test_msg = (
            "🚀 <b>Superset Job Pilot Connected!</b>\n\n"
            "Your campus placement alert system is active.\n"
            "You will receive instant alerts for every newly posted company drive, "
            "with deadline countdowns, 1-click Google Calendar links, and auto-apply status!"
        )
        test_notifier.send_message(test_msg)
        print("📨 Sent welcome message to your Telegram app!")
    else:
        print("\n⚠️ No message received yet.")
        print(f"Open https://t.me/{bot_username} on your phone/desktop, tap 'Start', and re-run:")
        print("  python main.py setup-telegram")
    print("="*60 + "\n")


def main():
    parser = argparse.ArgumentParser(description="Superset Campus Placement Job Pilot")
    parser.add_argument("command", choices=["run", "test", "daemon", "status", "setup-telegram", "scan-notices"], default="run", nargs="?",
                        help="Command to run: 'run', 'test', 'daemon', 'status', 'setup-telegram', 'scan-notices'")
    args = parser.parse_args()

    if args.command == "test":
        run_test()
    elif args.command == "daemon":
        run_daemon()
    elif args.command == "status":
        show_status()
    elif args.command == "setup-telegram":
        setup_telegram()
    elif args.command == "scan-notices":
        run_notices_scan(send_alerts=True)
    else:
        applier = AutoApplier()
        res = applier.run_sync_cycle()
        print(f"Sync complete. New: {res['new_jobs']}, Auto-Applied: {res['auto_applied']}")
        run_notices_scan(send_alerts=True)


if __name__ == "__main__":
    main()

