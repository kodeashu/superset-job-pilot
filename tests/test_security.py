import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("SJP_BUILD_ONLY", "1")

import security
import user_manager as um_mod
import web_server


class TestSecurityControls(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "test_users.db"
        with patch.object(um_mod.UserManager, "_migrate_owner_and_sanitize_files", return_value=None):
            self.test_um = um_mod.UserManager(db_path=self.db_path)
        self._orig_um = web_server.user_manager
        web_server.user_manager = self.test_um
        web_server.app.config["TESTING"] = True
        web_server.rate_limiter.reset()
        self.client = web_server.app.test_client()

        self.owner_email = "owner.student@example.edu"
        self.friend_email = "friend.student@example.edu"

    def tearDown(self):
        web_server.user_manager = self._orig_um
        self._tmp.cleanup()

    def _create_user_and_token(self, email: str, is_admin: bool = False):
        uid = hashlib.sha256(email.encode("utf-8")).hexdigest()[:16]
        with self.test_um._connect() as conn:
            conn.execute(
                """
                INSERT INTO users (
                    user_id, email, password_hash, encrypted_superset_password,
                    superset_uuid, full_name, roll_number, encrypted_profile,
                    auto_apply_enabled, telegram_chat_id, is_admin, token_version, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, 1, ?)
                """,
                (
                    uid,
                    email,
                    um_mod.hash_password("SyntheticPass!123"),
                    um_mod.encrypt_secret("SyntheticPass!123"),
                    "",
                    "Synthetic Student",
                    "00000001",
                    um_mod.encrypt_secret("{}"),
                    "",
                    1 if is_admin else 0,
                    int(time.time()),
                ),
            )
            conn.commit()
        token = self.test_um.create_session(uid, ttl_days=30)
        return uid, token

    def test_host_localhost_does_not_bypass_auth(self):
        with patch.dict(os.environ, {"LOCAL_OWNER_AUTOLOGIN": ""}):
            for ep in ("/api/profile", "/api/status"):
                res = self.client.get(
                    ep,
                    headers={"Host": "localhost"},
                    environ_base={"REMOTE_ADDR": "203.0.113.10"},
                )
                self.assertEqual(res.status_code, 401, f"{ep} should reject Host: localhost with 401")

    def test_owner_only_daemon_toggle_and_invites(self):
        _, friend_tok = self._create_user_and_token(self.friend_email, is_admin=False)
        _, owner_tok = self._create_user_and_token(self.owner_email, is_admin=True)

        with patch.object(web_server.Config, "SUPERSET_EMAIL", self.owner_email):
            res = self.client.post(
                "/api/daemon/toggle",
                headers={"Authorization": f"Bearer {friend_tok}", "X-Requested-With": "sjp"},
            )
            self.assertEqual(res.status_code, 403)

            res_inv_get = self.client.get(
                "/api/invites",
                headers={"Authorization": f"Bearer {friend_tok}"},
            )
            self.assertEqual(res_inv_get.status_code, 403)

            res_inv_bad = self.client.post(
                "/api/invites",
                json={"code": "bad code!@#", "max_uses": 5},
                headers={"Authorization": f"Bearer {owner_tok}", "X-Requested-With": "sjp"},
            )
            self.assertEqual(res_inv_bad.status_code, 400)
            self.assertEqual(res_inv_bad.get_json().get("error_code"), "invalid_invite_code")

            res_inv_ok = self.client.post(
                "/api/invites",
                json={"code": "FRIEND-2027", "max_uses": 999},
                headers={"Authorization": f"Bearer {owner_tok}", "X-Requested-With": "sjp"},
            )
            self.assertEqual(res_inv_ok.status_code, 200)
            inv = res_inv_ok.get_json()["invite"]
            self.assertEqual(inv["code"], "FRIEND-2027")
            self.assertEqual(inv["max_uses"], 50)

            res_inv_min = self.client.post(
                "/api/invites",
                json={"code": "FRIEND-MIN1", "max_uses": -4},
                headers={"Authorization": f"Bearer {owner_tok}", "X-Requested-With": "sjp"},
            )
            self.assertEqual(res_inv_min.status_code, 200)
            self.assertEqual(res_inv_min.get_json()["invite"]["max_uses"], 1)

            orig_state = web_server.daemon_active
            res_owner_toggle = self.client.post(
                "/api/daemon/toggle",
                headers={"Authorization": f"Bearer {owner_tok}", "X-Requested-With": "sjp"},
            )
            self.assertEqual(res_owner_toggle.status_code, 200)
            self.assertEqual(res_owner_toggle.get_json()["active"], not orig_state)
            web_server.daemon_active = orig_state

    def test_login_rate_limiter_triggers_429_on_6th_attempt(self):
        headers = {"X-Requested-With": "sjp", "X-Forwarded-For": "198.51.100.77"}
        with patch.object(
            self.test_um,
            "authenticate_or_register",
            return_value=(False, "Invalid credentials", None, ""),
        ):
            for attempt in range(5):
                res = self.client.post(
                    "/api/auth/login",
                    json={"email": f"nobody{attempt}@example.edu", "password": "wrong"},
                    headers=headers,
                )
                self.assertEqual(res.status_code, 401)

            res6 = self.client.post(
                "/api/auth/login",
                json={"email": "nobody5@example.edu", "password": "wrong"},
                headers=headers,
            )
            self.assertEqual(res6.status_code, 429)
            body = res6.get_json()
            self.assertIn("retry_after", body)
            self.assertGreaterEqual(body["retry_after"], 1)

    def test_security_headers_present_on_responses(self):
        res = self.client.get("/api/health")
        self.assertEqual(res.status_code, 200)
        self.assertIn("Content-Security-Policy", res.headers)
        self.assertIn("frame-ancestors 'none'", res.headers["Content-Security-Policy"])
        self.assertEqual(res.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(res.headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(res.headers.get("Referrer-Policy"), "strict-origin-when-cross-origin")

    def test_csrf_and_cross_site_fetch_metadata_rejected_before_auth(self):
        res_csrf = self.client.post("/api/auth/login", json={"email": "x@example.edu", "password": "y"})
        self.assertEqual(res_csrf.status_code, 403)
        self.assertEqual(res_csrf.get_json().get("error_code"), "csrf_header_missing")

        res_cross = self.client.get("/api/profile", headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(res_cross.status_code, 403)
        self.assertEqual(res_cross.get_json().get("error_code"), "cross_site_forbidden")

    def test_max_content_length_returns_413(self):
        _, tok = self._create_user_and_token(self.friend_email, is_admin=False)
        oversized = b'{"current_address":"' + (b"A" * (6 * 1024 * 1024 + 512)) + b'"}'
        res = self.client.post(
            "/api/profile",
            data=oversized,
            content_type="application/json",
            headers={"Authorization": f"Bearer {tok}", "X-Requested-With": "sjp"},
        )
        self.assertEqual(res.status_code, 413)

    def test_health_endpoint_is_minimal(self):
        res = self.client.get("/api/health")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json(), {"status": "ok"})

    def test_invalid_pdf_proxy_id_returns_400(self):
        _, tok = self._create_user_and_token(self.friend_email, is_admin=False)
        for bad_notice, bad_att in [("..bad", "att1"), ("notice1", "att$bad")]:
            res = self.client.get(
                f"/api/notices/{bad_notice}/attachments/{bad_att}/pdf",
                headers={"Authorization": f"Bearer {tok}"},
            )
            self.assertEqual(res.status_code, 400)
            self.assertEqual(res.get_json().get("error_code"), "invalid_id")

    def test_token_version_revocation_and_logout_all_clear_site_data(self):
        uid, tok = self._create_user_and_token(self.friend_email, is_admin=False)
        self.assertIsNotNone(self.test_um.get_user_by_token(tok))

        res_logout = self.client.post(
            "/api/auth/logout",
            json={"logout_all": True},
            headers={"Authorization": f"Bearer {tok}", "X-Requested-With": "sjp"},
        )
        self.assertEqual(res_logout.status_code, 200)
        self.assertIn('"cookies"', res_logout.headers.get("Clear-Site-Data", ""))
        self.assertIsNone(self.test_um.get_user_by_token(tok))

        tok2 = self.test_um.create_session(uid, ttl_days=30)
        self.assertIsNotNone(self.test_um.get_user_by_token(tok2))
        self.test_um.revoke_all_user_sessions(uid)
        self.assertIsNone(self.test_um.get_user_by_token(tok2))

    def test_cloud_vault_v2_roundtrip_and_legacy_v1_decryption(self):
        email = "vault.student@example.edu"
        pwd = "VaultSyntheticPassword!99"
        payload = {"full_name": "Synthetic User", "current_cgpa": "8.91", "skills": "Python, SQL"}

        vault_id, write_tok, blob_v2 = um_mod.encrypt_cloud_vault_blob(email, pwd, payload)
        self.assertTrue(vault_id)
        self.assertTrue(write_tok)
        self.assertTrue(blob_v2.startswith("cv2."))
        self.assertEqual(um_mod.decrypt_cloud_vault_blob(email, pwd, blob_v2), payload)
        self.assertIsNone(um_mod.decrypt_cloud_vault_blob(email, "wrong-password", blob_v2))

        _, _, enc_key_v1, mac_key_v1 = um_mod.derive_cloud_vault_credentials(
            email, pwd, iterations=um_mod.CLOUD_VAULT_V1_ITERATIONS
        )
        compressed = zlib.compress(json.dumps(payload, separators=(",", ":")).encode("utf-8"), 9)
        nonce = secrets.token_bytes(16)
        stream = um_mod._keystream(enc_key_v1, nonce, len(compressed))
        ciphertext = bytes(a ^ b for a, b in zip(compressed, stream))
        mac = hmac.new(mac_key_v1, nonce + ciphertext, hashlib.sha256).digest()
        blob_v1 = "cv1." + base64.urlsafe_b64encode(nonce + mac + ciphertext).decode("ascii")
        self.assertEqual(um_mod.decrypt_cloud_vault_blob(email, pwd, blob_v1), payload)

    def test_js_esc_neutralizes_xss_payloads(self):
        node_bin = shutil.which("node")
        self.assertIsNotNone(node_bin, "Node.js is required to verify frontend XSS esc()")
        m = re.search(
            r"const esc = s => String\(s \?\? ''\).*?\.replace\(/`/g, '&#96;'\);",
            web_server.HTML_TEMPLATE,
            re.S,
        )
        self.assertIsNotNone(m, "Could not locate const esc helper in HTML_TEMPLATE")
        js = (
            m.group(0)
            + "\n"
            + "const payloads = [\n"
            + "  '\\x22><img src=x onerror=alert(1)>',\n"
            + "  \"' onclick='alert(1)\",\n"
            + "  '`${alert(1)}`\'\n"
            + "];\n"
            + "for (const p of payloads) {\n"
            + "  const out = esc(p);\n"
            + "  if (/[<>\"'`]/.test(out)) { console.error('UNESCAPED:', out); process.exit(1); }\n"
            + "}\n"
            + "console.log('ESC_OK');\n"
        )
        proc = subprocess.run([node_bin, "-e", js], capture_output=True, text=True, check=True)
        self.assertIn("ESC_OK", proc.stdout)


if __name__ == "__main__":
    unittest.main()
