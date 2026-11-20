"""Configuration loader for Superset Job Pilot."""
import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env file
BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
load_dotenv(ENV_PATH)


class Config:
    # Superset Credentials
    SUPERSET_EMAIL = os.getenv("SUPERSET_EMAIL", "")
    SUPERSET_PASSWORD = os.getenv("SUPERSET_PASSWORD", "")
    SUPERSET_TOKEN = os.getenv("SUPERSET_TOKEN", "")  # Optional auth bearer/cookie
    SUPERSET_BASE_URL = os.getenv("SUPERSET_BASE_URL", "https://app.joinsuperset.com")
    
    # Auto-Apply Settings
    AUTO_APPLY_ELIGIBLE = os.getenv("AUTO_APPLY_ELIGIBLE", "true").lower() in ("true", "1", "yes")
    DEFAULT_RESUME_NAME = os.getenv("DEFAULT_RESUME_NAME", "Default_Resume.pdf")
    MIN_CTC_LPA = float(os.getenv("MIN_CTC_LPA", "0.0"))
    
    # Telegram Notifications
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
    
    # Gmail Monitoring (Optional IMAP sync)
    GMAIL_USER = os.getenv("GMAIL_USER", "")
    GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
    ENABLE_GMAIL_MONITOR = os.getenv("ENABLE_GMAIL_MONITOR", "false").lower() in ("true", "1", "yes")
    
    # Storage & Database
    DB_PATH = BASE_DIR / "data" / "superset_jobs.sqlite"
    
    # Execution intervals
    POLL_INTERVAL_MINUTES = int(os.getenv("POLL_INTERVAL_MINUTES", "15"))

    @classmethod
    def validate(cls):
        missing = []
        if not cls.SUPERSET_EMAIL and not cls.SUPERSET_TOKEN:
            missing.append("SUPERSET_EMAIL or SUPERSET_TOKEN")
        if not cls.TELEGRAM_BOT_TOKEN:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not cls.TELEGRAM_CHAT_ID:
            missing.append("TELEGRAM_CHAT_ID")
        return missing
