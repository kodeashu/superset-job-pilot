"""
Ambiguity Manager for Superset Job Pilot.
Monitors drive questionnaires for unclear, missing, or low-confidence data,
dispatches Telegram notifications to the user for clarification, and captures
answers to update candidate profile facts and RAG memory.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from ai_engine import AIEngine
from telegram_notifier import TelegramNotifier
from rag_engine import RAGEngine

logger = logging.getLogger("ambiguity_manager")
logger.setLevel(logging.INFO)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
TICKETS_PATH = DATA_DIR / "pending_clarifications.json"
PROFILE_PATH = DATA_DIR / "student_profile.json"


class AmbiguityManager:
    """
    Detects ambiguous questionnaire fields, issues clarification alerts via Telegram,
    and reconciles candidate answers into persistent RAG memory.
    """
    _instance = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(AmbiguityManager, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self.ai = AIEngine()
        self.rag = RAGEngine()
        self.notifier = TelegramNotifier()
        self.tickets: List[Dict[str, Any]] = []
        self._load_tickets()
        self._initialized = True

    def _load_tickets(self):
        if TICKETS_PATH.exists():
            try:
                with open(TICKETS_PATH, "r", encoding="utf-8") as f:
                    self.tickets = json.load(f)
            except Exception:
                self.tickets = []

    def _save_tickets(self):
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with open(TICKETS_PATH, "w", encoding="utf-8") as f:
                json.dump(self.tickets, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save clarification tickets: {e}")

    def evaluate_and_flag(self, 
                           company_name: str, 
                           job_id: str, 
                           question: Dict[str, Any], 
                           resolved_value: Any,
                           user_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Evaluates question clarity. If ambiguous or low-confidence, creates a ticket
        and dispatches a Telegram notification.
        """
        assessment = self.ai.evaluate_question_clarity(question, resolved_value)
        if not assessment.get("is_unclear", False):
            return None

        q_text = question.get("name") or question.get("question") or ""
        options = question.get("options") or []
        target_uid = user_id or "default"
        ticket_key = f"{target_uid}_{company_name}_{q_text}"

        # Deduplication check: Do not re-notify if pending ticket already exists
        existing = next((t for t in self.tickets if t.get("ticket_key") == ticket_key), None)
        if existing:
            if existing.get("status") == "RESOLVED":
                # If already resolved by user, use user's verified answer
                return existing
            return existing

        now_str = datetime.now(timezone.utc).astimezone().strftime("%d %b %Y, %I:%M %p %Z")
        ticket_id = f"clarif_{int(datetime.now().timestamp())}_{abs(hash(q_text)) % 1000}"

        ticket = {
            "ticket_id": ticket_id,
            "ticket_key": ticket_key,
            "user_id": target_uid,
            "created_at": now_str,
            "company_name": company_name,
            "job_id": job_id,
            "question_text": q_text,
            "options": options,
            "suggested_answer": assessment.get("suggested_answer", "NA"),
            "confidence": assessment.get("confidence", 0.5),
            "reason": assessment.get("reason", "Information unclear or missing from verified profile."),
            "trigger_type": assessment.get("trigger_type", "UNCLEAR_DATA"),
            "status": "PENDING",
            "resolved_answer": None
        }

        self.tickets.append(ticket)
        self._save_tickets()

        # Send Telegram Alert
        self._dispatch_clarification_alert(ticket)
        return ticket

    def _dispatch_clarification_alert(self, ticket: Dict[str, Any]):
        """Formats and sends an alert to the user's Telegram."""
        opts_str = ", ".join(f"'{o}'" for o in ticket["options"][:5]) if ticket["options"] else "Free text"
        msg = (
            f"⚠️ <b>Action Required: Unclear Data Detected</b>\n\n"
            f"🏢 <b>Drive:</b> {ticket['company_name']}\n"
            f"❓ <b>Question:</b> \"{ticket['question_text']}\"\n"
            f"📋 <b>Options:</b> [{opts_str}]\n"
            f"🤖 <b>AI Best Guess:</b> <code>{ticket['suggested_answer']}</code> (Confidence: {int(ticket['confidence']*100)}%)\n"
            f"💡 <b>Reason:</b> {ticket['reason']}\n\n"
            f"👉 <i>Open Mission Control (http://localhost:8080) to confirm or specify your exact answer.</i>"
        )
        try:
            self.notifier.send_message(msg)
            logger.info(f"Dispatched Telegram clarification alert for {ticket['company_name']}")
        except Exception as e:
            logger.error(f"Failed to send clarification alert: {e}")

    def resolve_ticket(self, ticket_id: str, answer: str, user_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Records the user's clarification, updates profile facts in the encrypted vault,
        and adds it to the RAG dynamic memory so it is remembered forever.
        """
        ticket = next((t for t in self.tickets if t.get("ticket_id") == ticket_id), None)
        if not ticket:
            raise ValueError(f"Clarification ticket '{ticket_id}' not found.")

        ticket["status"] = "RESOLVED"
        ticket["resolved_answer"] = answer
        ticket["resolved_at"] = datetime.now(timezone.utc).astimezone().strftime("%d %b %Y, %I:%M %p %Z")
        self._save_tickets()

        # 1. Update RAG Dynamic Memory
        self.rag.add_memory(
            key=ticket["question_text"][:50],
            question=ticket["question_text"],
            resolution=answer,
            context=f"Company: {ticket['company_name']}",
            tags=["user_clarified", "profile_extension"]
        )

        # 2. Persist into encrypted profile in UserManager vault
        try:
            from user_manager import UserManager
            um = UserManager()
            target_uid = user_id or ticket.get("user_id")
            if not target_uid or target_uid == "default":
                users = um.get_all_users()
                if users:
                    target_uid = users[0]["user_id"]
            if target_uid:
                user_obj = um.get_user_by_id(target_uid)
                if user_obj:
                    prof = user_obj.get("profile", {})
                    clarifs = prof.get("custom_clarifications", {})
                    clarifs[ticket["question_text"]] = answer
                    um.update_user_profile(target_uid, {"custom_clarifications": clarifs})
                    logger.info(f"Persisted clarification to encrypted vault for user {target_uid}")
        except Exception as e:
            logger.error(f"Error persisting clarification to encrypted vault: {e}")

        # 3. Also persist into student_profile.json if legacy file exists
        try:
            if PROFILE_PATH.exists():
                with open(PROFILE_PATH, "r", encoding="utf-8") as f:
                    p = json.load(f)
                if "custom_clarifications" not in p:
                    p["custom_clarifications"] = {}
                p["custom_clarifications"][ticket["question_text"]] = answer
                with open(PROFILE_PATH, "w", encoding="utf-8") as f:
                    json.dump(p, f, indent=2)
        except Exception as e:
            logger.error(f"Error persisting clarification to student_profile: {e}")

        # 4. Send confirmation Telegram message
        try:
            self.notifier.send_message(
                f"✅ <b>Clarification Saved: {ticket['company_name']}</b>\n"
                f"Question: \"{ticket['question_text'][:80]}\"\n"
                f"Your Answer: <code>{answer}</code>\n"
                f"<i>Knowledge base and RAG index updated. Future drives will use this answer automatically.</i>"
            )
        except Exception:
            pass

        return ticket

    def get_pending_tickets(self, user_id: Optional[str] = None) -> List[Dict[str, Any]]:
        self._load_tickets()
        if user_id:
            return [t for t in self.tickets if t.get("status") == "PENDING" and (t.get("user_id") in (user_id, "default"))]
        return [t for t in self.tickets if t.get("status") == "PENDING"]

    def get_all_tickets(self, limit: int = 50, user_id: Optional[str] = None) -> List[Dict[str, Any]]:
        self._load_tickets()
        filtered = self.tickets
        if user_id:
            filtered = [t for t in self.tickets if t.get("user_id") in (user_id, "default")]
        return list(reversed(filtered[-limit:]))
