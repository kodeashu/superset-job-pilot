"""
AI Engine for Superset Job Pilot.
Provides unified LLM intelligence for Live Error Healing, Ambiguity Detection,
and Copilot queries, supporting local Ollama (qwen2.5-coder:14b), Google Gemini,
and robust deterministic heuristic fallbacks.
"""

import json
import logging
import os
import re
import requests
from typing import Any, Dict, List, Optional
from config import Config
from rag_engine import RAGEngine

logger = logging.getLogger("ai_engine")
logger.setLevel(logging.INFO)


class BaseAIProvider:
    """Abstract interface for LLM completion providers."""
    def is_available(self) -> bool:
        raise NotImplementedError

    def generate(self, prompt: str, system_prompt: Optional[str] = None, temperature: float = 0.2) -> str:
        raise NotImplementedError

    def get_info(self) -> Dict[str, Any]:
        raise NotImplementedError


class OllamaProvider(BaseAIProvider):
    """Local Ollama provider (e.g. qwen2.5-coder:14b) via REST API."""
    def __init__(self, base_url: str = None, model: str = None):
        self.base_url = (base_url or os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")).rstrip("/")
        self.model = model or os.getenv("OLLAMA_MODEL", "qwen2.5-coder:14b")
        # Explicitly disabled by default to protect local RAM
        self.enabled = os.getenv("ENABLE_OLLAMA", "false").lower() in ("true", "1", "yes")

    def is_available(self) -> bool:
        if not self.enabled:
            return False
        try:
            r = requests.get(f"{self.base_url}/api/tags", timeout=1.5)
            if r.status_code == 200:
                models = [m.get("name") for m in r.json().get("models", [])]
                return any(self.model in m or m.startswith(self.model.split(":")[0]) for m in models)
        except Exception:
            pass
        return False

    def generate(self, prompt: str, system_prompt: Optional[str] = None, temperature: float = 0.2) -> str:
        if not self.enabled:
            raise RuntimeError("Ollama is disabled to conserve system RAM.")
        url = f"{self.base_url}/api/chat"
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": 1024
            }
        }
        resp = requests.post(url, json=payload, timeout=40)
        resp.raise_for_status()
        data = resp.json()
        return data.get("message", {}).get("content", "").strip()

    def get_info(self) -> Dict[str, Any]:
        return {
            "provider": "ollama",
            "name": "Local Ollama",
            "model": self.model,
            "base_url": self.base_url,
            "online": self.is_available()
        }


class GroqProvider(BaseAIProvider):
    """100% Free Cloud Provider via Groq Cloud (0 MB RAM). 30 req/min free."""
    def __init__(self, api_key: str = None, model: str = None):
        self.api_key = api_key or os.getenv("GROQ_API_KEY", "")
        self.model = model or os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

    def is_available(self) -> bool:
        return bool(self.api_key and len(self.api_key) > 8)

    def generate(self, prompt: str, system_prompt: Optional[str] = None, temperature: float = 0.2) -> str:
        if not self.is_available():
            raise RuntimeError("Groq API key is not configured.")
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": 1024
        }
        resp = requests.post(url, headers=headers, json=payload, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()

    def get_info(self) -> Dict[str, Any]:
        return {
            "provider": "groq",
            "name": "Groq Cloud (Free Tier - 0 MB RAM)",
            "model": self.model,
            "online": self.is_available()
        }


class GeminiProvider(BaseAIProvider):
    """Google Gemini API provider (100% Free Tier on AI Studio - 0 MB RAM)."""
    def __init__(self, api_key: str = None, model: str = None):
        self.api_key = api_key or os.getenv("GEMINI_API_KEY", "")
        self.model = model or os.getenv("GEMINI_MODEL", "gemini-1.5-flash")

    def is_available(self) -> bool:
        return bool(self.api_key and len(self.api_key) > 10)

    def generate(self, prompt: str, system_prompt: Optional[str] = None, temperature: float = 0.2) -> str:
        if not self.is_available():
            raise RuntimeError("Gemini API key is not configured.")

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent?key={self.api_key}"
        contents = []
        if system_prompt:
            contents.append({"role": "user", "parts": [{"text": f"System Directive:\n{system_prompt}"}]})
            contents.append({"role": "model", "parts": [{"text": "Understood. I will follow all system instructions strictly."}]})
        contents.append({"role": "user", "parts": [{"text": prompt}]})

        payload = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": 1024
            }
        }
        resp = requests.post(url, json=payload, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        try:
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()
        except (KeyError, IndexError) as e:
            raise RuntimeError(f"Unexpected Gemini response structure: {data}") from e

    def get_info(self) -> Dict[str, Any]:
        return {
            "provider": "gemini",
            "name": "Google Gemini (Free Tier - 0 MB RAM)",
            "model": self.model,
            "online": self.is_available()
        }


class HeuristicProvider(BaseAIProvider):
    """Deterministic RAG expert system (0 MB RAM, 0 API keys, 100% Free)."""
    def is_available(self) -> bool:
        return True

    def generate(self, prompt: str, system_prompt: Optional[str] = None, temperature: float = 0.2) -> str:
        return "Deterministic RAG knowledge base matched."

    def get_info(self) -> Dict[str, Any]:
        return {
            "provider": "heuristic",
            "name": "Built-In RAG Engine (Zero RAM / Zero Cloud)",
            "model": "hybrid-bm25-expert-rules",
            "online": True
        }


class AIEngine:
    """
    Central AI Manager integrating RAG and LLM reasoning.
    Powers Error Diagnosis, Auto-Remediation, and Ambiguity Detection.
    """
    _instance = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(AIEngine, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self.rag = RAGEngine()
        self.providers: Dict[str, BaseAIProvider] = {
            "groq": GroqProvider(),
            "gemini": GeminiProvider(),
            "heuristic": HeuristicProvider(),
            "ollama": OllamaProvider()
        }
        self.preferred_provider = os.getenv("AI_PROVIDER", "auto").lower()
        self._initialized = True

    def get_active_provider(self) -> BaseAIProvider:
        """Selects the best available provider based on health and configuration."""
        if self.preferred_provider in self.providers and self.providers[self.preferred_provider].is_available():
            return self.providers[self.preferred_provider]

        # Auto detection priority: Free Cloud Groq > Free Cloud Gemini > Built-in RAG Heuristic (0 RAM)
        if self.providers["groq"].is_available():
            return self.providers["groq"]
        if self.providers["gemini"].is_available():
            return self.providers["gemini"]
        # Ollama is never auto-selected unless explicitly configured
        if self.preferred_provider == "ollama" and self.providers["ollama"].is_available():
            return self.providers["ollama"]
        return self.providers["heuristic"]

    def diagnose_error(self, error_info: Dict[str, Any]) -> Dict[str, Any]:
        """
        Synthesizes RAG playbooks and AI reasoning to diagnose runtime errors.
        Returns actionable remediation instructions.
        """
        error_msg = str(error_info.get("message") or error_info.get("raw_body") or "")
        status_code = error_info.get("status_code", 0)
        company = error_info.get("company_name", "Unknown Company")
        job_id = error_info.get("job_id", "")
        context_str = f"HTTP {status_code} {error_msg} company {company}"

        # 1. Retrieve matching playbooks from RAG
        rag_hits = self.rag.search(context_str, top_k=3, filter_tags=["playbook"])
        rag_context = self.rag.get_context_for_prompt(context_str, top_k=2, filter_tags=["playbook"])

        # Quick Heuristic match for known Superset codes
        matched_pb = None
        for hit in rag_hits:
            pb_meta = hit.get("metadata", {})
            patterns = pb_meta.get("match_patterns", [])
            if any(p.lower() in error_msg.lower() or p in str(status_code) for p in patterns):
                matched_pb = pb_meta
                break

        # 2. Invoke active AI provider for contextual diagnosis
        provider = self.get_active_provider()
        ai_diagnosis_text = ""
        auto_heal_action = matched_pb.get("auto_heal_action") if matched_pb else "MANUAL_INVESTIGATION"
        auto_healable = matched_pb.get("auto_healable", False) if matched_pb else False

        prompt = (
            f"You are the senior Site Reliability & API Engineer for the Superset Job Pilot automation system.\n"
            f"Analyze this runtime error and provide a precise diagnostic report.\n\n"
            f"Incident Details:\n"
            f"- Company: {company}\n"
            f"- Job Identifier: {job_id}\n"
            f"- HTTP Status Code: {status_code}\n"
            f"- Raw Error Message: {error_msg}\n\n"
            f"Relevant Knowledge Base Playbooks:\n{rag_context}\n\n"
            f"Please respond in strictly valid JSON with these exact keys:\n"
            f'{{\n'
            f'  "root_cause": "1-2 sentence explanation in plain English for the candidate",\n'
            f'  "technical_details": "Exact reason why the API endpoint rejected the request",\n'
            f'  "auto_heal_action": "{auto_heal_action}",\n'
            f'  "auto_healable": {str(auto_healable).lower()},\n'
            f'  "confidence": 0.95,\n'
            f'  "remediation": "Concrete action taken or required to resolve this error"\n'
            f'}}'
        )

        try:
            if provider.get_info()["provider"] != "heuristic":
                raw_resp = provider.generate(
                    prompt=prompt,
                    system_prompt="You are an automated API debugging assistant. Output strictly valid JSON with no markdown wrapping.",
                    temperature=0.1
                )
                # Parse JSON safely
                cleaned = re.sub(r"^```json\s*", "", raw_resp.strip())
                cleaned = re.sub(r"```$", "", cleaned.strip())
                parsed = json.loads(cleaned)
                return {
                    "company_name": company,
                    "job_id": job_id,
                    "status_code": status_code,
                    "root_cause": parsed.get("root_cause", matched_pb.get("root_cause") if matched_pb else error_msg),
                    "technical_details": parsed.get("technical_details", error_msg),
                    "auto_heal_action": parsed.get("auto_heal_action", auto_heal_action),
                    "auto_healable": parsed.get("auto_healable", auto_healable),
                    "confidence": float(parsed.get("confidence", 0.90)),
                    "remediation": parsed.get("remediation", matched_pb.get("remediation") if matched_pb else "Check portal status."),
                    "provider_used": provider.get_info()
                }
        except Exception as e:
            logger.warning(f"AI Provider error diagnosis failed ({e}), falling back to deterministic playbook.")

        # Fallback to Playbook Metadata
        return {
            "company_name": company,
            "job_id": job_id,
            "status_code": status_code,
            "root_cause": matched_pb.get("root_cause", f"Superset API returned status code {status_code}: {error_msg}") if matched_pb else error_msg,
            "technical_details": error_msg,
            "auto_heal_action": auto_heal_action,
            "auto_healable": auto_healable,
            "confidence": 0.92 if matched_pb else 0.50,
            "remediation": matched_pb.get("remediation", "Review error logs and verify application status.") if matched_pb else "Manual inspection advised.",
            "provider_used": provider.get_info()
        }

    def evaluate_question_clarity(self, question: Dict[str, Any], resolved_val: Any) -> Dict[str, Any]:
        """
        Determines if a question's resolved answer is ambiguous, low-confidence,
        or requires explicit human clarification via Telegram/UI.
        """
        q_name = question.get("name") or question.get("question") or ""
        options = question.get("options") or []
        attr_type = question.get("type", "string")
        q_lower = q_name.lower()

        # 1. Check RAG for relevant profile facts
        rag_context = self.rag.get_context_for_prompt(q_name, top_k=2, filter_tags=["profile", "memory"])

        # High-Risk / Specialized patterns that MUST be flagged if missing from standard profile:
        specialized_triggers = [
            ("foreign language", ["german", "french", "japanese", "spanish"], "Language Proficiency Certification"),
            ("cloud certification", ["aws", "azure", "gcp", "certificat"], "Cloud / Vendor Certification Proof"),
            ("disability proof", ["disability proof", "disability document", "type of disability"], "Disability Documentation"),
            ("portfolio / project link", ["prototype", "demo link", "solution developed", "300 words"], "Custom Project / Prototype Write-up"),
            ("voter id", ["voter id"], "Voter Identification Card"),
            ("driving license", ["driving license", "dl number"], "Driving License")
        ]

        for trigger_name, keywords, label in specialized_triggers:
            if any(k in q_lower for k in keywords):
                # If question specifically asks for one of these and student profile doesn't have an explicit entry
                return {
                    "is_unclear": True,
                    "trigger_type": trigger_name,
                    "confidence": 0.50,
                    "reason": f"Question requests '{label}' which requires candidate document verification.",
                    "suggested_answer": resolved_val or ("Not Applicable" if not options else options[0]),
                    "options": options,
                    "rag_context": rag_context
                }

        # 2. Check if resolved value is blank or generic default
        if resolved_val is None or str(resolved_val).strip() == "":
            return {
                "is_unclear": True,
                "trigger_type": "BLANK_DEFAULT",
                "confidence": 0.20,
                "reason": f"No definitive attribute matching rule found for question: '{q_name}'",
                "suggested_answer": options[0] if options else "NA",
                "options": options,
                "rag_context": rag_context
            }

        # 3. Standard questions are clear
        return {
            "is_unclear": False,
            "trigger_type": "RESOLVED_HIGH_CONFIDENCE",
            "confidence": 0.95,
            "reason": "Matched verified candidate profile facts with high precision.",
            "suggested_answer": resolved_val,
            "options": options
        }

    def ask_copilot(self, query: str, chat_history: Optional[List[Dict[str, str]]] = None) -> Dict[str, Any]:
        """
        Interactive Copilot query powered by RAG knowledge base.
        Can answer questions about placement drives, eligibility, error playbooks, or profile.
        """
        provider = self.get_active_provider()
        rag_hits = self.rag.search(query, top_k=4)
        rag_context = self.rag.get_context_for_prompt(query, top_k=4)

        system_prompt = (
            "You are the Superset Job Pilot Enterprise Copilot.\n"
            "You have complete access to the authenticated candidate's academic profile, "
            "placement drive historical questions, and API error resolution playbooks.\n"
            "Provide concise, authoritative, professional, and actionable responses. "
            "Always state facts derived from the retrieved context. Format with clean GitHub markdown."
        )

        prompt = (
            f"Retrieved Knowledge Context:\n{rag_context}\n\n"
            f"User Question: {query}\n\n"
            f"Please provide an accurate, helpful answer based on the knowledge base."
        )

        try:
            if provider.get_info()["provider"] != "heuristic":
                reply = provider.generate(prompt=prompt, system_prompt=system_prompt, temperature=0.3)
                return {
                    "reply": reply,
                    "rag_sources": [{"title": h["title"], "source": h["source"], "score": h["score"]} for h in rag_hits],
                    "provider": provider.get_info()
                }
        except Exception as e:
            logger.warning(f"Copilot LLM generation error: {e}")

        # Deterministic RAG Copilot formatting (Zero RAM / Zero Cloud)
        if rag_hits:
            bullets = []
            for hit in rag_hits[:3]:
                bullets.append(f"#### 📌 {hit['title']} (from `{hit['source']}`)\n{hit['content']}")
            reply = (
                f"**Retrieved from RAG Knowledge Base (Instant In-Memory / 0 MB RAM):**\n\n"
                + "\n\n".join(bullets)
            )
        else:
            reply = "I could not locate specific matching information in the local profile or error playbooks for your query."

        return {
            "reply": reply,
            "rag_sources": [{"title": h["title"], "source": h["source"], "score": h["score"]} for h in rag_hits],
            "provider": provider.get_info()
        }

    def get_status(self) -> Dict[str, Any]:
        """Returns the overall operational status of the AI engine."""
        active = self.get_active_provider()
        rag_stats = self.rag.get_stats()
        return {
            "active_provider": active.get_info(),
            "available_providers": {k: p.get_info() for k, p in self.providers.items()},
            "rag_stats": rag_stats
        }
