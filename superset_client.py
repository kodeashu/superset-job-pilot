"""Superset portal client for authentication, job extraction, and auto-applying.
Reverse-engineered for app.joinsuperset.com API specification.
"""
import json
import logging
from datetime import datetime, timezone
import pytz
import requests
from config import Config

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")


class SupersetClient:
    def __init__(self, email: str = None, password: str = None, token: str = None):
        self.email = email or Config.SUPERSET_EMAIL
        self.password = password or Config.SUPERSET_PASSWORD
        self.token = token or Config.SUPERSET_TOKEN
        self.base_url = Config.SUPERSET_BASE_URL.rstrip("/")
        
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Origin": self.base_url,
            "Referer": f"{self.base_url}/",
        })
        self.session_key = self.token
        self.user_uuid = None
        self.user_id = None
        self.student_name = None
        self.organization = None

        if self.session_key:
            self._apply_session_headers(self.session_key)

    def _apply_session_headers(self, key: str):
        self.session.headers.update({
            "Authorization": f"Custom {key}",
            "x-superset-tenant-type": "STUDENT",
            "x-superset-tenant-qualifier": "ONBOARDED",
        })

    def login(self) -> bool:
        """
        Authenticates against Superset /tnpsuite-core/login endpoint.
        """
        if not self.email or not self.password:
            logger.warning("No credentials provided for Superset login.")
            return False

        login_url = f"{self.base_url}/tnpsuite-core/login"
        payload = {
            "username": self.email,
            "password": self.password,
        }

        try:
            logger.info(f"Connecting to Superset as {self.email}...")
            resp = self.session.post(login_url, json=payload, timeout=15)
            
            if resp.status_code == 200:
                data = resp.json()
                self.session_key = data.get("sessionKey")
                self.user_uuid = data.get("uuid")
                self.user_id = data.get("userId")
                self.student_name = data.get("name")
                self._apply_session_headers(self.session_key)
                logger.info(f"Successfully authenticated as {self.student_name} (UUID: {self.user_uuid})")
                return True
            else:
                logger.error(f"Login failed: HTTP {resp.status_code} - {resp.text[:200]}")
                return False
        except Exception as e:
            logger.error(f"Error during Superset login request: {e}")
            return False

    def fetch_job_profiles(self) -> list:
        """
        Fetches live campus placement job profiles for the authenticated student.
        """
        if not self.session_key or not self.user_uuid:
            if not self.login():
                logger.error("Cannot fetch jobs without active Superset session.")
                return []

        url = f"{self.base_url}/tnpsuite-core/students/{self.user_uuid}/job_profiles"
        try:
            resp = self.session.get(url, timeout=15)
            if resp.status_code == 200:
                raw_jobs = resp.json()
                if isinstance(raw_jobs, list):
                    logger.info(f"Retrieved {len(raw_jobs)} live job profiles from Superset.")
                    return [self._normalize_job(j) for j in raw_jobs if isinstance(j, dict)]
            else:
                logger.error(f"Failed to fetch job profiles: HTTP {resp.status_code} - {resp.text[:200]}")
        except Exception as e:
            logger.error(f"Error fetching Superset jobs: {e}")

        return []

    def apply_to_job(self, job_identifier: str, resume_id: str = None, custom_attributes: dict = None, profile_data: dict = None) -> dict:
        """
        Submits job application on Superset for the specified job profile.
        Uses exact Superset Angular tnpsuite-core specification.
        """
        if not self.session_key or not self.user_uuid:
            if not self.login():
                return {"success": False, "message": "Failed to log in to Superset"}

        # 1. Fetch job profile details and available resumes
        detail_url = f"{self.base_url}/tnpsuite-core/students/{self.user_uuid}/job_profiles/{job_identifier}"
        try:
            detail_resp = self.session.get(detail_url, timeout=15)
            if detail_resp.status_code != 200:
                logger.error(f"Failed to fetch job details for {job_identifier}: HTTP {detail_resp.status_code}")
                return {"success": False, "message": f"HTTP {detail_resp.status_code} fetching job details"}
            
            job_data = detail_resp.json()
            if job_data.get("jobApplicationStatus") == "APPLIED":
                return {"success": True, "message": "Already applied"}

            # Determine resume document identifier (Superset expects document 'identifier', not internal 'uuid')
            resume_doc_id = None
            default_resume = job_data.get("defaultResume")
            if default_resume and isinstance(default_resume, dict):
                resume_doc_id = default_resume.get("identifier") or default_resume.get("uuid")

            resumes_available = job_data.get("resumesAvailable") or []
            if resume_id and isinstance(resumes_available, list):
                for r in resumes_available:
                    if (
                        r.get("identifier") == resume_id
                        or r.get("uuid") == resume_id
                        or r.get("name") == resume_id
                    ):
                        resume_doc_id = r.get("identifier") or r.get("uuid")
                        break

            if not resume_doc_id and resumes_available:
                resume_doc_id = resumes_available[0].get("identifier") or resumes_available[0].get("uuid")

            # Fallback: query student profile defaultResumeId if still None
            if not resume_doc_id:
                try:
                    stu_resp = self.session.get(f"{self.base_url}/tnpsuite-core/students/{self.user_uuid}", timeout=10)
                    if stu_resp.status_code == 200:
                        stu_data = stu_resp.json()
                        resume_doc_id = stu_data.get("defaultResumeId")
                        if not resume_doc_id:
                            for doc in stu_data.get("documents", []):
                                if doc.get("type") == "RESUME" and doc.get("status") == "ACTIVE":
                                    resume_doc_id = doc.get("identifier") or doc.get("uuid")
                                    break
                except Exception as re_err:
                    logger.debug(f"Fallback resume lookup warning: {re_err}")

            # 2. Check application consent requirement
            consent_acceptances = []
            try:
                consent_url = f"{self.base_url}/tnpsuite-core/students/{self.user_uuid}/job_profiles/{job_identifier}/application-consent"
                consent_resp = self.session.get(consent_url, timeout=10)
                if consent_resp.status_code == 200:
                    cdata = consent_resp.json()
                    if cdata.get("required"):
                        consent_acceptances.append({
                            "consentType": cdata.get("consentType", "JOB_APPLICATION"),
                            "consentTextUuid": cdata.get("consentTextUuid"),
                        })
            except Exception as e:
                logger.debug(f"Consent check error: {e}")

            # 3. Resolve required company attributes/custom questions
            attributes_payload = {}
            if custom_attributes is not None and len(custom_attributes) > 0:
                attributes_payload = custom_attributes
                logger.info(f"Using {len(attributes_payload)} user-confirmed custom attributes for {job_data.get('companyName')}.")
            else:
                try:
                    from attribute_resolver import AttributeResolver
                    resolver = AttributeResolver(profile_data=profile_data)
                    attributes_payload = resolver.resolve_job_attributes(job_data)
                    if attributes_payload:
                        logger.info(f"Auto-resolved {len(attributes_payload)} custom questions for {job_data.get('companyName')}.")
                except Exception as e:
                    logger.warning(f"Error resolving job attributes: {e}")

            # Ensure all attribute values are strings (Superset Java backend expects Map<String, String>; arrays must be JSON stringified)
            import json as _json
            normalized_attributes = {}
            for k, v in (attributes_payload or {}).items():
                if isinstance(v, (list, dict)):
                    normalized_attributes[k] = _json.dumps(v)
                elif isinstance(v, bool):
                    normalized_attributes[k] = "Yes" if v else "No"
                elif v is None:
                    normalized_attributes[k] = ""
                else:
                    normalized_attributes[k] = str(v)

            # 4. Construct application payload matching Superset Next.js client
            payload = {
                "documentIds": [resume_doc_id] if resume_doc_id else [],
                "applyWithoutResume": not bool(resume_doc_id),
                "attributes": normalized_attributes,
                "consentAcceptances": consent_acceptances,
            }

            # 5. Dispatch application POST request
            apply_url = f"{self.base_url}/tnpsuite-core/students/{self.user_uuid}/job_profiles/{job_identifier}"
            resp = self.session.post(apply_url, params={"placement": "on_campus"}, json=payload, timeout=20)

            if resp.status_code in (200, 201, 204):
                comp_name = job_data.get('companyName', 'Company')
                logger.info(f"Successfully applied to job {job_identifier} ({comp_name})!")
                return {
                    "success": True, 
                    "company_name": comp_name,
                    "message": f"Application to {comp_name} submitted successfully!", 
                    "data": resp.json() if resp.content else {}
                }
            else:
                err_text = resp.text[:300]
                try:
                    err_json = resp.json()
                    err_text = err_json.get("message") or err_json.get("error") or err_json.get("detail") or err_text
                except Exception:
                    pass
                logger.warning(f"Apply returned HTTP {resp.status_code}: {err_text}")

                comp_name = job_data.get('companyName', 'Company')
                healing_result = {}
                try:
                    from error_healer import ErrorHealer
                    healer = ErrorHealer()
                    healing_result = healer.handle_error(
                        error_message=err_text,
                        status_code=resp.status_code,
                        company_name=comp_name,
                        job_id=job_identifier,
                        extra_data={"payload": payload}
                    )
                except Exception as he:
                    logger.debug(f"ErrorHealer hook warning: {he}")

                return {
                    "success": False, 
                    "status_code": resp.status_code,
                    "message": f"Superset Rejected (HTTP {resp.status_code}): {err_text}",
                    "ai_diagnosis": healing_result
                }

        except Exception as e:
            logger.error(f"Error applying to job {job_identifier}: {e}")
            healing_result = {}
            try:
                from error_healer import ErrorHealer
                healer = ErrorHealer()
                healing_result = healer.handle_error(
                    error_message=str(e),
                    status_code=500,
                    company_name="System",
                    job_id=job_identifier
                )
            except Exception:
                pass
            return {"success": False, "message": str(e), "ai_diagnosis": healing_result}

    def _normalize_job(self, raw: dict) -> dict:
        """Standardizes a Superset tnpsuite-core job profile object."""
        job_id = str(raw.get("jobProfileIdentifier") or raw.get("id") or "")
        company = raw.get("companyName") or "Campus Recruiter"
        role = raw.get("jobProfileTitle") or "Engineering Role"
        location = raw.get("jobProfileLocation") or "Pan India"
        
        # CTC display
        ctc_display = raw.get("placementCategoryName") or "CTC As per Policy"
        
        # Deadline parsing (Superset returns epoch milliseconds, e.g. 1783672259000)
        deadline_raw = raw.get("jobProfileApplicationDeadline")
        deadline_dt = None
        hours_left = None
        deadline_display = "Immediate"

        if deadline_raw:
            try:
                # If milliseconds timestamp
                if isinstance(deadline_raw, (int, float)) or (isinstance(deadline_raw, str) and deadline_raw.isdigit()):
                    epoch_sec = float(deadline_raw) / 1000.0 if float(deadline_raw) > 1e11 else float(deadline_raw)
                    deadline_dt = datetime.fromtimestamp(epoch_sec, tz=IST)
                else:
                    clean_time = str(deadline_raw).replace("Z", "+00:00")
                    dt = datetime.fromisoformat(clean_time)
                    deadline_dt = dt.astimezone(IST) if dt.tzinfo else IST.localize(dt)

                now_ist = datetime.now(IST)
                diff = (deadline_dt - now_ist).total_seconds() / 3600.0
                hours_left = max(0.0, diff)
                deadline_display = deadline_dt.strftime("%d %b %Y, %I:%M %p IST")
            except Exception as e:
                logger.debug(f"Deadline parse error: {e}")
                deadline_display = str(deadline_raw)

        # Status & Eligibility
        app_status = raw.get("jobApplicationStatus", "YET_TO_APPLY")
        is_applied = (app_status == "APPLIED")
        is_eligible = (app_status != "NOT_ELIGIBLE") and not raw.get("ageRestricted", False)

        # Precise closure & expiry calculation
        is_expired = False
        if hours_left is not None and hours_left <= 0:
            is_expired = True

        raw_status = raw.get("jobProfileStatus", "")
        is_closed_for_apply = (raw_status in ("CLOSED_FOR_APPLICATIONS", "COMPLETED", "IN_PROCESS", "ARCHIVED")) or is_expired
        is_open = (not is_applied) and is_eligible and (not is_closed_for_apply)

        profile_url = f"{self.base_url}/join/#/signup/student/jobprofiles/{job_id}" if job_id else self.base_url

        return {
            "id": job_id,
            "company_name": company,
            "job_role": role,
            "ctc_display": ctc_display,
            "location": location,
            "deadline_dt": deadline_dt,
            "deadline_display": deadline_display,
            "hours_left": hours_left,
            "is_expired": is_expired,
            "is_closed": is_closed_for_apply,
            "is_open": is_open,
            "is_eligible": is_eligible,
            "is_applied": is_applied,
            "app_status": app_status,
            "profile_url": profile_url,
            "raw": raw,
        }
