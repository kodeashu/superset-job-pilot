"""
Unit & Flask integration tests for:
  1. Zero-Knowledge Cloud Vault compression + Encrypt-then-MAC (`user_manager.py`)
  2. Custom Question Option-Validity & Role/Certification/Resume resolution (`attribute_resolver.py`)
  3. Editable Review & Apply Flask routes (/api/jobs/<id>/preview-application & /save-answers)
Uses 100% synthetic student fixtures — zero personal PII.
"""
import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from attribute_resolver import AttributeResolver
from user_manager import (
    UserManager,
    decrypt_cloud_vault_blob,
    encrypt_cloud_vault_blob,
    encrypt_secret,
    hash_password,
)


def _synthetic_full_profile():
    tiny_png_b64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    return {
        "full_name": "Rohan Verma",
        "email": "rohan@example.edu",
        "roll_number": "23050001",
        "dob": "2005-04-12",
        "fathers_name": "Suresh Verma",
        "mothers_name": "Meena Verma",
        "phone_number": "9000000001",
        "emergency_contact_number": "9000000002",
        "permanent_address": "123 Park Street, Bhubaneswar, Odisha",
        "current_cgpa": "8.45",
        "tenth_percentage": "94.2",
        "twelfth_percentage": "91.8",
        "active_backlogs": "0",
        "pan_card_number": "ABCDE1234F",
        "resume_link": "https://drive.google.com/file/d/synthetic-resume-id/view",
        "technical_skills": "Python, FastAPI, React, SQL, Cloud & Agentic AI Systems",
        "graduation_degree": "B.Tech",
        "resume_master": {
            "full_name": "Rohan Verma",
            "photo_b64": f"data:image/png;base64,{tiny_png_b64}",
            "projects": [
                {"id": "p1", "title": "CampusOps", "tech": "Python, React, AWS", "bullets": ["Built SaaS portal."]}
            ],
            "certifications": [
                {"title": "AWS Cloud Foundations", "issuer": "AWS Academy", "year": "2025"}
            ],
        },
        "custom_clarifications": {},
    }


class TestZeroKnowledgeCloudVault(unittest.TestCase):
    def test_cloud_vault_compress_encrypt_decrypt_roundtrip_and_tamper(self):
        prof = _synthetic_full_profile()
        email = "rohan@example.edu"
        pw = "SupersetPass!99"

        vault_id, write_token, blob = encrypt_cloud_vault_blob(email, pw, prof)
        self.assertEqual(len(vault_id), 64)
        self.assertEqual(len(write_token), 64)
        self.assertTrue(blob.startswith("cv1."))

        restored = decrypt_cloud_vault_blob(email, pw, blob)
        self.assertIsNotNone(restored)
        self.assertEqual(restored["dob"], "2005-04-12")
        self.assertEqual(restored["fathers_name"], "Suresh Verma")
        self.assertEqual(restored["resume_link"], prof["resume_link"])
        self.assertEqual(len(restored["resume_master"]["projects"]), 1)
        self.assertTrue(restored["resume_master"]["photo_b64"].startswith("data:image/png;base64,"))

        # Wrong password must fail decryption
        self.assertIsNone(decrypt_cloud_vault_blob(email, "WrongPassword", blob))

        # Byte tampering must fail HMAC verification
        flipped_char = "A" if blob[-2] != "A" else "B"
        tampered = blob[:-2] + flipped_char + blob[-1]
        self.assertIsNone(decrypt_cloud_vault_blob(email, pw, tampered))


class TestAttributeResolverAccuracy(unittest.TestCase):
    def test_certification_yes_no_returns_yes_not_free_text(self):
        prof = _synthetic_full_profile()
        resolver = AttributeResolver(profile_data=prof)

        attr_cert = {
            "attrKey": "q_cert",
            "name": "Do you have done any certification cousre as mentioned in the notice.",
            "attrType": "select:single",
            "mandatory": True,
            "options": json.dumps(["Yes", "No"]),
        }
        ans = resolver.resolve_attribute(attr_cert)
        self.assertEqual(ans, "Yes")
        self.assertIn(ans, ["Yes", "No"])

    def test_resume_link_and_missing_link_flagging(self):
        prof = _synthetic_full_profile()
        resolver = AttributeResolver(profile_data=prof)

        job_payload = {
            "jobProfile": {
                "attributeList": [
                    {
                        "attrKey": "q_resume",
                        "name": "Share your updated resume link with public access",
                        "attrType": "string",
                        "mandatory": True,
                    },
                    {
                        "attrKey": "q_profile",
                        "name": "Profile Applied for",
                        "attrType": "select:single",
                        "mandatory": True,
                        "options": json.dumps([
                            "B2B Sales Executive",
                            "AWS Application Developer",
                            "Azure Application Developer",
                            "Data Engineer",
                        ]),
                    },
                    {
                        "attrKey": "q_cert",
                        "name": "Do you have done any certification cousre as mentioned in the notice.",
                        "attrType": "select:single",
                        "mandatory": True,
                        "options": json.dumps(["Yes", "No"]),
                    },
                ]
            }
        }
        preview = resolver.preview_answers_for_job(job_payload)
        self.assertEqual(len(preview), 3)
        by_key = {item["key"]: item for item in preview}

        # Resume link resolved and marked answered
        self.assertEqual(by_key["q_resume"]["answer"], prof["resume_link"])
        self.assertTrue(by_key["q_resume"]["is_answered"])
        self.assertFalse(by_key["q_resume"]["needs_review"])

        # Role selection avoids B2B Sales, selects AWS Application Developer (in options), marks needs_review until edited
        self.assertEqual(by_key["q_profile"]["answer"], "AWS Application Developer")
        self.assertTrue(by_key["q_profile"]["needs_review"])

        # Certification Yes/No resolves to Yes
        self.assertEqual(by_key["q_cert"]["answer"], "Yes")

        # Empty resume_link in profile MUST mark is_answered=False and needs_review=True (not 100% answered!)
        prof_empty = dict(prof)
        prof_empty["resume_link"] = ""
        res_empty = AttributeResolver(profile_data=prof_empty)
        prev_empty = {i["key"]: i for i in res_empty.preview_answers_for_job(job_payload)}
        self.assertEqual(prev_empty["q_resume"]["answer"], "")
        self.assertFalse(prev_empty["q_resume"]["is_answered"])
        self.assertTrue(prev_empty["q_resume"]["needs_review"])

    def test_any_option_question_is_always_constrained_to_valid_option(self):
        resolver = AttributeResolver(profile_data=_synthetic_full_profile())
        attr = {
            "name": "Some unexpected question about technical skills",
            "attrType": "select:single",
            "options": ["Option Alpha", "Option Beta"],
        }
        ans = resolver.resolve_attribute(attr)
        self.assertIn(ans, ["Option Alpha", "Option Beta"])


class TestFlaskPreviewAndSaveAnswersApi(unittest.TestCase):
    def test_preview_returns_full_profile_and_save_answers_merges_vault(self):
        import web_server

        client = web_server.app.test_client()
        td_kwargs = {"ignore_cleanup_errors": True} if sys.version_info >= (3, 10) else {}
        with tempfile.TemporaryDirectory(**td_kwargs) as td:
            with patch.object(UserManager, "_migrate_owner_and_sanitize_files", return_value=None):
                um = UserManager(db_path=Path(td) / "vault.sqlite")
            prof = _synthetic_full_profile()
            with um._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO users (
                        user_id, email, password_hash, encrypted_superset_password,
                        superset_uuid, full_name, roll_number, encrypted_profile,
                        auto_apply_enabled, telegram_chat_id, is_admin, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, '', 0, 1000)
                    """,
                    (
                        "edit-u1",
                        "rohan@example.edu",
                        hash_password("pw"),
                        encrypt_secret("pw"),
                        "uuid-1",
                        "Rohan Verma",
                        "23050001",
                        encrypt_secret(json.dumps(prof)),
                    ),
                )
                conn.commit()

            def _get_u():
                return um.get_user_by_id("edit-u1")

            mock_sc = MagicMock()
            mock_sc.login.return_value = True
            mock_sc.base_url = "https://app.joinsuperset.com"
            mock_sc.user_uuid = "uuid-1"
            detail_resp = MagicMock(status_code=200)
            detail_resp.json.return_value = {
                "companyName": "AbsoluteLabs",
                "jobProfileTitle": "Cloud Engineer",
                "ctcDisplay": "12 LPA",
                "jobProfile": {
                    "jobProfileStatus": "OPEN",
                    "attributeList": [
                        {
                            "attrKey": "q_resume",
                            "name": "Share your updated resume link with public access",
                            "attrType": "string",
                            "mandatory": True,
                        },
                        {
                            "attrKey": "q_cert",
                            "name": "Do you have done any certification cousre as mentioned in the notice.",
                            "attrType": "select:single",
                            "mandatory": True,
                            "options": json.dumps(["Yes", "No"]),
                        },
                    ],
                },
            }
            consent_resp = MagicMock(status_code=200)
            consent_resp.json.return_value = {"required": False}
            mock_sc.session.get.side_effect = [detail_resp, consent_resp]

            with patch.object(web_server, "user_manager", um), \
                 patch.object(web_server, "get_current_user", side_effect=_get_u), \
                 patch.object(web_server, "_get_superset_for_user", return_value=mock_sc):

                # 1. Preview returns full_profile (without photo/whatsapp secrets) + accurate answers
                rp = client.get("/api/jobs/job-abs/preview-application")
                self.assertEqual(rp.status_code, 200)
                dp = rp.get_json()
                self.assertTrue(dp["success"])
                self.assertEqual(dp["full_profile"]["fathers_name"], "Suresh Verma")
                self.assertNotIn("resume_master", dp["full_profile"])
                q_map = {q["key"]: q for q in dp["questions"]}
                self.assertEqual(q_map["q_cert"]["answer"], "Yes")
                self.assertEqual(q_map["q_resume"]["answer"], prof["resume_link"])

                # 2. Save user-edited answers & profile fields before applying
                new_link = "https://drive.google.com/file/d/updated-link/view"
                rs = client.post("/api/jobs/job-abs/save-answers", json={
                    "profile_updates": {"resume_link": new_link, "fathers_name": "S. K. Verma"},
                    "clarifications_updates": {"Profile Applied for": "Azure Application Developer"},
                })
                self.assertEqual(rs.status_code, 200)
                ds = rs.get_json()
                self.assertTrue(ds["success"])
                self.assertEqual(ds["profile"]["resume_link"], new_link)
                self.assertEqual(ds["profile"]["fathers_name"], "S. K. Verma")
                self.assertEqual(
                    ds["profile"]["custom_clarifications"]["Profile Applied for"],
                    "Azure Application Developer",
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
