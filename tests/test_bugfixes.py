"""
Comprehensive Test Suite for Superset Job Pilot Bug Fixes.
Covers:
1. Strict password authentication & hash protection in UserManager.
2. Multi-User partitioning in AutoApplier SQLite tracking.
3. Clarification resolution persistence into encrypted UserManager vault.
4. Active drive status ('IN_PROCESS') classification in SupersetClient.
5. Accurate fallback logic in AttributeResolver for empty string profile attributes.
"""

import gc
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure project root is in sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from user_manager import UserManager, hash_password, verify_password, encrypt_secret
from auto_applier import AutoApplier
from ambiguity_manager import AmbiguityManager
from superset_client import SupersetClient
from attribute_resolver import AttributeResolver


class TestBugFixes(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.test_vault_path = Path(self.temp_dir) / "test_users_vault.sqlite"
        self.test_jobs_path = Path(self.temp_dir) / "test_superset_jobs.sqlite"

    def tearDown(self):
        gc.collect()
        try:
            shutil.rmtree(self.temp_dir, ignore_errors=True)
        except Exception:
            pass

    # --------------------------------------------------------------------------
    # Bug 1: Strict Password Verification & Backdoor Elimination
    # --------------------------------------------------------------------------
    @patch("telegram_notifier.TelegramNotifier.send_message", return_value=True)
    @patch("superset_client.SupersetClient.login", return_value=False)
    def test_strict_password_verification(self, mock_login, mock_tg):
        """Verify that fuzzy passwords and casing errors are rejected without hash overwrite."""
        with patch("user_manager.VAULT_DB_PATH", self.test_vault_path), \
             patch("user_manager.EXAMPLE_PROFILE_PATH", Path(self.temp_dir) / "example.json"):
            um = UserManager(db_path=self.test_vault_path)

            uid = "user_test_123"
            real_pw = "SecureP@ssw0rd!"
            email = "student@kiit.ac.in"
            
            with um._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO users (
                        user_id, email, password_hash, encrypted_superset_password,
                        superset_uuid, full_name, roll_number, encrypted_profile,
                        auto_apply_enabled, telegram_chat_id, is_admin, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1000)
                    """,
                    (
                        uid,
                        email,
                        hash_password(real_pw),
                        encrypt_secret(real_pw),
                        "uuid-123",
                        "Test Student",
                        "2105123",
                        encrypt_secret(json.dumps({})),
                        1,
                        "",
                    )
                )
                conn.commit()

            # 1. Exact password must succeed
            ok, msg, u, token = um.authenticate_or_register(email, real_pw)
            self.assertTrue(ok, "Exact password should authenticate successfully")
            self.assertIsNotNone(u)

            # 2. Fuzzy similar password (e.g. 80% similarity or substring) must FAIL
            similar_pw = "SecureP@ssw0rd?"  # One char off
            ok, msg, u, token = um.authenticate_or_register(email, similar_pw)
            self.assertFalse(ok, "Fuzzy similar password must NOT authenticate")

            # 3. Substring password must FAIL
            substring_pw = "SecureP@ssw0rd"
            ok, msg, u, token = um.authenticate_or_register(email, substring_pw)
            self.assertFalse(ok, "Substring password must NOT authenticate")

            # 4. Wrong case must FAIL
            wrong_case_pw = real_pw.lower()
            ok, msg, u, token = um.authenticate_or_register(email, wrong_case_pw)
            self.assertFalse(ok, "Wrong case password must NOT authenticate")

            # 5. Verify stored hash was NEVER overwritten by failed attempts
            with um._connect() as conn:
                row = conn.execute("SELECT password_hash FROM users WHERE user_id = ?", (uid,)).fetchone()
            self.assertTrue(verify_password(real_pw, row["password_hash"]), "Password hash must remain intact")

    # --------------------------------------------------------------------------
    # Bug 2: Multi-User Partitioning in SQLite Job Tracking
    # --------------------------------------------------------------------------
    def test_multi_user_job_tracking_isolation(self):
        """Verify that jobs recorded for User A do not block User B."""
        with patch("config.Config.DB_PATH", self.test_jobs_path):
            applier_a = AutoApplier(user_id="user_A")
            applier_b = AutoApplier(user_id="user_B")

            job = {
                "id": "job_google_swe_2026",
                "company_name": "Google",
                "job_role": "Software Engineer",
                "ctc_display": "₹32 LPA",
                "deadline_display": "30 Nov 2026",
                "is_eligible": True,
            }

            # Initially neither has recorded the job
            self.assertFalse(applier_a.is_job_recorded("job_google_swe_2026"))
            self.assertFalse(applier_b.is_job_recorded("job_google_swe_2026"))

            # User A records the job
            applier_a.record_job(job, status="AUTO_APPLIED", applied=True)

            # User A should now have it recorded, but User B MUST NOT have it recorded
            self.assertTrue(applier_a.is_job_recorded("job_google_swe_2026"))
            self.assertFalse(applier_b.is_job_recorded("job_google_swe_2026"))
            self.assertEqual(applier_a.get_tracked_count(), 1)
            self.assertEqual(applier_b.get_tracked_count(), 0)

            # User B records the same job separately
            applier_b.record_job(job, status="ELIGIBLE_PENDING", applied=False)
            self.assertTrue(applier_b.is_job_recorded("job_google_swe_2026"))
            self.assertEqual(applier_b.get_tracked_count(), 1)

            # User summaries should be isolated
            summary_a = applier_a.get_tracked_summary()
            summary_b = applier_b.get_tracked_summary()
            self.assertEqual(summary_a[0]["applied_status"], "AUTO_APPLIED")
            self.assertEqual(summary_b[0]["applied_status"], "ELIGIBLE_PENDING")

    # --------------------------------------------------------------------------
    # Bug 3: Clarification Ticket Persistence into Encrypted Vault
    # --------------------------------------------------------------------------
    @patch("telegram_notifier.TelegramNotifier.send_message", return_value=True)
    def test_clarification_resolution_persists_to_vault(self, mock_tg):
        """Verify that resolving a ticket saves the answer to the user's encrypted vault profile."""
        with patch("user_manager.VAULT_DB_PATH", self.test_vault_path), \
             patch("user_manager.EXAMPLE_PROFILE_PATH", Path(self.temp_dir) / "example.json"):
            um = UserManager(db_path=self.test_vault_path)

            uid = "user_clarif_test"
            with um._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO users (
                        user_id, email, password_hash, encrypted_superset_password,
                        superset_uuid, full_name, roll_number, encrypted_profile,
                        auto_apply_enabled, telegram_chat_id, is_admin, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1000)
                    """,
                    (
                        uid,
                        "clarif@kiit.ac.in",
                        hash_password("Pass123"),
                        encrypt_secret("Pass123"),
                        "uuid-456",
                        "Clarif Student",
                        "2105999",
                        encrypt_secret(json.dumps({"custom_clarifications": {}})),
                        1,
                        "",
                    )
                )
                conn.commit()

            am = AmbiguityManager()
            am.tickets.clear()

            question = {
                "name": "Which cloud certifications have you completed?",
                "options": ["AWS Solutions Architect", "Google Cloud Associate", "Azure Fundamentals"],
                "type": "string"
            }
            ticket = am.evaluate_and_flag(
                company_name="Amazon",
                job_id="job_amz_1",
                question=question,
                resolved_value=None,
                user_id=uid
            )
            self.assertIsNotNone(ticket)
            ticket_id = ticket["ticket_id"]

            resolved_ticket = am.resolve_ticket(
                ticket_id=ticket_id,
                answer="AWS Solutions Architect",
                user_id=uid
            )
            self.assertEqual(resolved_ticket["status"], "RESOLVED")
            self.assertEqual(resolved_ticket["resolved_answer"], "AWS Solutions Architect")

            # Check that user's profile in encrypted UserManager vault was updated
            user_updated = um.get_user_by_id(uid)
            custom_clarifs = user_updated["profile"].get("custom_clarifications", {})
            self.assertIn("Which cloud certifications have you completed?", custom_clarifs)
            self.assertEqual(custom_clarifs["Which cloud certifications have you completed?"], "AWS Solutions Architect")

            # Also check that AttributeResolver uses this resolved clarification
            resolver = AttributeResolver(user_id=uid, profile_data=user_updated["profile"])
            ans = resolver.resolve_attribute(question)
            self.assertEqual(ans, "AWS Solutions Architect")

    # --------------------------------------------------------------------------
    # Bug 4: Active Drive Status ('IN_PROCESS') Recognition
    # --------------------------------------------------------------------------
    def test_in_process_drive_status_classification(self):
        """Verify that jobs with jobProfileStatus 'IN_PROCESS' are open and not marked closed."""
        sc = SupersetClient(email="test@kiit.ac.in", password="test")
        
        raw_in_process = {
            "jobProfileIdentifier": "job_tcs_digital_2026",
            "companyName": "Tata Consultancy Services",
            "jobProfileTitle": "Digital Innovator",
            "placementCategoryName": "₹7.50 LPA",
            "jobProfileLocation": "Pan India",
            "jobApplicationStatus": "YET_TO_APPLY",
            "jobProfileStatus": "IN_PROCESS",
            "jobProfileApplicationDeadline": 9999999999000,
            "ageRestricted": False
        }
        normalized_open = sc._normalize_job(raw_in_process)
        self.assertTrue(normalized_open["is_open"], "IN_PROCESS drive should be open for application")
        self.assertFalse(normalized_open["is_closed"], "IN_PROCESS drive should NOT be marked closed")

        raw_closed = {
            "jobProfileIdentifier": "job_wipro_2026",
            "companyName": "Wipro",
            "jobProfileTitle": "Project Engineer",
            "jobApplicationStatus": "YET_TO_APPLY",
            "jobProfileStatus": "CLOSED_FOR_APPLICATIONS",
            "jobProfileApplicationDeadline": 9999999999000,
            "ageRestricted": False
        }
        normalized_closed = sc._normalize_job(raw_closed)
        self.assertFalse(normalized_closed["is_open"], "CLOSED_FOR_APPLICATIONS drive should not be open")
        self.assertTrue(normalized_closed["is_closed"], "CLOSED_FOR_APPLICATIONS drive should be closed")

    # --------------------------------------------------------------------------
    # Bug 5: Attribute Resolver Empty String Fallback & Handling
    # --------------------------------------------------------------------------
    def test_attribute_resolver_empty_string_fallbacks(self):
        """Verify that empty strings in profile attributes properly fall back to secondary fields."""
        profile = {
            "full_name": "Aayush Kumar",
            "tenth_gpa": "",
            "tenth_percentage": "94.2%",
            "twelfth_gpa": "",
            "twelfth_percentage": "91.8%",
            "mothers_name": "",
            "emergency_contact_name": "Sunita Devi",
            "emergency_contact_number": "",
            "phone_number": "9876543210",
            "intro_video_link": "",
            "resume_link": "https://drive.google.com/resume.pdf",
            "disability": "None",
            "disability_percent": "0"
        }
        resolver = AttributeResolver(profile_data=profile)

        # 1. 10th GPA empty string should fall back to percentage
        q_10th = {"name": "What was your 10th GPA score?", "attrType": "string"}
        self.assertEqual(resolver.resolve_attribute(q_10th), "94.2%")

        # 2. 12th GPA empty string should fall back to percentage
        q_12th = {"name": "Enter 12th GPA", "attrType": "string"}
        self.assertEqual(resolver.resolve_attribute(q_12th), "91.8%")

        # 3. Mother's name empty string should fall back to emergency contact name
        q_mother = {"name": "Mother's Full Name", "attrType": "string"}
        self.assertEqual(resolver.resolve_attribute(q_mother), "Sunita Devi")

        # 4. Emergency phone empty string should fall back to primary phone
        q_alt_phone = {"name": "Alternate Emergency Contact Number", "attrType": "string"}
        self.assertEqual(resolver.resolve_attribute(q_alt_phone), "9876543210")

        # 5. Video link empty string should fall back to resume link
        q_video = {"name": "Introductory Video URL", "attrType": "string"}
        self.assertEqual(resolver.resolve_attribute(q_video), "https://drive.google.com/resume.pdf")

        # 6. Candidate with disability
        profile_pwd = dict(profile)
        profile_pwd["disability"] = "Locomotor Disability"
        profile_pwd["disability_percent"] = "40"
        resolver_pwd = AttributeResolver(profile_data=profile_pwd)

        q_disability_bool = {"name": "Do you have any physical disability?", "attrType": "boolean"}
        self.assertTrue(resolver_pwd.resolve_attribute(q_disability_bool))

        q_disability_select = {
            "name": "Disability Category",
            "attrType": "string",
            "options": ["None", "Locomotor Disability", "Visual Impairment"]
        }
        self.assertEqual(resolver_pwd.resolve_attribute(q_disability_select), "Locomotor Disability")


    # --------------------------------------------------------------------------
    # Bug R7 (User-Found): Graduation Pass-In Returns "Regular" for B.Tech Students
    # Question: "Graduation Pass in (for MCA/M.Tech.) student - B.Tech Students can put NA"
    # Bug: No handler existed → fell to options[0] fallback = "Regular"
    # Fix: Detect graduation_degree; B.Tech → "NA", MCA/MTech → "Regular"
    # --------------------------------------------------------------------------
    def test_graduation_pass_in_btech_returns_na(self):
        """B.Tech students must get 'NA' for the MCA/MTech graduation pass-in question."""
        # B.Tech profile — should answer NA
        btech_profile = {"graduation_degree": "B.Tech"}
        resolver = AttributeResolver(profile_data=btech_profile)
        attr = {
            "name": "Graduation Pass in (for MCA/M.Tech.) student - B.Tech Students can put NA",
            "attrType": "select:single",
            "options": json.dumps(["Regular", "Private", "NA"])
        }
        answer = resolver.resolve_attribute(attr)
        self.assertEqual(answer, "NA",
            f"B.Tech student got '{answer}' instead of 'NA' for graduation pass-in question")

    def test_graduation_pass_in_mtech_returns_regular(self):
        """MCA/M.Tech students should get 'Regular' for the graduation pass-in question."""
        mtech_profile = {"graduation_degree": "M.Tech"}
        resolver = AttributeResolver(profile_data=mtech_profile)
        attr = {
            "name": "Graduation Pass in (for MCA/M.Tech.) student - B.Tech Students can put NA",
            "attrType": "select:single",
            "options": json.dumps(["Regular", "Private", "NA"])
        }
        answer = resolver.resolve_attribute(attr)
        self.assertEqual(answer, "Regular",
            f"M.Tech student got '{answer}' instead of 'Regular' for graduation pass-in question")

    def test_graduation_pass_in_mca_returns_regular(self):
        """MCA students should also get 'Regular', not 'NA'."""
        mca_profile = {"graduation_degree": "MCA"}
        resolver = AttributeResolver(profile_data=mca_profile)
        attr = {
            "name": "Graduation Pass in (for MCA/M.Tech.) student - B.Tech Students can put NA",
            "attrType": "select:single",
            "options": json.dumps(["Regular", "Private", "NA"])
        }
        answer = resolver.resolve_attribute(attr)
        self.assertEqual(answer, "Regular",
            f"MCA student got '{answer}' instead of 'Regular' for graduation pass-in question")

    def test_graduation_pass_in_btech_no_options_returns_na(self):
        """B.Tech student with no options still returns 'NA' string."""
        btech_profile = {"graduation_degree": "B.Tech"}
        resolver = AttributeResolver(profile_data=btech_profile)
        attr = {
            "name": "Graduation Pass in (for MCA/M.Tech.) student - B.Tech Students can put NA",
            "attrType": "string",
        }
        answer = resolver.resolve_attribute(attr)
        self.assertEqual(answer, "NA",
            f"B.Tech student (no options) got '{answer}' instead of 'NA'")


if __name__ == "__main__":
    unittest.main()
