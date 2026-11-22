# 🎓 Superset Campus Placement Job Pilot & Auto-Applier

An automated assistant built specifically for final year college students to **never miss a campus placement drive or tight deadline on Superset (`joinsuperset.com`)**.

---

## ⚡ Key Highlights

1. **🔥 Auto Shortlist Roll-Number PDF Scanner (Role-Specific Venue Precision)**:
   - Automatically downloads pre-signed PDF attachments from Superset homepage notices (e.g., shortlist lists, online test & GD schedules), caches extracted text in SQLite, and searches for your **Roll Number, Email, or Full Name** with exact page-number tracking.
   - Parses multi-profile campus schedule tables so your exact shortlisted profile's **Reporting Venue, Date, Time Slot, Process, and Meet Link** are extracted accurately.
   - Immediately triggers a **🔥 YOU ARE SHORTLISTED!** dashboard banner plus deduplicated **WhatsApp & Telegram** alerts.

2. **💬 Instant WhatsApp & Telegram Alerts (Job Profiles + Shortlist Venues + Offers)**:
   - Built-in **WhatsApp Linked Devices QR Bridge** (`whatsapp_bridge/bridge.mjs` — 100% free, scan QR once inside the dashboard, zero third-party waitlists or Meta verification), plus optional **Meta WhatsApp Cloud API**, **Twilio WhatsApp**, **Custom Webhooks** (`n8n` / `Make` / `WAHA`), and **CallMeBot**.
   - Sends instant structured WhatsApp notifications for newly opened eligible **Job Profiles** (Company, Role, CTC, Location, Deadline, Apply & Calendar links), upcoming **Shortlist Venue/Date/Time PDF matches**, and urgent **Pending Job Offers**, plus an on-demand **Send Live Digest Now** button.

3. **📢 Smart Notice Feed & 1-Click Inline PDF Viewer**:
   - Categorizes campus notices into **🔥 Shortlisted**, **🎯 Shortlists & Tests**, **⏰ Deadlines & Revisions**, and **💼 Job Openings**, automatically hiding system promos & duplicate spam.
   - Built-in **1-click native modal PDF viewer** streams pre-signed Superset PDFs inline and jumps directly to the page containing your roll number.

4. **🏆 Pending Offer Watchdog**:
   - Continuously polls Superset's `/pending_actions` endpoint and raises a persistent top-of-app banner + urgent WhatsApp & Telegram alert whenever a job offer with an acceptance deadline arrives.

5. **📄 Company Resume Tailor & 1-Page A4 PDF Studio**:
   - Click **Tailor CV** on any Superset campus drive or upcoming shortlist card to fetch the live Superset JD (+ attached JD PDF), rank your strongest projects from your encrypted portfolio, compute ATS keyword alignment, preview live in classic A4 Arial, and download a print-ready 1-page PDF (with optional formal passport photo toggle & existing-PDF CV importer).

6. **Multi-User Vault, Pre-Submission Review & Google Calendar Sync**:
   - Per-student isolated accounts, friend invite codes, pre-submission review modal, optional per-user Auto-Apply toggle, and 1-click Google Calendar reminders.

7. **Zero-Server Cloud Runner (GitHub Actions) & Web Mission Control**:
   - Supports scheduled headless monitoring (`python3 main.py run` / `scan-notices`) as well as the mobile-first Flask Web Mission Control (`python3 web_server.py`).

---

## 🚀 Setup Guide (3 Simple Steps)

### Step 1: Clone & Configure

```bash
cd superset-job-pilot
cp .env.example .env
```

Edit `.env` with:
- `SUPERSET_EMAIL`: Your college email used on Superset.
- `SUPERSET_PASSWORD`: Your Superset password.
- `AUTO_APPLY_ELIGIBLE`: Set to `true`.

### Step 2: Set up Instant Telegram Alerts (Takes 2 minutes)

1. Open Telegram and search for `@BotFather`.
2. Send `/newbot`, name your bot (e.g., `MyPlacementAlertBot`), and copy the **Bot Token**.
3. Search for `@userinfobot` on Telegram and send `/start` to get your **Chat ID**.
4. Paste both into `.env`:
   ```ini
   TELEGRAM_BOT_TOKEN=1234567890:ABCdef...
   TELEGRAM_CHAT_ID=123456789
   ```

### Step 3: Test and Run

To run a simulation with realistic campus drives (e.g. Cognizant GenC, Hexaware GET):
```bash
python3 main.py test
```

To run a single live check:
```bash
python3 main.py run
```

To view all tracked applications and history:
```bash
python3 main.py status
```

---

## 🐳 24/7 All-in-One Docker Deployment (No Laptop / No Tunnel Expiry)

The included [`Dockerfile`](Dockerfile), [`docker-compose.yml`](docker-compose.yml), [`railway.json`](railway.json), [`fly.toml`](fly.toml), and [`render.yaml`](render.yaml) bundle **Python 3.11 + Gunicorn**, **Node.js 22 WhatsApp Linked-Device QR Bridge**, and **Headless Chromium (1-Page A4 Resume PDF Export)** into a single container with persistent storage at `/app/data`.

### Option A: Local / VPS (`docker compose`)
```bash
docker compose up -d --build
# Open http://localhost:8080 (health check: http://localhost:8080/api/health)
```
All encrypted accounts (`users_vault.sqlite`), `.vault_secret`, and your WhatsApp Linked-Device session (`wa_auth/`) persist in the `superset_data` volume mounted at `/app/data`.

### Option B: Railway (Easiest 24/7 Cloud Host)
1. Create a new project in [Railway](https://railway.com) → **Deploy from GitHub repo** → select `superset-job-pilot` (`railway.json` auto-detects `Dockerfile` and `/api/health`).
2. Add a **Volume** mounted at `/app/data` (keeps your encrypted vault + WhatsApp QR linked across redeploys).
3. Add optional env vars (`APP_SECRET_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `GEMINI_API_KEY`) and generate a Railway Public Domain — then point Netlify `_redirects` (or visit the Railway domain directly) for permanent 24/7 access with zero `HTTP 530`.

### Option C: Fly.io (`bom` Mumbai Region)
```bash
fly launch --no-deploy --copy-config
fly volumes create superset_data --size 1 --region bom
fly secrets set APP_SECRET_KEY="$(openssl rand -base64 36)"
fly deploy
```

---

## ☁️ Deploy Free 24/7 on GitHub Actions

You don't need to keep your laptop running:
1. Push this folder to a **Private GitHub repository**.
2. Go to **Settings > Secrets and variables > Actions > New repository secret**.
3. Add the following secrets:
   - `SUPERSET_EMAIL`
   - `SUPERSET_PASSWORD`
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
4. GitHub Actions will automatically wake up every 15 minutes, inspect Superset for new openings, auto-apply, and alert you on Telegram!

---

## 💻 Optional: In-Browser Tampermonkey Script

If you like reviewing jobs in your browser:
1. Install [Tampermonkey](https://www.tampermonkey.net/) in Chrome / Edge.
2. Click **Create a new script** and paste the code from `userscript/superset_quick_apply.user.js`.
3. Visit `https://app.joinsuperset.com` — you'll see a floating toolbar with:
   - **⚡ Auto-Apply All Eligible**
   - **📅 Add to Google Calendar** buttons on every job profile card.
