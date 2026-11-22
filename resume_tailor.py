"""
Company-Specific Resume Tailor, Project Ranker, PDF Importer & 1-Page A4 PDF Builder
for Superset Job Pilot.

Multi-user isolated: pulls every student's projects, skills, certifications, education,
and optional passport photo strictly from their own encrypted vault profile.
"""
import html as _html
import io
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pypdf import PdfReader

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════
# 1. ROLE TRACKS & SKILL TAXONOMY (USED FOR JD ANALYSIS & PROJECT RANKING)
# ═══════════════════════════════════════════════════════════════════════════

SKILL_TAXONOMY = [
    # Languages
    "javascript", "typescript", "python", "java", "c++", "c#", "go", "golang", "dart", "sql", "html5", "html", "css3", "css", "kotlin", "swift",
    # Frontend
    "react", "react.js", "next.js", "nextjs", "angular", "vue", "vue.js", "tailwind", "radix ui", "react query", "redux", "vite", "responsive", "ui/ux", "figma",
    # Backend & APIs
    "node.js", "nodejs", "express", "flask", "django", "fastapi", "spring boot", "spring", "rest api", "rest apis", "restful", "graphql", "microservices", "webhooks", "authentication", "oauth", "rbac",
    # Databases & Cloud
    "postgresql", "postgres", "supabase", "mysql", "mongodb", "sqlite", "redis", "dynamodb", "aws", "aws s3", "s3", "gcp", "azure", "docker", "kubernetes", "netlify", "vercel", "ci/cd", "git", "github", "linux",
    # SaaS / Product / Domain
    "saas", "hrms", "e-commerce", "ecommerce", "admin panel", "dashboard", "analytics", "crm", "ats", "recruitment", "payment", "razorpay", "stripe", "pdf", "multi-user", "scalable",
    # Testing / QA
    "vitest", "jest", "testing library", "unittest", "pytest", "selenium", "cypress", "playwright", "qa", "manual testing", "regression", "api testing", "jira",
    # Mobile & AI/ML
    "flutter", "android", "ios", "machine learning", "deep learning", "tensorflow", "pytorch", "scikit-learn", "nlp", "llm", "gemini", "openai", "rag", "data engineering", "etl", "pandas", "spark",
]

ROLE_TRACKS = [
    (
        "Full Stack Developer",
        ["full stack", "fullstack", "full-stack", "saas", "hrms", "react", "next.js", "node.js", "postgresql", "rest api"],
        ["Frontend", "Backend & APIs", "Languages", "Databases & Cloud", "Testing & Tools"],
    ),
    (
        "Backend Engineer",
        ["backend", "back-end", "api", "microservices", "spring boot", "flask", "django", "node.js", "postgresql", "mysql", "sql", "database"],
        ["Backend & APIs", "Databases & Cloud", "Languages", "Testing & Tools", "Frontend"],
    ),
    (
        "Frontend Developer",
        ["frontend", "front-end", "react", "next.js", "angular", "vue", "typescript", "tailwind", "ui/ux", "responsive", "css"],
        ["Frontend", "Languages", "Testing & Tools", "Backend & APIs", "Databases & Cloud"],
    ),
    (
        "AI / ML Engineer",
        ["machine learning", "ai", "artificial intelligence", "llm", "gemini", "tensorflow", "pytorch", "nlp", "deep learning", "data science", "rag", "cognitive"],
        ["Languages", "AI & Machine Learning", "Backend & APIs", "Databases & Cloud", "Testing & Tools", "Frontend"],
    ),
    (
        "Data Engineer",
        ["data engineer", "data engineering", "etl", "sql", "postgresql", "spark", "pipeline", "analytics", "warehouse", "python"],
        ["Databases & Cloud", "Languages", "Backend & APIs", "Testing & Tools", "Frontend"],
    ),
    (
        "QA / Test Engineer",
        ["qa", "quality assurance", "tester", "testing", "selenium", "cypress", "playwright", "vitest", "unittest", "regression", "bug"],
        ["Testing & Tools", "Frontend", "Backend & APIs", "Databases & Cloud", "Languages"],
    ),
    (
        "Mobile App Developer",
        ["flutter", "android", "ios", "mobile", "dart", "react native", "kotlin", "swift"],
        ["Mobile Development", "Languages", "Frontend", "Backend & APIs", "Databases & Cloud"],
    ),
    (
        "Software Engineer",
        ["software engineer", "sde", "graduate engineer", "get", "developer", "system engineer", "trainee", "analyst"],
        ["Languages", "Frontend", "Backend & APIs", "Databases & Cloud", "Testing & Tools"],
    ),
]

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
]


def _find_chrome_binary() -> Optional[str]:
    env_bin = os.getenv("CHROME_BIN", "").strip()
    if env_bin and os.path.exists(env_bin) and os.access(env_bin, os.X_OK):
        return env_bin
    for cand in CHROME_CANDIDATES:
        if os.path.isabs(cand):
            if os.path.exists(cand) and os.access(cand, os.X_OK):
                return cand
        else:
            found = shutil.which(cand)
            if found:
                return found
    return None


# ═══════════════════════════════════════════════════════════════════════════
# 2. MASTER PORTFOLIO NORMALIZATION (STRICTLY PER-USER ENCRYPTED VAULT)
# ═══════════════════════════════════════════════════════════════════════════

def get_resume_master(profile: Dict[str, Any]) -> Dict[str, Any]:
    """
    Returns a clean normalized master resume dictionary derived exclusively
    from the authenticated student's encrypted vault profile.
    """
    prof = profile or {}
    saved = prof.get("resume_master") if isinstance(prof.get("resume_master"), dict) else {}

    # Default skills structure if user hasn't customized yet
    default_skills = [
        {"category": "Languages", "items": ""},
        {"category": "Frontend", "items": ""},
        {"category": "Backend & APIs", "items": ""},
        {"category": "Databases & Cloud", "items": ""},
        {"category": "Testing & Tools", "items": ""},
    ]
    raw_skills = saved.get("skills")
    skills_list: List[Dict[str, str]] = []
    if isinstance(raw_skills, list) and raw_skills:
        for s in raw_skills:
            if isinstance(s, dict) and s.get("category"):
                skills_list.append({
                    "category": str(s.get("category") or "").strip(),
                    "items": str(s.get("items") or "").strip(),
                })
    elif isinstance(raw_skills, dict) and raw_skills:
        for k, v in raw_skills.items():
            skills_list.append({"category": str(k).strip(), "items": str(v).strip()})
    if not skills_list:
        skills_list = default_skills

    # Normalize projects
    projects_list: List[Dict[str, Any]] = []
    for p in (saved.get("projects") or []):
        if not isinstance(p, dict) or not (p.get("title") or "").strip():
            continue
        bullets = [str(b).strip() for b in (p.get("bullets") or []) if str(b).strip()]
        projects_list.append({
            "id": str(p.get("id") or uuid.uuid4().hex[:8]),
            "title": str(p.get("title") or "").strip(),
            "link": str(p.get("link") or "").strip(),
            "badge": str(p.get("badge") or "").strip(),
            "tech": str(p.get("tech") or "").strip(),
            "tags": [str(t).strip().lower() for t in (p.get("tags") or []) if str(t).strip()],
            "bullets": bullets[:4],
        })

    # Normalize certifications
    certs_list: List[Dict[str, str]] = []
    for c in (saved.get("certifications") or []):
        if isinstance(c, dict) and (c.get("title") or "").strip():
            certs_list.append({
                "title": str(c.get("title") or "").strip(),
                "issuer": str(c.get("issuer") or "").strip(),
                "year": str(c.get("year") or "").strip(),
            })
        elif isinstance(c, str) and c.strip():
            certs_list.append({"title": c.strip(), "issuer": "", "year": ""})

    return {
        "full_name": str(saved.get("full_name") or prof.get("full_name") or "").strip(),
        "email": str(saved.get("email") or prof.get("email") or "").strip(),
        "phone": str(saved.get("phone") or prof.get("phone_number") or "").strip(),
        "location": str(saved.get("location") or prof.get("current_location") or "Bhubaneswar, Odisha").strip(),
        "github": str(saved.get("github") or prof.get("github_link") or "").strip(),
        "linkedin": str(saved.get("linkedin") or prof.get("linkedin_link") or "").strip(),
        "portfolio": str(saved.get("portfolio") or prof.get("portfolio_link") or "").strip(),
        "headline": str(saved.get("headline") or "").strip(),
        "summary_base": str(saved.get("summary_base") or "").strip(),
        "photo_b64": str(saved.get("photo_b64") or "").strip(),
        "education": {
            "degree": str(prof.get("graduation_degree") or "B.Tech").strip(),
            "branch": str(prof.get("graduation_branch") or "Computer Science and Engineering").strip(),
            "college": str(prof.get("graduation_college") or "KIIT University, Bhubaneswar").strip(),
            "grad_year": str(prof.get("graduation_passing_year") or "2027").strip(),
            "cgpa": str(prof.get("current_cgpa") or "").strip(),
            "twelfth_school": str(prof.get("twelfth_college") or "").strip(),
            "twelfth_board": str(prof.get("twelfth_board") or "").strip(),
            "twelfth_year": str(prof.get("twelfth_passing_year") or "").strip(),
            "twelfth_pct": str(prof.get("twelfth_percentage") or "").strip(),
            "twelfth_gpa": str(prof.get("twelfth_gpa") or "").strip(),
            "tenth_school": str(prof.get("tenth_school") or "").strip(),
            "tenth_board": str(prof.get("tenth_board") or "").strip(),
            "tenth_year": str(prof.get("tenth_passing_year") or "").strip(),
            "tenth_pct": str(prof.get("tenth_percentage") or "").strip(),
            "tenth_gpa": str(prof.get("tenth_gpa") or "").strip(),
        },
        "skills": skills_list,
        "projects": projects_list,
        "certifications": certs_list,
    }


# ═══════════════════════════════════════════════════════════════════════════
# 3. JD ANALYZER, PROJECT SCORER & TAILOR ENGINE
# ═══════════════════════════════════════════════════════════════════════════

def _extract_keywords(text: str) -> List[str]:
    low = (text or "").lower()
    found: List[str] = []
    for kw in SKILL_TAXONOMY:
        if len(kw) <= 3:
            if re.search(rf"(?<![a-z0-9]){re.escape(kw)}(?![a-z0-9])", low):
                found.append(kw)
        else:
            if kw in low:
                found.append(kw)
    # Deduplicate aliases (e.g. react + react.js -> react)
    deduped = []
    for kw in found:
        norm = kw.replace(".js", "").replace("nextjs", "next.js").replace("nodejs", "node.js").replace("restful", "rest api").replace("rest apis", "rest api")
        if norm not in deduped:
            deduped.append(norm)
    return deduped


def analyze_jd(company: str, role: str, jd_text: str = "") -> Dict[str, Any]:
    """Identifies the target engineering track, key required skills, location & photo cues."""
    combined = f"{company} {role} {jd_text}".lower()
    role_low = (role or "").lower()

    best_track = "Software Engineer"
    best_skill_order = ["Languages", "Frontend", "Backend & APIs", "Databases & Cloud", "Testing & Tools"]
    best_score = -1

    for track_name, track_kws, skill_order in ROLE_TRACKS:
        score = 0
        for kw in track_kws:
            if kw in role_low:
                score += 6
            if kw in combined:
                score += 2
        if score > best_score:
            best_score = score
            best_track = track_name
            best_skill_order = skill_order

    jd_keywords = _extract_keywords(f"{role} {jd_text}")
    if not jd_keywords:
        jd_keywords = ["react", "typescript", "python", "rest api", "sql", "git"]

    photo_requested = any(k in combined for k in ("photo in resume", "with photo", "photograph"))

    # Extract target city if mentioned in JD
    target_city = ""
    for city in ("Bengaluru", "Bangalore", "Hyderabad", "Pune", "Mumbai", "Chennai", "Gurugram", "Noida", "Delhi", "Kolkata", "Bhubaneswar"):
        if city.lower() in combined:
            target_city = "Bengaluru" if city.lower() == "bangalore" else city
            break

    return {
        "track": best_track,
        "skill_order": best_skill_order,
        "jd_keywords": jd_keywords,
        "photo_requested": photo_requested,
        "target_city": target_city,
    }


def _score_project(project: Dict[str, Any], jd_analysis: Dict[str, Any], role: str, jd_text: str) -> Dict[str, Any]:
    """Computes relevance score & human-readable rationale for a single project."""
    proj_blob = " ".join([
        project.get("title", ""),
        project.get("tech", ""),
        " ".join(project.get("tags") or []),
        " ".join(project.get("bullets") or []),
    ]).lower()

    jd_kws = jd_analysis.get("jd_keywords") or []
    track_low = (jd_analysis.get("track") or "").lower()
    role_low = (role or "").lower()
    jd_low = (jd_text or "").lower()

    matched_kws: List[str] = []
    score = 10  # baseline for any complete project

    for kw in jd_kws:
        if kw in proj_blob:
            matched_kws.append(kw)
            score += 9

    # Domain & production delivery bonuses
    if any(k in proj_blob for k in ("production", "live", "client", "deployed")):
        score += 10
    if ("saas" in jd_low or "hrms" in jd_low or "enterprise" in jd_low) and any(k in proj_blob for k in ("saas", "multi-user", "recruitment", "ats", "crm", "admin", "portal")):
        score += 14
    if "full stack" in track_low and any(k in proj_blob for k in ("react", "next.js", "flask", "node.js", "postgresql", "supabase", "sqlite", "rest")):
        score += 10
    if "mobile" in track_low and any(k in proj_blob for k in ("flutter", "dart", "android", "ios")):
        score += 18
    if ("ai" in track_low or "ml" in track_low) and any(k in proj_blob for k in ("gemini", "llm", "tensorflow", "machine learning", "ai", "rag")):
        score += 16
    if "qa" in track_low and any(k in proj_blob for k in ("vitest", "testing library", "unittest", "pytest", "test")):
        score += 16
    if "flutter" not in role_low and "mobile" not in track_low and "flutter" in proj_blob and "react" not in proj_blob and "next.js" not in proj_blob and "flask" not in proj_blob:
        # Slight penalty for pure-mobile project when applying to pure web/backend role
        score -= 8

    # Normalize score to 0..100 display range
    pct_score = max(15, min(99, score))
    if matched_kws:
        why = f"Matches {', '.join(matched_kws[:5])}"
    else:
        why = "Strong general software engineering project"

    return {
        **project,
        "match_score": pct_score,
        "matched_keywords": matched_kws[:8],
        "why_selected": why,
    }


def _clean_resume_text(text: str) -> str:
    """Enforces clean CV rules: strips any accidental backlog mentions or harsh crypto names."""
    if not text:
        return ""
    cleaned = re.sub(r",?\s*\(?\s*0\s+(?:active\s+)?backlogs?\s*\)?", "", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bPBKDF2(?:-HMAC(?:-SHA256)?)?\b", "encrypted", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned


def _build_deterministic_summary(master: Dict[str, Any], company: str, role: str, track: str, selected_projects: List[Dict[str, Any]]) -> Tuple[str, str]:
    """Generates a sharp, truthful, company-tailored headline & professional summary without requiring an LLM."""
    edu = master.get("education") or {}
    college = "KIIT University" if "kiit" in (edu.get("college") or "").lower() else (edu.get("college") or "University")
    grad_year = edu.get("grad_year") or "2027"
    cgpa_part = f", {edu['cgpa']} CGPA" if edu.get("cgpa") else ""
    branch = edu.get("branch") or "Computer Science & Engineering"

    # Build top stack preview from skills
    skill_tokens = []
    for s in master.get("skills") or []:
        for tok in [t.strip() for t in (s.get("items") or "").split(",") if t.strip()]:
            short = re.sub(r"\s*\([^)]*\)", "", tok).strip()
            if short and short not in skill_tokens:
                skill_tokens.append(short)
            if len(skill_tokens) >= 5:
                break
        if len(skill_tokens) >= 5:
            break

    stack_str = ", ".join(skill_tokens[:5]) if skill_tokens else "React, TypeScript, Python & SQL"
    headline = f"{track} | {stack_str}"

    proj_highlights = []
    for p in selected_projects[:3]:
        t = (p.get("title") or "").split("—")[0].split("–")[0].strip()
        if t:
            proj_highlights.append(t)
    proj_phrase = (
        f"Hands-on experience delivering {', '.join(proj_highlights[:-1])} and {proj_highlights[-1]}"
        if len(proj_highlights) >= 2
        else (f"Hands-on experience building {proj_highlights[0]}" if proj_highlights else "Hands-on experience building end-to-end web and software systems")
    )

    company_clean = (company or "").strip()
    role_clean = (role or track).strip()
    if len(role_clean) > 48:
        role_clean = track

    closing = (
        f"Eager to contribute end-to-end as a {role_clean} at {company_clean}."
        if company_clean
        else f"Eager to contribute end-to-end in a {role_clean} role."
    )

    summary = (
        f"Final-year B.Tech {branch} student at {college} ({grad_year} Batch{cgpa_part}) focused on "
        f"building scalable, maintainable software and responsive web applications. {proj_phrase}, "
        f"with practical strength across frontend development, REST API integration, relational database design, "
        f"and automated testing. {closing}"
    )
    return _clean_resume_text(headline), _clean_resume_text(summary)


def _try_ai_polish_summary(
    ai_engine,
    master: Dict[str, Any],
    company: str,
    role: str,
    jd_text: str,
    selected_projects: List[Dict[str, Any]],
    fallback_headline: str,
    fallback_summary: str,
) -> Tuple[str, str, str]:
    """Optionally polishes headline & 3-line summary via Groq/Gemini while keeping 100% factual accuracy."""
    if ai_engine is None:
        return fallback_headline, fallback_summary, "deterministic"
    try:
        provider = ai_engine.get_active_provider()
        info = provider.get_info()
        if info.get("provider") == "heuristic":
            return fallback_headline, fallback_summary, "deterministic"

        proj_names = [f"{p['title']} ({p.get('tech', '')})" for p in selected_projects]
        edu = master.get("education") or {}
        prompt = (
            f"Write a concise JSON object with keys \"headline\" (max 72 chars) and \"summary\" (3 crisp sentences, max 55 words) "
            f"tailoring this student's resume for Company: {company}, Role: {role}.\n"
            f"Education: B.Tech {edu.get('branch')} at {edu.get('college')} ({edu.get('grad_year')} Batch, CGPA {edu.get('cgpa')}).\n"
            f"Selected real projects: {'; '.join(proj_names)}.\n"
            f"Job description excerpt: {(jd_text or '')[:1200]}\n"
            f"STRICT RULES:\n"
            f"1. ONLY reference facts/projects listed above. Never invent companies, internships, or metrics.\n"
            f"2. NEVER mention backlogs (do not write '0 backlogs').\n"
            f"3. NEVER use low-level crypto jargon like PBKDF2 or HMAC.\n"
            f"4. Return ONLY valid JSON: {{\"headline\": \"...\", \"summary\": \"...\"}}."
        )
        raw = provider.generate(prompt, temperature=0.25)
        m = re.search(r"\{.*\}", raw, re.S)
        if m:
            parsed = json.loads(m.group(0))
            hl = _clean_resume_text(str(parsed.get("headline") or "").strip())
            sm = _clean_resume_text(str(parsed.get("summary") or "").strip())
            if len(hl) >= 12 and len(sm) >= 60:
                return hl[:95], sm[:520], info.get("name", "AI")
    except Exception as e:
        logger.debug(f"AI summary polish fallback: {e}")

    return fallback_headline, fallback_summary, "deterministic"


def tailor_resume_for_job(
    profile: Dict[str, Any],
    company_name: str = "",
    role: str = "",
    jd_text: str = "",
    max_projects: int = 3,
    include_photo: bool = True,
    selected_project_ids: Optional[List[str]] = None,
    custom_headline: str = "",
    custom_summary: str = "",
    use_ai: bool = True,
    ai_engine=None,
) -> Dict[str, Any]:
    """
    Tailors a student's master portfolio to a target company/JD, ranks projects,
    reorders skill groups, computes ATS keyword match %, and renders classic 1-page A4 Arial HTML.
    """
    master = get_resume_master(profile)
    jd_analysis = analyze_jd(company_name, role, jd_text)

    # 1. Score and rank every project in the student's portfolio
    scored_projects = [
        _score_project(p, jd_analysis, role, jd_text)
        for p in master["projects"]
    ]
    scored_projects.sort(key=lambda x: x["match_score"], reverse=True)

    # 2. Pick selected projects (manual override or top-N highest match)
    if selected_project_ids is not None and len(selected_project_ids) > 0:
        id_set = set(str(i) for i in selected_project_ids)
        selected_projects = [p for p in scored_projects if p["id"] in id_set]
        if not selected_projects:
            selected_projects = scored_projects[:max_projects]
    else:
        selected_projects = scored_projects[:max(1, min(4, int(max_projects or 3)))]

    selected_id_set = {p["id"] for p in selected_projects}
    for p in scored_projects:
        p["selected"] = p["id"] in selected_id_set

    # 3. Reorder non-empty skill categories to match target role track
    non_empty_skills = [s for s in master["skills"] if s.get("items")]
    order_map = {cat.lower(): idx for idx, cat in enumerate(jd_analysis["skill_order"])}
    ordered_skills = sorted(
        non_empty_skills,
        key=lambda s: order_map.get(s["category"].lower(), 99),
    )

    # 4. Craft headline & summary (deterministic + optional AI polish or user override)
    fb_headline, fb_summary = _build_deterministic_summary(
        master, company_name, role, jd_analysis["track"], selected_projects
    )
    if custom_headline.strip() or custom_summary.strip():
        headline = _clean_resume_text(custom_headline.strip() or fb_headline)
        summary = _clean_resume_text(custom_summary.strip() or fb_summary)
        tailor_engine = "custom_edited"
    elif use_ai and ai_engine is not None:
        headline, summary, tailor_engine = _try_ai_polish_summary(
            ai_engine, master, company_name, role, jd_text, selected_projects, fb_headline, fb_summary
        )
    else:
        headline, summary, tailor_engine = fb_headline, fb_summary, "deterministic"

    # 5. Compute ATS Keyword Coverage against JD
    resume_corpus = " ".join([
        headline,
        summary,
        " ".join(s["items"] for s in ordered_skills),
        " ".join(f"{p['title']} {p['tech']} {' '.join(p['bullets'])}" for p in selected_projects),
        " ".join(f"{c['title']} {c['issuer']}" for c in master["certifications"]),
    ]).lower()

    jd_kws = jd_analysis["jd_keywords"]
    matched_kws = [kw for kw in jd_kws if kw in resume_corpus]
    missing_kws = [kw for kw in jd_kws if kw not in resume_corpus][:8]
    ats_score = int(round((len(matched_kws) / max(1, len(jd_kws))) * 100))

    # Build clean display location (append Open to <City> when JD specifies another city)
    loc_display = master["location"]
    target_city = jd_analysis.get("target_city") or ""
    if target_city and target_city.lower() not in loc_display.lower():
        loc_display = f"{loc_display} (Open to {target_city})"

    tailored_data = {
        "company_name": company_name.strip(),
        "role": role.strip() or jd_analysis["track"],
        "track": jd_analysis["track"],
        "tailor_engine": tailor_engine,
        "full_name": master["full_name"] or "Student Name",
        "headline": headline,
        "summary": summary,
        "email": master["email"],
        "phone": master["phone"],
        "location": loc_display,
        "github": master["github"],
        "linkedin": master["linkedin"],
        "portfolio": master["portfolio"],
        "photo_b64": master["photo_b64"],
        "include_photo": bool(include_photo and master["photo_b64"]),
        "photo_requested_by_jd": jd_analysis["photo_requested"],
        "skills": ordered_skills,
        "selected_projects": selected_projects,
        "all_projects_ranked": scored_projects,
        "education": master["education"],
        "certifications": master["certifications"][:4],
        "ats_report": {
            "score": ats_score,
            "matched_keywords": matched_kws,
            "missing_keywords": missing_kws,
            "total_jd_keywords": len(jd_kws),
        },
    }

    tailored_data["preview_html"] = render_tailored_resume_html(tailored_data, include_photo=tailored_data["include_photo"])
    return tailored_data


# ═══════════════════════════════════════════════════════════════════════════
# 4. CLASSIC 1-PAGE A4 ARIAL HTML & PDF RENDERER
# ═══════════════════════════════════════════════════════════════════════════

def _clean_url_display(url: str) -> Tuple[str, str]:
    u = (url or "").strip()
    if not u:
        return "", ""
    href = u if u.lower().startswith(("http://", "https://")) else f"https://{u}"
    disp = re.sub(r"^https?://(www\.)?", "", href).rstrip("/")
    return href, disp


def render_tailored_resume_html(tailored: Dict[str, Any], include_photo: bool = True) -> str:
    """
    Renders a crisp, ATS-friendly, print-ready 1-page A4 resume in standard Arial.
    Every dynamic field is HTML-escaped; zero backlog details; auto-adapts cleanly
    with or without a formal photo.
    """
    e = _html.escape
    full_name = e(tailored.get("full_name") or "Student Name").upper()
    headline = e(tailored.get("headline") or "Full Stack Developer")
    summary = e(_clean_resume_text(tailored.get("summary") or ""))

    # Contact line 1: Location | Phone | Email
    c1_parts = []
    if tailored.get("location"):
        c1_parts.append(e(tailored["location"]))
    if tailored.get("phone"):
        c1_parts.append(e(tailored["phone"]))
    if tailored.get("email"):
        em = e(tailored["email"])
        c1_parts.append(f'<a href="mailto:{em}">{em}</a>')
    contact_line_1 = '<span class="sep">|</span>'.join(c1_parts)

    # Contact line 2: GitHub | LinkedIn | Portfolio
    c2_parts = []
    for k in ("github", "linkedin", "portfolio"):
        href, disp = _clean_url_display(tailored.get(k) or "")
        if href and disp:
            c2_parts.append(f'<a href="{e(href)}">{e(disp)}</a>')
    contact_line_2 = '<span class="sep">|</span>'.join(c2_parts)

    # Optional formal photo (accepts either raw base64 or data:image/... URI)
    photo_html = ""
    raw_photo = (tailored.get("photo_b64") or "").strip()
    if include_photo and raw_photo:
        if raw_photo.startswith("data:image/"):
            img_src = raw_photo
        else:
            img_src = f"data:image/jpeg;base64,{raw_photo}"
        photo_html = f'<div class="photo-box"><img src="{e(img_src)}" alt="{full_name}" /></div>'

    # Skills rows
    skills_rows = []
    for s in (tailored.get("skills") or []):
        cat = e(s.get("category") or "")
        items = e(s.get("items") or "")
        if cat and items:
            skills_rows.append(
                f'<div class="skill-row"><div class="skill-label">{cat}</div><div class="skill-val">{items}</div></div>'
            )
    skills_section = (
        f'<section class="section"><div class="section-title">Technical Skills</div>'
        f'<div class="skills-grid">{"".join(skills_rows)}</div></section>'
        if skills_rows
        else ""
    )

    # Projects
    proj_blocks = []
    selected_projects = tailored.get("selected_projects") or []
    for p in selected_projects:
        ptitle = e(p.get("title") or "")
        ptech = e(p.get("tech") or "")
        href, disp = _clean_url_display(p.get("link") or "")
        if href and disp:
            right_tag = f'<a class="proj-link" href="{e(href)}">{e(disp)}</a>'
        elif p.get("badge"):
            right_tag = f'<span class="proj-badge">{e(p["badge"])}</span>'
        else:
            right_tag = ""
        bullets_li = "".join(
            f"<li>{e(_clean_resume_text(b))}</li>"
            for b in (p.get("bullets") or [])[:3]
            if str(b).strip()
        )
        proj_blocks.append(
            f'<div class="project">'
            f'<div class="proj-head"><span class="proj-title">{ptitle}</span>{right_tag}</div>'
            f'{f"<div class=proj-tech>{ptech}</div>" if ptech else ""}'
            f'<ul class="bullets">{bullets_li}</ul>'
            f'</div>'
        )
    projects_section = (
        f'<section class="section"><div class="section-title">Projects</div>{"".join(proj_blocks)}</section>'
        if proj_blocks
        else ""
    )

    # Education (strictly NO backlog mention)
    edu = tailored.get("education") or {}
    edu_rows = []
    deg_title = f"{edu.get('degree') or 'B.Tech'} in {edu.get('branch') or 'Computer Science and Engineering'}"
    college = edu.get("college") or "University"
    grad_yr = edu.get("grad_year") or "2027"
    start_yr = str(int(grad_yr) - 4) if grad_yr.isdigit() else ""
    yr_span = f"{start_yr} – {grad_yr}" if start_yr else grad_yr
    cgpa_str = f"CGPA: {edu['cgpa']} / 10 &nbsp;|&nbsp; " if edu.get("cgpa") else ""
    edu_rows.append(
        f'<div class="edu-row"><div class="edu-main"><strong>{e(deg_title)}</strong> — {e(college)}</div>'
        f'<div class="edu-meta">{cgpa_str}{e(yr_span)}</div></div>'
    )
    if edu.get("twelfth_school") or edu.get("twelfth_pct") or edu.get("twelfth_gpa"):
        sch = edu.get("twelfth_school") or "Higher Secondary"
        brd = f", {edu['twelfth_board']}" if edu.get("twelfth_board") else ""
        score_bits = []
        if edu.get("twelfth_pct"):
            score_bits.append(f"{edu['twelfth_pct']}%")
        if edu.get("twelfth_gpa"):
            score_bits.append(f"(GPA: {edu['twelfth_gpa']} / 4.0)")
        score_txt = " ".join(score_bits)
        yr12 = edu.get("twelfth_year") or ""
        meta12 = " &nbsp;|&nbsp; ".join(x for x in [e(score_txt), e(yr12)] if x)
        edu_rows.append(
            f'<div class="edu-row"><div class="edu-main"><strong>Class XII (Science)</strong> — {e(sch)}{e(brd)}</div>'
            f'<div class="edu-meta">{meta12}</div></div>'
        )
    if edu.get("tenth_school") or edu.get("tenth_pct") or edu.get("tenth_gpa"):
        sch10 = edu.get("tenth_school") or "Secondary School"
        brd10 = f", {edu['tenth_board']}" if edu.get("tenth_board") else ""
        score_bits10 = []
        if edu.get("tenth_pct"):
            score_bits10.append(f"{edu['tenth_pct']}%")
        if edu.get("tenth_gpa"):
            score_bits10.append(f"(GPA: {edu['tenth_gpa']} / 4.0)")
        score_txt10 = " ".join(score_bits10)
        yr10 = edu.get("tenth_year") or ""
        meta10 = " &nbsp;|&nbsp; ".join(x for x in [e(score_txt10), e(yr10)] if x)
        edu_rows.append(
            f'<div class="edu-row"><div class="edu-main"><strong>Class X</strong> — {e(sch10)}{e(brd10)}</div>'
            f'<div class="edu-meta">{meta10}</div></div>'
        )
    edu_section = (
        f'<section class="section"><div class="section-title">Education</div>'
        f'<div class="edu-list">{"".join(edu_rows)}</div></section>'
    )

    # Certifications
    cert_items = []
    for c in (tailored.get("certifications") or [])[:4]:
        ct = e(c.get("title") or "")
        ci = e(c.get("issuer") or "")
        cy = e(c.get("year") or "")
        tail = " — " + ", ".join(x for x in [ci, cy] if x) if (ci or cy) else ""
        if ct:
            cert_items.append(f'<div class="cert-item"><strong>{ct}</strong>{tail}</div>')
    certs_section = (
        f'<section class="section"><div class="section-title">Training &amp; Certifications</div>'
        f'<div class="cert-grid">{"".join(cert_items)}</div></section>'
        if cert_items
        else ""
    )

    # Auto-tighten slightly if 4 projects selected so it still stays cleanly on 1 A4 page
    dense = len(selected_projects) >= 4
    body_pt = "8.3pt" if dense else "8.8pt"
    gap_pt = "4.8pt" if dense else "6.2pt"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>{full_name} — Resume</title>
  <style>
    @page {{ size: A4 portrait; margin: 0; }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    html, body {{
      width: 210mm;
      height: 297mm;
      background: #ffffff;
      color: #18181b;
      font-family: Arial, Helvetica, sans-serif;
      font-size: {body_pt};
      line-height: 1.35;
      -webkit-print-color-adjust: exact;
      print-color-adjust: exact;
    }}
    .page {{
      width: 210mm;
      min-height: 297mm;
      max-height: 297mm;
      padding: 9mm 12.5mm;
      overflow: hidden;
      display: flex;
      flex-direction: column;
      gap: {gap_pt};
    }}
    .header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12pt;
      padding-bottom: 6pt;
      border-bottom: 1.4pt solid #1e293b;
    }}
    .header-left {{ flex: 1; min-width: 0; }}
    .name {{ font-size: 18pt; font-weight: 700; letter-spacing: 0.4pt; color: #0f172a; line-height: 1.1; }}
    .headline {{ font-size: 9.4pt; font-weight: 700; color: #1e3a5f; margin-top: 2.5pt; }}
    .contact-row {{ margin-top: 4pt; font-size: 8.3pt; color: #334155; line-height: 1.44; }}
    .contact-row a {{ color: #1d4ed8; text-decoration: none; }}
    .sep {{ color: #94a3b8; margin: 0 4pt; }}
    .photo-box {{
      width: 22.5mm; height: 27mm; flex-shrink: 0;
      border: 1pt solid #cbd5e1; border-radius: 2.5pt; overflow: hidden; background: #f8fafc;
    }}
    .photo-box img {{ width: 100%; height: 100%; object-fit: cover; object-position: center 15%; display: block; }}
    .section {{ display: flex; flex-direction: column; gap: 3pt; }}
    .section-title {{
      font-size: 9pt; font-weight: 700; text-transform: uppercase; letter-spacing: 0.8pt;
      color: #0f172a; border-bottom: 0.8pt solid #cbd5e1; padding-bottom: 1.5pt; margin-bottom: 1pt;
    }}
    .summary {{ font-size: 8.5pt; color: #27272a; line-height: 1.38; }}
    .skills-grid {{ display: table; width: 100%; border-collapse: collapse; }}
    .skill-row {{ display: table-row; }}
    .skill-label {{
      display: table-cell; width: 29mm; font-size: 8.3pt; font-weight: 700; color: #1e293b;
      padding: 1pt 6pt 1pt 0; vertical-align: top; white-space: nowrap;
    }}
    .skill-val {{ display: table-cell; font-size: 8.3pt; color: #27272a; padding: 1pt 0; vertical-align: top; }}
    .project {{ margin-bottom: 3.5pt; }}
    .project:last-child {{ margin-bottom: 0; }}
    .proj-head {{ display: flex; justify-content: space-between; align-items: baseline; gap: 8pt; }}
    .proj-title {{ font-size: 9pt; font-weight: 700; color: #0f172a; }}
    .proj-link {{ font-size: 8pt; font-weight: 700; color: #1d4ed8; text-decoration: none; white-space: nowrap; }}
    .proj-badge {{ font-size: 8pt; color: #475569; white-space: nowrap; }}
    .proj-tech {{ font-size: 7.9pt; font-style: italic; color: #475569; margin: 0.5pt 0 1.8pt; }}
    ul.bullets {{ list-style: none; padding-left: 0; }}
    ul.bullets li {{
      position: relative; padding-left: 9pt; font-size: 8.35pt; color: #27272a; line-height: 1.34; margin-bottom: 1.4pt;
    }}
    ul.bullets li:last-child {{ margin-bottom: 0; }}
    ul.bullets li::before {{ content: "•"; position: absolute; left: 1.5pt; top: 0; color: #1e3a5f; font-weight: 700; }}
    .edu-list {{ display: flex; flex-direction: column; gap: 2.2pt; }}
    .edu-row {{ display: flex; justify-content: space-between; align-items: baseline; gap: 8pt; font-size: 8.45pt; }}
    .edu-main strong {{ color: #0f172a; }}
    .edu-meta {{ font-size: 8.2pt; color: #334155; white-space: nowrap; font-weight: 700; }}
    .cert-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 2.5pt 12pt; }}
    .cert-item {{ position: relative; padding-left: 9pt; font-size: 8.2pt; color: #27272a; line-height: 1.35; }}
    .cert-item::before {{ content: "•"; position: absolute; left: 1.5pt; color: #1e3a5f; font-weight: 700; }}
    .cert-item strong {{ color: #0f172a; }}
  </style>
</head>
<body>
  <div class="page">
    <header class="header">
      <div class="header-left">
        <div class="name">{full_name}</div>
        <div class="headline">{headline}</div>
        <div class="contact-row">
          {contact_line_1}{f"<br />{contact_line_2}" if contact_line_2 else ""}
        </div>
      </div>
      {photo_html}
    </header>
    {f'<section class="section"><div class="section-title">Professional Summary</div><p class="summary">{summary}</p></section>' if summary else ''}
    {skills_section}
    {projects_section}
    {edu_section}
    {certs_section}
  </div>
</body>
</html>"""


def render_html_to_pdf_bytes(html_str: str) -> Tuple[Optional[bytes], str]:
    """
    Renders 1-page A4 resume HTML to PDF bytes using headless Chrome/Chromium.
    Returns (pdf_bytes, status_msg). If Chrome is unavailable on the host,
    returns (None, 'CHROME_UNAVAILABLE') so the UI can fall back to browser print.
    """
    chrome = _find_chrome_binary()
    if not chrome:
        return None, "CHROME_UNAVAILABLE"

    with tempfile.TemporaryDirectory() as tmp:
        html_path = Path(tmp) / "resume.html"
        pdf_path = Path(tmp) / "resume.pdf"
        html_path.write_text(html_str, encoding="utf-8")

        cmd = [
            chrome,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--no-pdf-header-footer",
            f"--print-to-pdf={pdf_path}",
            html_path.as_uri(),
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
            if res.returncode == 0 and pdf_path.exists():
                data = pdf_path.read_bytes()
                if data.startswith(b"%PDF-"):
                    return data, "OK"
            return None, f"Chrome PDF error: {(res.stderr or '')[:200]}"
        except Exception as e:
            return None, str(e)


# ═══════════════════════════════════════════════════════════════════════════
# 5. EXISTING RESUME PDF IMPORTER (FOR FAST MULTI-USER ONBOARDING)
# ═══════════════════════════════════════════════════════════════════════════

def import_resume_from_pdf(pdf_bytes: bytes) -> Dict[str, Any]:
    """
    Parses a student's uploaded PDF resume via pypdf to pre-fill their
    links, skills, projects, and certifications into the Resume Tailor vault.
    """
    if not pdf_bytes:
        return {"success": False, "message": "Empty PDF file"}

    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        full_text = "\n".join((p.extract_text() or "") for p in reader.pages).strip()
    except Exception as e:
        return {"success": False, "message": f"Could not read PDF: {e}"}

    if not full_text:
        return {"success": False, "message": "No selectable text found in PDF"}

    # Extract URLs
    gh_m = re.search(r"(?:https?://)?github\.com/[A-Za-z0-9_.-]+", full_text, re.I)
    li_m = re.search(r"(?:https?://)?(?:www\.)?linkedin\.com/in/[A-Za-z0-9_.-]+", full_text, re.I)

    # Extract skill keywords grouped by category
    found_kws = _extract_keywords(full_text)
    lang_set = {"javascript", "typescript", "python", "java", "c++", "c#", "go", "dart", "sql", "html5", "css3"}
    fe_set = {"react", "next.js", "angular", "vue", "tailwind", "radix ui", "react query", "redux", "vite", "figma"}
    be_set = {"node.js", "express", "flask", "django", "fastapi", "spring boot", "rest api", "graphql", "microservices", "webhooks"}
    db_set = {"postgresql", "supabase", "mysql", "mongodb", "sqlite", "redis", "aws", "aws s3", "docker", "kubernetes", "netlify", "vercel", "git", "github", "linux"}
    test_set = {"vitest", "jest", "testing library", "unittest", "pytest", "selenium", "cypress", "playwright", "gemini", "tensorflow", "pytorch", "flutter"}

    def _fmt(s_set):
        return ", ".join(kw.title() if kw not in ("sql", "aws", "aws s3", "html5", "css3", "ci/cd", "qa") else kw.upper() for kw in found_kws if kw in s_set)

    skills = [
        {"category": "Languages", "items": _fmt(lang_set)},
        {"category": "Frontend", "items": _fmt(fe_set)},
        {"category": "Backend & APIs", "items": _fmt(be_set)},
        {"category": "Databases & Cloud", "items": _fmt(db_set)},
        {"category": "Testing & Tools", "items": _fmt(test_set)},
    ]

    # Split into rough sections to capture project headers & bullets
    lines = [ln.strip() for ln in full_text.splitlines() if ln.strip()]
    projects: List[Dict[str, Any]] = []
    certs: List[Dict[str, str]] = []

    section = ""
    current_proj: Optional[Dict[str, Any]] = None
    for ln in lines:
        up = ln.upper().strip(" :-–—")
        if up in ("PROJECTS", "KEY PROJECTS", "FULL-STACK & SAAS PROJECTS", "ACADEMIC PROJECTS"):
            section = "projects"
            continue
        if up in ("CERTIFICATIONS", "TRAINING & CERTIFICATIONS", "ACHIEVEMENTS", "EDUCATION", "SKILLS", "TECHNICAL SKILLS", "STRENGTHS"):
            if current_proj and current_proj["title"]:
                projects.append(current_proj)
                current_proj = None
            section = "certs" if "CERTIF" in up else up.lower()
            continue

        if section == "projects":
            if ln.startswith(("•", "-", "▪", "*")):
                btxt = ln.lstrip("•-▪* ").strip()
                if current_proj and btxt:
                    current_proj["bullets"].append(btxt)
            elif len(ln) <= 110 and ("|" in ln or "—" in ln or "–" in ln or " - " in ln):
                if current_proj and current_proj["title"]:
                    projects.append(current_proj)
                parts = re.split(r"\s*\|\s*", ln, maxsplit=1)
                current_proj = {
                    "id": uuid.uuid4().hex[:8],
                    "title": parts[0].strip(),
                    "link": "",
                    "badge": "",
                    "tech": parts[1].strip() if len(parts) > 1 else "",
                    "tags": _extract_keywords(ln),
                    "bullets": [],
                }
            elif current_proj and not current_proj["tech"] and "," in ln and len(ln) <= 120:
                current_proj["tech"] = ln
        elif section == "certs":
            ct = ln.lstrip("•-▪* ").strip()
            if ct and len(ct) > 5:
                certs.append({"title": ct, "issuer": "", "year": ""})

    if current_proj and current_proj["title"]:
        projects.append(current_proj)

    return {
        "success": True,
        "github": f"https://{gh_m.group(0).replace('https://', '').replace('http://', '')}" if gh_m else "",
        "linkedin": f"https://{li_m.group(0).replace('https://', '').replace('http://', '')}" if li_m else "",
        "skills": [s for s in skills if s["items"]],
        "projects": projects[:6],
        "certifications": certs[:6],
        "extracted_chars": len(full_text),
    }
