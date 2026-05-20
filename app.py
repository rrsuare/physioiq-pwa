"""
PhysioIQ PWA — Backend Server
A lightweight Flask app that serves as the backend for PhysioIQ.
Handles: Garmin data pulls, meal logging, report generation, and chat via Claude API.
"""

import os
import json
import sqlite3
import hashlib
import hmac
import time
from datetime import datetime, date, timedelta
from functools import wraps
from pathlib import Path

# Load .env file if present
env_path = Path(__file__).parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip())

from flask import Flask, request, jsonify, send_from_directory, g
import anthropic

# ---------------------------------------------------------------------------
# App Setup
# ---------------------------------------------------------------------------

app = Flask(__name__, static_folder="static")
app.config["DATABASE"] = os.environ.get("DATABASE_PATH", "physioiq.db")
app.config["ANTHROPIC_API_KEY"] = os.environ.get("ANTHROPIC_API_KEY", "")
app.config["APP_SECRET"] = os.environ.get("APP_SECRET", "change-me-in-production")
app.config["GARMIN_EMAIL"] = os.environ.get("GARMIN_EMAIL", "")
app.config["GARMIN_PASSWORD"] = os.environ.get("GARMIN_PASSWORD", "")

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db

@app.teardown_appcontext
def close_db(exception):
    db = g.pop("db", None)
    if db is not None:
        db.close()

def init_db():
    db = get_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT UNIQUE,
            profile_json TEXT DEFAULT '{}',
            system_prompt TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS garmin_data (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            date TEXT NOT NULL,
            weight_lb REAL,
            sleep_score REAL,
            hrv INTEGER,
            resting_hr INTEGER,
            body_battery INTEGER,
            stress_avg INTEGER,
            steps INTEGER,
            active_minutes INTEGER,
            calories_total INTEGER,
            raw_json TEXT,
            pulled_at TEXT DEFAULT (datetime('now')),
            UNIQUE(user_id, date)
        );

        CREATE TABLE IF NOT EXISTS meals (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            date TEXT NOT NULL,
            meal_type TEXT NOT NULL,  -- breakfast, lunch, dinner, snack
            description TEXT NOT NULL,
            calories INTEGER,
            protein_g REAL,
            carbs_g REAL,
            fat_g REAL,
            fiber_g REAL,
            notes TEXT,
            ai_analysis TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            date TEXT NOT NULL,
            report_type TEXT NOT NULL,  -- morning, post_workout, eod
            html_content TEXT NOT NULL,
            metrics_json TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            role TEXT NOT NULL,  -- user, assistant
            content TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS daily_state (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            date TEXT NOT NULL,
            state_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(user_id, date)
        );

        CREATE INDEX IF NOT EXISTS idx_garmin_date ON garmin_data(user_id, date);
        CREATE INDEX IF NOT EXISTS idx_meals_date ON meals(user_id, date);
        CREATE INDEX IF NOT EXISTS idx_reports_date ON reports(user_id, date, report_type);
        CREATE INDEX IF NOT EXISTS idx_chat_date ON chat_messages(user_id, created_at);
    """)
    db.commit()

# ---------------------------------------------------------------------------
# Auth (simple token-based)
# ---------------------------------------------------------------------------

def generate_token(user_id):
    payload = f"{user_id}:{int(time.time())}"
    sig = hmac.new(app.config["APP_SECRET"].encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{sig}"

def verify_token(token):
    try:
        parts = token.split(":")
        if len(parts) != 3:
            return None
        user_id, ts, sig = parts
        expected = hmac.new(app.config["APP_SECRET"].encode(), f"{user_id}:{ts}".encode(), hashlib.sha256).hexdigest()
        if hmac.compare_digest(sig, expected):
            return int(user_id)
    except Exception:
        pass
    return None

def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get("Authorization", "").replace("Bearer ", "")
        user_id = verify_token(token)
        if user_id is None:
            return jsonify({"error": "Unauthorized"}), 401
        g.user_id = user_id
        return f(*args, **kwargs)
    return decorated

# ---------------------------------------------------------------------------
# Claude API Integration
# ---------------------------------------------------------------------------

def get_claude_client():
    key = app.config["ANTHROPIC_API_KEY"]
    if not key:
        return None
    return anthropic.Anthropic(api_key=key)

def build_context(user_id, include_today=True):
    """Build the context string with today's data for Claude."""
    db = get_db()

    # User profile + system prompt
    user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        return ""

    today = date.today().isoformat()
    context_parts = []

    # Always include current date/time at the TOP so Claude knows what day it is
    context_parts.append(f"TODAY'S DATE: {datetime.now().strftime('%A, %B %d, %Y')}")
    context_parts.append(f"CURRENT TIME: {datetime.now().strftime('%I:%M %p')}")

    # Today's Garmin data
    if include_today:
        garmin = db.execute(
            "SELECT * FROM garmin_data WHERE user_id = ? AND date = ?",
            (user_id, today)
        ).fetchone()
        if garmin:
            context_parts.append(f"TODAY'S GARMIN DATA ({today}):")
            context_parts.append(f"  Weight: {garmin['weight_lb']} lb")
            context_parts.append(f"  Sleep Score: {garmin['sleep_score']}")
            context_parts.append(f"  HRV: {garmin['hrv']}")
            context_parts.append(f"  Resting HR: {garmin['resting_hr']}")
            context_parts.append(f"  Body Battery: {garmin['body_battery']}")
            context_parts.append(f"  Steps: {garmin['steps']}")

        # Yesterday for comparison
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        garmin_y = db.execute(
            "SELECT * FROM garmin_data WHERE user_id = ? AND date = ?",
            (user_id, yesterday)
        ).fetchone()
        if garmin_y:
            context_parts.append(f"\nYESTERDAY'S GARMIN DATA ({yesterday}):")
            context_parts.append(f"  Weight: {garmin_y['weight_lb']} lb")
            context_parts.append(f"  Sleep Score: {garmin_y['sleep_score']}")
            context_parts.append(f"  HRV: {garmin_y['hrv']}")

        # 7-day HRV average
        week_ago = (date.today() - timedelta(days=7)).isoformat()
        hrv_rows = db.execute(
            "SELECT hrv FROM garmin_data WHERE user_id = ? AND date >= ? AND hrv IS NOT NULL",
            (user_id, week_ago)
        ).fetchall()
        if hrv_rows:
            avg_hrv = sum(r["hrv"] for r in hrv_rows) / len(hrv_rows)
            context_parts.append(f"\n7-DAY HRV AVERAGE: {avg_hrv:.0f}")

    # Today's meals
    meals = db.execute(
        "SELECT * FROM meals WHERE user_id = ? AND date = ? ORDER BY created_at",
        (user_id, today)
    ).fetchall()
    if meals:
        context_parts.append(f"\nTODAY'S MEALS:")
        total_cal, total_p, total_c, total_f = 0, 0, 0, 0
        for m in meals:
            context_parts.append(f"  {m['meal_type'].title()}: {m['description']}")
            if m["calories"]:
                context_parts.append(f"    Calories: {m['calories']} | P: {m['protein_g']}g | C: {m['carbs_g']}g | F: {m['fat_g']}g")
                total_cal += m["calories"] or 0
                total_p += m["protein_g"] or 0
                total_c += m["carbs_g"] or 0
                total_f += m["fat_g"] or 0
        context_parts.append(f"  TOTALS SO FAR: {total_cal} cal | P: {total_p:.0f}g | C: {total_c:.0f}g | F: {total_f:.0f}g")

    # Recent daily state
    state = db.execute(
        "SELECT state_json FROM daily_state WHERE user_id = ? AND date = ?",
        (user_id, today)
    ).fetchone()
    if state:
        context_parts.append(f"\nTODAY'S STATE: {state['state_json']}")

    return "\n".join(context_parts)

def chat_with_claude(user_id, user_message):
    """Send a message to Claude with full PhysioIQ context."""
    client = get_claude_client()
    if not client:
        return {"error": "Anthropic API key not configured. Set ANTHROPIC_API_KEY environment variable."}

    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        return {"error": "User not found"}

    # Build system prompt
    system_prompt = user["system_prompt"] or "You are PhysioIQ, a personal body performance coach and nutrition trainer."
    context = build_context(user_id)
    full_system = f"{system_prompt}\n\n--- CURRENT DATA ---\n{context}" if context else system_prompt

    # Get recent chat history (last 20 messages for context)
    recent = db.execute(
        "SELECT role, content FROM chat_messages WHERE user_id = ? ORDER BY created_at DESC LIMIT 20",
        (user_id,)
    ).fetchall()
    messages = [{"role": r["role"], "content": r["content"]} for r in reversed(recent)]
    messages.append({"role": "user", "content": user_message})

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2048,
            system=full_system,
            messages=messages
        )
        assistant_msg = response.content[0].text

        # Save both messages
        db.execute(
            "INSERT INTO chat_messages (user_id, role, content) VALUES (?, 'user', ?)",
            (user_id, user_message)
        )
        db.execute(
            "INSERT INTO chat_messages (user_id, role, content) VALUES (?, 'assistant', ?)",
            (user_id, assistant_msg)
        )
        db.commit()

        return {"response": assistant_msg}

    except Exception as e:
        return {"error": str(e)}

# ---------------------------------------------------------------------------
# Garmin Integration
# ---------------------------------------------------------------------------

def pull_garmin_data(user_id, target_date=None):
    """Pull data from Garmin Connect and store in database."""
    try:
        from garminconnect import Garmin
    except ImportError:
        return {"error": "garminconnect not installed. Run: pip install garminconnect"}

    email = app.config["GARMIN_EMAIL"]
    password = app.config["GARMIN_PASSWORD"]
    if not email or not password:
        return {"error": "Garmin credentials not configured"}

    target = target_date or date.today().isoformat()

    try:
        # Token storage directory
        token_dir = os.path.join(os.path.dirname(app.config["DATABASE"]), ".garmin_tokens")
        os.makedirs(token_dir, exist_ok=True)
        token_file = os.path.join(token_dir, "tokens.json")

        garmin = Garmin(email, password)

        # Try to load saved tokens
        if os.path.exists(token_file):
            try:
                with open(token_file, "r") as f:
                    tokens = json.load(f)
                garmin.login(tokens)
            except Exception:
                garmin.login()
                with open(token_file, "w") as f:
                    json.dump(garmin.garth.dumps(), f)
        else:
            garmin.login()
            with open(token_file, "w") as f:
                json.dump(garmin.garth.dumps(), f)

        # Fetch data
        stats = garmin.get_stats(target)
        sleep = garmin.get_sleep_data(target)
        hrv_data = garmin.get_hrv_data(target)

        # Extract key metrics
        weight_lb = None
        try:
            weight_data = garmin.get_body_composition(target)
            if weight_data and weight_data.get("weight"):
                weight_lb = round(weight_data["weight"] / 1000 * 2.20462, 1)
        except Exception:
            pass

        sleep_score = None
        if sleep and sleep.get("dailySleepDTO", {}).get("sleepScores", {}).get("overall", {}).get("value"):
            sleep_score = sleep["dailySleepDTO"]["sleepScores"]["overall"]["value"]

        hrv = None
        if hrv_data and hrv_data.get("hrvSummary", {}).get("lastNightAvg"):
            hrv = hrv_data["hrvSummary"]["lastNightAvg"]

        resting_hr = stats.get("restingHeartRate")
        body_battery = stats.get("bodyBatteryChargedValue")
        stress_avg = stats.get("averageStressLevel")
        steps = stats.get("totalSteps")
        active_min = stats.get("activeSeconds", 0) // 60 if stats.get("activeSeconds") else None
        cal_total = stats.get("totalKilocalories")

        # Store in database
        db = get_db()
        db.execute("""
            INSERT OR REPLACE INTO garmin_data
            (user_id, date, weight_lb, sleep_score, hrv, resting_hr, body_battery,
             stress_avg, steps, active_minutes, calories_total, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (user_id, target, weight_lb, sleep_score, hrv, resting_hr, body_battery,
              stress_avg, steps, active_min, cal_total,
              json.dumps({"stats": stats, "sleep_score": sleep_score, "hrv_raw": hrv_data})))
        db.commit()

        return {
            "success": True,
            "date": target,
            "weight_lb": weight_lb,
            "sleep_score": sleep_score,
            "hrv": hrv,
            "resting_hr": resting_hr,
            "body_battery": body_battery
        }

    except Exception as e:
        return {"error": f"Garmin pull failed: {str(e)}"}

# ---------------------------------------------------------------------------
# Report Generation
# ---------------------------------------------------------------------------

def generate_report(user_id, report_type="morning"):
    """Generate a PhysioIQ report using Claude."""
    client = get_claude_client()
    if not client:
        return {"error": "Anthropic API key not configured"}

    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        return {"error": "User not found"}

    today = date.today().isoformat()
    context = build_context(user_id)

    # Check what data is available to inform Claude
    has_garmin = db.execute(
        "SELECT COUNT(*) as cnt FROM garmin_data WHERE user_id = ? AND date = ?",
        (user_id, today)
    ).fetchone()["cnt"] > 0
    has_meals = db.execute(
        "SELECT COUNT(*) as cnt FROM meals WHERE user_id = ? AND date = ?",
        (user_id, today)
    ).fetchone()["cnt"] > 0

    data_note = ""
    if not has_garmin and not has_meals:
        data_note = "\n\nNOTE: No Garmin data or meals have been logged for today yet. Generate the report using any available historical data, the user's profile, and general coaching guidance. For sections that require today's data, note that data is pending and provide placeholder guidance based on the user's goals and typical patterns."
    elif not has_garmin:
        data_note = "\n\nNOTE: No Garmin data is available for today (device may not have synced yet). Use available meal data and any historical patterns. For Garmin-dependent sections (sleep, HRV, body battery, etc.), note that data is pending."
    elif not has_meals:
        data_note = "\n\nNOTE: No meals have been logged for today yet. Use available Garmin data and provide nutrition guidance based on the user's targets."

    html_style_instructions = """

OUTPUT FORMAT: You MUST return ONLY raw HTML content (no markdown, no ```html fences, no doctype/html/head/body tags — just the inner content that goes inside a div).

STYLING RULES — follow these exactly:
- Dark theme background: #0d0d0d (page), #1c1c1e (cards), #2c2c2e (nested elements)
- Font: -apple-system, 'Inter', 'SF Pro', sans-serif
- Max-width: 393px; margin: 0 auto on the wrapper
- Color palette: green=#30d158, yellow=#ffd60a, orange=#ff9f0a, blue=#0a84ff, red=#ff453a, teal=#64d2ff, purple=#bf5af2, text=#f5f5f7, muted=#a1a1a6, dim=#636366

SECTION HEADER STYLE (use for every section):
<div style="background:#1c1c1e;border-radius:14px;padding:14px 16px;margin-bottom:10px;">
  <div style="font-size:11px;font-weight:700;color:SECTION_COLOR;text-transform:uppercase;letter-spacing:0.6px;margin-bottom:8px;">SECTION_TITLE</div>
  <!-- section content here -->
</div>

DATA ROW STYLE:
<div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:4px;">
  <span style="color:#a1a1a6;font-size:12px;">Label</span>
  <span style="font-size:16px;font-weight:700;color:VALUE_COLOR;">Value</span>
</div>

BADGE STYLE:
<span style="display:inline-block;padding:3px 10px;border-radius:20px;font-size:11px;font-weight:700;background:rgba(R,G,B,0.15);color:BADGE_COLOR;">Badge Text</span>

Use these color assignments for sections: green for positive/recovery, yellow for warnings/sleep, orange for alerts/nutrition, blue for data/metrics, red for critical alerts, teal for hydration/info, purple for summary/planning.
"""

    prompts = {
        "morning": f"""Generate the full PhysioIQ Morning Report for today. Include ALL 13 sections with colored headers:

1. HEADER — Report title with date, user name, and a readiness badge (PUSH/MODERATE/DIAL BACK/REST) using appropriate badge color
2. READINESS SCORE — Overall readiness assessment with score and color coding
3. SLEEP ANALYSIS — Sleep score, quality assessment, time metrics (use yellow header)
4. HRV STATUS — Current HRV, 7-day trend, recovery signal (use green header)
5. BODY BATTERY — Current charge, projected drain, recommendations (use teal header)
6. WEIGHT TREND — Current weight, trend direction, context (use blue header)
7. RESTING HEART RATE — Current RHR, trend, what it signals (use purple header)
8. STRESS LOAD — Average stress, breakdown, management tips (use orange header)
9. ACTIVITY TARGET — Steps, active minutes, today's movement goals (use green header)
10. NUTRITION PLAN — Today's macro targets, meal timing strategy (use orange header)
11. TRAINING RECOMMENDATION — What to do today based on readiness (use blue header)
12. HYDRATION PROTOCOL — Water intake targets, timing (use teal header)
13. COACH'S NOTE — Personal insight, motivation, key focus for the day (use purple header)

{html_style_instructions}{data_note}""",

        "post_workout": f"""Generate the PhysioIQ Post-Workout Report. Include these sections with colored headers:

1. HEADER — Post-workout report title with timestamp
2. WORKOUT SUMMARY — What was done, duration, intensity (use green header)
3. RECOVERY STATUS — Body battery drain, HR recovery, stress response (use teal header)
4. CALORIC IMPACT — Calories burned estimate, net balance (use orange header)
5. RECOVERY NUTRITION — What to eat NOW for recovery, macro targets (use blue header)
6. REMAINING DAILY TARGETS — Updated macro/calorie targets for rest of day (use yellow header)
7. HYDRATION RECOVERY — Fluid replacement needs (use teal header)
8. NEXT SESSION PREVIEW — When to train next, what to focus on (use purple header)
9. COACH'S NOTE — Performance observations, adjustments (use green header)

{html_style_instructions}{data_note}""",

        "eod": f"""Generate the PhysioIQ End-of-Day Report. Include these sections with colored headers:

1. HEADER — EOD report title with date and overall grade
2. DAILY SCORECARD — Overall grade for the day across all metrics (use blue header)
3. NUTRITION RECAP — Total macros vs targets, meal quality assessment (use orange header)
4. ACTIVITY SUMMARY — Steps, active minutes, calories burned (use green header)
5. RECOVERY METRICS — Sleep readiness prediction, HRV trend, body battery (use teal header)
6. WEIGHT TRACKING — Today's weight in context of weekly/monthly trend (use blue header)
7. WINS — What went well today, celebrate progress (use green header)
8. AREAS TO IMPROVE — Honest assessment of gaps (use yellow header)
9. TOMORROW'S GAME PLAN — Training, nutrition, and recovery priorities (use purple header)
10. COACH'S CLOSING NOTE — End-of-day insight and motivation (use purple header)

{html_style_instructions}{data_note}"""
    }

    system = user["system_prompt"] or "You are PhysioIQ, a personal body performance coach."
    full_system = f"{system}\n\n--- CURRENT DATA ---\n{context}"

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4096,
            system=full_system,
            messages=[{"role": "user", "content": prompts.get(report_type, prompts["morning"])}]
        )
        html = response.content[0].text

        # Strip markdown code fences if Claude wrapped the HTML in them
        html = html.strip()
        if html.startswith("```html"):
            html = html[7:]
        elif html.startswith("```"):
            html = html[3:]
        if html.endswith("```"):
            html = html[:-3]
        html = html.strip()

        # Store report
        db.execute("""
            INSERT INTO reports (user_id, date, report_type, html_content, metrics_json)
            VALUES (?, ?, ?, ?, ?)
        """, (user_id, today, report_type, html, context))
        db.commit()

        report_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        return {"success": True, "report_id": report_id, "html": html}

    except Exception as e:
        return {"error": str(e)}

# ---------------------------------------------------------------------------
# API Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/manifest.json")
def manifest():
    return send_from_directory("static", "manifest.json")

@app.route("/sw.js")
def service_worker():
    return send_from_directory("static", "sw.js")

# --- Auth ---

@app.route("/api/login", methods=["POST"])
def login():
    """Simple login — for single-user setup, just returns token for user 1."""
    db = get_db()
    user = db.execute("SELECT id FROM users LIMIT 1").fetchone()
    if not user:
        return jsonify({"error": "No user configured. Complete onboarding first."}), 404
    token = generate_token(user["id"])
    return jsonify({"token": token, "user_id": user["id"]})

@app.route("/api/onboard", methods=["POST"])
def onboard():
    """Create a new user with profile data."""
    data = request.json or {}
    db = get_db()

    # Check if user already exists
    existing = db.execute("SELECT id FROM users LIMIT 1").fetchone()
    if existing:
        return jsonify({"error": "User already exists. Use /api/login instead."}), 400

    name = data.get("name", "User")
    email = data.get("email", "")
    profile = json.dumps(data.get("profile", {}))
    system_prompt = data.get("system_prompt", "")

    # Auto-load full system prompt from file if available
    full_prompt_path = Path(__file__).parent / "ruben_system_prompt.txt"
    if full_prompt_path.exists():
        system_prompt = full_prompt_path.read_text()

    db.execute(
        "INSERT INTO users (name, email, profile_json, system_prompt) VALUES (?, ?, ?, ?)",
        (name, email, profile, system_prompt)
    )
    db.commit()
    user = db.execute("SELECT id FROM users ORDER BY id DESC LIMIT 1").fetchone()
    token = generate_token(user["id"])
    return jsonify({"token": token, "user_id": user["id"], "message": "Welcome to PhysioIQ!"})

# --- Chat ---

@app.route("/api/chat", methods=["POST"])
@require_auth
def chat():
    data = request.json or {}
    message = data.get("message", "").strip()
    if not message:
        return jsonify({"error": "Message required"}), 400
    result = chat_with_claude(g.user_id, message)
    return jsonify(result)

@app.route("/api/chat/history", methods=["GET"])
@require_auth
def chat_history():
    limit = request.args.get("limit", 50, type=int)
    db = get_db()
    messages = db.execute(
        "SELECT id, role, content, created_at FROM chat_messages WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
        (g.user_id, limit)
    ).fetchall()
    return jsonify([dict(m) for m in reversed(messages)])

# --- Meals ---

@app.route("/api/meals", methods=["GET"])
@require_auth
def get_meals():
    target_date = request.args.get("date", date.today().isoformat())
    db = get_db()
    meals = db.execute(
        "SELECT * FROM meals WHERE user_id = ? AND date = ? ORDER BY created_at",
        (g.user_id, target_date)
    ).fetchall()
    return jsonify([dict(m) for m in meals])

@app.route("/api/meals", methods=["POST"])
@require_auth
def add_meal():
    data = request.json or {}
    db = get_db()

    meal_date = data.get("date", date.today().isoformat())
    meal_type = data.get("meal_type", "snack")
    description = data.get("description", "").strip()

    if not description:
        return jsonify({"error": "Description required"}), 400

    # If no macros provided, ask Claude to estimate
    calories = data.get("calories")
    protein = data.get("protein_g")
    carbs = data.get("carbs_g")
    fat = data.get("fat_g")
    fiber = data.get("fiber_g")
    ai_analysis = None

    if calories is None:
        client = get_claude_client()
        if client:
            try:
                resp = client.messages.create(
                    model="claude-sonnet-4-20250514",
                    max_tokens=512,
                    system="You are a nutrition expert. Estimate the macros for the described meal. Respond ONLY with JSON: {\"calories\": N, \"protein_g\": N, \"carbs_g\": N, \"fat_g\": N, \"fiber_g\": N, \"analysis\": \"brief note\"}",
                    messages=[{"role": "user", "content": f"Estimate macros for: {description}"}]
                )
                text = resp.content[0].text.strip()
                # Extract JSON from response
                if "{" in text:
                    json_str = text[text.index("{"):text.rindex("}") + 1]
                    est = json.loads(json_str)
                    calories = est.get("calories")
                    protein = est.get("protein_g")
                    carbs = est.get("carbs_g")
                    fat = est.get("fat_g")
                    fiber = est.get("fiber_g")
                    ai_analysis = est.get("analysis")
            except Exception:
                pass

    db.execute("""
        INSERT INTO meals (user_id, date, meal_type, description, calories, protein_g, carbs_g, fat_g, fiber_g, notes, ai_analysis)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (g.user_id, meal_date, meal_type, description, calories, protein, carbs, fat, fiber,
          data.get("notes"), ai_analysis))
    db.commit()

    meal_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    meal = db.execute("SELECT * FROM meals WHERE id = ?", (meal_id,)).fetchone()
    return jsonify(dict(meal)), 201

@app.route("/api/meals/<int:meal_id>", methods=["PUT"])
@require_auth
def update_meal(meal_id):
    data = request.json or {}
    db = get_db()
    meal = db.execute("SELECT * FROM meals WHERE id = ? AND user_id = ?", (meal_id, g.user_id)).fetchone()
    if not meal:
        return jsonify({"error": "Meal not found"}), 404

    fields = ["meal_type", "description", "calories", "protein_g", "carbs_g", "fat_g", "fiber_g", "notes"]
    updates = []
    values = []
    for f in fields:
        if f in data:
            updates.append(f"{f} = ?")
            values.append(data[f])

    if updates:
        updates.append("updated_at = datetime('now')")
        values.append(meal_id)
        values.append(g.user_id)
        db.execute(f"UPDATE meals SET {', '.join(updates)} WHERE id = ? AND user_id = ?", values)
        db.commit()

    meal = db.execute("SELECT * FROM meals WHERE id = ?", (meal_id,)).fetchone()
    return jsonify(dict(meal))

@app.route("/api/meals/<int:meal_id>", methods=["DELETE"])
@require_auth
def delete_meal(meal_id):
    db = get_db()
    db.execute("DELETE FROM meals WHERE id = ? AND user_id = ?", (meal_id, g.user_id))
    db.commit()
    return jsonify({"success": True})

# --- Garmin ---

@app.route("/api/garmin/pull", methods=["POST"])
@require_auth
def garmin_pull():
    target = request.json.get("date") if request.json else None
    result = pull_garmin_data(g.user_id, target)
    return jsonify(result)

@app.route("/api/garmin/data", methods=["GET"])
@require_auth
def garmin_data():
    target_date = request.args.get("date", date.today().isoformat())
    days = request.args.get("days", 1, type=int)
    db = get_db()
    start = (datetime.fromisoformat(target_date) - timedelta(days=days - 1)).date().isoformat()
    rows = db.execute(
        "SELECT * FROM garmin_data WHERE user_id = ? AND date >= ? AND date <= ? ORDER BY date",
        (g.user_id, start, target_date)
    ).fetchall()
    return jsonify([dict(r) for r in rows])

# --- Reports ---

@app.route("/api/reports/generate", methods=["POST"])
@require_auth
def generate_report_endpoint():
    data = request.json or {}
    report_type = data.get("type", "morning")
    result = generate_report(g.user_id, report_type)
    return jsonify(result)

@app.route("/api/reports", methods=["GET"])
@require_auth
def get_reports():
    target_date = request.args.get("date", date.today().isoformat())
    db = get_db()
    reports = db.execute(
        "SELECT id, date, report_type, created_at FROM reports WHERE user_id = ? AND date = ? ORDER BY created_at DESC",
        (g.user_id, target_date)
    ).fetchall()
    return jsonify([dict(r) for r in reports])

@app.route("/api/reports/<int:report_id>", methods=["GET"])
@require_auth
def get_report(report_id):
    db = get_db()
    report = db.execute(
        "SELECT * FROM reports WHERE id = ? AND user_id = ?", (report_id, g.user_id)
    ).fetchone()
    if not report:
        return jsonify({"error": "Report not found"}), 404
    return jsonify(dict(report))

# --- Daily State ---

@app.route("/api/state", methods=["GET"])
@require_auth
def get_state():
    target_date = request.args.get("date", date.today().isoformat())
    db = get_db()
    state = db.execute(
        "SELECT * FROM daily_state WHERE user_id = ? AND date = ?",
        (g.user_id, target_date)
    ).fetchone()
    return jsonify(dict(state) if state else {"date": target_date, "state_json": "{}"})

@app.route("/api/state", methods=["POST"])
@require_auth
def update_state():
    data = request.json or {}
    db = get_db()
    today = date.today().isoformat()
    db.execute("""
        INSERT OR REPLACE INTO daily_state (user_id, date, state_json)
        VALUES (?, ?, ?)
    """, (g.user_id, today, json.dumps(data)))
    db.commit()
    return jsonify({"success": True})

# --- Data Export ---

@app.route("/api/export/meals", methods=["GET"])
@require_auth
def export_meals():
    """Export meal data as JSON (can be converted to CSV/Excel client-side)."""
    start = request.args.get("start", (date.today() - timedelta(days=30)).isoformat())
    end = request.args.get("end", date.today().isoformat())
    db = get_db()
    meals = db.execute(
        "SELECT * FROM meals WHERE user_id = ? AND date >= ? AND date <= ? ORDER BY date, created_at",
        (g.user_id, start, end)
    ).fetchall()
    return jsonify([dict(m) for m in meals])

@app.route("/api/export/garmin", methods=["GET"])
@require_auth
def export_garmin():
    start = request.args.get("start", (date.today() - timedelta(days=30)).isoformat())
    end = request.args.get("end", date.today().isoformat())
    db = get_db()
    data = db.execute(
        "SELECT * FROM garmin_data WHERE user_id = ? AND date >= ? AND date <= ? ORDER BY date",
        (g.user_id, start, end)
    ).fetchall()
    return jsonify([dict(d) for d in data])

# --- Meal Recommendation ---

@app.route("/api/meals/recommend", methods=["POST"])
@require_auth
def recommend_meal():
    """Get a meal recommendation based on what's been eaten today and targets."""
    data = request.json or {}
    meal_type = data.get("meal_type", "dinner")
    preferences = data.get("preferences", "")

    context = build_context(g.user_id)
    client = get_claude_client()
    if not client:
        return jsonify({"error": "API key not configured"}), 500

    db = get_db()
    user = db.execute("SELECT system_prompt FROM users WHERE id = ?", (g.user_id,)).fetchone()
    system = user["system_prompt"] if user else ""

    prompt = f"Based on what I've eaten today and my remaining macro targets, recommend a {meal_type}."
    if preferences:
        prompt += f" Preferences: {preferences}"
    prompt += " Include estimated macros. Keep it practical and specific."

    try:
        resp = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            system=f"{system}\n\n--- CURRENT DATA ---\n{context}",
            messages=[{"role": "user", "content": prompt}]
        )
        return jsonify({"recommendation": resp.content[0].text})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- System Prompt Update ---

@app.route("/api/system-prompt", methods=["PUT"])
@require_auth
def update_system_prompt():
    """Update the system prompt for the current user."""
    data = request.get_json()
    if not data or "system_prompt" not in data:
        return jsonify({"error": "system_prompt field required"}), 400
    db = get_db()
    db.execute("UPDATE users SET system_prompt = ? WHERE id = ?", (data["system_prompt"], g.user_id))
    db.commit()
    return jsonify({"status": "ok", "length": len(data["system_prompt"])})

@app.route("/api/load-full-prompt", methods=["POST"])
@require_auth
def load_full_prompt():
    """Load the full system prompt from system_prompt_template.md (filled for Ruben)."""
    prompt_path = Path(__file__).parent / "ruben_system_prompt.txt"
    if not prompt_path.exists():
        return jsonify({"error": "ruben_system_prompt.txt not found"}), 404
    prompt_text = prompt_path.read_text()
    db = get_db()
    db.execute("UPDATE users SET system_prompt = ? WHERE id = ?", (prompt_text, g.user_id))
    db.commit()
    return jsonify({"status": "ok", "length": len(prompt_text), "preview": prompt_text[:200]})

# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

with app.app_context():
    init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG", "0") == "1")
