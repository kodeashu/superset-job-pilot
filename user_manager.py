"""
Multi-User Authentication, Encrypted Profile Vault, and Invite Code Manager
for Superset Job Pilot.

Stores per-user credentials and academic profiles encrypted at rest in SQLite
(data/users_vault.sqlite) using PBKDF2-HMAC-SHA256 + authenticated stream cipher.
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import sys
import threading
import time
import uuid
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

from config import BASE_DIR, Config

logger = logging.getLogger(__name__)

VAULT_DB_PATH = Config.DATA_DIR / "users_vault.sqlite"
VAULT_SECRET_PATH = Config.DATA_DIR / ".vault_secret"
LEGACY_PROFILE_PATH = Config.DATA_DIR / "student_profile.json"
EXAMPLE_PROFILE_PATH = BASE_DIR / "data" / "student_profile.example.json"
ENV_PATH = BASE_DIR / ".env"

DEFAULT_PROFILE_TEMPLATE: Dict[str, Any] = {
    "full_name": "",
    "first_name": "",
    "middle_name": "",
    "last_name": "",
    "email": "",
    "roll_number": "",
    "identification_number": "",
    "dob": "",
    "dob_iso": "",
    "gender": "",
    "phone_number": "",
    "emergency_contact_name": "",
    "emergency_contact_number": "",
    "emergency_contact_address": "",
    "fathers_name": "",
    "mothers_name": "",
    "nationality": "Indian",
    "citizenship": "India",
    "citizenship_certificate_no": "NA",
    "national_id": "",
    "has_passport": "No",
    "passport_type": "NA",
    "passport_nationality": "",
    "passport_number": "NA",
    "passport_issue_date": "NA",
    "passport_expiry_date": "NA",
    "passport_issuing_authority": "NA",
    "has_pan": "No",
    "pan_card_number": "NA",
    "pan_first_name": "",
    "pan_middle_name": "",
    "pan_last_name": "",
    "aadhaar_last_4": "",
    "current_address": "KIIT University, Patia, Bhubaneswar, Odisha, India",
    "current_location": "Bhubaneswar, Odisha",
    "permanent_address": "",
    "permanent_location": "",
    "permanent_district": "",
    "permanent_country": "India",
    "tenth_school": "",
    "tenth_board": "",
    "tenth_passing_year": "",
    "tenth_gpa": "",
    "tenth_percentage": "",
    "tenth_pass_type": "Regular",
    "tenth_course_type": "Fulltime",
    "twelfth_college": "",
    "twelfth_board": "",
    "twelfth_passing_year": "",
    "twelfth_gpa": "",
    "twelfth_percentage": "",
    "twelfth_pass_type": "Regular",
    "twelfth_course_type": "Fulltime",
    "graduation_degree": "B.Tech",
    "graduation_branch": "Computer Science and Engineering",
    "graduation_college": "Kalinga Institute of Industrial Technology (KIIT), Bhubaneswar",
    "graduation_passing_year": "2027",
    "current_cgpa": "",
    "active_backlogs": "0",
    "history_backlogs": "0",
    "gap_10th_12th": "0",
    "gap_12th_grad": "0",
    "total_gap": "0",
    "marksheet_8th_sem": "July'27",
    "resume_link": "",
    "intro_video_link": "",
    "github_link": "",
    "linkedin_link": "",
    "portfolio_link": "",
    "disability": "None",
    "disability_percent": "0",
    "flexible_shift": "Yes",
    "flexible_location": "Yes",
    "flexible_domain": "Yes",
    "genuine_interest": "Yes",
    "employment_status": "No",
    "disclaimers_agree": "I Agree",
    "custom_clarifications": {},
}


def _get_or_create_secret_key() -> bytes:
    """Fetches APP_SECRET_KEY from env, .env, or persistent DATA_DIR/.vault_secret (Docker-safe)."""
    key_str = os.getenv("APP_SECRET_KEY", "").strip()
    if not key_str and ENV_PATH.exists():
        try:
            for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
                if line.startswith("APP_SECRET_KEY="):
                    key_str = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
        except Exception:
            pass

    if not key_str and VAULT_SECRET_PATH.exists():
        try:
            key_str = VAULT_SECRET_PATH.read_text(encoding="utf-8").strip()
        except Exception:
            pass

    if not key_str and os.getenv("RENDER", "").strip().lower() == "true":
        raise RuntimeError("APP_SECRET_KEY environment variable must be set on Render.")

    if not key_str:
        key_str = secrets.token_urlsafe(48)
        os.environ["APP_SECRET_KEY"] = key_str
        # Persist inside DATA_DIR (survives Docker container rebuilds when /app/data is a mounted volume)
        try:
            VAULT_SECRET_PATH.parent.mkdir(parents=True, exist_ok=True)
            VAULT_SECRET_PATH.write_text(key_str, encoding="utf-8")
            try:
                os.chmod(VAULT_SECRET_PATH, 0o600)
            except Exception:
                pass
        except Exception as e:
            logger.warning(f"Could not persist APP_SECRET_KEY to {VAULT_SECRET_PATH}: {e}")
        # Also persist to local .env if present (non-Docker local runs)
        if ENV_PATH.exists():
            try:
                with open(ENV_PATH, "a", encoding="utf-8") as f:
                    f.write(f"\n# Auto-generated 256-bit Vault Master Key\nAPP_SECRET_KEY={key_str}\n")
            except Exception as e:
                logger.warning(f"Could not persist APP_SECRET_KEY to .env: {e}")

    return hashlib.sha256(key_str.encode("utf-8")).digest()


def _keystream(enc_key: bytes, nonce: bytes, length: int) -> bytes:
    """Generates cryptographic keystream blocks using HMAC-SHA256 in counter mode."""
    out = bytearray()
    counter = 0
    while len(out) < length:
        block = hmac.new(enc_key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest()
        out.extend(block)
        counter += 1
    return bytes(out[:length])


def encrypt_secret(plaintext: str) -> str:
    """Authenticated encryption (Encrypt-then-MAC with PBKDF2-HMAC-SHA256)."""
    if plaintext is None:
        plaintext = ""
    master = _get_or_create_secret_key()
    nonce = os.urandom(16)
    enc_key = hashlib.pbkdf2_hmac("sha256", master, nonce + b"enc", 10000, dklen=32)
    mac_key = hashlib.pbkdf2_hmac("sha256", master, nonce + b"mac", 10000, dklen=32)

    pt_bytes = plaintext.encode("utf-8")
    stream = _keystream(enc_key, nonce, len(pt_bytes))
    ciphertext = bytes(a ^ b for a, b in zip(pt_bytes, stream))
    tag = hmac.new(mac_key, nonce + ciphertext, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(nonce + tag + ciphertext).decode("ascii")


def decrypt_secret(token: str) -> str:
    """Verifies HMAC tag and decrypts ciphertext."""
    if not token:
        return ""
    master = _get_or_create_secret_key()
    raw = base64.urlsafe_b64decode(token.encode("ascii"))
    if len(raw) < 48:
        raise ValueError("Corrupted encrypted payload")
    nonce = raw[:16]
    tag = raw[16:48]
    ciphertext = raw[48:]

    enc_key = hashlib.pbkdf2_hmac("sha256", master, nonce + b"enc", 10000, dklen=32)
    mac_key = hashlib.pbkdf2_hmac("sha256", master, nonce + b"mac", 10000, dklen=32)
    expected_tag = hmac.new(mac_key, nonce + ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected_tag):
        raise ValueError("Invalid encryption key or tampered vault record")

    stream = _keystream(enc_key, nonce, len(ciphertext))
    pt_bytes = bytes(a ^ b for a, b in zip(ciphertext, stream))
    return pt_bytes.decode("utf-8")


def hash_password(password: str, salt: Optional[bytes] = None) -> str:
    """Hashes a user password using PBKDF2-HMAC-SHA256 (120,000 iterations)."""
    salt = salt or os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120000)
    return base64.urlsafe_b64encode(salt + dk).decode("ascii")


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        raw = base64.urlsafe_b64decode(stored_hash.encode("ascii"))
        salt = raw[:16]
        expected = raw[16:]
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120000)
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════════════════
# ZERO-KNOWLEDGE ENCRYPTED CLOUD VAULT SYNC (NETLIFY BLOBS BACKING)
# ═══════════════════════════════════════════════════════════════════════════

CLOUD_VAULT_V1_ITERATIONS = 120_000
CLOUD_VAULT_V2_ITERATIONS = 310_000


def derive_cloud_vault_credentials(
    email: str,
    superset_password: str,
    iterations: int = CLOUD_VAULT_V2_ITERATIONS,
) -> Tuple[str, str, bytes, bytes]:
    """
    Derives deterministic per-user cloud storage coordinates & keys from (email, superset_password).
    The remote blob server receives only ciphertext (`cv2.*`) and SHA-256(write_token).
    """
    norm_email = (email or "").strip().lower()
    vault_id = hashlib.sha256(f"sjp-cv-id-v1:{norm_email}".encode("utf-8")).hexdigest()
    master = hashlib.pbkdf2_hmac(
        "sha256",
        (superset_password or "").encode("utf-8"),
        f"sjp-cv-salt-v1:{norm_email}".encode("utf-8"),
        int(iterations),
        dklen=96,
    )
    enc_key, mac_key, wt_key = master[:32], master[32:64], master[64:96]
    write_token = hmac.new(wt_key, vault_id.encode("ascii"), hashlib.sha256).hexdigest()
    return vault_id, write_token, enc_key, mac_key


def _derive_v1_credentials(email: str, superset_password: str) -> Tuple[str, str, bytes, bytes]:
    return derive_cloud_vault_credentials(email, superset_password, iterations=CLOUD_VAULT_V1_ITERATIONS)


def encrypt_cloud_vault_blob(email: str, superset_password: str, profile: Dict[str, Any]) -> Tuple[str, str, str]:
    """Compresses and Encrypt-then-MAC protects a profile dict as a zero-knowledge `cv2.*` blob."""
    vault_id, write_token, enc_key, mac_key = derive_cloud_vault_credentials(email, superset_password)
    compressed = zlib.compress(json.dumps(profile, separators=(",", ":")).encode("utf-8"), 9)
    nonce = secrets.token_bytes(16)
    stream = _keystream(enc_key, nonce, len(compressed))
    ciphertext = bytes(a ^ b for a, b in zip(compressed, stream))
    mac = hmac.new(mac_key, nonce + ciphertext, hashlib.sha256).digest()
    blob = "cv2." + base64.urlsafe_b64encode(nonce + mac + ciphertext).decode("ascii")
    return vault_id, write_token, blob


def decrypt_cloud_vault_blob(email: str, superset_password: str, blob: str) -> Optional[Dict[str, Any]]:
    """Verifies HMAC and decrypts/decompresses a zero-knowledge `cv2.*` or legacy `cv1.*` cloud vault blob."""
    if not blob or not isinstance(blob, str) or not (blob.startswith("cv2.") or blob.startswith("cv1.")):
        return None
    iterations = CLOUD_VAULT_V2_ITERATIONS if blob.startswith("cv2.") else CLOUD_VAULT_V1_ITERATIONS
    try:
        _, _, enc_key, mac_key = derive_cloud_vault_credentials(email, superset_password, iterations=iterations)
        raw = base64.urlsafe_b64decode(blob[4:].encode("ascii"))
        if len(raw) < 48:
            return None
        nonce = raw[:16]
        mac = raw[16:48]
        ciphertext = raw[48:]
        expected_mac = hmac.new(mac_key, nonce + ciphertext, hashlib.sha256).digest()
        if not hmac.compare_digest(mac, expected_mac):
            return None
        stream = _keystream(enc_key, nonce, len(ciphertext))
        compressed = bytes(a ^ b for a, b in zip(ciphertext, stream))
        parsed = json.loads(zlib.decompress(compressed).decode("utf-8"))
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def _cloud_sync_enabled() -> bool:
    return bool(Config.CLOUD_VAULT_SYNC_URL and "unittest" not in sys.modules)


def pull_cloud_vault_profile(email: str, superset_password: str, sync_url: str = "") -> Optional[Dict[str, Any]]:
    """Fetches and decrypts the user's cloud vault backup if available."""
    url = (sync_url or Config.CLOUD_VAULT_SYNC_URL or "").strip()
    if not url or not email or not superset_password:
        return None
    try:
        vault_id, v2_write_token, _, _ = derive_cloud_vault_credentials(email, superset_password)
        _, v1_write_token, _, _ = _derive_v1_credentials(email, superset_password)
        resp = requests.get(
            url,
            params={"id": vault_id},
            headers={"X-Vault-Token": v2_write_token, "X-Vault-Token-Legacy": v1_write_token},
            timeout=8,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        payload = data.get("payload") if isinstance(data, dict) else None
        if not payload:
            return None
        return decrypt_cloud_vault_blob(email, superset_password, payload)
    except Exception as e:
        logger.debug(f"Cloud vault pull skipped/failed for {email}: {e}")
        return None


def push_cloud_vault_profile(email: str, superset_password: str, profile: Dict[str, Any], sync_url: str = "") -> bool:
    """Encrypts and uploads the user's full profile vault to durable cloud blob storage."""
    url = (sync_url or Config.CLOUD_VAULT_SYNC_URL or "").strip()
    if not url or not email or not superset_password or not isinstance(profile, dict):
        return False
    try:
        vault_id, v2_write_token, blob = encrypt_cloud_vault_blob(email, superset_password, profile)
        _, v1_write_token, _, _ = _derive_v1_credentials(email, superset_password)
        resp = requests.post(
            url,
            json={
                "id": vault_id,
                "payload": blob,
                "write_token": v2_write_token,
                "legacy_write_token": v1_write_token,
            },
            timeout=10,
        )
        return resp.status_code == 200 and bool(resp.json().get("success"))
    except Exception as e:
        logger.debug(f"Cloud vault push skipped/failed for {email}: {e}")
        return False


def _push_cloud_vault_async(email: str, superset_password: str, profile: Dict[str, Any]):
    threading.Thread(
        target=push_cloud_vault_profile,
        args=(email, superset_password, dict(profile)),
        daemon=True,
    ).start()


class UserManager:
    """Manages multi-user accounts, encrypted student profiles, sessions, and invite codes."""

    def __init__(self, db_path: Path = None):
        self.db_path = db_path or VAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self._migrate_owner_and_sanitize_files()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    email TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    encrypted_superset_password TEXT NOT NULL,
                    superset_uuid TEXT DEFAULT '',
                    full_name TEXT DEFAULT '',
                    roll_number TEXT DEFAULT '',
                    encrypted_profile TEXT NOT NULL,
                    auto_apply_enabled INTEGER DEFAULT 0,
                    telegram_chat_id TEXT DEFAULT '',
                    is_admin INTEGER DEFAULT 0,
                    token_version INTEGER DEFAULT 1,
                    created_at INTEGER NOT NULL,
                    last_sync_at TEXT DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS revoked_tokens (
                    token_hash TEXT PRIMARY KEY,
                    revoked_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS invite_codes (
                    code TEXT PRIMARY KEY,
                    label TEXT DEFAULT 'College Friend Invite',
                    created_by TEXT DEFAULT 'admin',
                    uses_count INTEGER DEFAULT 0,
                    max_uses INTEGER DEFAULT 100,
                    active INTEGER DEFAULT 1,
                    created_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS user_jobs_cache (
                    user_id TEXT PRIMARY KEY,
                    jobs_json TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS user_notices_cache (
                    user_id TEXT PRIMARY KEY,
                    notices_json TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS user_pending_actions_cache (
                    user_id TEXT PRIMARY KEY,
                    actions_json TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS notice_pdf_cache (
                    attachment_id TEXT PRIMARY KEY,
                    filename TEXT DEFAULT '',
                    page_count INTEGER DEFAULT 0,
                    extracted_text TEXT NOT NULL,
                    scanned_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS user_sent_alerts (
                    user_id TEXT NOT NULL,
                    alert_key TEXT NOT NULL,
                    sent_at INTEGER NOT NULL,
                    PRIMARY KEY (user_id, alert_key)
                );
                """
            )
            # Ensure schema migration for token_version on older SQLite vaults
            try:
                conn.execute("ALTER TABLE users ADD COLUMN token_version INTEGER DEFAULT 1")
            except sqlite3.OperationalError:
                pass
            # Ensure default invite code KIIT2027 exists
            cur = conn.execute("SELECT code FROM invite_codes WHERE code = ?", ("KIIT2027",))
            if not cur.fetchone():
                conn.execute(
                    "INSERT INTO invite_codes (code, label, created_by, uses_count, max_uses, active, created_at) VALUES (?, ?, ?, 0, 500, 1, ?)",
                    ("KIIT2027", "KIIT 2027 Batch Shared Code", "admin", int(time.time())),
                )
            conn.commit()

    def _migrate_owner_and_sanitize_files(self):
        """
        Migrates the owner's credentials (.env) and legacy student_profile.json into the
        encrypted SQLite vault, writes an anonymized student_profile.example.json, and
        deletes plaintext PII from student_profile.json.
        """
        # Write clean example template if missing
        if not EXAMPLE_PROFILE_PATH.exists():
            try:
                with open(EXAMPLE_PROFILE_PATH, "w", encoding="utf-8") as f:
                    json.dump(DEFAULT_PROFILE_TEMPLATE, f, indent=2)
            except Exception as e:
                logger.debug(f"Could not write example profile template: {e}")

        owner_email = (Config.SUPERSET_EMAIL or "").strip().lower()
        owner_pw = Config.SUPERSET_PASSWORD or ""

        legacy_profile: Dict[str, Any] = {}
        if LEGACY_PROFILE_PATH.exists():
            try:
                raw_p = json.loads(LEGACY_PROFILE_PATH.read_text(encoding="utf-8"))
                if isinstance(raw_p, dict) and raw_p.get("full_name"):
                    legacy_profile = raw_p
            except Exception:
                pass

        if not owner_email and legacy_profile.get("email"):
            owner_email = str(legacy_profile["email"]).strip().lower()

        if owner_email and owner_pw:
            with self._connect() as conn:
                existing = conn.execute("SELECT user_id, encrypted_profile FROM users WHERE email = ?", (owner_email,)).fetchone()
                if not existing:
                    cloud_prof = pull_cloud_vault_profile(owner_email, owner_pw) if _cloud_sync_enabled() else None
                    merged_profile = {**DEFAULT_PROFILE_TEMPLATE, **(cloud_prof or {}), **legacy_profile}
                    merged_profile["email"] = owner_email
                    full_name = merged_profile.get("full_name") or "Student"
                    roll_no = merged_profile.get("roll_number") or owner_email.split("@")[0]
                    uid = str(uuid.uuid4())
                    conn.execute(
                        """
                        INSERT INTO users (
                            user_id, email, password_hash, encrypted_superset_password,
                            superset_uuid, full_name, roll_number, encrypted_profile,
                            auto_apply_enabled, telegram_chat_id, is_admin, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                        """,
                        (
                            uid,
                            owner_email,
                            hash_password(owner_pw),
                            encrypt_secret(owner_pw),
                            "",
                            full_name,
                            roll_no,
                            encrypt_secret(json.dumps(merged_profile)),
                            1 if Config.AUTO_APPLY_ELIGIBLE else 0,
                            Config.TELEGRAM_CHAT_ID or "",
                            int(time.time()),
                        ),
                    )
                    conn.commit()
                    logger.info(
                        f"Migrated owner account ({owner_email}) into encrypted users_vault.sqlite"
                        + (" (restored full profile from Cloud Vault)." if cloud_prof else ".")
                    )
                else:
                    # Ensure owner's encrypted profile has all rich fields from cloud vault & legacy_profile
                    try:
                        curr_p = json.loads(decrypt_secret(existing["encrypted_profile"]))
                    except Exception:
                        curr_p = {}
                    needs_cloud = (
                        not curr_p.get("dob")
                        or not curr_p.get("resume_link")
                        or not curr_p.get("resume_master")
                    )
                    cloud_prof = pull_cloud_vault_profile(owner_email, owner_pw) if (needs_cloud and _cloud_sync_enabled()) else None
                    if cloud_prof or (legacy_profile.get("full_name") and not curr_p.get("full_name")):
                        non_empty_curr = {k: v for k, v in curr_p.items() if v not in ("", None, [], {})}
                        merged_profile = {
                            **DEFAULT_PROFILE_TEMPLATE,
                            **(cloud_prof or {}),
                            **legacy_profile,
                            **non_empty_curr,
                        }
                        conn.execute(
                            "UPDATE users SET encrypted_profile = ?, full_name = ?, roll_number = ? WHERE user_id = ?",
                            (
                                encrypt_secret(json.dumps(merged_profile)),
                                merged_profile.get("full_name") or "Student",
                                merged_profile.get("roll_number") or owner_email.split("@")[0],
                                existing["user_id"],
                            ),
                        )
                        conn.commit()

        # Remove plaintext PII file after migration into encrypted vault
        if LEGACY_PROFILE_PATH.exists():
            try:
                with self._connect() as conn:
                    count = conn.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]
                if count > 0:
                    LEGACY_PROFILE_PATH.unlink()
                    logger.info("Removed plaintext data/student_profile.json after migrating to encrypted vault.")
            except Exception as e:
                logger.warning(f"Could not remove legacy student_profile.json: {e}")

    # ═══════════════════════════════════════════════════════════════════
    # SESSION & USER MANAGEMENT (30-Day Stateless HMAC-SHA256 + Revocation)
    # ═══════════════════════════════════════════════════════════════════

    @staticmethod
    def _session_signing_key() -> bytes:
        return hmac.new(_get_or_create_secret_key(), b"sjp-stateless-session-v1", hashlib.sha256).digest()

    @classmethod
    def _sign_session_token(cls, email: str, expires_at: int, token_version: int = 1) -> str:
        payload = {
            "e": email.strip().lower(),
            "exp": int(expires_at),
            "v": int(token_version),
            "n": secrets.token_hex(8),
        }
        raw_json = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        payload_b64 = base64.urlsafe_b64encode(raw_json).decode("ascii").rstrip("=")
        sig = hmac.new(cls._session_signing_key(), payload_b64.encode("ascii"), hashlib.sha256).digest()
        sig_b64 = base64.urlsafe_b64encode(sig).decode("ascii").rstrip("=")
        return f"sjp2.{payload_b64}.{sig_b64}"

    @classmethod
    def _verify_session_token(cls, raw_token: str) -> Optional[Tuple[str, int]]:
        parts = (raw_token or "").split(".")
        if len(parts) != 3 or parts[0] not in ("sjp1", "sjp2"):
            return None
        prefix, payload_b64, sig_b64 = parts
        expected_sig = base64.urlsafe_b64encode(
            hmac.new(cls._session_signing_key(), payload_b64.encode("ascii"), hashlib.sha256).digest()
        ).decode("ascii").rstrip("=")
        if not hmac.compare_digest(expected_sig, sig_b64):
            return None
        try:
            padded = payload_b64 + "=" * (-len(payload_b64) % 4)
            data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
            if int(data.get("exp", 0)) <= int(time.time()):
                return None
            email = str(data.get("e") or "").strip().lower()
            if not email:
                return None
            tok_ver = int(data.get("v", 1)) if prefix == "sjp2" else 1
            return email, tok_ver
        except Exception:
            return None

    def create_session(self, user_id: str, ttl_days: int = 30) -> str:
        now = int(time.time())
        expires = now + ttl_days * 86400
        with self._connect() as conn:
            row = conn.execute(
                "SELECT email, COALESCE(token_version, 1) AS token_version FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            email = row["email"] if row else ""
            tok_ver = int(row["token_version"]) if row else 1
            raw_token = self._sign_session_token(email, expires, tok_ver) if email else secrets.token_urlsafe(36)
            token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
            conn.execute(
                "INSERT OR REPLACE INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (token_hash, user_id, now, expires),
            )
            conn.commit()
        return raw_token

    def revoke_all_user_sessions(self, user_id: str):
        if not user_id:
            return
        with self._connect() as conn:
            conn.execute(
                "UPDATE users SET token_version = COALESCE(token_version, 1) + 1 WHERE user_id = ?",
                (user_id,),
            )
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            conn.commit()

    def destroy_session(self, raw_token: str):
        if not raw_token:
            return
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        with self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
            conn.execute(
                "INSERT OR REPLACE INTO revoked_tokens (token_hash, revoked_at) VALUES (?, ?)",
                (token_hash, int(time.time())),
            )
            conn.commit()

    def get_user_by_token(self, raw_token: str) -> Optional[Dict[str, Any]]:
        if not raw_token:
            return None
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        now = int(time.time())
        with self._connect() as conn:
            revoked = conn.execute(
                "SELECT 1 FROM revoked_tokens WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
            if revoked:
                return None

            # 1. Stateless HMAC-SHA256 signed token (survives Render/Docker ephemeral container restarts!)
            if raw_token.startswith(("sjp1.", "sjp2.")):
                verified = self._verify_session_token(raw_token)
                if not verified:
                    return None
                email, tok_ver = verified
                row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
                if not row:
                    return None
                user_obj = self._deserialize_user_row(row)
                if int(user_obj.get("token_version", 1)) != int(tok_ver):
                    return None
                return user_obj

            # 2. Legacy opaque SQLite session fallback
            row = conn.execute(
                """
                SELECT u.* FROM sessions s
                JOIN users u ON s.user_id = u.user_id
                WHERE s.token_hash = ? AND s.expires_at > ?
                """,
                (token_hash, now),
            ).fetchone()
            if not row:
                return None
            return self._deserialize_user_row(row)

    def get_user_by_id(self, user_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
            return self._deserialize_user_row(row) if row else None

    def get_all_users(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM users ORDER BY created_at ASC").fetchall()
            return [self._deserialize_user_row(r) for r in rows]

    def _deserialize_user_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        try:
            superset_password = decrypt_secret(row["encrypted_superset_password"])
        except Exception:
            superset_password = ""
        try:
            profile = json.loads(decrypt_secret(row["encrypted_profile"]))
        except Exception:
            profile = dict(DEFAULT_PROFILE_TEMPLATE)

        return {
            "user_id": row["user_id"],
            "email": row["email"],
            "superset_password": superset_password,
            "superset_uuid": row["superset_uuid"] or "",
            "full_name": row["full_name"] or profile.get("full_name") or row["email"].split("@")[0],
            "roll_number": row["roll_number"] or profile.get("roll_number") or row["email"].split("@")[0],
            "profile": profile,
            "auto_apply_enabled": bool(row["auto_apply_enabled"]),
            "telegram_chat_id": row["telegram_chat_id"] or "",
            "is_admin": bool(row["is_admin"]),
            "token_version": int(row["token_version"]) if "token_version" in row.keys() and row["token_version"] is not None else 1,
            "created_at": row["created_at"],
            "last_sync_at": row["last_sync_at"] or "",
        }

    # ═══════════════════════════════════════════════════════════════════
    # LOGIN & REGISTRATION (WITH LIVE SUPERSET AUTO-IMPORT)
    # ═══════════════════════════════════════════════════════════════════

    def authenticate_or_register(
        self,
        email: str,
        password: str,
        invite_code: str = "",
        is_register: bool = False,
        full_name_override: str = "",
    ) -> Tuple[bool, str, Optional[Dict[str, Any]], str]:
        """
        Authenticates an existing user or registers a new student account by verifying
        their credentials against Superset and importing their student profile.
        Returns (success, message, user_dict, session_token).
        """
        email_clean = (email or "").strip().lower()
        pw_clean = (password or "").strip()
        if not email_clean or not pw_clean:
            return False, "Please enter both your email and password.", None, ""

        with self._connect() as conn:
            existing = conn.execute("SELECT * FROM users WHERE email = ?", (email_clean,)).fetchone()

        if existing:
            user_deserialized = self._deserialize_user_row(existing)

            # Strict cryptographic verification of user's password against stored PBKDF2 hash
            if verify_password(pw_clean, existing["password_hash"]):
                curr_p = user_deserialized.get("profile") or {}
                # 1. Restore from Cloud Vault if container profile is missing rich fields
                if _cloud_sync_enabled() and (not curr_p.get("dob") or not curr_p.get("resume_master")):
                    cloud_prof = pull_cloud_vault_profile(email_clean, pw_clean)
                    if cloud_prof:
                        non_empty_curr = {k: v for k, v in curr_p.items() if v not in ("", None, [], {})}
                        merged = {**DEFAULT_PROFILE_TEMPLATE, **cloud_prof, **non_empty_curr}
                        self.update_user_profile(user_deserialized["user_id"], merged, push_cloud=False)
                        user_deserialized = self.get_user_by_id(user_deserialized["user_id"]) or user_deserialized
                        curr_p = user_deserialized.get("profile") or {}

                # 2. Enrich minimal profile from Superset API if still missing core fields
                if (
                    not user_deserialized.get("full_name")
                    or user_deserialized.get("full_name") == "Student"
                    or not curr_p.get("current_cgpa")
                ):
                    try:
                        from superset_client import SupersetClient
                        sc = SupersetClient(email=email_clean, password=pw_clean)
                        if sc.login():
                            imported = self._import_profile_from_superset(sc, email_clean, full_name_override)
                            non_empty_curr = {k: v for k, v in curr_p.items() if v not in ("", None, [], {})}
                            merged = {**imported, **non_empty_curr}
                            if merged.get("full_name") == "Student" and imported.get("full_name"):
                                merged["full_name"] = imported["full_name"]
                            self.update_user_profile(user_deserialized["user_id"], merged, push_cloud=False)
                            user_deserialized = self.get_user_by_id(user_deserialized["user_id"]) or user_deserialized
                    except Exception as enrich_err:
                        logger.debug(f"Non-blocking profile enrichment skipped: {enrich_err}")
                token = self.create_session(user_deserialized["user_id"])
                return True, f"Welcome back, {user_deserialized['full_name']}!", user_deserialized, token

            # Try live Superset login in case user updated their Superset password on the official portal
            from superset_client import SupersetClient
            sc = SupersetClient(email=email_clean, password=pw_clean)
            if sc.login():
                with self._connect() as conn:
                    conn.execute(
                        "UPDATE users SET password_hash = ?, encrypted_superset_password = ?, superset_uuid = ? WHERE user_id = ?",
                        (hash_password(pw_clean), encrypt_secret(pw_clean), sc.user_uuid or "", existing["user_id"]),
                    )
                    conn.commit()
                self.revoke_all_user_sessions(existing["user_id"])
                user = self.get_user_by_id(existing["user_id"])
                token = self.create_session(existing["user_id"])
                return True, f"Welcome back, {user['full_name']}!", user, token

            return False, "Invalid email or password. Tap the eye icon to check your password.", None, ""

        # Check invite code for new users
        code_clean = (invite_code or "KIIT2027").strip().upper()
        with self._connect() as conn:
            inv = conn.execute(
                "SELECT * FROM invite_codes WHERE UPPER(code) = ? AND active = 1",
                (code_clean,),
            ).fetchone()
            if not inv:
                return False, "Invalid or expired invite code. Ask your friend for a valid invite code (e.g. KIIT2027).", None, ""
            if inv["uses_count"] >= inv["max_uses"]:
                return False, "This invite code has reached its usage limit.", None, ""

        # Authenticate against live Superset API to verify account and auto-import profile
        from superset_client import SupersetClient
        sc = SupersetClient(email=email_clean, password=pw_clean)
        if not sc.login():
            return False, "Superset authentication failed. Please check your Superset email and password.", None, ""

        # Auto-import student details from Superset + merge any prior Cloud Vault backup
        imported_profile = self._import_profile_from_superset(sc, email_clean, full_name_override)
        cloud_prof = pull_cloud_vault_profile(email_clean, pw_clean) if _cloud_sync_enabled() else None
        if cloud_prof:
            non_empty_imp = {k: v for k, v in imported_profile.items() if v not in ("", None, [], {})}
            imported_profile = {**DEFAULT_PROFILE_TEMPLATE, **non_empty_imp, **cloud_prof}
        full_name = imported_profile.get("full_name") or full_name_override or email_clean.split("@")[0]
        roll_no = imported_profile.get("roll_number") or email_clean.split("@")[0]
        uid = str(uuid.uuid4())

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO users (
                    user_id, email, password_hash, encrypted_superset_password,
                    superset_uuid, full_name, roll_number, encrypted_profile,
                    auto_apply_enabled, telegram_chat_id, is_admin, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, '', 0, ?)
                """,
                (
                    uid,
                    email_clean,
                    hash_password(pw_clean),
                    encrypt_secret(pw_clean),
                    sc.user_uuid or "",
                    full_name,
                    roll_no,
                    encrypt_secret(json.dumps(imported_profile)),
                    int(time.time()),
                ),
            )
            conn.execute(
                "UPDATE invite_codes SET uses_count = uses_count + 1 WHERE UPPER(code) = ?",
                (code_clean,),
            )
            conn.commit()

        user = self.get_user_by_id(uid)
        token = self.create_session(uid)
        return True, f"Account created & synced with Superset for {full_name}!", user, token

    def _import_profile_from_superset(self, sc, email: str, full_name_override: str = "") -> Dict[str, Any]:
        """Pulls rich student profile fields directly from Superset /tnpsuite-core/students/{uuid}."""
        prof = dict(DEFAULT_PROFILE_TEMPLATE)
        prof["email"] = email
        roll_guess = email.split("@")[0]
        prof["roll_number"] = roll_guess
        prof["identification_number"] = roll_guess

        def _fmt_addr(addr_obj: Any) -> Tuple[str, str]:
            if not isinstance(addr_obj, dict):
                return "", ""
            parts = [
                str(addr_obj.get(k) or "").strip()
                for k in ("addressLine", "cityTownVillage", "district", "state", "pincode", "country")
            ]
            full_addr = ", ".join(p for p in parts if p)
            loc_parts = [str(addr_obj.get(k) or "").strip() for k in ("cityTownVillage", "state")]
            short_loc = ", ".join(p for p in loc_parts if p)
            return full_addr, short_loc

        try:
            url = f"{sc.base_url}/tnpsuite-core/students/{sc.user_uuid}"
            resp = sc.session.get(url, timeout=12)
            if resp.status_code == 200:
                sdata = resp.json()
                first = (sdata.get("firstName") or "").strip()
                middle = (sdata.get("middleName") or "").strip()
                last = (sdata.get("lastName") or "").strip()
                parts = [p for p in (first, middle, last) if p]
                full_name = " ".join(parts) or full_name_override or sc.student_name or roll_guess
                prof["full_name"] = full_name
                prof["first_name"] = first or full_name.split()[0]
                prof["middle_name"] = middle
                prof["last_name"] = last or (full_name.split()[-1] if len(full_name.split()) > 1 else "")
                prof["roll_number"] = str(sdata.get("rollNumber") or sdata.get("identificationNumber") or sdata.get("enrollmentNumber") or roll_guess)
                prof["identification_number"] = prof["roll_number"]
                if sdata.get("mobile") or sdata.get("phone"):
                    prof["phone_number"] = str(sdata.get("mobile") or sdata.get("phone"))
                if sdata.get("gender"):
                    prof["gender"] = str(sdata.get("gender")).capitalize()
                if sdata.get("dob"):
                    prof["dob"] = str(sdata.get("dob")).strip()
                if sdata.get("personalEmail"):
                    prof["personal_email"] = str(sdata.get("personalEmail")).strip()
                if sdata.get("collegeName"):
                    prof["graduation_college"] = str(sdata.get("collegeName")).strip()
                if sdata.get("currentCgpa") or sdata.get("cgpa"):
                    prof["current_cgpa"] = str(sdata.get("currentCgpa") or sdata.get("cgpa"))
                if sdata.get("departmentName") or sdata.get("branch"):
                    prof["graduation_branch"] = str(sdata.get("departmentName") or sdata.get("branch"))

                cur_full, cur_loc = _fmt_addr(sdata.get("currentAddress"))
                if cur_full:
                    prof["current_address"] = cur_full
                if cur_loc:
                    prof["current_location"] = cur_loc

                perm_full, perm_loc = _fmt_addr(sdata.get("permanentAddress"))
                if perm_full:
                    prof["permanent_address"] = perm_full
                if perm_loc:
                    prof["permanent_location"] = perm_loc

                inv = sdata.get("invitation") if isinstance(sdata.get("invitation"), dict) else {}
                if not prof.get("current_cgpa") and inv.get("currentCourseScoreValue"):
                    prof["current_cgpa"] = str(inv.get("currentCourseScoreValue"))
                if inv.get("currentCourseOngoingBacklogs") is not None:
                    prof["active_backlogs"] = str(inv.get("currentCourseOngoingBacklogs"))
                if inv.get("currentCourseTotalBacklogs") is not None:
                    prof["history_backlogs"] = str(inv.get("currentCourseTotalBacklogs"))
        except Exception as e:
            logger.debug(f"Auto-import profile from Superset warning: {e}")
            prof["full_name"] = full_name_override or sc.student_name or roll_guess

        return prof

    # ═══════════════════════════════════════════════════════════════════
    # PER-USER PROFILE, SETTINGS & JOB CACHE
    # ═══════════════════════════════════════════════════════════════════

    def update_user_profile(self, user_id: str, new_data: Dict[str, Any], push_cloud: bool = True) -> Dict[str, Any]:
        user = self.get_user_by_id(user_id)
        if not user:
            raise ValueError("User not found")
        profile = user["profile"]
        profile.update(new_data)
        full_name = profile.get("full_name") or user["full_name"]
        roll_no = profile.get("roll_number") or user["roll_number"]

        with self._connect() as conn:
            conn.execute(
                "UPDATE users SET encrypted_profile = ?, full_name = ?, roll_number = ? WHERE user_id = ?",
                (encrypt_secret(json.dumps(profile)), full_name, roll_no, user_id),
            )
            conn.commit()
        if push_cloud and _cloud_sync_enabled() and user.get("email") and user.get("superset_password"):
            _push_cloud_vault_async(user["email"], user["superset_password"], profile)
        return profile

    def set_user_auto_apply(self, user_id: str, enabled: bool) -> bool:
        with self._connect() as conn:
            conn.execute(
                "UPDATE users SET auto_apply_enabled = ? WHERE user_id = ?",
                (1 if enabled else 0, user_id),
            )
            conn.commit()
        return enabled

    def set_user_telegram_chat_id(self, user_id: str, chat_id: str):
        with self._connect() as conn:
            conn.execute(
                "UPDATE users SET telegram_chat_id = ? WHERE user_id = ?",
                ((chat_id or "").strip(), user_id),
            )
            conn.commit()

    def set_user_last_sync(self, user_id: str, sync_time_str: str):
        with self._connect() as conn:
            conn.execute(
                "UPDATE users SET last_sync_at = ? WHERE user_id = ?",
                (sync_time_str, user_id),
            )
            conn.commit()

    def get_cached_jobs(self, user_id: str) -> Tuple[List[Dict[str, Any]], int]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT jobs_json, updated_at FROM user_jobs_cache WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if not row:
                return [], 0
            try:
                return json.loads(row["jobs_json"]), int(row["updated_at"])
            except Exception:
                return [], 0

    def set_cached_jobs(self, user_id: str, jobs: List[Dict[str, Any]]):
        now = int(time.time())
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO user_jobs_cache (user_id, jobs_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET jobs_json = excluded.jobs_json, updated_at = excluded.updated_at
                """,
                (user_id, json.dumps(jobs, default=str), now),
            )
            conn.commit()

    # ─── NOTICES, SHORTLIST PDF CACHE & PENDING OFFERS WATCHDOG ───

    def get_cached_notices(self, user_id: str) -> Tuple[List[Dict[str, Any]], int]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT notices_json, updated_at FROM user_notices_cache WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if not row:
                return [], 0
            try:
                return json.loads(row["notices_json"]), int(row["updated_at"])
            except Exception:
                return [], 0

    def set_cached_notices(self, user_id: str, notices: List[Dict[str, Any]]):
        now = int(time.time())
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO user_notices_cache (user_id, notices_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET notices_json = excluded.notices_json, updated_at = excluded.updated_at
                """,
                (user_id, json.dumps(notices, default=str), now),
            )
            conn.commit()

    def get_cached_pending_actions(self, user_id: str) -> Tuple[List[Dict[str, Any]], int]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT actions_json, updated_at FROM user_pending_actions_cache WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if not row:
                return [], 0
            try:
                return json.loads(row["actions_json"]), int(row["updated_at"])
            except Exception:
                return [], 0

    def set_cached_pending_actions(self, user_id: str, actions: List[Dict[str, Any]]):
        now = int(time.time())
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO user_pending_actions_cache (user_id, actions_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET actions_json = excluded.actions_json, updated_at = excluded.updated_at
                """,
                (user_id, json.dumps(actions, default=str), now),
            )
            conn.commit()

    def get_cached_pdf_text(self, attachment_id: str) -> Optional[Dict[str, Any]]:
        if not attachment_id:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT filename, page_count, extracted_text, scanned_at FROM notice_pdf_cache WHERE attachment_id = ?",
                (attachment_id,),
            ).fetchone()
            return dict(row) if row else None

    def set_cached_pdf_text(self, attachment_id: str, filename: str, page_count: int, extracted_text: str):
        if not attachment_id:
            return
        now = int(time.time())
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO notice_pdf_cache (attachment_id, filename, page_count, extracted_text, scanned_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(attachment_id) DO UPDATE SET
                    filename = excluded.filename,
                    page_count = excluded.page_count,
                    extracted_text = excluded.extracted_text,
                    scanned_at = excluded.scanned_at
                """,
                (attachment_id, filename or "", int(page_count or 0), extracted_text or "", now),
            )
            conn.commit()

    def has_sent_alert(self, user_id: str, alert_key: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM user_sent_alerts WHERE user_id = ? AND alert_key = ?",
                (user_id, alert_key),
            ).fetchone()
            return bool(row)

    def mark_alert_sent(self, user_id: str, alert_key: str):
        now = int(time.time())
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO user_sent_alerts (user_id, alert_key, sent_at) VALUES (?, ?, ?)",
                (user_id, alert_key, now),
            )
            conn.commit()

    # ═══════════════════════════════════════════════════════════════════
    # INVITE CODE MANAGEMENT
    # ═══════════════════════════════════════════════════════════════════

    def list_invite_codes(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM invite_codes ORDER BY created_at DESC").fetchall()
            return [dict(r) for r in rows]

    def create_invite_code(self, code: str = "", label: str = "College Friend Invite", max_uses: int = 50) -> Dict[str, Any]:
        clean_code = (code or f"KIIT-{secrets.token_hex(2).upper()}").strip().upper()
        now = int(time.time())
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO invite_codes (code, label, created_by, uses_count, max_uses, active, created_at)
                VALUES (?, ?, 'admin', 0, ?, 1, ?)
                """,
                (clean_code, label, max_uses, now),
            )
            conn.commit()
        return {"code": clean_code, "label": label, "max_uses": max_uses, "uses_count": 0, "active": 1}
