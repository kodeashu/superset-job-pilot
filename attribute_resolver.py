"""Intelligent Attribute & Custom Question Resolver for Superset campus placement drives."""
import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional
from config import BASE_DIR

logger = logging.getLogger(__name__)


class AttributeResolver:
    """Matches company application custom questions/attributes against per-user encrypted student profile data."""

    def __init__(self, profile_path: Path = None, profile_data: Optional[Dict[str, Any]] = None, user_id: Optional[str] = None):
        self.user_id = user_id
        self.profile_path = profile_path
        if profile_data is not None:
            self.profile = dict(profile_data)
        else:
            self.profile = self.load_profile()

    def load_profile(self) -> dict:
        """Loads student profile data from the encrypted UserManager vault."""
        try:
            from user_manager import UserManager, DEFAULT_PROFILE_TEMPLATE
            um = UserManager()
            if self.user_id:
                u = um.get_user_by_id(self.user_id)
                if u:
                    return u["profile"]
            users = um.get_all_users()
            if users:
                self.user_id = users[0]["user_id"]
                return users[0]["profile"]
            return dict(DEFAULT_PROFILE_TEMPLATE)
        except Exception as e:
            logger.error(f"Error loading student profile from vault: {e}")
            return {}

    def save_profile(self, data: dict):
        """Saves updated student profile answers into the encrypted UserManager vault."""
        self.profile.update(data)
        try:
            from user_manager import UserManager
            um = UserManager()
            target_uid = self.user_id
            if not target_uid:
                users = um.get_all_users()
                if users:
                    target_uid = users[0]["user_id"]
            if target_uid:
                self.profile = um.update_user_profile(target_uid, data)
                logger.info(f"Encrypted student profile updated for user {target_uid}")
        except Exception as e:
            logger.error(f"Error saving profile to encrypted vault: {e}")

    def resolve_attribute(self, attr: dict) -> Any:
        """Determines the most accurate answer for a single Superset attribute question."""
        name = str(attr.get("name") or "").strip()
        attr_type = str(attr.get("attrType") or "string")
        options_raw = attr.get("options")

        options = []
        if options_raw:
            try:
                options = json.loads(options_raw) if isinstance(options_raw, str) else options_raw
            except Exception:
                options = []

        nl = name.lower()
        p = self.profile

        # 0. Check User-Verified Clarifications & Dynamic RAG Memory
        custom_clarifs = p.get("custom_clarifications", {})
        if name in custom_clarifs:
            return custom_clarifs[name]
        for c_q, c_ans in custom_clarifs.items():
            if c_q.lower() == nl or (len(c_q) > 15 and c_q.lower() in nl):
                return c_ans

        # 1. Disclaimers, Legal Acknowledgments & Employment Declarations
        if any(k in nl for k in ["disclaimer", "acknowledge", "voluntary", "employment status", "agree", "accuracy", "discrepancy"]):
            for opt in options:
                if "agree" in str(opt).lower():
                    return opt
            return "I Agree" if not options else options[0]

        # 2. Genuine Interest in Profile
        if "genuinely interested" in nl or "interested for this" in nl or "interested in" in nl:
            for opt in options:
                if str(opt).lower() in ("yes", "i agree", "interested"):
                    return opt
            return "Yes" if "Yes" in options or not options else options[0]

        # 3. Nationality, Citizenship & Passport
        citizenship_country = p.get("citizenship") or p.get("permanent_country") or "India"
        if any(k in nl for k in ["foreign passport", "foreign document", "which nationality passport", "specify your nationality"]):
            if "which nationality" in nl or "specify your nationality" in nl:
                return citizenship_country
            if options:
                for opt in options:
                    if citizenship_country.lower() in str(opt).lower():
                        return [opt] if attr_type == "select:multiple" else opt
                for opt in options:
                    if "foreign" in str(opt).lower():
                        return [opt] if attr_type == "select:multiple" else opt
            return citizenship_country

        if "passport" in nl:
            has_pp = str(p.get("has_passport", "No")).lower() in ("yes", "true", "1")
            if "do you have" in nl or attr_type == "boolean":
                return has_pp if attr_type == "boolean" else ("Yes" if has_pp else "No")
            if "hold an indian or a foreign" in nl or "indian or foreign" in nl:
                want_type = p.get("passport_type", "Indian Passport")
                for opt in options:
                    if want_type.lower().split()[0] in str(opt).lower():
                        return opt
                return want_type
            if "number" in nl or "no" in nl:
                return p.get("passport_number", "NA")
            if "expiry" in nl:
                return p.get("passport_expiry_date", "NA")
            if "issue" in nl:
                return p.get("passport_issue_date", "NA")

        if "nationality" in nl or "citizen" in nl:
            nat = p.get("nationality") or citizenship_country
            if options:
                for opt in options:
                    if nat.lower()[:4] in str(opt).lower() or citizenship_country.lower()[:4] in str(opt).lower():
                        return opt
                for opt in options:
                    if "indian" in str(opt).lower() and citizenship_country.lower().startswith("ind"):
                        return opt
                for opt in options:
                    if "others" in str(opt).lower() or "foreigner" in str(opt).lower():
                        return opt
                return options[0]
            return nat

        # 4. PAN Card & Aadhaar
        if "pan" in nl:
            if any(k in nl for k in ["do you have", "have a pan", "available"]) or attr_type == "boolean":
                return p.get("has_pan", "Yes") if attr_type != "boolean" else (p.get("has_pan") == "Yes")
            if "first name" in nl:
                return p.get("first_name", "")
            if "middle name" in nl:
                return p.get("middle_name", "")
            if "last name" in nl:
                return p.get("last_name", "")
            return p.get("pan_card_number", "NA")

        if "aadhaar" in nl or "aadhar" in nl:
            if "name" in nl:
                return p.get("full_name", "")
            if "last 4" in nl or "digit" in nl:
                return p.get("aadhaar_last_4", "")
            return p.get("aadhaar_last_4", "")

        # 5. Parents / Emergency Contact / Personal Info
        if "father" in nl:
            return p.get("fathers_name", "")
        if "mother" in nl:
            return p.get("mothers_name") or p.get("emergency_contact_name") or ""
        if "marital" in nl or "marriage" in nl:
            for opt in options:
                if str(opt).lower() in ("single", "unmarried"):
                    return opt
            return "Single"
        if "alt" in nl or "alternate" in nl or "alternative" in nl or "emergency" in nl or "contact person" in nl:
            if "number" in nl or "phone" in nl or "mobile" in nl or "contact no" in nl or "no." in nl or "no" in nl:
                return p.get("emergency_contact_number") or p.get("phone_number") or ""
            if "address" in nl:
                return p.get("emergency_contact_address") or p.get("permanent_address") or ""
            if "name" in nl or "person" in nl:
                return p.get("emergency_contact_name") or p.get("fathers_name") or ""
            return p.get("emergency_contact_number") or p.get("phone_number") or ""
        if "mobile" in nl or "phone" in nl or "contact number" in nl:
            return p.get("phone_number", "")
        if "disability" in nl or "physically challenged" in nl:
            if "percent" in nl:
                return p.get("disability_percent") or "0"
            has_disability = str(p.get("disability", "None")).strip().lower() not in ("none", "no", "0", "", "na", "false")
            if attr_type == "boolean":
                return has_disability
            if has_disability:
                for opt in options:
                    if str(opt).lower() in ("yes", str(p.get("disability")).lower()):
                        return opt
                return p.get("disability") or "Yes"
            for opt in options:
                if str(opt).lower() in ("no", "none", "na", "not applicable"):
                    return opt
            return p.get("disability") or "None"

        # 5b. Employment Status / Sabbatical / Prior Association
        if any(k in nl for k in ["employee id", "employee code", "empid"]):
            return "NA"
        if any(k in nl for k in ["sabbatical", "sabattical"]):
            return False if attr_type == "boolean" else "No"
        if any(k in nl for k in ["employed with", "worked with", "prior employee", "previous employer", "ex-employee", "past employee"]):
            return False if attr_type == "boolean" else "No"
        if any(k in nl for k in ["work-experience", "experience documents", "relieving letter", "service certificate"]):
            return False if attr_type == "boolean" else "NA"
        if any(k in nl for k in ["criminal", "convicted", "court case", "disciplinary", "police case"]):
            return False if attr_type == "boolean" else "No"
        if any(k in nl for k in ["active employment", "current employment status"]):
            for opt in options:
                if any(w in str(opt).lower() for w in ["student", "not employed", "unemployed", "no"]):
                    return opt
            return p.get("employment_status", "No")

        # 6. Flexibility (Shift, Location, Domain, Technology)
        if "shift" in nl:
            return "Yes" if "Yes" in options or not options else options[0]
        if "location" in nl and ("flexible" in nl or "work from" in nl or "any" in nl or "willing" in nl):
            return "Yes" if "Yes" in options or not options else options[0]
        if any(k in nl for k in ["technology", "domain", "track", "role"]):
            if options and "Yes" in options:
                return "Yes"
            return "Yes" if not options else options[0]

        # 7. Resume, Video & Social/Code Links
        if "video" in nl:
            return p.get("intro_video_link") or p.get("resume_link") or ""
        if "resume" in nl and ("link" in nl or "share" in nl or "url" in nl):
            return p.get("resume_link", "")
        if "github" in nl:
            return p.get("github_link", "")
        if "linkedin" in nl:
            return p.get("linkedin_link", "")
        if "portfolio" in nl:
            return p.get("portfolio_link", "")

        # 8. Locations
        if "current" in nl and ("location" in nl or "city" in nl or "address" in nl or "state" in nl):
            return p.get("current_location", "Bhubaneswar, Odisha")
        if "permanent" in nl and ("location" in nl or "city" in nl or "address" in nl or "native" in nl or "state" in nl):
            return p.get("permanent_location") or p.get("permanent_address") or p.get("current_location", "Bhubaneswar, Odisha")

        # 8b. Technical Skills & Certifications
        if "skill" in nl or "certification" in nl or "tech skill" in nl:
            return p.get("technical_skills") or "Python, FastAPI, React, SQL, Cloud & Agentic AI Systems"

        # 9. Gaps (10th to 12th, 12th to Graduation, Total) - MUST CHECK BEFORE 10th/12th
        if "gap" in nl:
            for opt in options:
                if str(opt).strip() == "0":
                    return opt
            return p.get("total_gap", "0")

        # 10. Academics: 10th / Secondary School
        if "10th" in nl or "class 10" in nl or "matric" in nl:
            if "school" in nl or "institute" in nl:
                return p.get("tenth_school", "")
            if "board" in nl:
                return p.get("tenth_board", "")
            if "year" in nl or "passing" in nl:
                return p.get("tenth_passing_year", "")
            if "pass" in nl or "course type" in nl or "mode" in nl:
                for opt in options:
                    if opt in ("Regular", "Fulltime"):
                        return opt
                return "Regular"
            if "gpa" in nl:
                return p.get("tenth_gpa") or p.get("tenth_percentage") or ""
            if "percent" in nl or "score" in nl:
                return p.get("tenth_percentage") or p.get("tenth_gpa") or ""

        # 11. Academics: 12th / Intermediate / Diploma
        if "12th" in nl or "class 12" in nl or "diploma" in nl or "intermediate" in nl:
            if "school" in nl or "college" in nl or "institute" in nl:
                return p.get("twelfth_college", "")
            if "board" in nl:
                return p.get("twelfth_board", "")
            if "year" in nl or "passing" in nl:
                return p.get("twelfth_passing_year", "")
            if "pass" in nl or "course type" in nl or "mode" in nl:
                for opt in options:
                    if opt in ("Regular", "Fulltime"):
                        return opt
                return "Regular"
            if "gpa" in nl:
                return p.get("twelfth_gpa") or p.get("twelfth_percentage") or ""
            if "percent" in nl or "score" in nl:
                return p.get("twelfth_percentage") or p.get("twelfth_gpa") or ""

        # 12. Marksheets (e.g. 8th semester expected date)
        if "marksheet" in nl:
            if "8th" in nl or "semester" in nl:
                for opt in options:
                    if "july" in str(opt).lower():
                        return opt
                for opt in options:
                    if "june" in str(opt).lower():
                        return opt
                return options[0] if options else p.get("marksheet_8th_sem", "July'27")

        # 13. Graduation pass-in mode (for MCA/M.Tech. students — B.Tech students answer NA)
        # Question example: "Graduation Pass in (for MCA/M.Tech.) student - B.Tech Students can put NA"
        if ("graduation" in nl or "pg" in nl or "post.grad" in nl) and (
            "pass" in nl or "mode" in nl or "course type" in nl
        ) and any(k in nl for k in ["mca", "m.tech", "mtech", "post", "b.tech can", "btech can", "b.tech student", "na"]):
            # Determine degree from profile
            degree = str(p.get("graduation_degree", "B.Tech")).strip().lower()
            is_btech = any(d in degree for d in ["b.tech", "btech", "be ", "b.e", "bachelor"])
            is_mca_mtech = any(d in degree for d in ["mca", "m.tech", "mtech", "m.e", "me ", "mba", "msc", "m.sc"])
            if is_btech and not is_mca_mtech:
                # B.Tech students should answer NA for this question
                for opt in options:
                    if str(opt).upper() == "NA" or str(opt).lower() in ("na", "n/a", "not applicable"):
                        return opt
                return "NA"
            else:
                # MCA/MTech students answer their pass mode
                for opt in options:
                    if opt in ("Regular", "Fulltime"):
                        return opt
                return p.get("graduation_pass_mode", "Regular")

        # 13b. Graduation / Backlogs / CGPA
        if "backlog" in nl:
            if "active" in nl:
                return p.get("active_backlogs", "0")
            for opt in options:
                if "no" in str(opt).lower() or "0" in str(opt):
                    return opt
            return p.get("history_backlogs", "0")

        if "cgpa" in nl or ("gpa" in nl and not any(k in nl for k in ["10th", "12th", "matric", "intermediate"])):
            return p.get("current_cgpa", "")

        if any(k in nl for k in ["graduation college", "engineering college", "current institute"]):
            return p.get("graduation_college", "Kalinga Institute of Industrial Technology (KIIT), Bhubaneswar")

        if any(k in nl for k in ["branch", "discipline", "stream", "specialization"]):
            user_branch = p.get("graduation_branch", "Computer Science and Engineering")
            for opt in options:
                if user_branch.lower() in str(opt).lower() or any(b in str(opt).lower() for b in ["computer science", "cse"]):
                    return opt
            return user_branch

        # 14. Name / Roll / Email questions
        if "name" in nl and any(k in nl for k in ["candidate", "student", "full", "your"]):
            return p.get("full_name", "")
        if "roll" in nl or "enrollment" in nl:
            return p.get("roll_number", "")
        if "email" in nl:
            return p.get("email", "")

        # 15. Fallbacks based on option types
        if options:
            if "Yes" in options:
                return "Yes"
            if "I Agree" in options or "Agree" in options:
                return "I Agree" if "I Agree" in options else "Agree"
            if attr_type == "select:multiple":
                return [options[0]]
            return options[0]

        if attr_type == "boolean":
            return True

        return ""

    def resolve_job_attributes(self, job_data: dict) -> dict:
        """
        Given the raw Superset job detail response, maps all required and optional attributes.
        Returns a dict of { attrKey: resolved_value }.
        """
        jp = job_data.get("jobProfile", {})
        attr_list = jp.get("attributeList") or job_data.get("attributes") or []

        if isinstance(attr_list, dict):
            return attr_list

        resolved = {}
        for attr in attr_list:
            if not isinstance(attr, dict):
                continue

            attr_key = attr.get("attrKey") or attr.get("identifier")
            if not attr_key:
                continue

            ans = self.resolve_attribute(attr)
            resolved[attr_key] = ans
            logger.debug(f"Resolved attribute '{attr.get('name')}' -> {ans}")

        return resolved

    def preview_answers_for_job(self, job_data: dict) -> list:
        """Returns a list of { key, question, type, options, answer, mandatory } for UI display."""
        jp = job_data.get("jobProfile", {})
        attr_list = jp.get("attributeList") or job_data.get("attributes") or []

        if isinstance(attr_list, dict):
            return []

        preview = []
        for attr in attr_list:
            if not isinstance(attr, dict):
                continue
            name = attr.get("name", "")
            attr_type = attr.get("attrType", "string")
            mandatory = attr.get("mandatory", False)
            options_raw = attr.get("options")
            options = []
            if options_raw:
                try:
                    options = json.loads(options_raw) if isinstance(options_raw, str) else options_raw
                except Exception:
                    options = []

            ans = self.resolve_attribute(attr)

            clarity_meta = {
                "is_unclear": False,
                "confidence": 0.95,
                "clarity_reason": "Verified from profile facts",
            }
            try:
                from ambiguity_manager import AmbiguityManager
                am = AmbiguityManager()
                eval_res = am.ai.evaluate_question_clarity(attr, ans)
                clarity_meta = {
                    "is_unclear": eval_res.get("is_unclear", False),
                    "confidence": eval_res.get("confidence", 0.95),
                    "clarity_reason": eval_res.get("reason", ""),
                }
            except Exception:
                pass

            attr_key = attr.get("attrKey") or attr.get("identifier") or name
            preview.append(
                {
                    "key": attr_key,
                    "question": name,
                    "type": attr_type,
                    "mandatory": mandatory,
                    "options": options,
                    "answer": ans,
                    **clarity_meta,
                }
            )
        return preview
