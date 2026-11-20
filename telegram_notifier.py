"""Telegram Notifier with inline buttons and rich job cards."""
import html
import logging
import requests
from config import Config

logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self, bot_token: str = None, chat_id: str = None):
        self.bot_token = bot_token or Config.TELEGRAM_BOT_TOKEN
        self.chat_id = chat_id or Config.TELEGRAM_CHAT_ID
        self.api_url = f"https://api.telegram.org/bot{self.bot_token}"

    def send_message(self, text: str, reply_markup: dict = None) -> bool:
        """Sends a message via Telegram Bot API with optional inline keyboard buttons."""
        if not self.bot_token or not self.chat_id:
            logger.warning("Telegram credentials not configured. Skipping alert.")
            return False

        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup

        try:
            resp = requests.post(f"{self.api_url}/sendMessage", json=payload, timeout=10)
            if resp.status_code == 200:
                logger.info("Telegram notification sent successfully.")
                return True
            else:
                logger.error(f"Failed to send Telegram message: {resp.text}")
                return False
        except Exception as e:
            logger.error(f"Error sending Telegram notification: {e}")
            return False

    def send_job_alert(self, job: dict, cal_url: str, auto_applied: bool = False) -> bool:
        """Formats and sends an urgent job card with calendar & apply buttons."""
        company = job.get("company_name", "Unknown Company")
        role = job.get("job_role", "Job Opening")
        ctc = job.get("ctc_display", "Not Disclosed")
        location = job.get("location", "Pan India / Flexible")
        deadline = job.get("deadline_display", "Check Portal")
        hours_left = job.get("hours_left", None)
        profile_url = job.get("profile_url", Config.SUPERSET_BASE_URL)
        eligible = job.get("is_eligible", True)

        # Status badge
        if auto_applied:
            status_text = "🟢 <b>AUTO-APPLIED SUCCESSFULLY</b>\n<i>Applied using your default profile & resume.</i>"
        elif eligible:
            status_text = "🟡 <b>ELIGIBLE - ACTION REQUIRED</b>\n<i>Please apply before deadline!</i>"
        else:
            status_text = "⚪ <b>NOT ELIGIBLE</b>\n<i>Criteria did not match profile.</i>"

        urgency_emoji = "🔥" if (hours_left is not None and hours_left <= 12) else "📢"
        countdown_text = f"⏳ <b>Time Left:</b> {hours_left:.1f} hours remaining!" if hours_left is not None else ""

        message = f"""
{urgency_emoji} <b>SUPERSET CAMPUS DRIVE ALERT</b> {urgency_emoji}
━━━━━━━━━━━━━━━━━━━
🏢 <b>Company:</b> {company}
💼 <b>Role:</b> {role}
💰 <b>CTC / Package:</b> {ctc}
📍 <b>Location:</b> {location}
⏰ <b>Deadline:</b> {deadline}
{countdown_text}

{status_text}
━━━━━━━━━━━━━━━━━━━
"""
        # Inline buttons
        buttons = []
        row1 = []
        if profile_url:
            row1.append({"text": "🚀 Open Superset", "url": profile_url})
        if cal_url:
            row1.append({"text": "📅 Add to Google Calendar", "url": cal_url})
        
        buttons.append(row1)

        reply_markup = {"inline_keyboard": buttons}
        return self.send_message(message.strip(), reply_markup)

    def send_shortlist_alert(self, notice: dict, match: dict, student_name: str = "", roll_number: str = "") -> bool:
        """Sends an urgent 🔥 YOU ARE SHORTLISTED! alert with extracted venue, date & time."""
        title = html.escape(str(notice.get("title") or "Campus Placement Shortlist"))
        ev = notice.get("event_details") or {}
        event_type = html.escape(str(ev.get("event_type") or "Recruitment Round"))
        ev_date = html.escape(str(ev.get("date") or "Check Notice"))
        ev_time = html.escape(str(ev.get("time") or "Check Notice"))
        ev_venue = html.escape(str(ev.get("venue") or "Check Notice / Virtual"))
        meet_link = ev.get("meeting_link") or ""

        matched_by = html.escape(str(match.get("match_type") or "ROLL"))
        att_name = html.escape(str(match.get("attachment_name") or "Notice Body"))
        page_num = match.get("page_number")
        page_str = f" (Page {page_num})" if page_num else ""
        matched_line = html.escape(str(match.get("matched_line") or "")[:220])
        who = html.escape(f"{student_name} ({roll_number})".strip(" ()") or "Candidate")

        link_line = f"\n🔗 <b>Meeting / Test Link:</b> {html.escape(meet_link)}" if meet_link else ""
        row_line = f"\n📄 <b>Matched Entry:</b> <code>{matched_line}</code>" if matched_line else ""

        message = f"""
🔥 <b>YOU ARE SHORTLISTED!</b> 🔥
━━━━━━━━━━━━━━━━━━━
🎓 <b>Student:</b> {who}
📢 <b>Notice:</b> {title}
🎯 <b>Stage:</b> {event_type}
📅 <b>Date:</b> {ev_date}
🕒 <b>Time / Slot:</b> {ev_time}
📍 <b>Venue / Mode:</b> {ev_venue}{link_line}

🔎 <b>Verified In:</b> {att_name}{page_str} [{matched_by}]{row_line}
━━━━━━━━━━━━━━━━━━━
<i>Open Superset Job Pilot → Notices to view the full PDF in 1 click.</i>
"""
        buttons = [[{"text": "🚀 Open Superset Portal", "url": Config.SUPERSET_BASE_URL + "/students/"}]]
        if meet_link and meet_link.startswith(("http://", "https://")):
            buttons[0].append({"text": "🎥 Join Meeting", "url": meet_link})
        return self.send_message(message.strip(), {"inline_keyboard": buttons})

    def send_pending_offer_alert(self, action: dict, student_name: str = "") -> bool:
        """Sends an urgent 🏆 PENDING JOB OFFER alert so an acceptance deadline is never missed."""
        company = html.escape(str(action.get("company_name") or "Campus Recruiter"))
        title = html.escape(str(action.get("title") or "Job Offer"))
        deadline = html.escape(str(action.get("deadline_display") or "Check Superset Immediately"))
        hours_left = action.get("hours_left")
        countdown = f"\n⏳ <b>Time Remaining:</b> {hours_left:.1f} hours!" if hours_left is not None else ""
        portal_url = action.get("portal_url") or (Config.SUPERSET_BASE_URL + "/students/")
        who = html.escape(student_name or "Candidate")

        message = f"""
🏆 <b>URGENT: PENDING JOB OFFER!</b> 🏆
━━━━━━━━━━━━━━━━━━━
🎉 <b>Congratulations, {who}!</b>
🏢 <b>Company:</b> {company}
💼 <b>Offer / Role:</b> {title}
⏰ <b>Acceptance Deadline:</b> {deadline}{countdown}

⚠️ <b>Action Required:</b> Log into Superset and respond before the deadline expires!
━━━━━━━━━━━━━━━━━━━
"""
        reply_markup = {"inline_keyboard": [[{"text": "🏆 View & Respond on Superset", "url": portal_url}]]}
        return self.send_message(message.strip(), reply_markup)

    def test_connection(self) -> dict:
        """Verifies bot token validity and fetches bot info."""
        try:
            resp = requests.get(f"{self.api_url}/getMe", timeout=10)
            return resp.json()
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def detect_chat_id(self) -> str:
        """Polls getUpdates to automatically capture user chat ID."""
        try:
            resp = requests.get(f"{self.api_url}/getUpdates", timeout=10).json()
            if resp.get("ok") and resp.get("result"):
                for update in reversed(resp["result"]):
                    msg = update.get("message") or update.get("channel_post") or update.get("my_chat_member")
                    if msg and "chat" in msg:
                        return str(msg["chat"]["id"])
                    if msg and "from" in msg:
                        return str(msg["from"]["id"])
        except Exception as e:
            logger.error(f"Error checking Telegram updates: {e}")
        return ""

