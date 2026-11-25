import os
import unittest

os.environ.setdefault("SJP_BUILD_ONLY", "1")

import normalizers
from attribute_resolver import AttributeResolver


class TestNormalizersAndDependencies(unittest.TestCase):
    def test_extract_question_required_always_returns_bool(self):
        self.assertIs(normalizers.extract_question_required({}), False)
        self.assertIs(normalizers.extract_question_required({"mandatory": None, "required": None}), False)
        self.assertIs(normalizers.extract_question_required({"mandatory": True}), True)
        self.assertIs(normalizers.extract_question_required({"isMandatory": "true"}), True)
        self.assertIs(normalizers.extract_question_required({"isRequired": "false"}), False)
        self.assertIs(normalizers.extract_question_required({"validation": "required"}), True)

    def test_dependent_q3_no_auto_resolves_q4_na_and_yes_resolves_certs(self):
        job_data = {
            "id": "job-abs-1",
            "jobProfile": {
                "attributeList": [
                    {
                        "attrKey": "q1",
                        "name": "Share your updated resume link with public access",
                        "attrType": "string",
                        "mandatory": True,
                    },
                    {
                        "attrKey": "q2",
                        "name": "Profile Applied for",
                        "attrType": "select:single",
                        "mandatory": True,
                        "options": ["AWS Application Developer", "Data Engineer"],
                    },
                    {
                        "attrKey": "q3",
                        "name": "Do you have done any certification course as mentioned in the notice?",
                        "attrType": "select:single",
                        "mandatory": True,
                        "options": ["Yes", "No"],
                    },
                    {
                        "attrKey": "q4",
                        "name": "If Yes, please mention your certification details (AWS/Azure/GCP)",
                        "attrType": "string",
                        "mandatory": False,
                    },
                ]
            },
        }

        profile_no_certs = {
            "full_name": "Synthetic Candidate",
            "resume_link": "https://example.edu/resume.pdf",
            "technical_skills": "",
        }
        resolver_no = AttributeResolver(profile_data=profile_no_certs, user_id="u_no")
        preview_no = resolver_no.preview_answers_for_job(job_data)
        self.assertEqual(len(preview_no), 4)
        for item in preview_no:
            self.assertIsInstance(item["required"], bool)
            self.assertIsInstance(item["mandatory"], bool)
        self.assertEqual(preview_no[2]["answer"], "No")
        self.assertEqual(preview_no[3]["answer"], "NA")
        self.assertEqual(preview_no[3]["depends_on"]["when_value"], "Yes")

        profile_with_certs = {
            "full_name": "Synthetic Candidate",
            "resume_link": "https://example.edu/resume.pdf",
            "technical_skills": "AWS Certified Cloud Practitioner, Python, Cloud",
        }
        resolver_yes = AttributeResolver(profile_data=profile_with_certs, user_id="u_yes")
        preview_yes = resolver_yes.preview_answers_for_job(job_data)
        self.assertEqual(preview_yes[2]["answer"], "Yes")
        self.assertIn("AWS Certified Cloud Practitioner", preview_yes[3]["answer"])

    def test_parse_ctc(self):
        lpa = normalizers.parse_ctc("6.5 - 8.5 LPA")
        self.assertEqual(lpa["min_lpa"], 6.5)
        self.assertEqual(lpa["max_lpa"], 8.5)
        self.assertEqual(lpa["unit"], "LPA")

        single_lpa = normalizers.parse_ctc("12 LPA")
        self.assertEqual(single_lpa["min_lpa"], 12.0)
        self.assertEqual(single_lpa["max_lpa"], 12.0)
        self.assertEqual(single_lpa["unit"], "LPA")

        stipend = normalizers.parse_ctc("Stipend 25k/month")
        self.assertEqual(stipend["unit"], "MONTHLY")
        self.assertEqual(stipend["min_lpa"], 3.0)

        empty = normalizers.parse_ctc("")
        self.assertEqual(empty["display"], "CTC As per Policy")
        self.assertEqual(empty["unit"], "OTHER")

    def test_normalize_profile_aliases_emergency_contact_phone(self):
        prof = normalizers.normalize_profile({
            "full_name": "Synthetic Candidate",
            "emergency_contact_phone": "0000012345",
        })
        self.assertEqual(prof["emergency_contact_number"], "0000012345")
        self.assertNotIn("emergency_contact_phone", prof)

    def test_normalize_job_outputs_structured_top_level_fields(self):
        norm = normalizers.normalize_job({
            "id": "job123",
            "company": "Acme Synthetic",
            "title": "Backend Intern",
            "raw": {
                "placementCategoryName": "7.2 LPA",
                "jobProfileApplicationDeadline": "2026-12-01T18:00:00Z",
                "attributeList": [
                    {"name": "Preferred location", "attrType": "select:single", "options": ["Pune", "Remote"]}
                ],
            },
        })
        self.assertEqual(norm["id"], "job123")
        self.assertEqual(norm["company"], "Acme Synthetic")
        self.assertEqual(norm["title"], "Backend Intern")
        self.assertEqual(norm["ctc_min_lpa"], 7.2)
        self.assertTrue(norm["has_questions"])
        self.assertEqual(norm["questions_count"], 1)
        self.assertIsNotNone(norm["deadline_iso"])
        self.assertIsInstance(norm["deadline_display"], str)


if __name__ == "__main__":
    unittest.main()
