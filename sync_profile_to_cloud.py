"""
Syncs the local encrypted student profile, Resume Tailor master portfolio (including photo),
and WhatsApp alert preferences from local data/users_vault.sqlite to a remote Superset Job Pilot
instance (e.g. Render / Railway / Fly.io) over authenticated HTTPS.

Contains ZERO hardcoded PII — reads strictly from local .env and local data/users_vault.sqlite.

Usage:
    python sync_profile_to_cloud.py https://superset-job-pilot.onrender.com
"""
import sys
import requests

from config import Config
from user_manager import UserManager


def main():
    if len(sys.argv) < 2:
        print("Usage: python sync_profile_to_cloud.py <CLOUD_BASE_URL>")
        sys.exit(1)

    base_url = sys.argv[1].strip().rstrip("/")
    email = (Config.SUPERSET_EMAIL or "").strip().lower()
    password = (Config.SUPERSET_PASSWORD or "").strip()
    if not email or not password:
        print("Error: SUPERSET_EMAIL and SUPERSET_PASSWORD must be set in local .env.")
        sys.exit(1)

    um = UserManager()
    local_user = None
    for u in um.get_all_users():
        if (u.get("email") or "").strip().lower() == email:
            local_user = u
            break

    if not local_user:
        print(f"Error: No local vault account found for {email}.")
        sys.exit(1)

    prof = dict(local_user.get("profile") or {})
    resume_master = prof.pop("resume_master", None)
    wa_cfg = prof.pop("whatsapp_config", None)

    sess = requests.Session()
    print(f"1/5 Authenticating against {base_url}...")
    r_login = sess.post(
        f"{base_url}/api/auth/login",
        json={"email": email, "password": password},
        timeout=60,
    )
    if r_login.status_code != 200 or not r_login.json().get("success"):
        print(f"Login failed ({r_login.status_code}): {r_login.text[:200]}")
        sys.exit(1)

    token = r_login.json().get("token", "")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    print(f"2/5 Transferring full profile ({len(prof)} fields)...")
    r_prof = sess.post(f"{base_url}/api/profile", json=prof, headers=headers, timeout=30)
    if r_prof.status_code != 200 or not r_prof.json().get("success"):
        print(f"Profile sync failed ({r_prof.status_code}): {r_prof.text[:200]}")
        sys.exit(1)

    if isinstance(resume_master, dict):
        proj_cnt = len(resume_master.get("projects") or [])
        has_photo = bool(resume_master.get("photo_b64"))
        print(f"3/5 Transferring Resume Tailor master ({proj_cnt} projects, photo={'yes' if has_photo else 'no'})...")
        r_res = sess.post(f"{base_url}/api/resume/master", json=resume_master, headers=headers, timeout=30)
        if r_res.status_code != 200 or not r_res.json().get("success"):
            print(f"Resume master sync failed ({r_res.status_code}): {r_res.text[:200]}")
            sys.exit(1)
    else:
        print("3/5 No local resume_master found — skipped.")

    if isinstance(wa_cfg, dict):
        print(f"4/5 Syncing WhatsApp alert preferences (provider={wa_cfg.get('provider', 'local_qr')})...")
        r_wa = sess.post(f"{base_url}/api/whatsapp/config", json=wa_cfg, headers=headers, timeout=20)
        if r_wa.status_code != 200:
            print(f"Warning: WhatsApp prefs sync returned {r_wa.status_code}: {r_wa.text[:160]}")
    else:
        print("4/5 No local whatsapp_config found — skipped.")

    # Also sync auto-apply preference
    sess.post(
        f"{base_url}/api/user/auto-apply",
        json={"enabled": bool(local_user.get("auto_apply_enabled"))},
        headers=headers,
        timeout=20,
    )

    print("5/5 Triggering live Superset drive & background shortlist scan on cloud instance...")
    r_sync = sess.post(f"{base_url}/api/sync", headers=headers, timeout=60)

    r_status = sess.get(f"{base_url}/api/status", headers=headers, timeout=20).json()
    r_master_check = sess.get(f"{base_url}/api/resume/master", headers=headers, timeout=20).json()
    m_out = r_master_check.get("master") or {}
    print(
        f"DONE! Cloud instance verified: "
        f"jobs={r_status.get('total_jobs')} | applied={r_status.get('applied_jobs')} | "
        f"cgpa_set={bool(r_status.get('cgpa'))} | "
        f"resume_projects={len(m_out.get('projects') or [])} | "
        f"resume_photo={'yes' if m_out.get('photo_b64') else 'no'} | "
        f"sync_ok={r_sync.status_code == 200}"
    )


if __name__ == "__main__":
    main()
