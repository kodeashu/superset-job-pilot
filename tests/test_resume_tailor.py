"""
Unit & Flask integration tests for the Company Resume Tailor & 1-Page A4 PDF Studio.
Uses 100% synthetic student fixtures — zero personal PII.
"""
import base64
import io
import unittest
from unittest.mock import MagicMock, patch

from pypdf import PdfReader

from resume_tailor import (
    analyze_jd,
    import_resume_from_pdf,
    render_html_to_pdf_bytes,
    tailor_resume_for_job,
)


def _sample_profile():
    # Minimal 1x1 JPEG bytes encoded as base64 for photo toggle tests
    tiny_png_b64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    return {
        "full_name": "Rohan Verma",
        "email": "rohan@example.edu",
        "phone_number": "+91 90000 00000",
        "current_location": "Bhubaneswar, Odisha",
        "graduation_degree": "B.Tech",
        "graduation_branch": "Computer Science and Engineering",
        "graduation_college": "KIIT University, Bhubaneswar",
        "graduation_passing_year": "2027",
        "current_cgpa": "8.1",
        "active_backlogs": "0",
        "twelfth_college": "Model Senior Secondary",
        "twelfth_board": "CBSE",
        "twelfth_passing_year": "2023",
        "twelfth_percentage": "86.0",
        "tenth_school": "Model High School",
        "tenth_board": "CBSE",
        "tenth_passing_year": "2021",
        "tenth_percentage": "92.0",
        "resume_master": {
            "full_name": "Rohan Verma",
            "email": "rohan@example.edu",
            "phone": "+91 90000 00000",
            "location": "Bhubaneswar, Odisha",
            "github": "https://github.com/rohan-dev",
            "linkedin": "https://linkedin.com/in/rohan-verma",
            "portfolio": "https://rohan-shop.example.com",
            "photo_b64": f"data:image/png;base64,{tiny_png_b64}",
            "skills": [
                {"category": "Languages", "items": "JavaScript, TypeScript, Python, Java, SQL"},
                {"category": "Frontend", "items": "React.js, Next.js, Tailwind CSS, Vite"},
                {"category": "Backend & APIs", "items": "Node.js, Flask, RESTful APIs"},
                {"category": "Databases & Cloud", "items": "PostgreSQL, SQLite, AWS S3, Docker, Git"},
                {"category": "Testing & Tools", "items": "Vitest, Testing Library, unittest"},
            ],
            "projects": [
                {
                    "id": "p-saas",
                    "title": "CampusOps — Multi-User SaaS Console",
                    "link": "https://github.com/rohan-dev/campus-ops",
                    "tech": "Python, Flask, REST APIs, SQLite, JavaScript, AWS S3",
                    "bullets": [
                        "Built a multi-user SaaS portal with PBKDF2-HMAC encrypted accounts and REST APIs.",
                        "Added automated PDF parsing and unittest API suite.",
                    ],
                },
                {
                    "id": "p-ecom",
                    "title": "ShopLive — Production E-Commerce Web App",
                    "link": "https://rohan-shop.example.com",
                    "tech": "React, TypeScript, PostgreSQL, Supabase, AWS S3, Vitest",
                    "bullets": [
                        "Shipped a live client e-commerce app with admin panel and Razorpay integration.",
                        "Wrote component tests with Vitest and Testing Library.",
                    ],
                },
                {
                    "id": "p-ats",
                    "title": "HireTrack — Next.js Recruitment & ATS Portal",
                    "tech": "Next.js, React, TypeScript, PostgreSQL, Node.js API Routes",
                    "bullets": [
                        "Architected an HRMS recruitment portal with 15+ REST API routes and PostgreSQL tables.",
                    ],
                },
                {
                    "id": "p-flutter",
                    "title": "NewsCheck — Flutter AI Fake News App <script>alert(1)</script>",
                    "tech": "Flutter, Dart, Android, Google Gemini API, TensorFlow",
                    "bullets": [
                        "Built a cross-platform Flutter mobile app using Gemini AI and TensorFlow.",
                    ],
                },
            ],
            "certifications": [
                {"title": "Full Stack: Java Spring Boot & Angular", "issuer": "ExcelR", "year": "2025"},
                {"title": "AWS Cloud Foundations", "issuer": "AWS Academy", "year": "2025"},
            ],
        },
    }


class TestResumeTailorCore(unittest.TestCase):
    def test_ranks_fullstack_saas_projects_above_mobile_for_hrms_drive(self):
        prof = _sample_profile()
        jd = (
            "Full Stack Developer for Enterprise HRMS SaaS platform in Bengaluru. "
            "Build responsive React, TypeScript, Next.js frontends, REST APIs with Node.js or Python, "
            "PostgreSQL schemas, AWS S3, and automated testing. Hard copy of resume with photo."
        )
        res = tailor_resume_for_job(prof, company_name="AcmeHR", role="Full Stack Developer", jd_text=jd, max_projects=3, include_photo=True, use_ai=False)

        self.assertEqual(res["track"], "Full Stack Developer")
        self.assertTrue(res["photo_requested_by_jd"])
        self.assertIn("Open to Bengaluru", res["location"])

        selected_ids = [p["id"] for p in res["selected_projects"]]
        self.assertEqual(len(selected_ids), 3)
        # All 3 Full-Stack SaaS/Web projects beat the Flutter mobile app!
        self.assertNotIn("p-flutter", selected_ids)
        self.assertIn("p-ats", selected_ids)
        self.assertIn("p-ecom", selected_ids)
        self.assertIn("p-saas", selected_ids)

        html = res["preview_html"]
        self.assertIn("font-family: Arial, Helvetica, sans-serif", html)
        self.assertIn('<div class="photo-box">', html)
        self.assertNotIn("backlog", html.lower())
        self.assertNotIn("pbkdf2", html.lower())
        self.assertNotIn("hmac", html.lower())
        self.assertGreaterEqual(res["ats_report"]["score"], 60)

    def test_ranks_flutter_ai_first_for_mobile_ai_role_and_escapes_xss(self):
        prof = _sample_profile()
        jd = "Mobile App Developer — Flutter, Dart, Android, Google Gemini AI, TensorFlow"
        res = tailor_resume_for_job(prof, company_name="MobileAI", role="Flutter Mobile Developer", jd_text=jd, max_projects=2, include_photo=False, use_ai=False)

        self.assertEqual(res["selected_projects"][0]["id"], "p-flutter")
        html = res["preview_html"]
        self.assertNotIn('<div class="photo-box">', html)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_chrome_pdf_export_and_pdf_importer_roundtrip(self):
        prof = _sample_profile()
        res = tailor_resume_for_job(prof, company_name="AcmeHR", role="Full Stack Developer", jd_text="React TypeScript PostgreSQL Python REST API", max_projects=3, include_photo=True, use_ai=False)
        pdf_bytes, msg = render_html_to_pdf_bytes(res["preview_html"])
        if msg == "CHROME_UNAVAILABLE":
            self.skipTest("Chrome/Chromium binary not installed in this environment")

        self.assertIsNotNone(pdf_bytes)
        self.assertTrue(pdf_bytes.startswith(b"%PDF-"))
        reader = PdfReader(io.BytesIO(pdf_bytes))
        self.assertEqual(len(reader.pages), 1)
        extracted = reader.pages[0].extract_text() or ""
        self.assertIn("ROHAN VERMA", extracted)
        self.assertNotIn("backlog", extracted.lower())

        # Round-trip PDF importer
        imported = import_resume_from_pdf(pdf_bytes)
        self.assertTrue(imported["success"])
        self.assertIn("github.com/rohan-dev", imported["github"])
        self.assertTrue(len(imported["skills"]) >= 3)
        self.assertTrue(len(imported["projects"]) >= 2)


class TestFlaskResumeApi(unittest.TestCase):
    def test_resume_master_tailor_and_export_routes(self):
        import web_server

        client = web_server.app.test_client()
        # Unauthenticated remote must return 401
        r_unauth = client.get("/api/resume/master", headers={"Host": "public.example.com", "X-Forwarded-For": "1.2.3.4"})
        self.assertEqual(r_unauth.status_code, 401)

        prof = _sample_profile()
        fake_user = {
            "user_id": "resume-test-u1",
            "email": "rohan@example.edu",
            "superset_password": "pw",
            "full_name": "Rohan Verma",
            "roll_number": "23050001",
            "profile": prof,
            "auto_apply_enabled": False,
            "telegram_chat_id": "",
            "is_admin": False,
            "last_sync_at": "",
        }

        with patch.object(web_server, "get_current_user", return_value=fake_user):
            # 1. GET master portfolio
            r1 = client.get("/api/resume/master")
            self.assertEqual(r1.status_code, 200)
            self.assertEqual(len(r1.get_json()["master"]["projects"]), 4)

            # 2. POST tailor resume for custom JD
            r2 = client.post("/api/resume/tailor", json={
                "company_name": "Empulz",
                "role": "Full Stack Developer",
                "custom_jd": "React, TypeScript, Next.js, Python, PostgreSQL, REST APIs, SaaS HRMS",
                "max_projects": 3,
                "include_photo": True,
                "use_ai": False,
            })
            self.assertEqual(r2.status_code, 200)
            d2 = r2.get_json()
            self.assertTrue(d2["success"])
            self.assertEqual(len(d2["tailored"]["selected_projects"]), 3)
            html_out = d2["tailored"]["preview_html"]

            # 3. POST export PDF
            r3 = client.post("/api/resume/export-pdf", json={"html": html_out, "company_name": "Empulz"})
            self.assertIn(r3.status_code, (200, 503))
            if r3.status_code == 200:
                self.assertEqual(r3.mimetype, "application/pdf")
                self.assertTrue(r3.data.startswith(b"%PDF-"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
