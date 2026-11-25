"""
Security controls for Superset Job Pilot:
  - Owner authorization checks
  - Thread-safe sliding-window rate limiter
  - Sec-Fetch-Site / Origin / X-Requested-With CSRF & isolation guards
  - Response security headers (CSP, nosniff, X-Frame-Options, COOP, HSTS, Permissions-Policy)
  - Strict profile/ID/photo sanitization & safe error envelope helper
"""
import logging
import re
import threading
import time
import uuid
from collections import defaultdict, deque
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

from flask import jsonify

from config import Config
from user_manager import DEFAULT_PROFILE_TEMPLATE

logger = logging.getLogger("superset_security")

SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
INVITE_CODE_RE = re.compile(r"^[A-Z0-9-]{4,32}$")
PHOTO_DATA_URL_RE = re.compile(
    r"^data:image/(png|jpeg|jpg|webp);base64,[A-Za-z0-9+/=\s]+$",
    re.IGNORECASE,
)

# All 61 fields rendered by PROFILE_SECTIONS_DEF plus DEFAULT_PROFILE_TEMPLATE keys and aliases
PROFILE_UI_KEYS = {
    "full_name", "first_name", "middle_name", "last_name", "dob", "gender",
    "phone_number", "personal_email", "fathers_name", "mothers_name",
    "nationality", "citizenship", "current_location", "current_address",
    "permanent_location", "permanent_address", "emergency_contact_name",
    "emergency_contact_number", "emergency_contact_address",
    "has_pan", "pan_card_number", "pan_first_name", "pan_middle_name",
    "pan_last_name", "aadhaar_last_4", "national_id", "has_passport",
    "passport_number", "passport_type", "passport_nationality",
    "passport_issue_date", "passport_expiry_date", "passport_issuing_authority",
    "citizenship_certificate_no", "disability", "disability_percent",
    "roll_number", "tenth_school", "tenth_board", "tenth_passing_year",
    "tenth_gpa", "tenth_percentage", "tenth_pass_mode", "twelfth_college",
    "twelfth_board", "twelfth_passing_year", "twelfth_gpa", "twelfth_percentage",
    "twelfth_pass_mode", "graduation_college", "graduation_degree",
    "graduation_branch", "graduation_passing_year", "graduation_pass_mode",
    "current_cgpa", "gap_10th_12th", "gap_12th_grad", "total_gap",
    "active_backlogs", "history_backlogs", "marksheet_8th_sem",
    "resume_link", "github_link", "linkedin_link", "portfolio_link",
    "intro_video_link", "technical_skills", "employment_status",
    "flexible_shift", "flexible_location", "disclaimers_agree",
}

STRUCTURED_PROFILE_KEYS = {"custom_clarifications", "resume_master", "whatsapp_config"}

ALLOWED_PROFILE_KEYS = (
    set(DEFAULT_PROFILE_TEMPLATE.keys())
    | PROFILE_UI_KEYS
    | STRUCTURED_PROFILE_KEYS
    | {"email", "emergency_contact_phone"}
)

TRUSTED_ORIGINS_HOSTS = {
    "superset-job-pilot-ashu.netlify.app",
    "superset-job-pilot.onrender.com",
}

CSP_POLICY = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com data:; "
    "img-src 'self' data: blob:; "
    "connect-src 'self'; "
    "media-src 'self' blob:; "
    "worker-src 'self' blob:; "
    "frame-src 'self' blob:; "
    "object-src 'self' blob:; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)


def is_owner_user(user: Optional[dict]) -> bool:
    """Returns True only for an administrator or the configured site owner."""
    if not user or not isinstance(user, dict):
        return False
    if bool(user.get("is_admin")):
        return True
    owner_email = (Config.SUPERSET_EMAIL or "").strip().lower()
    user_email = str(user.get("email") or "").strip().lower()
    return bool(owner_email and user_email and user_email == owner_email)


def get_client_ip(req) -> str:
    """Extracts the client's first-hop IP address safely behind proxies."""
    xff = (req.headers.get("X-Forwarded-For") or "").strip()
    if xff:
        first_hop = xff.split(",")[0].strip()
        if first_hop:
            return first_hop[:64]
    return (getattr(req, "remote_addr", None) or "unknown")[:64]


class RateLimiter:
    """Thread-safe sliding-window in-memory rate limiter."""

    def __init__(self):
        self._lock = threading.Lock()
        self._hits: Dict[Tuple[str, str], deque] = defaultdict(deque)

    def check(self, bucket: str, key: str, limit: int, window_sec: int) -> Tuple[bool, int]:
        now = time.monotonic()
        cutoff = now - max(1, int(window_sec))
        map_key = (str(bucket), str(key or "anon"))
        with self._lock:
            q = self._hits[map_key]
            while q and q[0] <= cutoff:
                q.popleft()
            if len(q) >= limit:
                retry_after = max(1, int(window_sec - (now - q[0])) + 1)
                return False, retry_after
            q.append(now)
            return True, 0

    def reset(self):
        with self._lock:
            self._hits.clear()


rate_limiter = RateLimiter()


def apply_security_headers(resp, is_https: bool):
    """Attaches defense-in-depth HTTP security headers to every Flask response."""
    resp.headers.setdefault("Content-Security-Policy", CSP_POLICY)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("Permissions-Policy", "camera=(), geolocation=(), microphone=()")
    resp.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin-allow-popups")
    existing_vary = resp.headers.get("Vary", "")
    vary_tokens = [v.strip() for v in existing_vary.split(",") if v.strip()]
    for needed in ("Sec-Fetch-Dest", "Sec-Fetch-Mode", "Sec-Fetch-Site"):
        if needed not in vary_tokens:
            vary_tokens.append(needed)
    resp.headers["Vary"] = ", ".join(vary_tokens)
    if is_https:
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return resp


def check_request_isolation_and_csrf(req) -> Optional[Tuple[dict, int]]:
    """
    Runs BEFORE auth on `/api/*` (except `/api/health`) to block cross-site requests,
    untrusted Origins, and CSRF attempts lacking `X-Requested-With: sjp`.
    """
    path = getattr(req, "path", "") or ""
    if not path.startswith("/api/") or path == "/api/health":
        return None

    sec_fetch_site = (req.headers.get("Sec-Fetch-Site") or "").strip().lower()
    if sec_fetch_site == "cross-site":
        return (
            {
                "success": False,
                "error_code": "cross_site_forbidden",
                "error": "Cross-site request blocked",
                "message": "Cross-site API requests are forbidden.",
            },
            403,
        )

    method = (getattr(req, "method", "GET") or "GET").upper()
    if method in ("POST", "PUT", "PATCH", "DELETE"):
        origin = (req.headers.get("Origin") or "").strip()
        if origin:
            parsed = urlparse(origin)
            origin_host = (parsed.netloc or "").lower()
            req_host = (getattr(req, "host", "") or "").lower()
            if not origin_host or (origin_host != req_host and origin_host not in TRUSTED_ORIGINS_HOSTS):
                return (
                    {
                        "success": False,
                        "error_code": "origin_forbidden",
                        "error": "Untrusted request origin",
                        "message": "Origin not permitted for state-changing requests.",
                    },
                    403,
                )

        if (req.headers.get("X-Requested-With") or "").strip() != "sjp":
            return (
                {
                    "success": False,
                    "error_code": "csrf_header_missing",
                    "error": "Missing X-Requested-With header",
                    "message": "CSRF protection: X-Requested-With header required.",
                },
                403,
            )

    return None


def is_valid_identifier(val: Any) -> bool:
    return bool(isinstance(val, str) and SAFE_ID_RE.match(val))


def sanitize_profile_patch(data: dict) -> dict:
    """
    Whitelists profile keys, aliases `emergency_contact_phone` -> `emergency_contact_number`,
    caps scalar strings at 2,000 chars, and protects nested structures.
    """
    if not isinstance(data, dict):
        return {}

    cleaned: Dict[str, Any] = {}
    working = dict(data)

    # Alias legacy/alternate field name without losing data
    if "emergency_contact_phone" in working and not working.get("emergency_contact_number"):
        working["emergency_contact_number"] = working.get("emergency_contact_phone")

    for raw_k, raw_v in working.items():
        if not isinstance(raw_k, str):
            continue
        k = "emergency_contact_number" if raw_k == "emergency_contact_phone" else raw_k
        if k not in ALLOWED_PROFILE_KEYS:
            continue

        if k == "custom_clarifications":
            if isinstance(raw_v, dict):
                safe_c: Dict[str, Any] = {}
                for ck, cv in list(raw_v.items())[:200]:
                    ck_s = str(ck or "").strip()[:300]
                    if not ck_s:
                        continue
                    if isinstance(cv, list):
                        safe_c[ck_s] = [str(item)[:300] for item in cv[:40]]
                    elif isinstance(cv, bool):
                        safe_c[ck_s] = cv
                    elif cv is None:
                        safe_c[ck_s] = ""
                    elif not isinstance(cv, dict):
                        safe_c[ck_s] = str(cv)[:2000]
                cleaned[k] = safe_c
        elif k in ("resume_master", "whatsapp_config"):
            if isinstance(raw_v, dict):
                cleaned[k] = raw_v
        else:
            if isinstance(raw_v, (dict, list)):
                continue
            cleaned[k] = "" if raw_v is None else str(raw_v)[:2000]

    return cleaned


def validate_photo_data_url(photo_b64: str) -> Tuple[bool, str]:
    """Validates an optional formal photo base64 payload (<= 1.5 MB, safe PNG/JPEG/WebP image)."""
    s = str(photo_b64 or "").strip()
    if not s:
        return True, ""
    if len(s) > 1_500_000:
        return False, "Photo too large. Maximum size is 1.5 MB."
    if PHOTO_DATA_URL_RE.match(s):
        return True, s
    # Backward compatibility: accept raw base64 strings saved by earlier canvas cropper and normalize to JPEG data URL
    if re.match(r"^[A-Za-z0-9+/=\s]{64,}$", s):
        return True, "data:image/jpeg;base64," + "".join(s.split())
    return False, "Invalid photo format. Only PNG, JPEG, or WebP base64 images are allowed."


def api_error(
    message: str,
    status: int = 400,
    error_code: str = "bad_request",
    exc: Optional[Exception] = None,
    **extra,
):
    """Builds a unified JSON error response without leaking stack traces or secrets."""
    payload: Dict[str, Any] = {
        "success": False,
        "error_code": error_code,
        "error": message,
        "message": message,
    }
    if exc is not None:
        eid = uuid.uuid4().hex[:8]
        logger.error(f"[error_id={eid}] {error_code}: {exc}", exc_info=True)
        payload["error_id"] = eid
    if extra:
        payload.update(extra)
    return jsonify(payload), status
