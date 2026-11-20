# 🎓 Superset Campus Placement Job Pilot & Auto-Applier

An automated assistant built specifically for final year college students to **never miss a campus placement drive or tight deadline on Superset (`joinsuperset.com`)**.

---

## ⚡ Key Highlights

1. **Auto-Apply for Eligible Openings**:
   - Superset's internal engine already determines whether you meet the eligibility criteria (CGPA cutoff, allowed branches, ongoing backlogs, batch year 2026).
   - When an eligible drive appears and `AUTO_APPLY_ELIGIBLE=true`, the bot submits your application using your pre-uploaded default resume immediately.

2. **Instant Telegram Alerts with Countdown**:
   - Sends a formatted card directly to your mobile Telegram with:
     - 🏢 Company name & Job title
     - 💰 CTC / Package (LPA)
     - 📍 Locations
     - ⏳ Deadline countdown (e.g., `5.2 hours remaining!`)
     - 🟢 Auto-Applied status confirmation

3. **Google Calendar Sync with Reminders**:
   - Every alert includes a **1-Click "Add to Google Calendar" button**.
   - Generates Google Calendar events with pre-configured alarms **24h, 3h, and 1h before the deadline**, so your phone rings before registration closes!

4. **Zero-Server Cloud Runner (GitHub Actions)**:
   - Free 24/7 cloud runner. Runs every 15 minutes without needing your laptop to stay awake.

5. **Tampermonkey Browser Script (For Laptop Browsing)**:
   - Floating widget on `app.joinsuperset.com` with a 1-click **"⚡ Auto-Apply All Eligible"** button and direct Google Calendar buttons right on each job card.

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
2. Click **Create a new script** and paste the code from [`userscript/superset_quick_apply.user.js`](file:///home/antigravity/superset-job-pilot/userscript/superset_quick_apply.user.js).
3. Visit `https://app.joinsuperset.com` — you'll see a floating toolbar with:
   - **⚡ Auto-Apply All Eligible**
   - **📅 Add to Google Calendar** buttons on every job profile card.
