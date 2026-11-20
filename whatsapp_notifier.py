"""
Multi-Provider WhatsApp Notifier for Superset Job Pilot.

Supports per-student encrypted configuration with 4 sanctioned API providers:
  1. callmebot  (Free, 30-second personal setup via CallMeBot WhatsApp API)
  2. meta       (Official Meta WhatsApp Business Cloud API)
  3. twilio     (Official Twilio WhatsApp Sandbox / Business API)
  4. webhook    (Custom HTTP JSON webhook bridge e.g. n8n / Make / WAHA)

Dispatches structured WhatsApp alerts for:
  - New open eligible Job Profiles (Company, Role, CTC, Location, Deadline, Apply & Calendar links)
  - Upcoming Shortlist PDF Matches (Profile, Process, Date, Reporting Time, Venue, Page #, Meet link)
  - Urgent Pending Job Offers (Company, Role, Acceptance Deadline)
"""
import logging
import re
from typing import Any, Dict, Optional, Tuple

import requests

from config import Config

logger = logging.getLogger(__name__)


def normalize_whatsapp_phone(phone: str) -> str:
    """Normalizes phone input to international digits without '+' (defaults 10-digit Indian numbers to 91)."""
    digits = re.sub(r"\D+", "", str(phone or "").strip())
    if len(digits) == 10:
        digits = "91" + digits
    return digits


class WhatsAppNotifier:
    def __init__(
        self,
        provider: str = "",
        phone: str = "",
        api_key: str = "",
        meta_phone_number_id: str = "",
        meta_access_token: str = "",
        twilio_account_sid: str = "",
        twilio_auth_token: str = "",
        twilio_from: str = "",
        webhook_url: str = "",
        enabled: bool = True,
        notify_jobs: bool = True,
        notify_shortlists: bool = True,
        notify_offers: bool = True,
    ):
        self.provider = (provider or Config.WHATSAPP_PROVIDER or "callmebot").strip().lower()
        self.phone = normalize_whatsapp_phone(phone or Config.WHATSAPP_PHONE)
        self.api_key = (api_key or Config.WHATSAPP_API_KEY or "").strip()
        self.meta_phone_number_id = (meta_phone_number_id or Config.WHATSAPP_META_PHONE_ID or "").strip()
        self.meta_access_token = (meta_access_token or Config.WHATSAPP_META_TOKEN or "").strip()
        self.twilio_account_sid = (twilio_account_sid or Config.TWILIO_ACCOUNT_SID or "").strip()
        self.twilio_auth_token = (twilio_auth_token or Config.TWILIO_AUTH_TOKEN or "").strip()
        self.twilio_from = (twilio_from or Config.TWILIO_WHATSAPP_FROM or "whatsapp:+14155238886").strip()
        self.webhook_url = (webhook_url or Config.WHATSAPP_WEBHOOK_URL or "").strip()
        self.enabled = bool(enabled)
        self.notify_jobs = bool(notify_jobs)
        self.notify_shortlists = bool(notify_shortlists)
        self.notify_offers = bool(notify_offers)

    @classmethod
    def from_user_profile(cls, user: Optional[Dict[str, Any]]) -> "WhatsAppNotifier":
        """Builds a WhatsAppNotifier from a student's private encrypted profile vault."""
        prof = (user or {}).get("profile") or {}
        cfg = prof.get("whatsapp_config") if isinstance(prof.get("whatsapp_config"), dict) else {}
        if cfg:
            return cls(
                provider=cfg.get("provider", "callmebot"),
                phone=cfg.get("phone") or prof.get("phone_number", ""),
                api_key=cfg.get("api_key", ""),
                meta_phone_number_id=cfg.get("meta_phone_number_id", ""),
                meta_access_token=cfg.get("meta_access_token", ""),
                twilio_account_sid=cfg.get("twilio_account_sid", ""),
                twilio_auth_token=cfg.get("twilio_auth_token", ""),
                twilio_from=cfg.get("twilio_from", ""),
                webhook_url=cfg.get("webhook_url", ""),
                enabled=cfg.get("enabled", True),
                notify_jobs=cfg.get("notify_jobs", True),
                notify_shortlists=cfg.get("notify_shortlists", True),
                notify_offers=cfg.get("notify_offers", True),
            )
        # Fallback to host .env only for admin user if no per-user config set yet
        if user and user.get("is_admin"):
            return cls()
        return cls(enabled=False, phone="", api_key="")

    def is_configured(self) -> bool:
        if not self.enabled:
            return False
        if self.provider == "callmebot":
            return bool(self.phone and self.api_key)
        if self.provider == "meta":
            return bool(self.phone and self.meta_phone_number_id and self.meta_access_token)
        if self.provider == "twilio":
            return bool(self.phone and self.twilio_account_sid and self.twilio_auth_token and self.twilio_from)
        if self.provider == "webhook":
            return bool(self.webhook_url)
        return False

    def send_message(self, text: str, event_type: str = "general") -> Tuple[bool, str]:
        """Dispatches a formatted WhatsApp message via the configured provider."""
        if not self.is_configured():
            return False, "WhatsApp is not configured or is disabled."

        msg_text = (text or "").strip()
        if not msg_text:
            return False, "Empty message text."

        try:
            if self.provider == "callmebot":
                resp = requests.get(
                    "https://api.callmebot.com/whatsapp.php",
                    params={
                        "phone": self.phone,
                        "text": msg_text,
                        "apikey": self.api_key,
                    },
                    timeout=15,
                )
                body = (resp.text or "").strip()
                if resp.status_code == 200 and "error" not in body.lower():
                    logger.info(f"WhatsApp alert sent via CallMeBot ({event_type}).")
                    return True, "Sent via CallMeBot WhatsApp"
                logger.warning(f"CallMeBot WhatsApp error (HTTP {resp.status_code}): {body[:180]}")
                return False, f"CallMeBot rejected request (HTTP {resp.status_code}): {body[:160]}"

            if self.provider == "meta":
                url = f"https://graph.facebook.com/v21.0/{self.meta_phone_number_id}/messages"
                headers = {
                    "Authorization": f"Bearer {self.meta_access_token}",
                    "Content-Type": "application/json",
                }
                payload = {
                    "messaging_product": "whatsapp",
                    "to": self.phone,
                    "type": "text",
                    "text": {"preview_url": False, "body": msg_text},
                }
                resp = requests.post(url, headers=headers, json=payload, timeout=15)
                if resp.status_code in (200, 201):
                    logger.info(f"WhatsApp alert sent via Meta Cloud API ({event_type}).")
                    return True, "Sent via Meta WhatsApp Cloud API"
                return False, f"Meta API error (HTTP {resp.status_code}): {resp.text[:160]}"

            if self.provider == "twilio":
                url = f"https://api.twilio.com/2010-04-01/Accounts/{self.twilio_account_sid}/Messages.json"
                to_wa = self.phone if self.phone.startswith("whatsapp:") else f"whatsapp:+{self.phone}"
                from_wa = self.twilio_from if self.twilio_from.startswith("whatsapp:") else f"whatsapp:{self.twilio_from}"
                resp = requests.post(
                    url,
                    auth=(self.twilio_account_sid, self.twilio_auth_token),
                    data={"From": from_wa, "To": to_wa, "Body": msg_text},
                    timeout=15,
                )
                if resp.status_code in (200, 201):
                    logger.info(f"WhatsApp alert sent via Twilio ({event_type}).")
                    return True, "Sent via Twilio WhatsApp"
                return False, f"Twilio error (HTTP {resp.status_code}): {resp.text[:160]}"

            if self.provider == "webhook":
                resp = requests.post(
                    self.webhook_url,
                    json={"phone": self.phone, "text": msg_text, "event_type": event_type},
                    timeout=15,
                )
                if 200 <= resp.status_code < 300:
                    logger.info(f"WhatsApp alert dispatched via webhook ({event_type}).")
                    return True, "Sent via custom WhatsApp webhook"
                return False, f"Webhook error (HTTP {resp.status_code}): {resp.text[:160]}"

            return False, f"Unsupported WhatsApp provider: {self.provider}"
        except Exception as e:
            logger.error(f"WhatsApp send error ({self.provider}): {e}")
            return False, str(e)

    # ═══════════════════════════════════════════════════════════════════
    # FORMATTED WHATSAPP ALERT TEMPLATES
    # ═══════════════════════════════════════════════════════════════════

    def format_job_opening_message(self, job: Dict[str, Any], cal_url: str = "", auto_applied: bool = False) -> str:
        company = str(job.get("company_name") or "Campus Recruiter").strip()
        role = str(job.get("job_role") or "Job Opening").strip()
        ctc = str(job.get("ctc_display") or "As per Policy").strip()
        location = str(job.get("location") or "Pan India").strip()
        deadline = str(job.get("deadline_display") or "Check Portal").strip()
        hours_left = job.get("hours_left")
        profile_url = str(job.get("profile_url") or Config.SUPERSET_BASE_URL).strip()
        eligible = job.get("is_eligible", True)

        if auto_applied:
            status_line = "AUTO-APPLIED SUCCESSFULLY"
        elif eligible:
            status_line = "ELIGIBLE — ACTION REQUIRED"
        else:
            status_line = "NOT ELIGIBLE"

        lines = [
            "📢 *NEW SUPERSET JOB OPENING*",
            f"*Company:* {company}",
            f"*Role:* {role}",
            f"*CTC:* {ctc}",
            f"*Location:* {location}",
            f"*Deadline:* {deadline}" + (f" ({hours_left:.1f}h left)" if isinstance(hours_left, (int, float)) else ""),
            f"*Status:* {status_line}",
            "",
            f"Apply: {profile_url}",
        ]
        if cal_url:
            lines.append(f"Reminder: {cal_url}")
        return "\n".join(lines)

    def format_shortlist_message(
        self,
        notice: Dict[str, Any],
        match: Dict[str, Any],
        student_name: str = "",
        roll_number: str = "",
    ) -> str:
        title = str(notice.get("title") or "Campus Shortlist Notice").strip()
        ev = notice.get("event_details") or {}
        profile_lbl = str(ev.get("matched_profile") or "").strip()
        process_lbl = str(ev.get("process") or ev.get("event_type") or "Recruitment Round").strip()
        ev_date = str(ev.get("date") or "Check Notice").strip()
        date_tag = str(ev.get("date_status_label") or "").strip()
        ev_time = str(ev.get("time") or "Check Notice").strip()
        ev_venue = str(ev.get("venue") or "Check Notice / Virtual").strip()
        meet_link = str(ev.get("meeting_link") or "").strip()

        att_name = str(match.get("attachment_name") or "Notice").strip()
        page_num = match.get("page_number")
        page_str = f" (Page {page_num})" if page_num else ""
        matched_line = str(match.get("matched_line") or "").strip()[:180]
        who = f"{student_name} ({roll_number})".strip(" ()") or "Student"

        lines = [
            "🔥 *YOU ARE SHORTLISTED!*",
            f"*Student:* {who}",
            f"*Drive:* {title}",
        ]
        if profile_lbl:
            lines.append(f"*Shortlisted Profile:* {profile_lbl}")
        lines.extend([
            f"*Process:* {process_lbl}",
            f"*Date:* {ev_date}" + (f" ({date_tag})" if date_tag else ""),
            f"*Reporting Time:* {ev_time}",
            f"*Venue / Mode:* {ev_venue}",
        ])
        if meet_link:
            lines.append(f"*Meeting Link:* {meet_link}")
        lines.append(f"*Verified In:* {att_name}{page_str}")
        if matched_line:
            lines.append(f"*Row:* `{matched_line}`")
        lines.append(f"\nPortal: {Config.SUPERSET_BASE_URL}/students/")
        return "\n".join(lines)

    def format_pending_offer_message(self, action: Dict[str, Any], student_name: str = "") -> str:
        company = str(action.get("company_name") or "Campus Recruiter").strip()
        title = str(action.get("title") or "Job Offer").strip()
        deadline = str(action.get("deadline_display") or "Check Superset Immediately").strip()
        hours_left = action.get("hours_left")
        portal_url = str(action.get("portal_url") or f"{Config.SUPERSET_BASE_URL}/students/").strip()

        lines = [
            "🏆 *URGENT: PENDING JOB OFFER!*",
            f"Congratulations, {student_name or 'Candidate'}!",
            f"*Company:* {company}",
            f"*Offer / Role:* {title}",
            f"*Acceptance Deadline:* {deadline}" + (f" ({hours_left:.1f}h left)" if isinstance(hours_left, (int, float)) else ""),
            "",
            f"Respond immediately: {portal_url}",
        ]
        return "\n".join(lines)

    def send_job_alert(self, job: Dict[str, Any], cal_url: str = "", auto_applied: bool = False) -> bool:
        if not self.notify_jobs:
            return False
        ok, _ = self.send_message(self.format_job_opening_message(job, cal_url=cal_url, auto_applied=auto_applied), event_type="job_opening")
        return ok

    def send_shortlist_alert(self, notice: Dict[str, Any], match: Dict[str, Any], student_name: str = "", roll_number: str = "") -> bool:
        if not self.notify_shortlists:
            return False
        ok, _ = self.send_message(self.format_shortlist_message(notice, match, student_name=student_name, roll_number=roll_number), event_type="shortlist")
        return ok

    def send_pending_offer_alert(self, action: Dict[str, Any], student_name: str = "") -> bool:
        if not self.notify_offers:
            return False
        ok, _ = self.send_message(self.format_pending_offer_message(action, student_name=student_name), event_type="pending_offer")
        return ok

    def send_test_message(self, student_name: str = "") -> Tuple[bool, str]:
        who = student_name or "Student"
        msg = (
            f"✅ *Superset Job Pilot — WhatsApp Connected!*\n\n"
            f"Hi {who}, you will now receive instant WhatsApp alerts for:\n"
            f"• New eligible *Job Openings* (Role, CTC, Deadline, Apply link)\n"
            f"• Upcoming *Shortlist Matches* (Exact Venue, Date, Time, Page #)\n"
            f"• Urgent *Pending Job Offers*"
        )
        return self.send_message(msg, event_type="test")
