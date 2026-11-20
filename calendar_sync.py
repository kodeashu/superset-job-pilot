"""Google Calendar Link & iCalendar (.ics) generator with reminders."""
import urllib.parse
from datetime import datetime, timedelta
import pytz

IST = pytz.timezone("Asia/Kolkata")


class CalendarSync:
    @staticmethod
    def generate_google_calendar_url(title: str, deadline_dt: datetime, description: str, location: str = "Superset Portal") -> str:
        """
        Generates a direct 1-click Google Calendar event URL.
        Clicking this URL on Android, iPhone, or browser adds the deadline event instantly.
        """
        # Ensure deadline is in UTC for Google Calendar URL format
        if deadline_dt.tzinfo is None:
            deadline_dt = IST.localize(deadline_dt)
        
        utc_dt = deadline_dt.astimezone(pytz.utc)
        # Event duration: 30 minutes before deadline to deadline
        start_utc = utc_dt - timedelta(minutes=30)
        end_utc = utc_dt

        start_str = start_utc.strftime("%Y%m%dT%H%M%SZ")
        end_str = end_utc.strftime("%Y%m%dT%H%M%SZ")

        params = {
            "action": "TEMPLATE",
            "text": f"🚨 DEADLINE: {title}",
            "dates": f"{start_str}/{end_str}",
            "details": f"{description}\n\n⚠️ Added automatically by Superset Job Pilot",
            "location": location,
        }
        return f"https://calendar.google.com/calendar/render?{urllib.parse.urlencode(params)}"

    @staticmethod
    def generate_ics_content(uid: str, title: str, deadline_dt: datetime, description: str, url: str) -> str:
        """
        Generates a standard RFC 5545 iCalendar string (.ics) with 3 pre-configured reminders:
        - 24 hours before
        - 3 hours before
        - 1 hour before
        """
        if deadline_dt.tzinfo is None:
            deadline_dt = IST.localize(deadline_dt)
        utc_dt = deadline_dt.astimezone(pytz.utc)
        start_utc = utc_dt - timedelta(minutes=30)
        
        now_str = datetime.now(pytz.utc).strftime("%Y%m%dT%H%M%SZ")
        start_str = start_utc.strftime("%Y%m%dT%H%M%SZ")
        end_str = utc_dt.strftime("%Y%m%dT%H%M%SZ")

        escaped_desc = description.replace("\n", "\\n").replace(",", "\\,")

        ics = f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//SupersetJobPilot//CampusPlacementAlerts//EN
CALSCALE:GREGORIAN
METHOD:PUBLISH
BEGIN:VEVENT
UID:{uid}@supersetjobpilot.local
DTSTAMP:{now_str}
DTSTART:{start_str}
DTEND:{end_str}
SUMMARY:🚨 DEADLINE: {title}
DESCRIPTION:{escaped_desc}\\n\\nDirect Link: {url}
URL:{url}
STATUS:CONFIRMED
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:Superset Deadline in 24 Hours: {title}
TRIGGER:-P1D
END:VALARM
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:URGENT: Superset Deadline in 3 Hours: {title}
TRIGGER:-PT3H
END:VALARM
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:CRITICAL: Superset Deadline in 1 Hour: {title}
TRIGGER:-PT1H
END:VALARM
END:VEVENT
END:VCALENDAR"""
        return ics
