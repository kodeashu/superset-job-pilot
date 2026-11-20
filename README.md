# 🎓 Superset Campus Placement Job Pilot & Auto-Applier

An automated assistant built specifically for final year college students to **never miss a campus placement drive or tight deadline on Superset (`joinsuperset.com`)**.

---

## ⚡ Key Highlights

1. **🔥 Auto Shortlist Roll-Number PDF Scanner**:
   - Automatically downloads pre-signed PDF attachments from Superset homepage notices (e.g., shortlist lists, online test & GD schedules), caches extracted text in SQLite, and searches for your **Roll Number, Email, or Full Name** with exact page-number tracking.
   - Immediately triggers a **🔥 YOU ARE SHORTLISTED!** dashboard banner & deduplicated Telegram alert with extracted **Venue, Date, Time Slot, and Google Meet/Test Link**.

2. **📢 Smart Notice Feed & 1-Click Inline PDF Viewer**:
   - Categorizes campus notices into **🔥 Shortlisted**, **🎯 Shortlists & Tests**, **⏰ Deadlines & Revisions**, and **💼 Job Openings**, automatically hiding system promos & duplicate spam.
   - Built-in **1-click native modal PDF viewer** streams pre-signed Superset PDFs inline and jumps directly to the page containing your roll number.

3. **🏆 Pending Offer Watchdog**:
   - Continuously polls Superset's `/pending_actions` endpoint and raises a persistent top-of-app banner + urgent Telegram alert whenever a job offer with an acceptance deadline arrives.

4. **📄 Company Resume Tailor & 1-Page A4 PDF Studio**:
   - Click **Tailor CV** on any Superset campus drive or upcoming shortlist card to fetch the live Superset JD (+ attached JD PDF), rank your strongest projects from your encrypted portfolio, compute ATS keyword alignment, preview live in classic A4 Arial, and download a print-ready 1-page PDF (with optional formal passport photo toggle & existing-PDF CV importer).

5. **Multi-User Encrypted Vault & Pre-Submission Review**:
   - Per-student isolated accounts (`PBKDF2-HMAC-SHA256` encrypted SQLite vault), friend invite codes, pre-submission review modal, and optional per-user Auto-Apply toggle.

5. **Instant Telegram Alerts & Google Calendar Sync**:
   - Formatted alerts with company name, CTC, deadline countdown, and 1-click Google Calendar reminders.

6. **Zero-Server Cloud Runner (GitHub Actions) & Tampermonkey Script**:
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
