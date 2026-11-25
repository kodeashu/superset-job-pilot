"""
Canonical data normalizers for Superset Job Pilot:
  - CTC / Stipend parser
  - IST timestamp parser & ISO-8601 serializer
  - Job profile normalizer (eliminating frontend `raw` coupling)
  - Profile normalizer (`emergency_contact_phone` alias unification)
  - Custom question `required` bool extraction & `If Yes/No` dependency detector
"""
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import pytz

IST = pytz.timezone("Asia/Kolkata")

_IF_COND_RE = re.compile(r"^\s*if\s+(yes|no)\b", re.IGNORECASE)
_RANGE_LPA_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[-–to]+\s*(\d+(?:\.\d+)?)\s*(?:lpa|l\.p\.a)", re.IGNORECASE)
_SINGLE_LPA_RE = re.compile(r"(?:>|>=|<|<=|upto|up\s*to)?\s*(\d+(?:\.\d+)?)\s*(?:lpa|l\.p\.a)", re.IGNORECASE)
_INR_ANNUAL_RE = re.compile(r"(?:₹|rs\.?|inr)?\s*(\d{1,3}(?:,\d{2,3})+|\d{5,9})", re.IGNORECASE)
_MONTHLY_STIPEND_RE = re.compile(
    r"(\d+(?:\.\d+)?|\d{1,3}(?:,\d{3})+)\s*(k)?\s*(?:/|per)?\s*(?:month|mo|p\.?m\.?)\b",
    re.IGNORECASE,
)


def parse_ctc(raw_str: Any) -> Dict[str, Any]:
    """Parses CTC/stipend strings (`12 LPA`, `₹ 8,00,000`, `8-12 LPA`, `Stipend 30k/month`)."""
    display = str(raw_str or "").strip() or "CTC As per Policy"
    low = display.lower()

    def _tier(val_lpa: Optional[float]) -> int:
        if val_lpa is None:
            if "> 14" in low or "14 lpa" in low:
                return 3
            if "> 7" in low or "7 lpa" in low or "14" in low:
                return 2
            if "3 lpa" in low or "7" in low:
                return 1
            return 0
        if val_lpa >= 14.0 or ("> 14" in low):
            return 3
        if val_lpa >= 7.0:
            return 2
        if val_lpa > 0:
            return 1
        return 0

    m_monthly = _MONTHLY_STIPEND_RE.search(display)
    if m_monthly:
        num = float(m_monthly.group(1).replace(",", ""))
        if m_monthly.group(2):
            num *= 1000.0
        ann_lpa = round((num * 12.0) / 100_000.0, 2)
        return {
            "min_lpa": ann_lpa,
            "max_lpa": ann_lpa,
            "unit": "MONTHLY",
            "display": display,
            "tier": _tier(ann_lpa),
        }

    m_range = _RANGE_LPA_RE.search(display)
    if m_range:
        lo, hi = float(m_range.group(1)), float(m_range.group(2))
        lo_v, hi_v = min(lo, hi), max(lo, hi)
        return {
            "min_lpa": lo_v,
            "max_lpa": hi_v,
            "unit": "LPA",
            "display": display,
            "tier": _tier(hi_v),
        }

    m_single = _SINGLE_LPA_RE.search(display)
    if m_single:
        val = float(m_single.group(1))
        return {
            "min_lpa": val,
            "max_lpa": val,
            "unit": "LPA",
            "display": display,
            "tier": _tier(val),
        }

    m_inr = _INR_ANNUAL_RE.search(display)
    if m_inr:
        amount = float(m_inr.group(1).replace(",", ""))
        if amount >= 100_000:
            lpa = round(amount / 100_000.0, 2)
            return {
                "min_lpa": lpa,
                "max_lpa": lpa,
                "unit": "LPA",
                "display": display,
                "tier": _tier(lpa),
            }

    return {
        "min_lpa": None,
        "max_lpa": None,
        "unit": "OTHER",
        "display": display,
        "tier": _tier(None),
    }


def parse_ist_datetime(raw_val: Any) -> Tuple[Optional[datetime], Optional[str], str, Optional[float]]:
    """Converts epoch ms/s or ISO strings into timezone-aware IST datetime, ISO-8601 string, display, and hours_left."""
    if raw_val in (None, "", 0):
        return None, None, "Immediate", None
    dt_ist: Optional[datetime] = None
    try:
        if isinstance(raw_val, datetime):
            dt_ist = raw_val.astimezone(IST) if raw_val.tzinfo else IST.localize(raw_val)
        elif isinstance(raw_val, (int, float)) or (isinstance(raw_val, str) and raw_val.strip().isdigit()):
            num = float(raw_val)
            epoch_sec = num / 1000.0 if num > 1e11 else num
            dt_ist = datetime.fromtimestamp(epoch_sec, tz=IST)
        else:
            clean = str(raw_val).strip().replace("Z", "+00:00")
            dt = datetime.fromisoformat(clean)
            dt_ist = dt.astimezone(IST) if dt.tzinfo else IST.localize(dt)
    except Exception:
        return None, None, str(raw_val), None

    now_ist = datetime.now(IST)
    hours_left = max(0.0, (dt_ist - now_ist).total_seconds() / 3600.0)
    display = dt_ist.strftime("%d %b %Y, %I:%M %p IST")
    return dt_ist, dt_ist.isoformat(), display, hours_left


def extract_question_required(attr: dict) -> bool:
    """Normalizes Superset question mandatory/required flags to strict bool (never None)."""
    if not isinstance(attr, dict):
        return False
    for key in ("mandatory", "isMandatory", "required", "isRequired"):
        if key in attr and attr[key] is not None:
            val = attr[key]
            if isinstance(val, str):
                return val.strip().lower() in ("true", "yes", "1", "required")
            return bool(val)
    validation = str(attr.get("validation") or "").strip().lower()
    return validation == "required"


def detect_question_dependency(question_text: str, prev_questions: List[dict]) -> Optional[Dict[str, str]]:
    """Detects `If Yes/No ...` follow-up questions and links them to the preceding Yes/No parent question."""
    if not question_text or not prev_questions:
        return None
    m = _IF_COND_RE.match(str(question_text))
    if not m:
        return None
    when_val = "Yes" if m.group(1).lower() == "yes" else "No"

    for pq in reversed(prev_questions):
        opts = [str(o).strip().lower() for o in (pq.get("options") or [])]
        q_type = str(pq.get("type") or "").lower()
        if (opts and set(opts).issubset({"yes", "no", "y", "n", "true", "false"})) or q_type in ("boolean", "bool"):
            return {
                "parent_key": pq.get("key") or "",
                "when_value": when_val,
                "fallback_value": "NA",
            }
    return None


def normalize_profile(prof: Optional[dict]) -> Dict[str, Any]:
    """Unifies field aliases (`emergency_contact_phone` -> `emergency_contact_number`) cleanly."""
    if not isinstance(prof, dict):
        return {}
    out = dict(prof)
    if out.get("emergency_contact_phone") and not out.get("emergency_contact_number"):
        out["emergency_contact_number"] = str(out["emergency_contact_phone"])
    out.pop("emergency_contact_phone", None)
    return out


def normalize_job(job_dict: dict) -> Dict[str, Any]:
    """Produces canonical top-level job fields while preserving backward-compatible keys."""
    if not isinstance(job_dict, dict):
        return {}
    out = dict(job_dict)
    raw = out.get("raw") if isinstance(out.get("raw"), dict) else {}

    ctc_raw = raw.get("placementCategoryName") or out.get("ctc_display") or raw.get("ctcDisplay") or ""
    ctc_info = parse_ctc(ctc_raw)
    out["ctc_display"] = out.get("ctc_display") or ctc_info["display"]
    out["ctc_min_lpa"] = ctc_info["min_lpa"]
    out["ctc_max_lpa"] = ctc_info["max_lpa"]
    out["ctc_unit"] = ctc_info["unit"]
    out["ctc_tier"] = ctc_info["tier"]

    dl_source = raw.get("jobProfileApplicationDeadline") or out.get("deadline_iso") or out.get("deadline_dt")
    _, dl_iso, dl_disp, dl_hours = parse_ist_datetime(dl_source)
    out["deadline_iso"] = dl_iso
    if not out.get("deadline_display"):
        out["deadline_display"] = dl_disp
    if out.get("hours_left") is None and dl_hours is not None:
        out["hours_left"] = dl_hours

    created_raw = raw.get("createdAt") or out.get("created_at_ms") or 0
    try:
        created_ms = float(created_raw)
        if 0 < created_ms < 1e11:
            created_ms *= 1000.0
        out["created_at_ms"] = int(created_ms)
    except Exception:
        out["created_at_ms"] = 0

    _, applied_iso, _, _ = parse_ist_datetime(raw.get("jobApplicationLastAppliedAt") or out.get("applied_at_iso"))
    out["applied_at_iso"] = applied_iso

    out["app_stage"] = str(
        out.get("app_stage")
        or raw.get("jobApplicationCurrentStage")
        or raw.get("jobProfileCurrentStage")
        or ("YET_TO_PROCESS" if out.get("is_applied") else "")
    )
    out["sector"] = str(out.get("sector") or raw.get("sectorName") or "")
    out["position_type"] = str(out.get("position_type") or raw.get("positionTypeLabel") or "")

    is_elig = bool(out.get("is_eligible", True))
    if not is_elig:
        reason = raw.get("nonEligibilityReason") or raw.get("ineligibleReason") or ("Age restricted" if raw.get("ageRestricted") else "Criteria mismatch")
        out["eligibility_reason"] = str(reason)
    else:
        out["eligibility_reason"] = ""

    attrs = raw.get("attributeList")
    if isinstance(attrs, list):
        out["questions_count"] = len(attrs)
        out["has_questions"] = len(attrs) > 0
    else:
        out.setdefault("questions_count", 0)
        out.setdefault("has_questions", False)

    return out
