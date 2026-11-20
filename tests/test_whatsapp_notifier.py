"""
Unit & Flask integration tests for WhatsApp Job Opening & Shortlist Venue Alerts.
Uses 100% synthetic student fixtures — zero personal PII.
"""
import json
import tempfile
import time
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytz

from notice_scanner import extract_event_details, notify_new_job_openings_for_user
from user_manager import UserManager, encrypt_secret
from whatsapp_notifier import WhatsAppNotifier, normalize_whatsapp_phone

IST = pytz.timezone("Asia/Kolkata")


def _make_isolated_um_and_user(tmpdir: str, user_id: str = "wa-test-u1"):
    with patch.object(UserManager, "_migrate_owner_and_sanitize_files", return_value=None):
        um = UserManager(db_path=Path(tmpdir) / "vault.sqlite")
    prof = {"full_name": "Rohan Verma", "roll_number": "23050001", "email": "rohan@example.edu"}
    with um._connect() as conn:
        conn.execute(
            """
            INSERT INTO users (
                user_id, email, password_hash, encrypted_superset_password,
                superset_uuid, full_name, roll_number, encrypted_profile,
                auto_apply_enabled, telegram_chat_id, is_admin, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                "rohan@example.edu",
                "hash",
                encrypt_secret("pw"),
                "uuid-1",
                "Rohan Verma",
                "23050001",
                encrypt_secret(json.dumps(prof)),
                0,
                "",
                0,
                int(time.time()),
            ),
        )
        conn.commit()
    return um, user_id


SAMPLE_MULTI_ROLE_TABLE_HTML = """
<p>Shortlisted students for <b>Campus Drive</b> scheduled on <b>21st Nov 2026</b>.</p>
<table>
  <thead>
    <tr>
      <th>Sl. No.</th>
      <th>Profile</th>
      <th>Process</th>
      <th>Reporting Time</th>
      <th>Reporting Venue</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td>1</td>
      <td>B2B Sales Consultant</td>
      <td>Group Discussion &amp; Interview</td>
      <td>9.30 AM</td>
      <td>Ladies Club, Campus-20</td>
    </tr>
    <tr>
      <td>2</td>
      <td>Full Stack Developer &amp; QA Engineer</td>
      <td>Technical Group Discussion involving live programming exercises. &amp; Interview</td>
      <td>9.30 AM</td>
      <td>Computer Labs, Campus-6</td>
    </tr>
  </tbody>
</table>
"""


class TestWhatsAppNotifierUnit(unittest.TestCase):
    def test_phone_normalization(self):
        self.assertEqual(normalize_whatsapp_phone("9876543210"), "919876543210")
        self.assertEqual(normalize_whatsapp_phone("+91 98765-43210"), "919876543210")
        self.assertEqual(normalize_whatsapp_phone("919876543210"), "919876543210")
        self.assertEqual(normalize_whatsapp_phone("+1 (415) 555-0199"), "14155550199")

    def test_callmebot_dispatch_and_error(self):
        wa = WhatsAppNotifier(provider="callmebot", phone="9876543210", api_key="KEY123")
        self.assertTrue(wa.is_configured())

        mock_resp = MagicMock(status_code=200, text="Message queued successfully")
        with patch("whatsapp_notifier.requests.get", return_value=mock_resp) as mock_get:
            ok, msg = wa.send_message("Hello campus", event_type="test")
            self.assertTrue(ok)
            self.assertIn("CallMeBot", msg)
            args, kwargs = mock_get.call_args
            self.assertEqual(args[0], "https://api.callmebot.com/whatsapp.php")
            self.assertEqual(kwargs["params"]["phone"], "919876543210")
            self.assertEqual(kwargs["params"]["apikey"], "KEY123")

        mock_err = MagicMock(status_code=200, text="Error: Invalid APIKey")
        with patch("whatsapp_notifier.requests.get", return_value=mock_err):
            ok, msg = wa.send_message("Hello campus", event_type="test")
            self.assertFalse(ok)
            self.assertIn("Invalid APIKey", msg)

    def test_meta_twilio_and_webhook_providers(self):
        ok_resp = MagicMock(status_code=200, text="{}")

        # Meta Cloud API
        wa_meta = WhatsAppNotifier(
            provider="meta", phone="919876543210",
            meta_phone_number_id="12345", meta_access_token="META_TOK"
        )
        self.assertTrue(wa_meta.is_configured())
        with patch("whatsapp_notifier.requests.post", return_value=ok_resp) as m_post:
            ok, _ = wa_meta.send_message("Meta test")
            self.assertTrue(ok)
            self.assertIn("/12345/messages", m_post.call_args[0][0])
            self.assertEqual(m_post.call_args[1]["json"]["to"], "919876543210")

        # Twilio
        wa_tw = WhatsAppNotifier(
            provider="twilio", phone="919876543210",
            twilio_account_sid="AC123", twilio_auth_token="AUTH", twilio_from="whatsapp:+14155238886"
        )
        self.assertTrue(wa_tw.is_configured())
        with patch("whatsapp_notifier.requests.post", return_value=ok_resp) as m_post:
            ok, _ = wa_tw.send_message("Twilio test")
            self.assertTrue(ok)
            self.assertEqual(m_post.call_args[1]["data"]["To"], "whatsapp:+919876543210")

        # Local QR Bridge
        wa_qr = WhatsAppNotifier(provider="local_qr", bridge_url="http://127.0.0.1:8091", phone="")
        self.assertTrue(wa_qr.is_configured())
        ok_qr_resp = MagicMock(status_code=200)
        ok_qr_resp.json.return_value = {"success": True, "message": "Delivered directly on WhatsApp"}
        with patch("whatsapp_notifier.requests.post", return_value=ok_qr_resp) as m_post:
            ok, msg = wa_qr.send_message("QR bridge test", event_type="shortlist")
            self.assertTrue(ok)
            self.assertIn("WhatsApp", msg)
            self.assertEqual(m_post.call_args[0][0], "http://127.0.0.1:8091/send")
            self.assertEqual(m_post.call_args[1]["json"]["event_type"], "shortlist")

        # Webhook
        wa_wh = WhatsAppNotifier(provider="webhook", phone="919876543210", webhook_url="https://hook.example.com/wa")
        self.assertTrue(wa_wh.is_configured())
        with patch("whatsapp_notifier.requests.post", return_value=ok_resp) as m_post:
            ok, _ = wa_wh.send_message("Hook test", event_type="job_opening")
            self.assertTrue(ok)
            self.assertEqual(m_post.call_args[1]["json"]["event_type"], "job_opening")

    def test_formatted_job_and_shortlist_messages(self):
        wa = WhatsAppNotifier(provider="callmebot", phone="919876543210", api_key="K")
        job_msg = wa.format_job_opening_message(
            {
                "company_name": "AcmeTech",
                "job_role": "Software Engineer",
                "ctc_display": "12 LPA",
                "location": "Bengaluru",
                "deadline_display": "22 Nov 2026, 11:59 PM IST",
                "hours_left": 18.5,
                "is_eligible": True,
                "profile_url": "https://app.joinsuperset.com/students/jobprofiles/job-1",
            },
            cal_url="https://calendar.google.com/test",
            auto_applied=False,
        )
        self.assertIn("NEW SUPERSET JOB OPENING", job_msg)
        self.assertIn("AcmeTech", job_msg)
        self.assertIn("12 LPA", job_msg)
        self.assertIn("ELIGIBLE — ACTION REQUIRED", job_msg)
        self.assertIn("https://calendar.google.com/test", job_msg)

        shortlist_msg = wa.format_shortlist_message(
            {
                "title": "Shortlisted students for Empulz scheduled on 21st Nov 2026",
                "event_details": {
                    "matched_profile": "Full Stack Developer & QA Engineer",
                    "process": "Technical Group Discussion & Interview",
                    "date": "21st Nov 2026",
                    "date_status_label": "Today",
                    "time": "9.30 AM",
                    "venue": "Computer Labs, Campus-6",
                    "meeting_link": "",
                },
            },
            {
                "attachment_name": "Shortlisted_List.pdf",
                "page_number": 3,
                "matched_line": "44 23050001 Rohan Verma Full Stack Developer",
            },
            student_name="Rohan Verma",
            roll_number="23050001",
        )
        self.assertIn("YOU ARE SHORTLISTED!", shortlist_msg)
        self.assertIn("Full Stack Developer & QA Engineer", shortlist_msg)
        self.assertIn("Computer Labs, Campus-6", shortlist_msg)
        self.assertIn("9.30 AM", shortlist_msg)
        self.assertIn("Page 3", shortlist_msg)


class TestRoleSpecificVenueAndDeduplication(unittest.TestCase):
    def test_multi_role_schedule_table_picks_exact_profile_venue(self):
        # Full Stack Developer candidate must get Computer Labs, Campus-6 (NOT Ladies Club, Campus-20!)
        ev_fs = extract_event_details(
            notice_title="Shortlisted students for Empulz scheduled on 21st Nov 2026",
            notice_text="Reporting Venue: Ladies Club, Campus-20",
            matched_line="44 23050001 Rohan Verma Full Stack Developer",
            safe_html=SAMPLE_MULTI_ROLE_TABLE_HTML,
            reference_date=date(2026, 11, 21),
        )
        self.assertEqual(ev_fs["venue"], "Computer Labs, Campus-6")
        self.assertIn("Full Stack Developer", ev_fs["matched_profile"])
        self.assertIn("Technical Group Discussion", ev_fs["process"])
        self.assertEqual(ev_fs["time"], "9.30 AM")
        self.assertFalse(ev_fs["is_past_event"])

        # B2B Sales candidate must get Ladies Club, Campus-20
        ev_sales = extract_event_details(
            notice_title="Shortlisted students for Empulz scheduled on 21st Nov 2026",
            notice_text="Reporting Venue: Ladies Club, Campus-20",
            matched_line="12 23050002 Priya Sen B2B Sales Consultant",
            safe_html=SAMPLE_MULTI_ROLE_TABLE_HTML,
            reference_date=date(2026, 11, 21),
        )
        self.assertEqual(ev_sales["venue"], "Ladies Club, Campus-20")
        self.assertIn("B2B Sales", ev_sales["matched_profile"])

    def test_job_opening_whatsapp_deduplication(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            um, uid = _make_isolated_um_and_user(td, "wa-dedup-u1")
            um.update_user_profile(uid, {
                "whatsapp_config": {
                    "provider": "callmebot",
                    "phone": "919876543210",
                    "api_key": "KEY123",
                    "enabled": True,
                    "notify_jobs": True,
                    "notify_shortlists": True,
                    "notify_offers": True,
                }
            })
            user = um.get_user_by_id(uid)
            jobs = [
                {
                    "id": "job-open-1",
                    "company_name": "OpenCo",
                    "job_role": "SDE",
                    "ctc_display": "10 LPA",
                    "location": "Pune",
                    "deadline_display": "25 Nov 2026",
                    "deadline_dt": datetime(2026, 11, 25, 18, 0, tzinfo=IST).isoformat(),
                    "is_open": True,
                    "is_eligible": True,
                    "is_applied": False,
                    "profile_url": "https://app.joinsuperset.com/students/jobprofiles/job-open-1",
                },
                {
                    "id": "job-closed-2",
                    "company_name": "ClosedCo",
                    "job_role": "SDE",
                    "is_open": False,
                    "is_eligible": True,
                    "is_applied": False,
                },
                {
                    "id": "job-applied-3",
                    "company_name": "AppliedCo",
                    "job_role": "SDE",
                    "is_open": True,
                    "is_eligible": True,
                    "is_applied": True,
                },
            ]

            mock_resp = MagicMock(status_code=200, text="Message queued")
            with patch("whatsapp_notifier.requests.get", return_value=mock_resp) as mock_get:
                sent1 = notify_new_job_openings_for_user(um, user, jobs)
                self.assertEqual(sent1, 1)
                self.assertEqual(mock_get.call_count, 1)
                self.assertIn("OpenCo", mock_get.call_args[1]["params"]["text"])

                # Second call must deduplicate and send 0
                sent2 = notify_new_job_openings_for_user(um, user, jobs)
                self.assertEqual(sent2, 0)
                self.assertEqual(mock_get.call_count, 1)


class TestFlaskWhatsAppApi(unittest.TestCase):
    def test_whatsapp_config_test_and_digest_routes(self):
        import web_server

        client = web_server.app.test_client()
        # Unauthenticated remote must return 401
        r_unauth = client.get("/api/whatsapp/config", headers={"Host": "public.example.com", "X-Forwarded-For": "1.2.3.4"})
        self.assertEqual(r_unauth.status_code, 401)

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            um, uid = _make_isolated_um_and_user(td, "wa-flask-u1")
            um.set_cached_notices(uid, [
                {
                    "id": "n-empulz",
                    "title": "Shortlisted students for Empulz scheduled on 21st Nov 2026",
                    "is_shortlisted": True,
                    "is_past_event": False,
                    "shortlist_match": {
                        "attachment_name": "Shortlisted.pdf",
                        "page_number": 3,
                        "matched_line": "44 23050001 Rohan Verma Full Stack Developer",
                    },
                    "event_details": {
                        "matched_profile": "Full Stack Developer & QA Engineer",
                        "process": "Technical GD & Interview",
                        "date": "21st Nov 2026",
                        "date_status_label": "Today",
                        "time": "9.30 AM",
                        "venue": "Computer Labs, Campus-6",
                    },
                }
            ])
            um.set_cached_jobs(uid, [
                {
                    "id": "j-1",
                    "company_name": "AcmeCloud",
                    "job_role": "Full Stack Engineer",
                    "ctc_display": "14 LPA",
                    "location": "Bengaluru",
                    "deadline_display": "23 Nov 2026",
                    "is_open": True,
                    "is_eligible": True,
                    "is_applied": False,
                    "profile_url": "https://app.joinsuperset.com/students/jobprofiles/j-1",
                }
            ])

            def _get_u():
                return um.get_user_by_id(uid)

            with patch.object(web_server, "user_manager", um), patch.object(web_server, "get_current_user", side_effect=_get_u):
                # 1. Initially unconfigured
                r0 = client.get("/api/whatsapp/config")
                self.assertEqual(r0.status_code, 200)
                self.assertFalse(r0.get_json()["config"]["configured"])

                # 2. Save CallMeBot config
                r1 = client.post("/api/whatsapp/config", json={
                    "provider": "callmebot",
                    "phone": "9876543210",
                    "api_key": "SECRET999",
                    "enabled": True,
                    "notify_jobs": True,
                    "notify_shortlists": True,
                    "notify_offers": True,
                })
                self.assertEqual(r1.status_code, 200)
                cfg1 = r1.get_json()["config"]
                self.assertTrue(cfg1["configured"])
                self.assertEqual(cfg1["phone"], "919876543210")
                self.assertTrue(cfg1["has_api_key"])
                self.assertNotIn("api_key", cfg1)  # Secret is never echoed back!

                # 3. Re-saving toggles with blank api_key preserves existing encrypted key
                r2 = client.post("/api/whatsapp/config", json={
                    "provider": "callmebot",
                    "phone": "919876543210",
                    "api_key": "",
                    "enabled": True,
                    "notify_jobs": True,
                    "notify_shortlists": True,
                    "notify_offers": False,
                })
                self.assertEqual(r2.status_code, 200)
                self.assertTrue(r2.get_json()["config"]["has_api_key"])
                self.assertFalse(r2.get_json()["config"]["notify_offers"])

                # 4. POST /api/whatsapp/test dispatches via CallMeBot
                mock_ok = MagicMock(status_code=200, text="Message queued")
                with patch("whatsapp_notifier.requests.get", return_value=mock_ok) as m_get:
                    rt = client.post("/api/whatsapp/test")
                    self.assertEqual(rt.status_code, 200)
                    self.assertTrue(rt.get_json()["success"])
                    self.assertIn("WhatsApp Connected", m_get.call_args[1]["params"]["text"])

                # 5. POST /api/whatsapp/send-digest sends exact shortlist venue + open job profile!
                with patch("whatsapp_notifier.requests.get", return_value=mock_ok) as m_get, patch("web_server.time.sleep"):
                    rd = client.post("/api/whatsapp/send-digest")
                    self.assertEqual(rd.status_code, 200)
                    dj = rd.get_json()
                    self.assertTrue(dj["success"])
                    self.assertEqual(dj["shortlists_sent"], 1)
                    self.assertEqual(dj["open_jobs_included"], 1)
                    self.assertEqual(m_get.call_count, 2)
                    sent_texts = [c[1]["params"]["text"] for c in m_get.call_args_list]
                    self.assertTrue(any("Computer Labs, Campus-6" in t and "Full Stack Developer" in t for t in sent_texts))
                    self.assertTrue(any("AcmeCloud" in t and "14 LPA" in t for t in sent_texts))

                # 6. POST /api/whatsapp/qr/start auto-arms local_qr and returns QR data URL
                mock_qr_resp = MagicMock(status_code=200, headers={"content-type": "application/json"})
                mock_qr_resp.json.return_value = {
                    "running": True,
                    "connected": False,
                    "state": "qr_ready",
                    "qr_data_url": "data:image/png;base64,TESTQR",
                }
                with patch.object(web_server, "_ensure_whatsapp_bridge_running", return_value=(True, "Bridge online")), \
                     patch("web_server.requests.post", return_value=mock_qr_resp):
                    rqr = client.post("/api/whatsapp/qr/start", json={})
                    self.assertEqual(rqr.status_code, 200)
                    dq = rqr.get_json()
                    self.assertTrue(dq["success"])
                    self.assertEqual(dq["state"], "qr_ready")
                    self.assertIn("TESTQR", dq["qr_data_url"])
                    # Verify local_qr was auto-armed in the user's vault
                    u_after = um.get_user_by_id(uid)
                    self.assertEqual(u_after["profile"]["whatsapp_config"]["provider"], "local_qr")


if __name__ == "__main__":
    unittest.main(verbosity=2)
