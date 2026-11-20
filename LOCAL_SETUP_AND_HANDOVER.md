# 🚀 Superset Job Pilot — Multi-User Setup & Developer Guide

This document explains how to run **Superset Job Pilot** locally, add new student accounts via invite codes, or extend it with add-ons.

---

## 📦 Architecture & Security

- **Multi-User Encrypted Vault (`user_manager.py`)**: All student profiles and Superset credentials are encrypted at rest inside `data/users_vault.sqlite` (gitignored) using `PBKDF2-HMAC-SHA256` and authenticated stream encryption keyed by `APP_SECRET_KEY`.
- **Zero Hardcoded PII**: No student names, roll numbers, passwords, IDs, or phone numbers are stored in source code.
- **Per-User Auto-Apply & Pre-Submit Review**: Each student can toggle autonomous auto-apply ON or OFF for their own account, or use the interactive Pre-Submission Review & Confirmation Modal.
- **Invite Code Registration**: Friends can register using an invite code (default: `KIIT2027` or custom codes generated in Settings) and their `@kiit.ac.in` Superset login. Their profile and resumes are imported automatically from Superset.

---

## ⚡ 1. Running Locally

### Step 1: Install Requirements
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Step 2: Configure `.env` (Optional for Admin Seed)
Copy `.env.example` to `.env`. An `APP_SECRET_KEY` will be generated automatically on first launch.

### Step 3: Start the Multi-User Web Server
```bash
python web_server.py
```
Open **`http://localhost:8080`** in your browser and sign in or register with your Superset email, password, and invite code (`KIIT2027`).
