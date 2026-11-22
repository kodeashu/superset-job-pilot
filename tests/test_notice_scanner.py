"""
Unit & integration test suite for:
  1. Auto Shortlist Roll-Number PDF Scanner
  2. Smart Notice Feed Categorizer, HTML Sanitizer & Duplicate Spam Filter
  3. Pending Offer Watchdog (/pending_actions)
  4. Authenticated 1-Click PDF Proxy & Notice API Endpoints

All test fixtures use synthetic student identities — zero personal PII.
"""
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from datetime import date
from notice_scanner import (
    classify_notice_category,
    extract_event_details,
    extract_pdf_text_with_pages,
    html_to_plain_text,
    parse_event_date,
    sanitize_notice_html,
    scan_notices_for_user,
    search_student_in_text,
    split_marked_pages,
    summarize_notices,
)
from user_manager import UserManager


def _build_synthetic_pdf(page_texts) -> bytes:
    """Creates a minimal valid multi-page PDF with real extractable text using pypdf."""
    writer = PdfWriter()
    for text in page_texts:
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject({
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        })
        font_ref = writer._add_object(font)
        resources = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})
        })
        page[NameObject("/Resources")] = resources

        stream_lines = ["BT", "/F1 11 Tf", "50 740 Td", "14 TL"]
        for i, line in enumerate(text.splitlines()):
            safe = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            if i == 0:
                stream_lines.append(f"({safe}) Tj")
            else:
                stream_lines.append(f"T* ({safe}) Tj")
        stream_lines.append("ET")

        stream = DecodedStreamObject()
        stream.set_data("\n".join(stream_lines).encode("latin-1", errors="ignore"))
        stream_ref = writer._add_object(stream)
        page[NameObject("/Contents")] = stream_ref

    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


class TestNoticeScannerCore(unittest.TestCase):
    def test_sanitize_notice_html_strips_xss(self):
        dirty = (
            '<p>Drive on 21<sup>st</sup> Nov</p>'
            '<script>alert("xss")</script>'
            '<img src="x" onerror="alert(1)">'
            '<a href="javascript:alert(1)">Bad Link</a>'
            '<a href="https://meet.google.com/abc-defg-hij" onclick="steal()">Join GD</a>'
        )
        clean = sanitize_notice_html(dirty)
        self.assertNotIn("<script", clean.lower())
        self.assertNotIn("onerror", clean.lower())
        self.assertNotIn("onclick", clean.lower())
        self.assertNotIn("javascript:", clean.lower())
        self.assertIn("https://meet.google.com/abc-defg-hij", clean)
        self.assertIn('rel="noopener noreferrer"', clean)

    def test_html_to_plain_text_preserves_inline_sup(self):
        html = "<p>Ladies Club, Campus-20, 6<sup>th</sup> Floor</p><p>9.30 AM</p>"
        plain = html_to_plain_text(html)
        self.assertIn("6th Floor", plain)
        self.assertNotIn("6\nth", plain)

    def test_pdf_extraction_and_roll_number_boundary_matching(self):
        pdf_bytes = _build_synthetic_pdf([
            "DEPARTMENT OF TRAINING & PLACEMENT\nShortlisted Students - 2027 Batch\nSl No Roll No Name",
            "1 230599990 Rahul Verma rahul@example.com Panel-A 4:00 PM",
            "42 23051234 Aarav Sharma aarav@example.com Panel-B 5:00 PM meet.google.com/abc-defg-hij",
        ])
        marked_text, page_count = extract_pdf_text_with_pages(pdf_bytes)
        self.assertEqual(page_count, 3)
        self.assertEqual(len(split_marked_pages(marked_text)), 3)

        # Exact roll match on Page 3
        m = search_student_in_text(marked_text, roll_number="23051234", full_name="Aarav Sharma")
        self.assertIsNotNone(m)
        self.assertEqual(m["match_type"], "EXACT_ROLL")
        self.assertEqual(m["page_number"], 3)
        self.assertIn("Panel-B 5:00 PM", m["matched_line"])

        # Boundary safety: roll 23059999 must NOT match 230599990
        no_false = search_student_in_text(marked_text, roll_number="23059999", full_name="Single")
        self.assertIsNone(no_false)

        # Full name fallback when roll doesn't match
        m_name = search_student_in_text(marked_text, roll_number="99999999", full_name="Rahul Verma")
        self.assertIsNotNone(m_name)
        self.assertEqual(m_name["match_type"], "FULL_NAME")
        self.assertEqual(m_name["page_number"], 2)

    def test_extract_event_details_physical_and_virtual(self):
        # Physical KIIT Campus venue + date + time
        phys = extract_event_details(
            notice_title="AcmeCorp Internship cum PPO Recruitment Drive on 21st Nov'2026 for 2027 Batch",
            notice_text="Reporting Venue : Ladies Club, Campus-20, Rooftop Convention Center, 6th Floor\nReporting Time : 9.30 AM",
        )
        self.assertEqual(phys["event_type"], "Recruitment Drive")
        self.assertIn("21st Nov", phys["date"])
        self.assertIn("9.30 AM", phys["time"])
        self.assertIn("Ladies Club", phys["venue"])

        # Virtual GD where matched student row overrides generic notice time & gives Meet link
        virt = extract_event_details(
            notice_title="BetaTech GD today(20th Nov'2026) for 2027 Batch",
            notice_text="Group Discussion is scheduled today in virtual mode starting 7:30 PM.",
            matched_line="42 23051234 Aarav Sharma Panel-B 5:00 PM meet.google.com/abc-defg-hij",
        )
        self.assertEqual(virt["event_type"], "Group Discussion")
        self.assertEqual(virt["time"], "5:00 PM")
        self.assertEqual(virt["venue"], "Virtual / Online Mode")
        self.assertEqual(virt["meeting_link"], "https://meet.google.com/abc-defg-hij")

    def test_classify_notice_categories(self):
        cat1, spam1 = classify_notice_category({"title": "Superset Android App Available in Beta", "lastModifiedByUserName": "Superset", "type": "NOTICE"})
        self.assertEqual((cat1, spam1), ("system_spam", True))

        cat2, spam2 = classify_notice_category({"title": "Open for applications - AcmeCorp's Job Profile - SDE"})
        self.assertEqual((cat2, spam2), ("job_openings", False))

        cat3, spam3 = classify_notice_category({"title": "Extension Notice- Registration for Hyundai Motor India"})
        self.assertEqual((cat3, spam3), ("deadlines_revisions", False))

        cat4, spam4 = classify_notice_category({"title": "DeltaX Recruitment Drive - Online Test on 20th Nov'2026"})
        self.assertEqual((cat4, spam4), ("shortlists_tests", False))

    def test_parse_event_date_filters_past_dates_off_homepage(self):
        ref = date(2026, 11, 21)
        # Today (21st Nov'2026) -> NOT past, shows Today
        d_today = parse_event_date("21st Nov'2026", reference_date=ref)
        self.assertFalse(d_today["is_past_event"])
        self.assertEqual(d_today["date_status_label"], "Today")
        self.assertEqual(d_today["event_date_iso"], "2026-11-21")

        # Past multi-day range (18th & 19th Nov'2026) -> Passed!
        d_past_range = parse_event_date("18th & 19th Nov'2026", reference_date=ref)
        self.assertTrue(d_past_range["is_past_event"])
        self.assertEqual(d_past_range["event_date_iso"], "2026-11-19")
        self.assertEqual(d_past_range["date_status_label"], "Passed")

        # Active multi-day range (20th & 21st Nov'2026) -> NOT past on day 2!
        d_active_range = parse_event_date("20th & 21st Nov'2026", reference_date=ref)
        self.assertFalse(d_active_range["is_past_event"])
        self.assertEqual(d_active_range["date_status_label"], "Today")

        # summarize_notices excludes expired shortlists from upcoming_shortlisted_notices
        summary = summarize_notices(
            notices=[
                {"id": "n-today", "is_shortlisted": True, "category": "shortlists_tests", "event_details": {"date": "21st Nov'2026"}},
                {"id": "n-old", "is_shortlisted": True, "category": "shortlists_tests", "event_details": {"date": "15th Nov'2026"}},
            ],
            pending_actions=[],
            reference_date=ref,
        )
        self.assertEqual(summary["counts"]["shortlisted_upcoming"], 1)
        self.assertEqual(summary["counts"]["shortlisted_past"], 1)
        self.assertEqual([n["id"] for n in summary["upcoming_shortlisted_notices"]], ["n-today"])
        self.assertEqual([n["id"] for n in summary["past_shortlisted_notices"]], ["n-old"])


class TestEndToEndNoticeOrchestrator(unittest.TestCase):
    def setUp(self):
        td_kwargs = {"ignore_cleanup_errors": True} if sys.version_info >= (3, 10) else {}
        self.tmpdir = tempfile.TemporaryDirectory(**td_kwargs)
        self.db_path = Path(self.tmpdir.name) / "test_vault.sqlite"
        # Prevent migrating host owner into test DB
        with patch.object(UserManager, "_migrate_owner_and_sanitize_files", return_value=None):
            self.um = UserManager(db_path=self.db_path)

        self.user = {
            "user_id": "test-user-1",
            "email": "23051234@kiit.ac.in",
            "full_name": "Aarav Sharma",
            "roll_number": "23051234",
            "profile": {"roll_number": "23051234", "full_name": "Aarav Sharma"},
            "telegram_chat_id": "123456",
        }
        self.pdf_bytes = _build_synthetic_pdf([
            "KIIT T&P Shortlisted list for DeltaX Online Test on 22nd Nov'2026",
            "12 23051234 Aarav Sharma 23051234@kiit.ac.in Computer Lab-2, Campus-6 4:30 PM",
        ])

    def tearDown(self):
        import gc
        gc.collect()
        try:
            self.tmpdir.cleanup()
        except Exception:
            pass

    def test_scan_caches_pdf_dedupes_spam_and_alerts_once(self):
        mock_client = MagicMock()
        mock_client.fetch_notices.return_value = [
            {
                "identifier": "n-promo",
                "title": "Superset Android App Available in Beta",
                "content": "<p>Download beta app</p>",
                "lastModifiedByUserName": "Superset",
                "type": "NOTICE",
                "publishedAt": 1795100000000,
                "attachments": [],
            },
            {
                "identifier": "n-shortlist",
                "title": "DeltaX Recruitment Drive - Online Test on 22nd Nov'2026",
                "content": "<p>Online test scheduled on 22nd Nov'2026 at Computer Lab-2, Campus-6 at 4:30 PM.</p>",
                "lastModifiedByUserName": "TNP Coordinator",
                "type": "MESSAGE",
                "publishedAt": 1795175428000,
                "attachments": [{"identifier": "att-pdf-1", "name": "Shortlisted list for DeltaX 22nd Nov'2026.pdf", "type": "DOC"}],
            },
            # Exact duplicate spam post
            {
                "identifier": "n-job-1",
                "title": "Open for applications - Acme's Job Profile - SDE",
                "content": "<p>Apply now</p>",
                "lastModifiedByUserName": "TNP Coordinator",
                "type": "MESSAGE",
                "publishedAt": 1795170000000,
                "attachments": [],
            },
            {
                "identifier": "n-job-dup",
                "title": "Open for applications - Acme's Job Profile - SDE",
                "content": "<p>Apply now</p>",
                "lastModifiedByUserName": "TNP Coordinator",
                "type": "MESSAGE",
                "publishedAt": 1795160000000,
                "attachments": [],
            },
        ]
        mock_client.download_notice_attachment.return_value = (self.pdf_bytes, "application/pdf", "https://s3.example/doc.pdf")
        mock_client.fetch_pending_actions.return_value = [
            {
                "id": "offer-1",
                "company_name": "AcmeCorp",
                "title": "Software Engineer Offer",
                "action_type": "JOB_OFFER",
                "deadline_display": "25 Nov 2026, 05:00 PM IST",
                "hours_left": 18.5,
                "is_urgent": True,
                "portal_url": "https://app.joinsuperset.com/students/jobprofiles/jp-1",
            }
        ]

        with patch("notice_scanner.TelegramNotifier") as MockTG:
            tg_inst = MockTG.return_value
            tg_inst.send_shortlist_alert.return_value = True
            tg_inst.send_pending_offer_alert.return_value = True

            # First pass: downloads PDF, detects shortlist & offer, sends 1 shortlist + 1 offer alert
            res1 = scan_notices_for_user(mock_client, self.um, self.user, send_alerts=True)
            self.assertEqual(res1["counts"]["shortlisted"], 1)
            self.assertEqual(res1["counts"]["pending_offers"], 1)
            self.assertEqual(res1["counts"]["spam_hidden"], 2)  # 1 system promo + 1 duplicate
            self.assertEqual(res1["counts"]["clean"], 2)
            self.assertEqual(res1["scan_stats"]["pdfs_downloaded_now"], 1)
            self.assertEqual(res1["scan_stats"]["pdfs_from_cache"], 0)
            self.assertEqual(res1["scan_stats"]["new_shortlist_alerts"], 1)
            self.assertEqual(res1["scan_stats"]["new_offer_alerts"], 1)

            hit = res1["shortlisted_notices"][0]
            self.assertEqual(hit["shortlist_match"]["page_number"], 2)
            self.assertEqual(hit["shortlist_match"]["match_type"], "EXACT_ROLL")
            self.assertIn("Campus-6", hit["event_details"]["venue"])

            # Second pass: uses SQLite PDF cache (0 S3 downloads) & sends 0 duplicate alerts!
            res2 = scan_notices_for_user(mock_client, self.um, self.user, send_alerts=True)
            self.assertEqual(res2["scan_stats"]["pdfs_downloaded_now"], 0)
            self.assertEqual(res2["scan_stats"]["pdfs_from_cache"], 1)
            self.assertEqual(res2["scan_stats"]["new_shortlist_alerts"], 0)
            self.assertEqual(res2["scan_stats"]["new_offer_alerts"], 0)
            self.assertEqual(mock_client.download_notice_attachment.call_count, 1)


class TestFlaskNoticesApi(unittest.TestCase):
    def test_notices_pending_and_pdf_proxy_routes(self):
        import web_server

        app = web_server.app
        client = app.test_client()

        # Unauthenticated remote host must get 401
        resp_unauth = client.get("/api/notices", headers={"Host": "public.example.com", "X-Forwarded-For": "1.2.3.4"})
        self.assertEqual(resp_unauth.status_code, 401)

        fake_user = {
            "user_id": "flask-test-user",
            "email": "23051234@kiit.ac.in",
            "superset_password": "pw",
            "full_name": "Aarav Sharma",
            "roll_number": "23051234",
            "profile": {"roll_number": "23051234", "full_name": "Aarav Sharma"},
            "auto_apply_enabled": False,
            "telegram_chat_id": "",
            "is_admin": False,
            "last_sync_at": "",
        }
        fake_pdf = _build_synthetic_pdf(["Shortlist Page 1\n23051234 Aarav Sharma"])
        fake_superset = MagicMock()
        fake_superset.login.return_value = True
        fake_superset.download_notice_attachment.return_value = (fake_pdf, "application/pdf", "https://s3.example/a.pdf")
        fake_superset.fetch_pending_actions.return_value = [
            {
                "id": "offer-99",
                "company_name": "TestCorp",
                "title": "SDE-1",
                "deadline_display": "28 Nov 2026, 05:00 PM IST",
                "hours_left": 24.0,
                "is_urgent": True,
                "portal_url": "https://app.joinsuperset.com/students/",
            }
        ]

        with patch.object(web_server, "get_current_user", return_value=fake_user), \
             patch.object(web_server, "_get_superset_for_user", return_value=fake_superset), \
             patch.object(web_server.user_manager, "get_cached_notices", return_value=([{"id": "n1", "title": "Test", "attachments": [{"id": "a1", "name": "Empulz 21st Nov'2026.pdf"}]}], 9999999999)), \
             patch.object(web_server.user_manager, "get_cached_pending_actions", return_value=([], 0)), \
             patch.object(web_server.user_manager, "set_cached_pending_actions", return_value=None):
            # Inline PDF preview
            pdf_res = client.get("/api/notices/n1/attachments/a1/pdf")
            self.assertEqual(pdf_res.status_code, 200)
            self.assertEqual(pdf_res.mimetype, "application/pdf")
            self.assertIn("inline;", pdf_res.headers.get("Content-Disposition", ""))
            self.assertTrue(pdf_res.data.startswith(b"%PDF-"))

            # Attachment download mode
            dl_res = client.get("/api/notices/n1/attachments/a1/pdf?download=1")
            self.assertEqual(dl_res.status_code, 200)
            self.assertIn("attachment;", dl_res.headers.get("Content-Disposition", ""))

            # Pending Offer Watchdog refresh
            pa_res = client.get("/api/pending-actions?refresh=1")
            self.assertEqual(pa_res.status_code, 200)
            pa_json = pa_res.get_json()
            self.assertEqual(pa_json["count"], 1)
            self.assertEqual(pa_json["pending_actions"][0]["company_name"], "TestCorp")


if __name__ == "__main__":
    unittest.main(verbosity=2)
