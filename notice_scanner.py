"""
Homepage Notice Intelligence, Auto Shortlist Roll-Number PDF Scanner,
Smart Notice Categorizer, and Pending Offer Watchdog for Superset Job Pilot.
"""
import io
import logging
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import pytz
from bs4 import BeautifulSoup
from pypdf import PdfReader

from telegram_notifier import TelegramNotifier

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

PAGE_MARKER_PREFIX = "<<<SJP_PAGE:"
PAGE_MARKER_SUFFIX = ">>>"

ALLOWED_HTML_TAGS = {
    "p", "br", "b", "strong", "i", "em", "u", "ul", "ol", "li",
    "table", "thead", "tbody", "tr", "th", "td", "a", "span", "div",
    "h1", "h2", "h3", "h4", "h5", "h6", "hr", "blockquote", "code", "pre",
}
ALLOWED_URL_SCHEMES = ("http://", "https://", "mailto:")


# ═══════════════════════════════════════════════════════════════════════════
# 1. SERVER-SIDE HTML SANITIZATION & PLAIN TEXT EXTRACTION
# ═══════════════════════════════════════════════════════════════════════════

def sanitize_notice_html(raw_html: str) -> str:
    """
    Defence-in-depth server-side HTML sanitizer for Superset notice bodies.
    Strips scripts, iframes, styles, event handlers, and unsafe URLs.
    """
    if not raw_html:
        return ""
    try:
        soup = BeautifulSoup(raw_html, "html.parser")
        for bad in soup(["script", "style", "iframe", "object", "embed", "form", "input", "meta", "link", "svg"]):
            bad.decompose()

        for tag in list(soup.find_all(True)):
            if tag.name not in ALLOWED_HTML_TAGS:
                tag.unwrap()
                continue

            clean_attrs = {}
            if tag.name == "a":
                href = (tag.get("href") or "").strip()
                if href.lower().startswith(ALLOWED_URL_SCHEMES):
                    clean_attrs["href"] = href
                    clean_attrs["target"] = "_blank"
                    clean_attrs["rel"] = "noopener noreferrer"
            elif tag.name in ("td", "th"):
                for span_attr in ("colspan", "rowspan"):
                    val = str(tag.get(span_attr) or "").strip()
                    if val.isdigit():
                        clean_attrs[span_attr] = val
            tag.attrs = clean_attrs

        return str(soup).strip()
    except Exception as e:
        logger.debug(f"HTML sanitize fallback: {e}")
        return html_to_plain_text(raw_html)


def html_to_plain_text(raw_html: str) -> str:
    """Converts notice HTML into clean readable plain text preserving block line breaks without splitting inline tags like <sup>."""
    if not raw_html:
        return ""
    try:
        soup = BeautifulSoup(raw_html, "html.parser")
        for bad in soup(["script", "style"]):
            bad.decompose()
        for br in soup.find_all("br"):
            br.replace_with("\n")
        for block in soup.find_all(["p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6"]):
            block.append("\n")
        for cell in soup.find_all(["td", "th"]):
            cell.append(" | ")
        text = soup.get_text("")
        lines = [re.sub(r"[ \t\xa0]+", " ", ln).strip(" |") for ln in text.splitlines()]
        return "\n".join(ln for ln in lines if ln)
    except Exception:
        cleaned = re.sub(r"<[^>]+>", " ", raw_html)
        return re.sub(r"\s+", " ", cleaned).strip()


# ═══════════════════════════════════════════════════════════════════════════
# 2. PDF TEXT EXTRACTION WITH PAGE MARKERS
# ═══════════════════════════════════════════════════════════════════════════

def extract_pdf_text_with_pages(pdf_bytes: bytes) -> Tuple[str, int]:
    """
    Extracts text from every page of a PDF using pypdf, preserving page numbers
    via <<<SJP_PAGE:N>>> markers so cached text still reports the exact page number.
    """
    if not pdf_bytes:
        return "", 0
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        page_count = len(reader.pages)
        parts: List[str] = []
        for idx, page in enumerate(reader.pages, start=1):
            try:
                page_txt = (page.extract_text() or "").strip()
            except Exception as pe:
                logger.debug(f"Page {idx} extraction error: {pe}")
                page_txt = ""
            parts.append(f"{PAGE_MARKER_PREFIX}{idx}{PAGE_MARKER_SUFFIX}\n{page_txt}")
        return "\n".join(parts), page_count
    except Exception as e:
        logger.warning(f"Failed to parse PDF bytes ({len(pdf_bytes)}B): {e}")
        return "", 0


def split_marked_pages(marked_text: str) -> List[Tuple[int, str]]:
    """Splits cached page-marked PDF text back into [(page_num, page_text), ...]."""
    if not marked_text:
        return []
    pattern = re.compile(rf"{re.escape(PAGE_MARKER_PREFIX)}(\d+){re.escape(PAGE_MARKER_SUFFIX)}\n?")
    matches = list(pattern.finditer(marked_text))
    if not matches:
        return [(1, marked_text)]

    pages: List[Tuple[int, str]] = []
    for i, m in enumerate(matches):
        pnum = int(m.group(1))
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(marked_text)
        pages.append((pnum, marked_text[start:end].strip()))
    return pages


# ═══════════════════════════════════════════════════════════════════════════
# 3. PRECISION STUDENT MATCHER (ROLL NUMBER -> EMAIL -> FULL NAME)
# ═══════════════════════════════════════════════════════════════════════════

def _clean_roll(roll: str) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", (roll or "").strip())


def _extract_line_context(page_text: str, start_idx: int, end_idx: int) -> Tuple[str, str]:
    """Returns (matched_line, surrounding_snippet) around a regex match span."""
    line_start = page_text.rfind("\n", 0, start_idx)
    line_start = 0 if line_start == -1 else line_start + 1
    line_end = page_text.find("\n", end_idx)
    line_end = len(page_text) if line_end == -1 else line_end
    matched_line = re.sub(r"\s+", " ", page_text[line_start:line_end]).strip()

    snip_start = max(0, line_start - 120)
    snip_end = min(len(page_text), line_end + 180)
    snippet = re.sub(r"\s+", " ", page_text[snip_start:snip_end]).strip()
    return matched_line[:280], snippet[:400]


def search_student_in_text(
    text: str,
    roll_number: str = "",
    full_name: str = "",
    email: str = "",
    extra_emails: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Searches page-marked PDF text (or notice plain text) for a student's:
      1. KIIT Roll Number (exact boundary match — highest confidence)
      2. Email address (exact case-insensitive match)
      3. Full Name (strict multi-word match >= 2 words, >= 6 chars to avoid false positives)

    Returns match metadata dict or None.
    """
    if not text or not text.strip():
        return None

    pages = split_marked_pages(text)
    roll_clean = _clean_roll(roll_number)

    # Also derive roll number from KIIT email prefix if roll_number wasn't set (e.g. 23051234@kiit.ac.in)
    if not roll_clean and email and "@" in email:
        prefix = email.split("@", 1)[0].strip()
        if prefix.isdigit() and len(prefix) >= 6:
            roll_clean = prefix

    # Priority 1: Exact Roll Number match
    if roll_clean and len(roll_clean) >= 5:
        roll_re = re.compile(rf"(?<![0-9A-Za-z]){re.escape(roll_clean)}(?![0-9A-Za-z])", re.IGNORECASE)
        for pnum, ptxt in pages:
            m = roll_re.search(ptxt)
            if m:
                line, snip = _extract_line_context(ptxt, m.start(), m.end())
                return {
                    "matched": True,
                    "match_type": "EXACT_ROLL",
                    "matched_value": roll_clean,
                    "page_number": pnum,
                    "matched_line": line,
                    "snippet": snip,
                }

    # Priority 2: Exact Email match
    emails_to_check = []
    for em in [email] + list(extra_emails or []):
        em_clean = (em or "").strip().lower()
        if em_clean and "@" in em_clean and em_clean not in emails_to_check:
            emails_to_check.append(em_clean)

    for em_clean in emails_to_check:
        em_re = re.compile(rf"(?<![A-Za-z0-9._%+-]){re.escape(em_clean)}(?![A-Za-z0-9._%+-])", re.IGNORECASE)
        for pnum, ptxt in pages:
            m = em_re.search(ptxt)
            if m:
                line, snip = _extract_line_context(ptxt, m.start(), m.end())
                return {
                    "matched": True,
                    "match_type": "EXACT_EMAIL",
                    "matched_value": em_clean,
                    "page_number": pnum,
                    "matched_line": line,
                    "snippet": snip,
                }

    # Priority 3: Multi-word Full Name match
    name_tokens = [tok for tok in re.split(r"\s+", (full_name or "").strip()) if len(tok) >= 2]
    if len(name_tokens) >= 2 and sum(len(t) for t in name_tokens) >= 6:
        # Match full sequence with flexible spacing (handles line wraps or middle spaces)
        name_pattern = r"\b" + r"\s+".join(re.escape(t) for t in name_tokens) + r"\b"
        name_re = re.compile(name_pattern, re.IGNORECASE)
        for pnum, ptxt in pages:
            m = name_re.search(ptxt)
            if m:
                line, snip = _extract_line_context(ptxt, m.start(), m.end())
                return {
                    "matched": True,
                    "match_type": "FULL_NAME",
                    "matched_value": " ".join(name_tokens),
                    "page_number": pnum,
                    "matched_line": line,
                    "snippet": snip,
                }

    return None


# ═══════════════════════════════════════════════════════════════════════════
# 4. EVENT LOGISTICS EXTRACTOR (DATE, TIME, VENUE, MODE, MEETING LINK)
# ═══════════════════════════════════════════════════════════════════════════

DATE_PATTERNS = [
    # e.g. 21st Nov'2026, 20th Nov’2026, 18th & 19th Nov'2026, 22nd November 2026
    re.compile(
        r"\b(?:\d{1,2}(?:st|nd|rd|th)?\s*(?:&|and|to|-)\s*)?\d{1,2}(?:st|nd|rd|th)?\s+"
        r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|"
        r"Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
        r"[\s,'’`-]*\d{2,4}\b",
        re.IGNORECASE,
    ),
    # e.g. 21/11/2026 or 21-11-2026
    re.compile(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b"),
]

TIME_PATTERNS = [
    # e.g. 9.30 AM, 09:30 AM - 11:00 AM, 5:00 PM IST, 10 AM
    re.compile(
        r"\b\d{1,2}(?:[:.]\d{2})?\s*(?:AM|PM|A\.M\.|P\.M\.)(?:\s*(?:-|to|–)\s*\d{1,2}(?:[:.]\d{2})?\s*(?:AM|PM|A\.M\.|P\.M\.))?(?:\s*IST)?\b",
        re.IGNORECASE,
    ),
]

MEET_LINK_RE = re.compile(
    r"(?:https?://)?(?:meet\.google\.com/[a-z0-9-]+|[\w.-]*zoom\.us/j/[^\s,;)<>\"']+|teams\.microsoft\.com/l/meetup-join/[^\s,;)<>\"']+)",
    re.IGNORECASE,
)

VENUE_PATTERNS = [
    # Explicit "Reporting Venue : ..." or "Venue : ..."
    re.compile(
        r"(?:Reporting\s+Venue|Venue|Location)\s*[:\-–]\s*([^\n|]{4,140}?)(?=(?:\s+Reporting\s+Time|\s+Time\s*:|\s+Date\s*:|\n|$))",
        re.IGNORECASE,
    ),
    # KIIT specific campus venues: Ladies Club, Computer Lab, Campus-20..., Auditorium, Campus-3...
    re.compile(
        r"\b((?:Ladies\s+Club|Computer\s+Labs?|Convention\s+Center|Auditorium|Seminar\s+Hall|Conference\s+Hall|Placement\s+Cell|T&P\s+Cell|Campus[\s\-]?\d+)[^\n.;|]{0,110})",
        re.IGNORECASE,
    ),
]


def detect_event_type(title: str, body: str = "") -> str:
    combined = f"{title} {body[:300]}".lower()
    if "group discussion" in combined or re.search(r"\bgd\b", combined):
        return "Group Discussion"
    if "mock" in combined and "test" in combined:
        return "Mock & Online Test"
    if "online test" in combined or "assessment" in combined or "coding test" in combined or "aptitude" in combined:
        return "Online Test / Assessment"
    if "interview" in combined:
        return "Interview"
    if "pre-placement" in combined or re.search(r"\bppt\b", combined):
        return "Pre-Placement Talk"
    if "extension notice" in combined or "deadline" in combined:
        return "Deadline Extension"
    if "revised notice" in combined or "rescheduled" in combined:
        return "Revised Schedule"
    if "registration" in combined:
        return "Drive Registration"
    if "shortlist" in combined or "recruitment drive" in combined:
        return "Recruitment Drive"
    if title.lower().startswith("open for applications"):
        return "Job Opening"
    return "Campus Notice"


def extract_event_details(
    notice_title: str,
    notice_text: str = "",
    pdf_header_text: str = "",
    matched_line: str = "",
) -> Dict[str, str]:
    """
    Extracts structured Date, Time, Venue/Mode, Meeting Link, and Event Type.
    Prioritizes student-specific matched_line (e.g. GD Panel Time & Meet Link),
    then notice title/body, then PDF page-1 header.
    """
    event_type = detect_event_type(notice_title, notice_text)

    # 1. Meeting link (matched row first, then notice body, then PDF header)
    meeting_link = ""
    for src in (matched_line, notice_text, pdf_header_text):
        if not src:
            continue
        m = MEET_LINK_RE.search(src)
        if m:
            raw_link = m.group(0).rstrip(".,);")
            meeting_link = raw_link if raw_link.lower().startswith("http") else f"https://{raw_link}"
            break

    # 2. Time / Slot (matched row FIRST because panels have personal slots!)
    ev_time = ""
    for src in (matched_line, notice_text, pdf_header_text, notice_title):
        if not src:
            continue
        for pat in TIME_PATTERNS:
            m = pat.search(src)
            if m:
                ev_time = re.sub(r"\s+", " ", m.group(0)).strip()
                break
        if ev_time:
            break

    # 3. Date (title FIRST since KIIT puts exact event date in title, avoiding notice-issue date confusion!)
    ev_date = ""
    for src in (notice_title, notice_text, pdf_header_text):
        if not src:
            continue
        # Look for "scheduled on <DATE>" or "Reporting Date : <DATE>" first inside body
        if src is not notice_title:
            sched_m = re.search(
                r"(?:scheduled\s+(?:on|today\s*\()|Reporting\s+Date\s*[:\-]|Test\s+Date\s*[:\-]|Date\s+of\s+(?:Test|Interview|GD)\s*[:\-])\s*([^\n.;]{5,45})",
                src,
                re.IGNORECASE,
            )
            if sched_m:
                for pat in DATE_PATTERNS:
                    dm = pat.search(sched_m.group(1))
                    if dm:
                        ev_date = re.sub(r"\s+", " ", dm.group(0)).strip()
                        break
                if ev_date:
                    break
        for pat in DATE_PATTERNS:
            dm = pat.search(src)
            if dm:
                ev_date = re.sub(r"\s+", " ", dm.group(0)).strip()
                break
        if ev_date:
            break

    # 4. Venue / Mode
    ev_venue = ""
    combined_for_venue = "\n".join(x for x in (notice_text, pdf_header_text) if x)
    for pat in VENUE_PATTERNS:
        vm = pat.search(combined_for_venue)
        if vm:
            cand = re.sub(r"\s+", " ", vm.group(1)).strip(" :,-–|")
            # Avoid picking up table column header words like "Reporting Time"
            if len(cand) >= 4 and not cand.lower().startswith("reporting time"):
                ev_venue = cand[:120]
                break

    if not ev_venue:
        low_all = f"{notice_title} {combined_for_venue}".lower()
        if "virtual mode" in low_all or "online mode" in low_all or meeting_link:
            ev_venue = "Virtual / Online Mode"
        elif "physical mode" in low_all or "kiit campus" in low_all:
            ev_venue = "KIIT Campus (Physical Mode)"

    # 5. Panel name if present in matched_line (e.g. Intellipaat GD rows)
    return {
        "event_type": event_type,
        "date": ev_date,
        "time": ev_time,
        "venue": ev_venue,
        "meeting_link": meeting_link,
    }


# ═══════════════════════════════════════════════════════════════════════════
# 5. SMART NOTICE CLASSIFICATION & DUPLICATE SPAM SUPPRESSION
# ═══════════════════════════════════════════════════════════════════════════

def classify_notice_category(raw_notice: Dict[str, Any], plain_text: str = "") -> Tuple[str, bool]:
    """
    Categorizes a Superset notice into one of 4 buckets + returns (category, is_spam):
      - 'shortlists_tests'    : Shortlists, Online Tests, GDs, Interviews, PPTs, Assessments
      - 'deadlines_revisions' : Extension notices, Revised notices, Registrations, Reschedules
      - 'job_openings'        : Auto-generated 'Open for applications - ...' posts
      - 'system_spam'         : Superset platform promos / app download announcements
    """
    title = (raw_notice.get("title") or "").strip()
    tl = title.lower()
    author = (raw_notice.get("lastModifiedByUserName") or raw_notice.get("publishedByUser") or "").strip()
    ntype = (raw_notice.get("type") or "").upper()
    atts = raw_notice.get("attachments") or []
    att_names = " ".join((a.get("name") or "").lower() for a in atts)
    body_low = (plain_text or "")[:400].lower()

    if author.lower() == "superset" or (ntype == "NOTICE" and any(k in tl for k in ["android app", "ios app", "beta", "launchpad", "superset ai", "webinar"])):
        return "system_spam", True

    if tl.startswith("open for applications"):
        return "job_openings", False

    # Deadlines / extensions / revisions / registrations
    if any(k in tl for k in ["extension notice", "revised notice", "reschedul", "postponed", "cancelled", "last date", "deadline", "registration for", "reminder"]):
        # If it also attaches a shortlist list PDF, classify as shortlists_tests so student sees it immediately!
        if "shortlist" in att_names or "shortlist" in tl:
            return "shortlists_tests", False
        return "deadlines_revisions", False

    # Shortlists, tests, GDs, interviews, drives
    if any(k in tl or k in att_names for k in [
        "shortlist", "online test", "mock test", "interview", "group discussion",
        "assessment", "recruitment drive", "pre-placement", "ppt", "venue", "selected", "result", "offer",
    ]) or re.search(r"\bgd\b", tl):
        return "shortlists_tests", False

    if any(k in body_low for k in ["shortlisted students", "online test", "reporting venue", "reporting time"]):
        return "shortlists_tests", False

    return "deadlines_revisions", False


def _normalize_dedupe_key(title: str, category: str) -> str:
    """Produces a normalized key to spot duplicate announcements/spam."""
    clean = re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()
    return f"{category}::{clean}"


def normalize_raw_notice(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Converts a raw Superset notice JSON object into a clean normalized dict."""
    nid = str(raw.get("identifier") or raw.get("id") or "")
    title = (raw.get("title") or "Untitled Notice").strip()
    raw_html = raw.get("content") or ""
    safe_html = sanitize_notice_html(raw_html)
    plain = html_to_plain_text(raw_html)

    pub_raw = raw.get("publishedAt") or raw.get("lastModifiedOn") or 0
    published_ts = 0
    published_display = "Recently"
    if isinstance(pub_raw, (int, float)) and pub_raw > 0:
        epoch_sec = float(pub_raw) / 1000.0 if float(pub_raw) > 1e11 else float(pub_raw)
        published_ts = int(epoch_sec)
        published_display = datetime.fromtimestamp(epoch_sec, tz=IST).strftime("%d %b %Y, %I:%M %p IST")
    elif isinstance(pub_raw, str) and pub_raw.strip():
        published_display = pub_raw.strip()

    category, is_spam = classify_notice_category(raw, plain)

    attachments = []
    for a in (raw.get("attachments") or []):
        if not isinstance(a, dict):
            continue
        aid = str(a.get("identifier") or a.get("uuid") or "")
        aname = (a.get("name") or a.get("fileName") or "attachment").strip()
        if not aid:
            continue
        is_pdf = aname.lower().endswith(".pdf")
        is_img = aname.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))
        attachments.append({
            "id": aid,
            "name": aname,
            "type": a.get("type") or ("DOC" if is_pdf else "IMAGE" if is_img else "FILE"),
            "is_pdf": is_pdf,
            "is_image": is_img,
            "page_count": 0,
            "scanned": False,
            "matched": False,
        })

    event_details = extract_event_details(title, plain)

    return {
        "id": nid,
        "title": title,
        "author": (raw.get("lastModifiedByUserName") or raw.get("publishedByUser") or "T&P Cell").strip(),
        "designation": (raw.get("lastModifiedByUserDesignation") or "").strip(),
        "type": (raw.get("type") or "MESSAGE").upper(),
        "category": category,
        "is_spam": is_spam,
        "is_duplicate": False,
        "published_ts": published_ts,
        "published_display": published_display,
        "plain_text": plain,
        "snippet": plain[:260] + ("..." if len(plain) > 260 else ""),
        "safe_html": safe_html,
        "has_comments": bool(raw.get("hasComments")),
        "attachments": attachments,
        "pdf_count": sum(1 for a in attachments if a["is_pdf"]),
        "is_shortlisted": False,
        "shortlist_match": None,
        "event_details": event_details,
    }


# ═══════════════════════════════════════════════════════════════════════════
# 6. FULL PER-USER NOTICE SCANNER + PENDING OFFER WATCHDOG ORCHESTRATOR
# ═══════════════════════════════════════════════════════════════════════════

def scan_notices_for_user(
    superset_client,
    user_manager,
    user: Dict[str, Any],
    max_pages: int = 2,
    page_size: int = 25,
    max_pdfs_to_download: int = 18,
    send_alerts: bool = True,
) -> Dict[str, Any]:
    """
    Fetches live homepage notices, downloads & caches any uncached PDFs, scans
    every PDF + notice body for the logged-in student's roll number / email / full name,
    polls /pending_actions for job offers, updates per-user SQLite caches, and fires
    deduplicated Telegram alerts for brand-new shortlist hits or pending offers.
    """
    user_id = user["user_id"]
    profile = user.get("profile") or {}
    roll_number = str(user.get("roll_number") or profile.get("roll_number") or "").strip()
    full_name = str(user.get("full_name") or profile.get("full_name") or "").strip()
    email = str(user.get("email") or profile.get("email") or "").strip()
    extra_emails = [
        str(profile.get(k) or "").strip()
        for k in ("personal_email", "secondary_email", "alternate_email")
        if profile.get(k)
    ]

    raw_notices = superset_client.fetch_notices(page=0, size=page_size, max_pages=max_pages)
    normalized = [normalize_raw_notice(rn) for rn in raw_notices]

    # Mark exact/near-duplicate notices (preserving the newest occurrence)
    seen_dedupe_keys = set()
    for n in normalized:
        dkey = _normalize_dedupe_key(n["title"], n["category"])
        if dkey in seen_dedupe_keys:
            n["is_duplicate"] = True
        else:
            seen_dedupe_keys.add(dkey)

    # Prioritize scanning shortlist/test/deadline PDFs first
    priority_order = {"shortlists_tests": 0, "deadlines_revisions": 1, "job_openings": 2, "system_spam": 3}
    scan_queue = sorted(
        range(len(normalized)),
        key=lambda idx: (priority_order.get(normalized[idx]["category"], 9), idx),
    )

    pdfs_downloaded_now = 0
    pdfs_from_cache = 0
    pdfs_scanned_total = 0
    new_shortlist_alerts = 0
    pdf_header_by_notice: Dict[str, str] = {}

    for idx in scan_queue:
        n = normalized[idx]
        if n["is_spam"]:
            continue

        best_match: Optional[Dict[str, Any]] = None
        notice_pdf_headers: List[str] = []

        # 1. Scan PDF attachments (check SQLite cache before hitting S3)
        for att in n["attachments"]:
            if not att["is_pdf"]:
                continue

            cached_pdf = user_manager.get_cached_pdf_text(att["id"])
            pdf_text = ""
            page_count = 0

            if cached_pdf is not None:
                pdf_text = cached_pdf.get("extracted_text") or ""
                page_count = int(cached_pdf.get("page_count") or 0)
                pdfs_from_cache += 1
                att["scanned"] = True
                att["page_count"] = page_count
            elif pdfs_downloaded_now < max_pdfs_to_download:
                pdf_bytes, _, _ = superset_client.download_notice_attachment(n["id"], att["id"])
                if pdf_bytes:
                    pdf_text, page_count = extract_pdf_text_with_pages(pdf_bytes)
                    user_manager.set_cached_pdf_text(att["id"], att["name"], page_count, pdf_text)
                    pdfs_downloaded_now += 1
                    att["scanned"] = True
                    att["page_count"] = page_count

            if att["scanned"]:
                pdfs_scanned_total += 1
                pages = split_marked_pages(pdf_text)
                if pages:
                    notice_pdf_headers.append(pages[0][1][:1600])

                match = search_student_in_text(
                    pdf_text,
                    roll_number=roll_number,
                    full_name=full_name,
                    email=email,
                    extra_emails=extra_emails,
                )
                if match:
                    att["matched"] = True
                    match["attachment_id"] = att["id"]
                    match["attachment_name"] = att["name"]
                    match["source"] = "PDF"
                    # Prefer EXACT_ROLL over EMAIL/NAME if multiple attachments match
                    if not best_match or (
                        match["match_type"] == "EXACT_ROLL" and best_match["match_type"] != "EXACT_ROLL"
                    ):
                        best_match = match

        if notice_pdf_headers:
            pdf_header_by_notice[n["id"]] = "\n".join(notice_pdf_headers)

        # 2. Also scan notice HTML plain text (in case roll number is pasted directly in notice body!)
        if not best_match and n["category"] in ("shortlists_tests", "deadlines_revisions"):
            body_match = search_student_in_text(
                n["plain_text"],
                roll_number=roll_number,
                full_name=full_name,
                email=email,
                extra_emails=extra_emails,
            )
            if body_match:
                body_match["attachment_id"] = ""
                body_match["attachment_name"] = "Notice Body"
                body_match["source"] = "NOTICE_BODY"
                best_match = body_match

        # 3. Enrich event logistics with PDF page-1 header + student's specific matched row!
        n["event_details"] = extract_event_details(
            notice_title=n["title"],
            notice_text=n["plain_text"],
            pdf_header_text=pdf_header_by_notice.get(n["id"], ""),
            matched_line=(best_match or {}).get("matched_line", ""),
        )

        if best_match:
            n["is_shortlisted"] = True
            n["shortlist_match"] = best_match

            if send_alerts:
                alert_key = f"shortlist::{n['id']}"
                if not user_manager.has_sent_alert(user_id, alert_key):
                    chat_id = user.get("telegram_chat_id") or None
                    notifier = TelegramNotifier(chat_id=chat_id)
                    if notifier.send_shortlist_alert(n, best_match, full_name, roll_number):
                        new_shortlist_alerts += 1
                    # Mark as alerted even if Telegram isn't paired for this friend so we don't retry forever
                    user_manager.mark_alert_sent(user_id, alert_key)

    # Persist scanned notices cache
    user_manager.set_cached_notices(user_id, normalized)

    # 4. Pending Offer Watchdog (/pending_actions)
    pending_actions = superset_client.fetch_pending_actions()
    user_manager.set_cached_pending_actions(user_id, pending_actions)

    new_offer_alerts = 0
    if send_alerts and pending_actions:
        chat_id = user.get("telegram_chat_id") or None
        notifier = TelegramNotifier(chat_id=chat_id)
        for act in pending_actions:
            offer_key = f"pending_offer::{act['id']}"
            if not user_manager.has_sent_alert(user_id, offer_key):
                if notifier.send_pending_offer_alert(act, full_name):
                    new_offer_alerts += 1
                user_manager.mark_alert_sent(user_id, offer_key)

    now_ts = int(time.time())
    summary = summarize_notices(
        notices=normalized,
        pending_actions=pending_actions,
        notices_updated_at=now_ts,
        pending_updated_at=now_ts,
        student_roll=roll_number,
        student_name=full_name,
    )
    summary["scan_stats"] = {
        "pdfs_downloaded_now": pdfs_downloaded_now,
        "pdfs_from_cache": pdfs_from_cache,
        "pdfs_scanned_total": pdfs_scanned_total,
        "new_shortlist_alerts": new_shortlist_alerts,
        "new_offer_alerts": new_offer_alerts,
    }
    return summary


def summarize_notices(
    notices: List[Dict[str, Any]],
    pending_actions: List[Dict[str, Any]],
    notices_updated_at: int = 0,
    pending_updated_at: int = 0,
    student_roll: str = "",
    student_name: str = "",
) -> Dict[str, Any]:
    """Builds a consistent API response payload for the Smart Notice Feed & Watchdog."""
    clean_notices = [n for n in notices if not n.get("is_spam") and not n.get("is_duplicate")]
    shortlisted = [n for n in notices if n.get("is_shortlisted")]

    counts = {
        "total": len(notices),
        "clean": len(clean_notices),
        "shortlisted": len(shortlisted),
        "shortlists_tests": sum(1 for n in clean_notices if n.get("category") == "shortlists_tests"),
        "deadlines_revisions": sum(1 for n in clean_notices if n.get("category") == "deadlines_revisions"),
        "job_openings": sum(1 for n in clean_notices if n.get("category") == "job_openings"),
        "spam_hidden": sum(1 for n in notices if n.get("is_spam") or n.get("is_duplicate")),
        "pending_offers": len(pending_actions),
    }

    last_scanned_str = (
        datetime.fromtimestamp(notices_updated_at, tz=IST).strftime("%d %b %Y, %I:%M %p IST")
        if notices_updated_at
        else "Not scanned yet"
    )
    offer_checked_str = (
        datetime.fromtimestamp(pending_updated_at, tz=IST).strftime("%d %b %Y, %I:%M %p IST")
        if pending_updated_at
        else "Active"
    )

    return {
        "success": True,
        "notices": notices,
        "shortlisted_notices": shortlisted,
        "pending_actions": pending_actions,
        "counts": counts,
        "student_roll": student_roll,
        "student_name": student_name,
        "last_scanned_ts": notices_updated_at,
        "last_scanned_display": last_scanned_str,
        "pending_checked_ts": pending_updated_at,
        "pending_checked_display": offer_checked_str,
    }
