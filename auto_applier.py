"""Job orchestrator, SQLite database tracker, and auto-apply engine."""
import logging
import sqlite3
from datetime import datetime, timezone
from config import Config
from calendar_sync import CalendarSync
from superset_client import SupersetClient
from telegram_notifier import TelegramNotifier
from gmail_monitor import GmailMonitor

logger = logging.getLogger(__name__)


class AutoApplier:
    def __init__(self):
        self.db_path = Config.DB_PATH
        self.superset = SupersetClient()
        self.telegram = TelegramNotifier()
        self.gmail = GmailMonitor()
        self._init_db()

    def _init_db(self):
        """Initializes SQLite tables for tracking seen and applied jobs."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tracked_jobs (
                id TEXT PRIMARY KEY,
                company_name TEXT,
                job_role TEXT,
                ctc TEXT,
                deadline TEXT,
                is_eligible INTEGER,
                applied_status TEXT,
                notified_at TEXT,
                applied_at TEXT
            )
        """)
        conn.commit()
        conn.close()

    def is_job_recorded(self, job_id: str) -> bool:
        """Checks if a job has already been notified/processed."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM tracked_jobs WHERE id = ?", (job_id,))
        row = cursor.fetchone()
        conn.close()
        return row is not None

    def record_job(self, job: dict, status: str, applied: bool = False):
        """Saves a job record into SQLite."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        now_iso = datetime.now(timezone.utc).isoformat()
        applied_time = now_iso if applied else None

        cursor.execute("""
            INSERT OR REPLACE INTO tracked_jobs 
            (id, company_name, job_role, ctc, deadline, is_eligible, applied_status, notified_at, applied_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            job["id"],
            job["company_name"],
            job["job_role"],
            job["ctc_display"],
            job.get("deadline_display", ""),
            1 if job.get("is_eligible") else 0,
            status,
            now_iso,
            applied_time
        ))
        conn.commit()
        conn.close()

    def get_tracked_count(self) -> int:
        """Returns the total number of tracked jobs in the SQLite database."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM tracked_jobs")
        count = cursor.fetchone()[0]
        conn.close()
        return count

    def run_sync_cycle(self, mock_jobs: list = None) -> dict:
        """
        Executes one full detection, auto-apply, and alert cycle:
        1. Fetches active profiles from Superset (or uses mock_jobs for testing)
        2. Filters new un-processed jobs
        3. Executes auto-apply for eligible positions if configured
        4. Generates calendar links with reminders
        5. Sends instant Telegram notifications
        """
        import pytz
        ist = pytz.timezone("Asia/Kolkata")
        now_ist = datetime.now(ist)

        results = {
            "total_found": 0,
            "new_jobs": 0,
            "auto_applied": 0,
            "notified": 0,
            "errors": []
        }

        # 1. Fetch from Superset
        if mock_jobs is not None:
            jobs = mock_jobs
        else:
            if not self.superset.login():
                logger.warning("Superset login did not succeed, proceeding with available token or cached data.")
            jobs = self.superset.fetch_job_profiles()

        results["total_found"] = len(jobs)
        logger.info(f"Discovered {len(jobs)} total jobs on Superset.")

        is_baseline = (self.get_tracked_count() == 0 and mock_jobs is None)
        if is_baseline:
            logger.info("Initializing baseline database index for historical jobs...")

        for job in jobs:
            job_id = job.get("id")
            if not job_id:
                continue

            if self.is_job_recorded(job_id):
                logger.debug(f"Job {job_id} ({job.get('company_name')}) already tracked. Skipping.")
                continue

            raw_status = job.get("raw", {}).get("jobProfileStatus", "")
            is_closed = raw_status in ("CLOSED_FOR_APPLICATIONS", "COMPLETED")
            deadline_dt = job.get("deadline_dt")
            is_expired = (deadline_dt and deadline_dt < now_ist) if deadline_dt else False

            # If initializing baseline, silently record historical/expired jobs
            if is_baseline and (is_closed or is_expired or job.get("is_applied")):
                self.record_job(job, status=job.get("app_status", "ARCHIVED"), applied=job.get("is_applied", False))
                continue

            results["new_jobs"] += 1
            company = job.get("company_name", "Company")
            role = job.get("job_role", "Role")
            is_eligible = job.get("is_eligible", False)
            cal_deadline = deadline_dt or now_ist

            # 2. Auto-Apply if eligible and open
            applied = False
            status = "NOT_ELIGIBLE"
            
            if is_eligible and not is_closed and not is_expired:
                if Config.AUTO_APPLY_ELIGIBLE:
                    # Pre-flight Ambiguity Check: Ensure no mandatory questions have unclear data
                    unclear_detected = False
                    try:
                        from ambiguity_manager import AmbiguityManager
                        from attribute_resolver import AttributeResolver
                        am = AmbiguityManager()
                        resolver = AttributeResolver()
                        
                        detail_url = f"{self.superset.base_url}/tnpsuite-core/students/{self.superset.user_uuid}/job_profiles/{job_id}"
                        detail_resp = self.superset.session.get(detail_url, timeout=8)
                        if detail_resp.status_code == 200:
                            attrs = detail_resp.json().get("jobProfile", {}).get("attributeList") or []
                            for attr in attrs:
                                if attr.get("mandatory", False):
                                    resolved_v = resolver.resolve_attribute(attr)
                                    ticket = am.evaluate_and_flag(company, job_id, attr, resolved_v)
                                    if ticket and ticket.get("status") == "PENDING":
                                        unclear_detected = True
                                        logger.warning(f"⚠️ Pausing auto-apply for {company}: Mandatory attribute '{attr.get('name')}' requires candidate clarification.")
                                        results["errors"].append(f"{company}: Pending clarification on '{attr.get('name')}'. Alert dispatched to Telegram.")
                                        status = "WAITING_CLARIFICATION"
                                        break
                    except Exception as e:
                        logger.debug(f"Pre-flight ambiguity check exception: {e}")

                    if not unclear_detected:
                        logger.info(f"⚡ Auto-applying to eligible open job: {company} - {role}")
                        res = self.superset.apply_to_job(job_id, resume_id=Config.DEFAULT_RESUME_NAME)
                        if res.get("success"):
                            applied = True
                            status = "AUTO_APPLIED"
                            results["auto_applied"] += 1
                        else:
                            status = "AUTO_APPLY_FAILED"
                            results["errors"].append(f"{company}: {res.get('message')}")
                else:
                    status = "ELIGIBLE_PENDING"
            elif is_closed or is_expired:
                status = "CLOSED_OR_EXPIRED"

            # 3. Generate 1-Click Google Calendar URL
            cal_title = f"{company} - {role} Registration Deadline"
            cal_desc = (
                f"Role: {role}\n"
                f"Company: {company}\n"
                f"CTC: {job.get('ctc_display', 'N/A')}\n"
                f"Eligibility: {'ELIGIBLE' if is_eligible else 'NOT ELIGIBLE'}\n"
                f"Portal Link: {job.get('profile_url', '')}"
            )
            cal_url = CalendarSync.generate_google_calendar_url(
                title=cal_title,
                deadline_dt=cal_deadline,
                description=cal_desc
            )

            # 4. Dispatch Telegram Notification (only for active/new jobs)
            if not is_closed and not is_expired:
                try:
                    notified = self.telegram.send_job_alert(job, cal_url=cal_url, auto_applied=applied)
                    if notified:
                        results["notified"] += 1
                except Exception as te:
                    logger.warning(f"Telegram notification dispatch failed for {company}: {te}")

            # 5. Record to SQLite database
            self.record_job(job, status=status, applied=applied)

        if is_baseline:
            logger.info("Baseline index complete. Database is armed and watching for new job profiles.")

        return results

    def get_tracked_summary(self) -> list:
        """Returns all tracked jobs from SQLite."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM tracked_jobs ORDER BY notified_at DESC")
        rows = [dict(r) for r in cursor.fetchall()]
        conn.close()
        return rows
