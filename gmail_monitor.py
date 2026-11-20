"""Gmail IMAP Monitor for Superset notifications and campus placement alerts."""
import email
from email.header import decode_header
import imaplib
import logging
import re
from datetime import datetime
from bs4 import BeautifulSoup
from config import Config

logger = logging.getLogger(__name__)


class GmailMonitor:
    def __init__(self, user: str = None, password: str = None):
        self.user = user or Config.GMAIL_USER
        self.password = password or Config.GMAIL_APP_PASSWORD
        self.imap_server = "imap.gmail.com"

    def fetch_unprocessed_emails(self, max_count: int = 10) -> list:
        """
        Connects to Gmail via IMAP SSL and searches for recent Superset emails.
        Returns a list of parsed email dictionaries.
        """
        if not self.user or not self.password:
            logger.info("Gmail credentials not provided. Skipping IMAP fetch.")
            return []

        try:
            mail = imaplib.IMAP4_SSL(self.imap_server)
            mail.login(self.user, self.password)
            mail.select("INBOX")

            # Search queries: from Superset or mentions of placement/job profile
            search_query = '(OR (FROM "joinsuperset.com") (SUBJECT "Superset"))'
            status, messages = mail.search(None, search_query)

            if status != "OK":
                logger.warning("No matching emails found.")
                mail.logout()
                return []

            email_ids = messages[0].split()
            # Fetch latest N emails
            recent_ids = email_ids[-max_count:]
            parsed_emails = []

            for e_id in reversed(recent_ids):
                status, data = mail.fetch(e_id, "(RFC822)")
                if status != "OK":
                    continue

                raw_email = data[0][1]
                msg = email.message_from_bytes(raw_email)
                
                subject, encoding = decode_header(msg.get("Subject", ""))[0]
                if isinstance(subject, bytes):
                    subject = subject.decode(encoding or "utf-8", errors="ignore")

                sender = msg.get("From", "")
                date_str = msg.get("Date", "")
                
                body_text = self._extract_body(msg)
                links = self._extract_links(body_text)

                parsed_emails.append({
                    "email_id": e_id.decode(),
                    "subject": subject,
                    "sender": sender,
                    "date_str": date_str,
                    "body": body_text,
                    "links": links,
                    "superset_links": [l for l in links if "joinsuperset.com" in l],
                })

            mail.logout()
            return parsed_emails

        except Exception as e:
            logger.error(f"Gmail IMAP connection error: {e}")
            return []

    def _extract_body(self, msg) -> str:
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                content_disposition = str(part.get("Content-Disposition"))
                if content_type in ("text/plain", "text/html") and "attachment" not in content_disposition:
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        text = payload.decode(charset, errors="ignore")
                        if content_type == "text/html":
                            soup = BeautifulSoup(text, "html.parser")
                            body += " " + soup.get_text(separator=" ")
                        else:
                            body += " " + text
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                body = payload.decode(msg.get_content_charset() or "utf-8", errors="ignore")
        return body

    def _extract_links(self, text: str) -> list:
        url_pattern = r'https?://[^\s<>"]+|www\.[^\s<>"]+'
        return list(set(re.findall(url_pattern, text)))
