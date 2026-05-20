# PhysioIQ PWA — Setup Guide

## What You Have

```
physioiq-app/
├── app.py                    # Backend server (Flask)
├── static/
│   ├── index.html            # iPhone-optimized PWA frontend
│   ├── manifest.json         # PWA manifest (add-to-homescreen)
│   └── sw.js                 # Service worker (offline support)
├── system_prompt_template.md # Customizable coaching prompt
├── requirements.txt          # Python dependencies
├── Dockerfile                # Container config
├── railway.toml              # Railway deployment config
└── SETUP.md                  # This file
```

## Quick Start (Local Testing)

### 1. Install dependencies
```bash
cd physioiq-app
pip install -r requirements.txt
```

### 2. Set environment variables
```bash
export ANTHROPIC_API_KEY="sk-ant-api03-YOUR-KEY-HERE"
export GARMIN_EMAIL="your-garmin-email@example.com"
export GARMIN_PASSWORD="your-garmin-password"
export APP_SECRET="any-random-string-for-security"
```

### 3. Run the app
```bash
python app.py
```

Open http://localhost:5000 on your phone (same WiFi network) or computer.

## Getting Your Anthropic API Key

1. Go to **https://console.anthropic.com**
2. Sign up or log in
3. Go to **Settings → API Keys**
4. Click **Create Key**
5. Copy the key (starts with `sk-ant-api03-...`)
6. Add a payment method under **Settings → Billing**
   - API usage is pay-per-use (~$0.03-0.10 per interaction)
   - Typical daily cost: $2-4 for heavy usage

## Deploy to Railway (Recommended — Free Tier Available)

### 1. Create a Railway account
Go to **https://railway.app** and sign up with GitHub.

### 2. Create a new project
- Click **New Project → Deploy from GitHub Repo**
- Connect your GitHub and push this code to a repo, OR:
- Click **New Project → Empty Project → Add a Service → Deploy from Local**

### 3. Set environment variables in Railway
In your service settings, add these variables:
- `ANTHROPIC_API_KEY` = your key
- `GARMIN_EMAIL` = your Garmin Connect email
- `GARMIN_PASSWORD` = your Garmin Connect password
- `APP_SECRET` = any random string (e.g., run `openssl rand -hex 32`)
- `DATABASE_PATH` = /data/physioiq.db

### 4. Add a persistent volume
- In the service settings, add a **Volume**
- Mount path: `/data`
- This keeps your database persistent across deployments

### 5. Deploy
Railway auto-detects the Dockerfile and deploys. You'll get a URL like:
`https://physioiq-production-xxxx.up.railway.app`

### 6. Add to iPhone Home Screen
1. Open the Railway URL in Safari on your iPhone
2. Tap the **Share** button (square with arrow)
3. Tap **Add to Home Screen**
4. Name it "PhysioIQ"
5. Done — it now looks and behaves like a native app

## Alternative: Deploy to Render (Also Free Tier)

1. Go to **https://render.com** and sign up
2. Click **New → Web Service**
3. Connect your repo or upload code
4. Set environment variables (same as Railway)
5. Add a **Disk** mounted at `/data` for the database
6. Deploy

## Migrating Garmin Credentials

Your existing Garmin tokens are at:
```
~/Dropbox (Personal)/rs personal files/RS - Personal/BODY PERFORMANCE ANALYSIS/MEAL PLAN FILE/GARMIN_DATA/.garmin_tokens/
```

The PWA uses the same `garminconnect` library but authenticates with email/password directly (stored as environment variables). On first run, it creates new tokens automatically. No migration needed — just provide your Garmin Connect email and password.

## Customizing the System Prompt

The `system_prompt_template.md` file contains the full coaching personality and rules. To customize:

1. Edit the template with your specific protocols (TDEE, supplements, etc.)
2. During onboarding, the app generates a basic system prompt from your profile
3. To use the full template: update your user record's `system_prompt` field in the database, or rebuild with the template values filled in

For advanced customization, you can update the system prompt via the SQLite database:
```bash
sqlite3 /data/physioiq.db
UPDATE users SET system_prompt = 'your full prompt here' WHERE id = 1;
```

## Architecture

```
iPhone (PWA) ──→ Flask Backend ──→ Claude API (coaching)
                      │
                      ├──→ SQLite DB (meals, metrics, chat, reports)
                      │
                      └──→ Garmin Connect (daily data pull)
```

Each layer is independent:
- If Garmin fails → reports generate with available data
- If Claude API errors → data is safe, just retry
- If phone is offline → service worker shows cached shell
