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
import logging
import traceback
import threading
from datetime import datetime, date, timedelta
from functools import wraps
from pathlib import Path
from io import BytesIO
import requests as http_requests  # renamed to avoid clash with flask.request

# Set up logging so errors show in Render logs
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("physioiq")

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

# CORS — allow lift tracker and other PhysioIQ tools to POST workout data
ALLOWED_ORIGINS = [
    "https://zippy-pastelito-b0f74c.netlify.app",
    "https://physioiq-pwa.onrender.com",
]

@app.after_request
def add_cors_headers(response):
    origin = request.headers.get("Origin", "")
    if origin in ALLOWED_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    return response

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(app.config["DATABASE"], check_same_thread=False)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db

def get_standalone_db():
    """Get a DB connection outside of Flask request context (for background threads)."""
    db = sqlite3.connect(app.config["DATABASE"], check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    return db

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
            html_content TEXT NOT NULL DEFAULT '',
            metrics_json TEXT,
            status TEXT NOT NULL DEFAULT 'complete',  -- generating, complete, error
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

        CREATE TABLE IF NOT EXISTS workout_logs (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            date TEXT NOT NULL,
            workout_type TEXT NOT NULL,  -- swim, lift, both
            log_text TEXT NOT NULL,
            notes TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS user_settings (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            updated_at TEXT DEFAULT (datetime('now')),
            UNIQUE(user_id, key)
        );

        CREATE INDEX IF NOT EXISTS idx_garmin_date ON garmin_data(user_id, date);
        CREATE INDEX IF NOT EXISTS idx_meals_date ON meals(user_id, date);
        CREATE INDEX IF NOT EXISTS idx_reports_date ON reports(user_id, date, report_type);
        CREATE INDEX IF NOT EXISTS idx_chat_date ON chat_messages(user_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_workout_date ON workout_logs(user_id, date);
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
    """Build rich context string with today's data for Claude, including deep Garmin metrics."""
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

    # Today's Garmin data — extract RICH detail from raw_json
    if include_today:
        garmin = db.execute(
            "SELECT * FROM garmin_data WHERE user_id = ? AND date = ?",
            (user_id, today)
        ).fetchone()
        if garmin:
            context_parts.append(f"\nTODAY'S GARMIN DATA ({today}):")
            context_parts.append(f"  Weight: {garmin['weight_lb']} lb")
            context_parts.append(f"  Sleep Score: {garmin['sleep_score']}")
            context_parts.append(f"  HRV: {garmin['hrv']}")
            context_parts.append(f"  Resting HR: {garmin['resting_hr']}")
            context_parts.append(f"  Body Battery: {garmin['body_battery']}")
            context_parts.append(f"  Stress Avg: {garmin['stress_avg']}")
            context_parts.append(f"  Steps: {garmin['steps']}")
            context_parts.append(f"  Active Minutes: {garmin['active_minutes']}")
            context_parts.append(f"  Total Calories: {garmin['calories_total']}")

            # Extract rich data from raw_json
            raw = None
            if garmin['raw_json']:
                try:
                    raw = json.loads(garmin['raw_json'])
                except Exception:
                    pass

            if raw:
                stats = raw.get("stats") or {}

                # Sleep stages from sleep data
                sleep_data = raw.get("sleep")
                if sleep_data:
                    daily_sleep = sleep_data.get("dailySleepDTO", {})
                    context_parts.append(f"\n  SLEEP DETAIL:")
                    sleep_start = daily_sleep.get("sleepStartTimestampLocal")
                    sleep_end = daily_sleep.get("sleepEndTimestampLocal")
                    if sleep_start and sleep_end:
                        # Convert epoch millis to readable time
                        try:
                            start_dt = datetime.fromtimestamp(sleep_start / 1000)
                            end_dt = datetime.fromtimestamp(sleep_end / 1000)
                            context_parts.append(f"    Bed Time: {start_dt.strftime('%I:%M %p')}")
                            context_parts.append(f"    Wake Time: {end_dt.strftime('%I:%M %p')}")
                            duration_min = (sleep_end - sleep_start) / 60000
                            hours = int(duration_min // 60)
                            mins = int(duration_min % 60)
                            context_parts.append(f"    Total Duration: {hours}h {mins}m")
                        except Exception:
                            pass

                    deep_min = daily_sleep.get("deepSleepSeconds", 0) // 60 if daily_sleep.get("deepSleepSeconds") else 0
                    light_min = daily_sleep.get("lightSleepSeconds", 0) // 60 if daily_sleep.get("lightSleepSeconds") else 0
                    rem_min = daily_sleep.get("remSleepSeconds", 0) // 60 if daily_sleep.get("remSleepSeconds") else 0
                    awake_min = daily_sleep.get("awakeSleepSeconds", 0) // 60 if daily_sleep.get("awakeSleepSeconds") else 0
                    total_sleep_min = deep_min + light_min + rem_min + awake_min

                    if total_sleep_min > 0:
                        context_parts.append(f"    Deep Sleep: {deep_min} min ({deep_min*100//total_sleep_min}%)")
                        context_parts.append(f"    Light Sleep: {light_min} min ({light_min*100//total_sleep_min}%)")
                        context_parts.append(f"    REM Sleep: {rem_min} min ({rem_min*100//total_sleep_min}%)")
                        context_parts.append(f"    Awake: {awake_min} min ({awake_min*100//total_sleep_min}%)")

                    # Sleep scores breakdown
                    sleep_scores = daily_sleep.get("sleepScores", {})
                    if sleep_scores:
                        for score_key in ["qualityOfSleep", "totalSleep", "stress", "remPercentage", "restlessness", "lightPercentage", "deepPercentage"]:
                            score_val = sleep_scores.get(score_key, {})
                            if isinstance(score_val, dict) and score_val.get("value") is not None:
                                qual = score_val.get("qualifierKey", "")
                                context_parts.append(f"    Sleep Score — {score_key}: {score_val['value']} ({qual})")

                    # Respiration
                    avg_resp = daily_sleep.get("averageRespiration")
                    if avg_resp:
                        context_parts.append(f"    Avg Respiration: {avg_resp} br/min")

                # Stress detail from stats
                context_parts.append(f"\n  STRESS DETAIL:")
                stress_keys = {
                    "averageStressLevel": "Average Stress",
                    "maxStressLevel": "Max Stress",
                    "stressDuration": "Stress Duration (sec)",
                    "restStressDuration": "Rest Stress Duration (sec)",
                    "lowStressDuration": "Low Stress Duration (sec)",
                    "mediumStressDuration": "Medium Stress Duration (sec)",
                    "highStressDuration": "High Stress Duration (sec)",
                }
                for key, label in stress_keys.items():
                    val = stats.get(key)
                    if val is not None:
                        context_parts.append(f"    {label}: {val}")

                # Body battery detail
                context_parts.append(f"\n  BODY BATTERY DETAIL:")
                bb_keys = {
                    "bodyBatteryChargedValue": "Charged (High)",
                    "bodyBatteryDrainedValue": "Drained (Low)",
                    "bodyBatteryHighestValue": "Highest",
                    "bodyBatteryLowestValue": "Lowest",
                    "bodyBatteryMostRecentValue": "Most Recent",
                }
                for key, label in bb_keys.items():
                    val = stats.get(key)
                    if val is not None:
                        context_parts.append(f"    {label}: {val}")

                # Activity/fitness stats
                context_parts.append(f"\n  ACTIVITY DETAIL:")
                activity_keys = {
                    "totalSteps": "Total Steps",
                    "dailyStepGoal": "Step Goal",
                    "totalDistanceMeters": "Distance (meters)",
                    "activeSeconds": "Active Seconds",
                    "sedentarySeconds": "Sedentary Seconds",
                    "highlyActiveSeconds": "Highly Active Seconds",
                    "moderateIntensityMinutes": "Moderate Intensity Minutes",
                    "vigorousIntensityMinutes": "Vigorous Intensity Minutes",
                    "intensityMinutesGoal": "Intensity Minutes Goal",
                    "floorsAscended": "Floors Ascended",
                    "floorsDescended": "Floors Descended",
                    "floorsAscendedGoal": "Floors Goal",
                    "totalKilocalories": "Total Calories",
                    "activeKilocalories": "Active Calories",
                    "bmrKilocalories": "BMR Calories",
                    "wellnessKilocalories": "Wellness Calories",
                    "burnedKilocalories": "Burned Calories",
                    "consumedKilocalories": "Consumed Calories",
                    "remainingKilocalories": "Remaining Calories",
                    "netCalorieGoal": "Net Calorie Goal",
                    "netRemainingKilocalories": "Net Remaining Calories",
                }
                for key, label in activity_keys.items():
                    val = stats.get(key)
                    if val is not None:
                        context_parts.append(f"    {label}: {val}")

                # Heart rate detail
                context_parts.append(f"\n  HEART RATE DETAIL:")
                hr_keys = {
                    "restingHeartRate": "Resting HR",
                    "minHeartRate": "Min HR",
                    "maxHeartRate": "Max HR",
                    "averageHeartRate": "Average HR",
                    "minAvgHeartRate": "Min Avg HR",
                    "maxAvgHeartRate": "Max Avg HR",
                }
                for key, label in hr_keys.items():
                    val = stats.get(key)
                    if val is not None:
                        context_parts.append(f"    {label}: {val}")

                # HRV detail from raw
                hrv_raw = raw.get("hrv_raw")
                if hrv_raw:
                    hrv_summary = hrv_raw.get("hrvSummary", {})
                    if hrv_summary:
                        context_parts.append(f"\n  HRV DETAIL:")
                        for hkey in ["lastNightAvg", "lastNight5MinHigh", "status", "baseline", "weeklyAvg", "lastNightAvg"]:
                            hval = hrv_summary.get(hkey)
                            if hval is not None:
                                context_parts.append(f"    {hkey}: {hval}")
                        # baseline sub-object
                        baseline = hrv_summary.get("baseline")
                        if isinstance(baseline, dict):
                            context_parts.append(f"    Baseline Low: {baseline.get('lowUpper')}")
                            context_parts.append(f"    Baseline Balanced Low: {baseline.get('balancedLow')}")
                            context_parts.append(f"    Baseline Balanced Upper: {baseline.get('balancedUpper')}")

                # Any other interesting stats keys not covered above
                covered_keys = set(list(stress_keys.keys()) + list(bb_keys.keys()) + list(activity_keys.keys()) + list(hr_keys.keys()))
                interesting_extras = {}
                for k, v in stats.items():
                    if k not in covered_keys and v is not None and v != 0 and not isinstance(v, (dict, list)):
                        interesting_extras[k] = v
                if interesting_extras:
                    context_parts.append(f"\n  OTHER STATS:")
                    for k, v in interesting_extras.items():
                        context_parts.append(f"    {k}: {v}")

                # ACTIVITIES (swim, run, lift, etc.) from Garmin
                activities = raw.get("activities", [])
                if activities:
                    context_parts.append(f"\n  GARMIN ACTIVITIES TODAY ({len(activities)} found):")
                    for i, act in enumerate(activities, 1):
                        context_parts.append(f"\n    ACTIVITY {i}: {act.get('name', 'Unknown')}")
                        context_parts.append(f"      Type: {act.get('type', 'unknown')}")
                        context_parts.append(f"      Start: {act.get('start', 'unknown')}")
                        context_parts.append(f"      Duration: {act.get('duration_min', 0)} min ({act.get('duration_sec', 0)} sec)")
                        if act.get('distance_m'):
                            context_parts.append(f"      Distance: {act.get('distance_m', 0):.0f} m / {act.get('distance_yards', 0):.0f} yards / {act.get('distance_miles', 0):.2f} miles")
                        context_parts.append(f"      Calories: {act.get('calories', 0)}")
                        context_parts.append(f"      Avg HR: {act.get('avg_hr', 0)} | Max HR: {act.get('max_hr', 0)}")
                        if act.get('training_effect_aerobic'):
                            context_parts.append(f"      TE Aerobic: {act.get('training_effect_aerobic', 0)} | TE Anaerobic: {act.get('training_effect_anaerobic', 0)}")
                        if act.get('laps'):
                            context_parts.append(f"      Laps: {act.get('laps', 0)}")
                        if act.get('pool_length'):
                            context_parts.append(f"      Pool Length: {act.get('pool_length', 0)} m")
                        if act.get('strokes'):
                            context_parts.append(f"      Strokes: {act.get('strokes', 0)}")
                        if act.get('avg_stroke_distance'):
                            context_parts.append(f"      Avg Stroke Distance: {act.get('avg_stroke_distance', 0):.2f} m")
                        if act.get('swim_SWOLF'):
                            context_parts.append(f"      Avg SWOLF: {act.get('swim_SWOLF', 0)}")
                        if act.get('avg_speed'):
                            # Convert m/s to pace per 100m for swimming
                            spd = act.get('avg_speed', 0)
                            if spd and spd > 0 and 'swim' in act.get('type', '').lower():
                                pace_sec_per_100 = 100 / spd
                                pace_min = int(pace_sec_per_100 // 60)
                                pace_s = int(pace_sec_per_100 % 60)
                                context_parts.append(f"      Pace: {pace_min}:{pace_s:02d} per 100m")
                            elif spd and spd > 0:
                                context_parts.append(f"      Avg Speed: {spd:.2f} m/s")
                else:
                    context_parts.append(f"\n  GARMIN ACTIVITIES TODAY: None recorded")

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
            context_parts.append(f"  Resting HR: {garmin_y['resting_hr']}")
            context_parts.append(f"  Body Battery: {garmin_y['body_battery']}")
            context_parts.append(f"  Stress Avg: {garmin_y['stress_avg']}")
            context_parts.append(f"  Steps: {garmin_y['steps']}")

        # Day before yesterday for 3-day trend
        day_before = (date.today() - timedelta(days=2)).isoformat()
        garmin_db = db.execute(
            "SELECT * FROM garmin_data WHERE user_id = ? AND date = ?",
            (user_id, day_before)
        ).fetchone()
        if garmin_db:
            context_parts.append(f"\n2 DAYS AGO GARMIN DATA ({day_before}):")
            context_parts.append(f"  Weight: {garmin_db['weight_lb']} lb")
            context_parts.append(f"  HRV: {garmin_db['hrv']}")
            context_parts.append(f"  Resting HR: {garmin_db['resting_hr']}")

        # 7-day averages and trends
        week_ago = (date.today() - timedelta(days=7)).isoformat()
        week_rows = db.execute(
            "SELECT date, hrv, resting_hr, weight_lb, sleep_score, body_battery, stress_avg FROM garmin_data WHERE user_id = ? AND date >= ? ORDER BY date",
            (user_id, week_ago)
        ).fetchall()
        if week_rows:
            context_parts.append(f"\n7-DAY HISTORY (for trend analysis):")
            hrv_vals = [r["hrv"] for r in week_rows if r["hrv"] is not None]
            rhr_vals = [r["resting_hr"] for r in week_rows if r["resting_hr"] is not None]
            wt_vals = [r["weight_lb"] for r in week_rows if r["weight_lb"] is not None]
            sleep_vals = [r["sleep_score"] for r in week_rows if r["sleep_score"] is not None]

            if hrv_vals:
                context_parts.append(f"  HRV 7-day avg: {sum(hrv_vals)/len(hrv_vals):.1f} | min: {min(hrv_vals)} | max: {max(hrv_vals)} | count: {len(hrv_vals)}")
            if rhr_vals:
                context_parts.append(f"  RHR 7-day avg: {sum(rhr_vals)/len(rhr_vals):.1f} | min: {min(rhr_vals)} | max: {max(rhr_vals)}")
            if wt_vals:
                context_parts.append(f"  Weight 7-day avg: {sum(wt_vals)/len(wt_vals):.1f} | min: {min(wt_vals)} | max: {max(wt_vals)}")
                if len(wt_vals) >= 2:
                    weekly_delta = wt_vals[-1] - wt_vals[0]
                    context_parts.append(f"  Weight weekly delta: {weekly_delta:+.1f} lb")
            if sleep_vals:
                context_parts.append(f"  Sleep Score 7-day avg: {sum(sleep_vals)/len(sleep_vals):.1f}")

            # Day-by-day for trend visualization
            day_parts = []
            for r in week_rows:
                day_parts.append(f"{r['date']}:HRV={r['hrv']}/RHR={r['resting_hr']}/Wt={r['weight_lb']}")
            context_parts.append(f"  Day-by-day: {', '.join(day_parts)}")

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

    # Today's workout logs (lift data, swim notes, etc.)
    workouts = db.execute(
        "SELECT * FROM workout_logs WHERE user_id = ? AND date = ? ORDER BY created_at",
        (user_id, today)
    ).fetchall()
    if workouts:
        context_parts.append(f"\nTODAY'S WORKOUT LOGS:")
        for w in workouts:
            context_parts.append(f"  [{w['workout_type'].upper()}] Logged at {w['created_at']}:")
            context_parts.append(f"  {w['log_text']}")
            if w['notes']:
                context_parts.append(f"  Notes: {w['notes']}")

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

    # Auto-pull Garmin data if not already pulled today
    today = date.today().isoformat()
    has_garmin_today = db.execute(
        "SELECT COUNT(*) as cnt FROM garmin_data WHERE user_id = ? AND date = ?",
        (user_id, today)
    ).fetchone()["cnt"] > 0
    if not has_garmin_today and app.config["GARMIN_EMAIL"] and app.config["GARMIN_PASSWORD"]:
        try:
            result = pull_garmin_data(user_id, today)
            if result and result.get("error"):
                logger.warning("Garmin auto-pull in chat failed: %s", result["error"])
        except Exception as e:
            logger.error("Garmin auto-pull in chat exception: %s", str(e))

    # Auto-detect and save lift/workout logs from chat messages
    msg_lower = user_message.lower()
    if ("lift log" in msg_lower or "physioiq lift" in msg_lower or
        ("set " in msg_lower and "reps" in msg_lower) or
        ("×" in user_message and "reps" in msg_lower)):
        workout_type = "lift"
        if "swim" in msg_lower:
            workout_type = "both"
        try:
            db.execute("""
                INSERT INTO workout_logs (user_id, date, workout_type, log_text, notes)
                VALUES (?, ?, ?, ?, ?)
            """, (user_id, today, workout_type, user_message, "Auto-detected from chat"))
            db.commit()
            logger.info("Auto-saved workout log from chat: type=%s, length=%d", workout_type, len(user_message))
        except Exception as e:
            logger.warning("Failed to auto-save workout log: %s", str(e))

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

def pull_garmin_data(user_id, target_date=None, force=False):
    """Pull data from Garmin Connect and store in database."""
    try:
        from garminconnect import Garmin
    except ImportError:
        logger.error("garminconnect not installed")
        return {"error": "garminconnect not installed. Run: pip install garminconnect"}

    email = app.config["GARMIN_EMAIL"]
    password = app.config["GARMIN_PASSWORD"]
    if not email or not password:
        logger.error("Garmin credentials not configured (email=%s, password=%s)", bool(email), bool(password))
        return {"error": "Garmin credentials not configured"}

    target = target_date or date.today().isoformat()

    # --- Rate-limit cooldown: don't retry login within 15 minutes of a 429 failure ---
    token_dir = os.path.join(os.path.dirname(app.config["DATABASE"]), ".garmin_tokens")
    os.makedirs(token_dir, exist_ok=True)
    token_file = os.path.join(token_dir, "tokens.json")
    cooldown_file = os.path.join(token_dir, "rate_limit_cooldown.txt")
    COOLDOWN_SECONDS = 900  # 15 minutes

    if not force and os.path.exists(cooldown_file):
        try:
            with open(cooldown_file, "r") as f:
                last_fail = float(f.read().strip())
            elapsed = time.time() - last_fail
            if elapsed < COOLDOWN_SECONDS:
                remaining = int(COOLDOWN_SECONDS - elapsed)
                logger.info("Garmin rate-limit cooldown active — %d seconds remaining. Skipping pull.", remaining)
                return {"error": f"Garmin rate-limited. Retry in {remaining}s. Use /api/garmin/pull with force=true to override."}
        except Exception:
            pass

    logger.info("Garmin pull starting for user=%s date=%s email=%s", user_id, target, email)

    try:
        garmin = Garmin(email, password)

        # Try to load saved session tokens first
        logged_in = False
        if os.path.exists(token_file):
            try:
                with open(token_file, "r") as f:
                    token_data = f.read()
                logger.info("Found saved Garmin tokens, attempting token login...")
                garmin.garth.loads(token_data)
                # Set display_name from garth profile (same source as login())
                garmin.display_name = garmin.garth.profile["displayName"]
                # Validate the token works by making a lightweight call
                full_name = garmin.get_full_name()
                logged_in = True
                logger.info("Garmin token login successful (display_name: %s, full_name: %s)", garmin.display_name, full_name)
                # Clear cooldown on successful token login
                if os.path.exists(cooldown_file):
                    os.remove(cooldown_file)
            except Exception as e:
                logger.warning("Garmin token login failed: %s — falling back to password login", str(e))
                logged_in = False

        if not logged_in:
            # Patch garth's session with proper 429 retry handling
            # This makes it wait with exponential backoff instead of hammering the endpoint
            try:
                from urllib3.util.retry import Retry
                from requests.adapters import HTTPAdapter
                retry_strategy = Retry(
                    total=3,
                    backoff_factor=10,  # 10s, 20s, 40s between retries
                    status_forcelist=[429, 500, 502, 503, 504],
                    respect_retry_after_header=True,
                    allowed_methods=["GET", "POST"],
                )
                adapter = HTTPAdapter(max_retries=retry_strategy)
                # Patch garth's internal client session
                if hasattr(garmin, 'garth') and hasattr(garmin.garth, 'sess'):
                    garmin.garth.sess.mount("https://", adapter)
                    garmin.garth.sess.mount("http://", adapter)
                    logger.info("Patched garth session with 429-aware retry (backoff=10s)")
            except Exception as patch_err:
                logger.warning("Could not patch garth retry config: %s", str(patch_err))

            logger.info("Attempting Garmin password login for %s ...", email)
            try:
                garmin.login()
            except Exception as login_err:
                err_str = str(login_err)
                if "429" in err_str or "too many" in err_str.lower() or "rate" in err_str.lower():
                    # Write cooldown file so we don't retry for 15 minutes
                    logger.error("Garmin 429 rate limit hit — activating %ds cooldown", COOLDOWN_SECONDS)
                    try:
                        with open(cooldown_file, "w") as f:
                            f.write(str(time.time()))
                    except Exception:
                        pass
                raise login_err
            logger.info("Garmin password login successful (display_name: %s)", garmin.display_name)
            # Save tokens for next time
            try:
                with open(token_file, "w") as f:
                    f.write(garmin.garth.dumps())
                logger.info("Garmin tokens saved to %s", token_file)
            except Exception as e:
                logger.warning("Could not save Garmin tokens: %s", str(e))

        # Fetch data (with 403 retry — stale tokens can cause Forbidden)
        logger.info("Fetching Garmin stats for %s ...", target)
        try:
            stats = garmin.get_stats(target)
        except Exception as stats_err:
            if "403" in str(stats_err) or "Forbidden" in str(stats_err):
                logger.warning("Garmin get_stats got 403 — deleting cached tokens and retrying with fresh login")
                if os.path.exists(token_file):
                    os.remove(token_file)
                garmin = Garmin(email, password)
                garmin.login()
                logger.info("Fresh login successful (display_name: %s), retrying get_stats", garmin.display_name)
                stats = garmin.get_stats(target)
                # Save the fresh tokens
                try:
                    with open(token_file, "w") as f:
                        f.write(garmin.garth.dumps())
                except Exception:
                    pass
            else:
                raise stats_err
        logger.info("Garmin stats keys: %s", list(stats.keys()) if stats else "None")

        sleep = None
        try:
            sleep = garmin.get_sleep_data(target)
            logger.info("Garmin sleep data received: %s", bool(sleep))
        except Exception as e:
            logger.warning("Garmin sleep data fetch failed: %s", str(e))

        hrv_data = None
        try:
            hrv_data = garmin.get_hrv_data(target)
            logger.info("Garmin HRV data received: %s", bool(hrv_data))
        except Exception as e:
            logger.warning("Garmin HRV data fetch failed: %s", str(e))

        # Extract key metrics
        weight_lb = None
        try:
            weight_data = garmin.get_body_composition(target)
            logger.info("Garmin body_composition keys: %s", list(weight_data.keys()) if weight_data and isinstance(weight_data, dict) else type(weight_data))
            if weight_data:
                # Try top-level weight first
                raw_wt = weight_data.get("weight")
                # Try dateWeightList (common Garmin API structure)
                if not raw_wt and weight_data.get("dateWeightList"):
                    wt_list = weight_data["dateWeightList"]
                    if wt_list and len(wt_list) > 0:
                        raw_wt = wt_list[-1].get("weight")  # most recent entry
                        logger.info("Garmin weight from dateWeightList: raw=%s", raw_wt)
                # Try totalAverage
                if not raw_wt and weight_data.get("totalAverage"):
                    raw_wt = weight_data["totalAverage"].get("weight")
                    logger.info("Garmin weight from totalAverage: raw=%s", raw_wt)
                if raw_wt and raw_wt > 0:
                    weight_lb = round(raw_wt / 1000 * 2.20462, 1)
                    logger.info("Garmin weight: %s lb (raw grams: %s)", weight_lb, raw_wt)
                else:
                    logger.info("Garmin body_composition returned but no weight value found")
        except Exception as e:
            logger.warning("Garmin weight fetch failed: %s", str(e))

        # Fallback: try to extract weight from stats if body_composition failed
        if weight_lb is None and stats:
            stats_weight = stats.get("weight")
            if stats_weight and stats_weight > 0:
                weight_lb = round(stats_weight / 1000 * 2.20462, 1)
                logger.info("Garmin weight from stats fallback: %s lb (raw grams: %s)", weight_lb, stats_weight)

        sleep_score = None
        if sleep:
            try:
                sleep_score = sleep.get("dailySleepDTO", {}).get("sleepScores", {}).get("overall", {}).get("value")
                logger.info("Sleep score: %s", sleep_score)
            except Exception:
                pass

        hrv = None
        if hrv_data:
            try:
                hrv = hrv_data.get("hrvSummary", {}).get("lastNightAvg")
                logger.info("HRV: %s", hrv)
            except Exception:
                pass

        resting_hr = stats.get("restingHeartRate") if stats else None
        body_battery = stats.get("bodyBatteryChargedValue") if stats else None
        stress_avg = stats.get("averageStressLevel") if stats else None
        steps = stats.get("totalSteps") if stats else None
        active_min = stats.get("activeSeconds", 0) // 60 if stats and stats.get("activeSeconds") else None
        cal_total = stats.get("totalKilocalories") if stats else None

        logger.info("Garmin metrics — RHR:%s BB:%s stress:%s steps:%s cal:%s",
                     resting_hr, body_battery, stress_avg, steps, cal_total)

        # Fetch ACTIVITIES (swim, run, lift, etc.) for the target date
        activities = []
        try:
            acts_raw = garmin.get_activities_by_date(target, target)
            if acts_raw:
                for act in acts_raw:
                    a = {
                        "name": act.get("activityName", ""),
                        "type": act.get("activityType", {}).get("typeKey", ""),
                        "sport": act.get("sportTypeId", ""),
                        "start": act.get("startTimeLocal", ""),
                        "duration_sec": act.get("duration", 0),
                        "distance_m": act.get("distance", 0),
                        "calories": act.get("calories", 0),
                        "avg_hr": act.get("averageHR", 0),
                        "max_hr": act.get("maxHR", 0),
                        "avg_speed": act.get("averageSpeed", 0),
                        "training_effect_aerobic": act.get("aerobicTrainingEffect", 0),
                        "training_effect_anaerobic": act.get("anaerobicTrainingEffect", 0),
                        "laps": act.get("lapCount", 0),
                        "steps": act.get("steps", 0),
                        "strokes": act.get("strokes", 0),
                        "avg_stroke_distance": act.get("avgStrokeDistance", 0),
                        "pool_length": act.get("poolLength", 0),
                        "swim_SWOLF": act.get("averageSwolf", 0),
                        "elevationGain": act.get("elevationGain", 0),
                        "vo2max": act.get("vO2MaxValue", 0),
                    }
                    # Duration in minutes
                    a["duration_min"] = round(a["duration_sec"] / 60, 1) if a["duration_sec"] else 0
                    # Distance in yards (for swimming) or miles
                    if a["distance_m"]:
                        a["distance_yards"] = round(a["distance_m"] * 1.09361, 0)
                        a["distance_miles"] = round(a["distance_m"] / 1609.34, 2)
                    activities.append(a)
                logger.info("Garmin activities found: %d — types: %s", len(activities), [a["type"] for a in activities])
            else:
                logger.info("No Garmin activities found for %s", target)
        except Exception as e:
            logger.warning("Garmin activities fetch failed: %s", str(e))

        # Store in database — include full sleep data AND activities in raw_json
        db = get_db()
        db.execute("""
            INSERT OR REPLACE INTO garmin_data
            (user_id, date, weight_lb, sleep_score, hrv, resting_hr, body_battery,
             stress_avg, steps, active_minutes, calories_total, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (user_id, target, weight_lb, sleep_score, hrv, resting_hr, body_battery,
              stress_avg, steps, active_min, cal_total,
              json.dumps({"stats": stats, "sleep": sleep, "sleep_score": sleep_score, "hrv_raw": hrv_data, "activities": activities})))
        db.commit()

        logger.info("Garmin data stored successfully for %s", target)
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
        logger.error("Garmin pull FAILED: %s\n%s", str(e), traceback.format_exc())
        return {"error": f"Garmin pull failed: {str(e)}"}

# ---------------------------------------------------------------------------
# Report Generation
# ---------------------------------------------------------------------------

def generate_report(user_id, report_type="morning", report_id=None):
    """Generate a PhysioIQ report using Claude. If report_id is given, updates that row (async mode)."""
    client = get_claude_client()
    if not client:
        return {"error": "Anthropic API key not configured"}

    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        return {"error": "User not found"}

    today = date.today().isoformat()

    # Auto-pull Garmin data before generating the report
    if app.config["GARMIN_EMAIL"] and app.config["GARMIN_PASSWORD"]:
        try:
            result = pull_garmin_data(user_id, today)
            if result and result.get("error"):
                logger.warning("Garmin auto-pull in report failed: %s", result["error"])
        except Exception as e:
            logger.error("Garmin auto-pull in report exception: %s", str(e))

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

    has_workouts = db.execute(
        "SELECT COUNT(*) as cnt FROM workout_logs WHERE user_id = ? AND date = ?",
        (user_id, today)
    ).fetchone()["cnt"] > 0

    data_notes = []
    data_notes.append("""
ABSOLUTE RULE — DO NOT FABRICATE DATA:
You MUST ONLY use data explicitly provided in the CURRENT DATA section above. If a data point is not present, say "No data available" or "Not logged". NEVER invent, estimate, or hallucinate:
- Exercise names, sets, reps, or weights (unless they appear in WORKOUT LOGS above)
- Meal items, calories, or macros (unless they appear in TODAY'S MEALS above)
- Swim distances, times, or HR data (unless they appear in GARMIN ACTIVITIES above)
- Specific numerical values for any metric not in the data
If there are no meals logged, show the meal table as empty with zeros. If there's no swim activity, say "No swim recorded in Garmin." If there's no lift log, say "No workout log submitted." Do NOT fill in plausible-looking fake data.""")

    if not has_garmin:
        data_notes.append("DATA STATUS — GARMIN: No Garmin data available for today. Do not fabricate HRV, RHR, sleep, weight, or activity values.")
    if not has_meals:
        data_notes.append("DATA STATUS — MEALS: No meals logged today. Show meal table as empty with all zeros. Do not invent meal items.")
    if not has_workouts:
        data_notes.append("DATA STATUS — WORKOUT LOG: No workout/lift log submitted today. Do not fabricate exercises, sets, reps, or weights. If Garmin shows an activity (like swimming), use that data. But do NOT invent lift details.")

    data_note = "\n\n".join(data_notes)

    html_style_instructions = """

OUTPUT FORMAT: Return a <style> block followed by HTML content. No markdown fences, no ```html, no doctype/html/head/body tags. Just <style>...</style> then the HTML divs.

START YOUR OUTPUT WITH THIS EXACT CSS (copy it verbatim, do not modify):

<style>
:root{--bg:#0d0d0d;--card:#1c1c1e;--line:#2a2a2c;--txt:#f0f0f0;--dim:#9a9a9d;
  --green:#4dff88;--yellow:#ffe066;--orange:#ffb347;--blue:#4da6ff;--red:#ff6b6b;--purple:#cc88ff;--teal:#66ccff}
*{box-sizing:border-box;margin:0;padding:0}
.rpt{color:var(--txt);font-family:'Inter',-apple-system,sans-serif;font-size:14px;line-height:1.45;padding:14px 12px;max-width:393px;margin:0 auto}
.rpt h1{font-size:18px;font-weight:700;letter-spacing:-.3px}
.rpt h2{font-size:13px;font-weight:600;color:var(--dim);text-transform:uppercase;letter-spacing:.6px;margin-bottom:8px}
.card{background:var(--card);border-radius:12px;padding:14px;margin-bottom:12px;border:1px solid var(--line)}
.card.alert{border-color:var(--orange);background:rgba(255,179,71,.06)}
.card.alert-red{border-color:var(--red);background:rgba(255,107,107,.06)}
.card.alert-green{border-color:var(--green);background:rgba(77,255,136,.05)}
.row{display:flex;justify-content:space-between;align-items:center;padding:6px 0;border-bottom:1px solid var(--line)}
.row:last-child{border-bottom:none}
.row .lbl{color:var(--dim);font-size:13px}
.row .val{font-weight:600;font-size:14px}
.badge{display:inline-block;padding:3px 9px;border-radius:99px;font-size:11px;font-weight:600;letter-spacing:.3px}
.b-green{background:rgba(77,255,136,.15);color:var(--green)}
.b-yellow{background:rgba(255,224,102,.15);color:var(--yellow)}
.b-orange{background:rgba(255,179,71,.15);color:var(--orange)}
.b-red{background:rgba(255,107,107,.15);color:var(--red)}
.b-blue{background:rgba(77,166,255,.15);color:var(--blue)}
.b-purple{background:rgba(204,136,255,.15);color:var(--purple)}
.hdr{display:flex;flex-direction:column;gap:6px;margin-bottom:14px}
.hdr-top{display:flex;justify-content:space-between;align-items:center}
.hdr .date{color:var(--dim);font-size:12px}
.hdr .badges{display:flex;gap:6px;flex-wrap:wrap}
.kpi{display:flex;gap:8px;margin-top:10px}
.kpi-box{flex:1;background:rgba(255,255,255,.03);border-radius:8px;padding:10px;text-align:center}
.kpi-num{font-size:18px;font-weight:700;color:var(--txt)}
.kpi-lbl{font-size:10px;color:var(--dim);text-transform:uppercase;letter-spacing:.4px;margin-top:3px}
.bar-wrap{margin:8px 0}
.bar-lbl{display:flex;justify-content:space-between;font-size:11px;color:var(--dim);margin-bottom:3px}
.bar{height:8px;background:rgba(255,255,255,.06);border-radius:4px;overflow:hidden}
.bar-fill{height:100%;border-radius:4px}
.calc{background:rgba(0,0,0,.25);border-radius:6px;padding:8px;margin-top:8px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;color:var(--dim);line-height:1.55}
.calc .ttl{color:var(--txt);font-weight:600}
.foot{text-align:center;color:var(--dim);font-size:11px;padding:14px 0 4px}
.note{font-size:12px;color:var(--dim);font-style:italic;margin-top:6px}
.meal-tbl{width:100%;font-size:11px;margin-top:6px;border-collapse:collapse}
.meal-tbl th{text-align:left;color:var(--dim);font-weight:500;padding:4px 3px;border-bottom:1px solid var(--line)}
.meal-tbl td{padding:4px 3px;border-bottom:1px solid var(--line)}
.meal-tbl .sec{color:var(--purple);font-weight:600;background:rgba(204,136,255,.05)}
.meal-tbl .num{text-align:right;color:var(--blue)}
.meal-tbl .sub{color:var(--dim);font-style:italic}
.meal-tbl .tot{font-weight:700;color:var(--green);background:rgba(77,255,136,.06)}
</style>

Then wrap ALL content in <div class="rpt">...</div>.

HTML PATTERNS TO USE (use CSS classes, NOT inline styles):

HEADER:
<div class="hdr">
  <div class="hdr-top">
    <div><h1>Report Title</h1><div class="date">Day Date · Location · Details</div></div>
    <div class="badges"><span class="badge b-green">Badge</span><span class="badge b-red">Badge</span></div>
  </div>
</div>

CARD (normal, alert-green, alert-red, alert):
<div class="card">
  <h2>Section Title</h2>
  <!-- content -->
</div>
<div class="card alert-green"><h2 style="color:var(--green)">Positive Section</h2>...</div>
<div class="card alert-red"><h2 style="color:var(--red)">Warning Section</h2>...</div>
<div class="card alert"><h2 style="color:var(--orange)">Caution Section</h2>...</div>

DATA ROW:
<div class="row"><span class="lbl">Label</span><span class="val">Value</span></div>
<div class="row"><span class="lbl">Label</span><span class="val" style="color:var(--red)">Red Value</span></div>

KPI BOXES:
<div class="kpi">
  <div class="kpi-box"><div class="kpi-num" style="color:var(--green)">2,925</div><div class="kpi-lbl">TDEE</div></div>
  <div class="kpi-box"><div class="kpi-num" style="color:var(--yellow)">2,125</div><div class="kpi-lbl">target</div></div>
</div>

PROGRESS BAR:
<div class="bar-wrap">
  <div class="bar-lbl"><span>Protein 205g</span><span>vs 220g target (93%)</span></div>
  <div class="bar"><div class="bar-fill" style="width:93%;background:var(--green)"></div></div>
</div>

MONOSPACE CALCULATION:
<div class="calc">
  BMR ............................... 1,620<br>
  NEAT + TEF .......................... 480<br>
  <span class="ttl">TDEE ............................. 2,925</span>
</div>

MEAL TABLE:
<table class="meal-tbl">
  <tr><th>Item</th><th class="num">P</th><th class="num">Cal</th><th class="num">C</th></tr>
  <tr class="sec"><td colspan="4">MEAL TIMING ✓</td></tr>
  <tr><td>Food Item</td><td class="num">30</td><td class="num">160</td><td class="num">4</td></tr>
  <tr class="sub"><td>subtotal</td><td class="num">30</td><td class="num">160</td><td class="num">4</td></tr>
  <tr class="tot"><td>DAY TOTAL</td><td class="num">205</td><td class="num">2,004</td><td class="num">141</td></tr>
</table>

NOTE (italic muted):
<div class="note">Contextual note here</div>
NOTE (important, white text):
<div class="note" style="color:var(--txt);font-style:normal">Important commentary with <b>bold key points</b></div>

FOOTER:
<div class="foot">PhysioIQ · Type · Date · Key Stats Summary</div>

CRITICAL RULES:
- Use CSS class names from the stylesheet, NOT inline styles (except color overrides on .val and .kpi-num)
- Every section must be a .card div
- Include contextual .note paragraphs in EVERY card explaining what the numbers MEAN
- Use .alert-green for positive findings, .alert-red for warnings/flags, .alert for cautions
- Include a .meal-tbl with FULL meal breakdown when meal data is available
- Include .bar-wrap progress bars for macro tracking
- Include .calc monospace blocks for TDEE/energy calculations
- Use .badge spans for status indicators
- ALWAYS include a .foot footer line summarizing key stats
"""

    prompts = {
        "morning": f"""Generate Ruben's PhysioIQ Morning Report. This must be a RICH, detailed, coach-quality report — NOT a generic dashboard. Every section must have contextual commentary explaining what the numbers MEAN, not just listing them.

REQUIRED SECTIONS (in this order):

1. **HEADER** — "Morning Report" title with today's date and day of week. Include TWO badges in the top-right:
   - Readiness badge (PUSH/PUSH-lean/MODERATE/DIAL BACK/REST) with appropriate color
   - HRV badge showing today's HRV value
   Use the header style with flexbox layout.

2. **HRV / READINESS (alert card)** — This is the MOST IMPORTANT section. Use an alert-style card (green border if HRV is above 7-day avg, orange if below).
   - HRV overnight value with delta vs yesterday (absolute AND percentage) — e.g., "68.2 ms ↑ +14.4 vs yest 53.8 (+27%)"
   - HRV vs 7-day average with interpretation
   - RHR with delta vs yesterday — e.g., "37 bpm ↓ -2 vs yest 39"
   - Overnight stress avg / max
   - Readiness score with explanation of what's HELPING and what's HOLDING IT BACK
   - **CONTEXTUAL PARAGRAPH**: Interpret what this combination means. Is this supercompensation? Declining trend? Stable baseline? Connect HRV + RHR + stress into a narrative. Example: "HRV 68.2 is your highest reading in weeks. This is classic supercompensation — after the week of stress, once you got home your nervous system bounced way above baseline."
   - Give a clear CALL: "PUSH-leaning MODERATE. Hit the lift hard but don't add anything extra."

3. **WEIGHT** — Current weight in KPI boxes (today's weight, target weight = 173 lb, delta vs last reading).
   - Weekly velocity if data available
   - Context: travel effects, glycogen status, trend direction
   - Commentary note about what to expect

4. **SLEEP DETAIL** — Duration prominently displayed.
   - Visual sleep stage BAR (deep=green, light=yellow, REM=purple, awake=red) with percentages labeled
   - Each stage on its own row: minutes + commentary (e.g., "Deep: 79 min ✓ (body protected this)", "REM: 20 min (low — flight late-arrival pattern)")
   - Respiration rate
   - Contextual paragraph: what did the body prioritize? What's good? What's low and why?

5. **TODAY'S TIMELINE** — Chronological plan using timeline row style:
   - Pre-swim food + supplements + timing
   - Post-swim nutrition
   - Sauna
   - Evening supplements + sleep target

6. **ENERGY BALANCE** — Full TDEE calculation in monospace calc style:
   - BMR line
   - NEAT + TEF line
   - Swim burn (show MET calculation)
   - Lift burn (show MET calculation)
   - Sauna burn
   - TDEE total (bold)
   - Eat target
   - Planned deficit
   - Context note

7. **MACRO TARGETS** — Calories, protein floor, carbs, fat, sodium in data rows.
   - Context for each if relevant (e.g., "recover from yesterday's fat spike")

8. **SUPPLEMENTS** — Full daily schedule with timing in data rows.
   - Note calcium citrate timing rules
   - Note if smoothie day affects schedule

9. **MERCURY STATUS** — Weekly budget used / remaining, what's available, low-mercury options.

10. **PRE-WORKOUT NUTRITION** — SPECIFIC food + timing recommendations for before the swim. Example: "Banana + Applesauce + SaltStick 2 caps · 16 oz water + RE-LYTE" not "eat a light snack."

11. **COACH'S NOTE** — This is the SUMMARY. Must cover:
    - Key finding from HRV/recovery
    - Sleep concern if any
    - Readiness call with reasoning (PUSH/MODERATE/DIAL BACK)
    - One thing to watch for today
    - Motivational closer tied to data

12. **FOOTER** — Single compact line: "PhysioIQ · Morning · DATE · Location · Wt VALUE · HRV VALUE · RHR VALUE · READINESS_CALL"

IMPORTANT: The morning report must NOT include workout intensity details, swim yardage targets, HR zone targets, training effect targets, or exercise protocols. Those belong ONLY in the Post-Workout report. The morning report focuses on recovery metrics, readiness, nutrition, and supplements.

{html_style_instructions}{data_note}""",

        "post_workout": f"""Generate Ruben's PhysioIQ Post-Workout Report. This must be RICH, detailed, data-driven — not generic.

CONTEXT: The data section above includes Garmin data (swim activity), workout logs (lift data), and meals logged today. Use ALL of it.

REQUIRED SECTIONS (use the CSS classes from the stylesheet):

1. HEADER (.hdr) — "PW Report" title. Date line with day, location, workout details. Badges for swim rating and any nutrition flags.

2. SWIM SUMMARY (.card.alert-green if good, .card if neutral) — h2 with "Swim · [Assessment]". Data rows (.row) for: Distance, Moving time, Pace, Avg HR · Max, TE Aerobic, MET estimate. Include a .note with coaching interpretation — compare to recent sessions, rate the effort, explain what the intensity means for recovery.

3. LIFT SUMMARY (.card) — If workout log data exists: h2 with "Lift · [Type]". List exercises, sets, reps, weights. Total volume. Highlight PRs. If no lift data, note it.

4. TDEE / ENERGY BALANCE (.card) — h2 "TDEE · Pre-Workout Estimate". KPI boxes (.kpi) showing TDEE, eat target, and planned intake. Full monospace calculation (.calc) showing BMR, NEAT+TEF, swim burn (MET × weight × duration), lift burn, sauna, total. Include a .note explaining the numbers.

5. NUTRITION FLAG (.card.alert-red if bad, .card.alert if caution) — Identify the BIGGEST nutrition issue. Show planned vs target with gap. Explain WHY it matters (glycogen, recovery, etc). Give SPECIFIC fix recommendations with exact foods and amounts.

6. FULL MEAL LOG (.card with .meal-tbl) — h2 "Full Meal Log". Build a complete meal table using .meal-tbl with columns: Item, P (protein g), Cal, C (carbs g). Group by meal timing (.sec rows). Include .sub subtotal rows and .tot day total row. This is CRITICAL — always include the full meal breakdown.

7. MACRO ANALYSIS (.card with .bar-wrap) — Progress bars for Protein, Calories, and Carbs showing actual vs target with percentages. Color code: green if >90%, yellow if 70-90%, red if <70%. Include .note with assessment.

8. WHAT'S WORKING (.card.alert-green) — h2 "What's Working ✓". List specific things done right today with bullet points. Be genuine and data-specific.

9. RECOVERY SNAPSHOT (.card) — Current readiness, HRV, RHR, sleep from last night. .note explaining recovery state.

10. SUPPLEMENTS REMAINING (.card) — What supplements are left for today with timing. Note calcium citrate rules.

11. TONIGHT + TOMORROW (.card) — Bed time target, sleep target, tomorrow's plan. .note tying it back to today's data.

12. FOOTER (.foot) — Single line: "PhysioIQ · PW · DATE · Workout type · TDEE value · Plan value · KEY ACTION"

{html_style_instructions}{data_note}""",

        "eod": f"""Generate Ruben's PhysioIQ End-of-Day Report with the FULL GRADING SYSTEM.

REQUIRED SECTIONS:

1. **HEADER** — "End-of-Day Report" with date. Prominently display the OVERALL LETTER GRADE as a large badge.

2. **DAILY SCORECARD** — This is the centerpiece. Show a table/grid of 6 criteria, each with:
   - Criterion name
   - Letter grade (A/B/C/F) with color coding (A=green, B=yellow, C=orange, F=red)
   - Brief explanation (e.g., "Protein 178/185g (96%), Cals 2050/2100 (98%), Carbs 195/200 (97%)")

   The 6 criteria are:
   1. **Macro Targets (P/Cal/C)**: A=all 3 within 95%+, B=2 of 3 within 90%, C=1 of 3 within 90%, F=all off >20%
   2. **Deficit Band** (-600 to -800): A=in band or planned surplus, B=within ±200, C=within ±500, F=>-1100 or >+800
   3. **Workout Execution**: A=hit planned intensity+duration, B=modified but completed, C=partial/dialed back, F=skipped without reason
   4. **Supplement Compliance**: A=13/13+9PM on time, B=11-12/13, C=9-10/13, F=<9
   5. **Recovery Discipline**: A=sleep≥7hr+mag+no PM caffeine+no alcohol, B=1 miss, C=2 misses, F=3+ misses or sleep<5hr
   6. **Mercury/Nutrition Flags**: A=no flags, B=1 minor, C=1 major or 2 minor, F=mercury cap violated

   Overall grade: A=5-6 at A, A-=4 at A rest B, B+=4 at A/B no C/F, B=mostly B 1-2 A, C+=2+ at C, C/D=3+ at C or any F, F=any single F

3. **NUTRITION RECAP** — Total macros vs targets with percentage compliance for each macro. Meal-by-meal summary. Flag any issues. (orange header)

4. **ACTIVITY SUMMARY** — Steps vs goal, active minutes, calories burned, workout details with comparison to plan. (green header)

5. **ENERGY BALANCE** — Final TDEE calculation vs actual intake. Show actual deficit and compare to target band (-600 to -800). (blue header)

6. **RECOVERY METRICS** — Current body battery, HRV trend direction, sleep readiness prediction for tonight. (teal header)

7. **WEIGHT TRACKING** — Today's weight in context of weekly and monthly trend. Weekly velocity. (blue header)

8. **WINS** — What went well today. Celebrate with specific data points. Be genuine. (green header)

9. **AREAS TO IMPROVE** — Honest assessment of gaps. Specific, actionable. No sugar-coating but no catastrophizing. (yellow header)

10. **TOMORROW'S GAME PLAN** — Training plan, nutrition adjustments based on today's grades, recovery priorities. (purple header)

11. **COACH'S CLOSING NOTE** — Summarize the day's grade, acknowledge effort, set up tomorrow. End on a forward-looking note. (purple header)

{html_style_instructions}{data_note}"""
    }

    system = user["system_prompt"] or "You are PhysioIQ, a personal body performance coach."
    full_system = f"{system}\n\n--- CURRENT DATA ---\n{context}"

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=8192,
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

        # Store report — either update existing placeholder or insert new
        if report_id:
            db.execute("""
                UPDATE reports SET html_content = ?, metrics_json = ?, status = 'complete'
                WHERE id = ?
            """, (html, context, report_id))
        else:
            db.execute("""
                INSERT INTO reports (user_id, date, report_type, html_content, metrics_json, status)
                VALUES (?, ?, ?, ?, ?, 'complete')
            """, (user_id, today, report_type, html, context))
            report_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.commit()

        return {"success": True, "report_id": report_id, "html": html}

    except Exception as e:
        logger.error("Report generation failed: %s", str(e))
        if report_id:
            try:
                db.execute("""
                    UPDATE reports SET html_content = ?, status = 'error' WHERE id = ?
                """, (f"Error: {str(e)}", report_id))
                db.commit()
            except Exception:
                pass
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

@app.route("/api/meals/import", methods=["POST"])
@require_auth
def import_meal_plan():
    """Parse pasted meal plan text (from Excel) and bulk-import meals.
    Expects tab-separated or structured text with meal sections like PRE-SWIM, POST-SWIM, BREAKFAST, etc.
    Each food line should have: food_item, qty, protein_g, calories, carbs_g, consumed? columns."""
    data = request.json or {}
    raw_text = data.get("text", "").strip()
    if not raw_text:
        return jsonify({"error": "No meal plan text provided"}), 400

    today = date.today().isoformat()
    db = get_db()

    # Delete existing meals for today (fresh import replaces all)
    db.execute("DELETE FROM meals WHERE user_id = ? AND date = ?", (g.user_id, today))

    # Parse the meal plan text
    meals_imported = []
    current_section = "other"
    section_map = {
        "pre-swim": "pre_swim", "pre swim": "pre_swim",
        "post-swim": "post_swim", "post swim": "post_swim",
        "breakfast": "breakfast", "lunch": "lunch",
        "afternoon snack": "snack", "snack": "snack",
        "dinner": "dinner", "night": "night",
        "late night": "late_night",
    }

    lines = raw_text.split("\n")
    for line in lines:
        line_stripped = line.strip()
        if not line_stripped:
            continue

        # Check if this is a section header (e.g., "▸  PRE-SWIM  ~5:30 AM" or "PRE-SWIM subtotal:")
        line_lower = line_stripped.lower()

        # Skip subtotal/total lines
        if "subtotal" in line_lower or line_lower.startswith("totals") or line_lower.startswith("daily totals") or line_lower.startswith("target") or line_lower.startswith("vs ") or "consumed so far" in line_lower or "remaining needed" in line_lower:
            continue

        # Skip header rows and non-food lines
        if line_lower.startswith("meal / time") or line_lower.startswith("meal/time") or line_lower.startswith("food item") or "supplement" in line_lower or "sauna" in line_lower or "physioiq" in line_lower or "tdee" in line_lower or "test_revert" in line_lower or "notes / changes" in line_lower or "claude day grade" in line_lower:
            continue

        # Detect section headers
        found_section = False
        for key, val in section_map.items():
            if key in line_lower and ("▸" in line_stripped or "subtotal" not in line_lower):
                current_section = val
                found_section = True
                break
        if found_section:
            continue

        # Try to parse as a food line — split by tabs
        parts = line.split("\t")
        # Clean up parts
        parts = [p.strip() for p in parts]

        # Look for a line with: food_name, qty, protein, calories, carbs
        # The Excel format has: FOOD ITEM | QTY | PROTEIN g | CALORIES | CARBS g | CONSUMED? | NOTES
        # But columns might shift. Try to find numeric values.
        food_name = None
        protein = 0
        calories = 0
        carbs = 0
        fat = 0
        consumed = None

        if len(parts) >= 5:
            # Try standard format: [empty/food], food, qty, protein, calories, carbs, consumed, notes
            # Find the food name (first non-empty non-numeric string)
            for i, p in enumerate(parts):
                if p and not p.replace(".", "").replace("-", "").isdigit() and p.lower() not in ("yes", "no", ""):
                    food_name = p
                    break

            if food_name:
                # Find numeric values after the food name
                nums = []
                for p in parts:
                    try:
                        v = float(p)
                        nums.append(v)
                    except (ValueError, TypeError):
                        pass

                # Expected order: qty, protein, calories, carbs (we want protein, calories, carbs)
                if len(nums) >= 4:
                    # qty, protein, calories, carbs
                    protein = nums[1]
                    calories = nums[2]
                    carbs = nums[3]
                elif len(nums) >= 3:
                    protein = nums[0]
                    calories = nums[1]
                    carbs = nums[2]

                # Check consumed status
                for p in parts:
                    if p.lower() == "yes":
                        consumed = True
                    elif p.lower() == "no":
                        consumed = False

                # Skip zero-calorie placeholder lines
                if calories == 0 and protein == 0 and carbs == 0:
                    continue

                # Save the meal
                notes = f"Consumed: {'Yes' if consumed else 'Planned'}" if consumed is not None else "Planned"
                db.execute("""
                    INSERT INTO meals (user_id, date, meal_type, description, calories, protein_g, carbs_g, fat_g, fiber_g, notes)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (g.user_id, today, current_section, food_name, calories, protein, carbs, fat, 0, notes))

                meals_imported.append({
                    "section": current_section,
                    "food": food_name,
                    "protein": protein,
                    "calories": calories,
                    "carbs": carbs,
                    "consumed": consumed
                })

    db.commit()
    return jsonify({
        "success": True,
        "meals_imported": len(meals_imported),
        "meals": meals_imported
    })


# --- User Settings ---

@app.route("/api/settings", methods=["GET"])
@require_auth
def get_settings():
    db = get_db()
    rows = db.execute("SELECT key, value FROM user_settings WHERE user_id = ?", (g.user_id,)).fetchall()
    return jsonify({r["key"]: r["value"] for r in rows})


@app.route("/api/settings", methods=["POST"])
@require_auth
def save_settings():
    data = request.json or {}
    db = get_db()
    for key, value in data.items():
        db.execute("""
            INSERT INTO user_settings (user_id, key, value, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value, updated_at = datetime('now')
        """, (g.user_id, key, str(value)))
    db.commit()
    return jsonify({"success": True})


# --- Dropbox Meal Plan Fetch ---

@app.route("/api/meals/fetch-plan", methods=["POST"])
@require_auth
def fetch_meal_plan_from_dropbox():
    """Download the Excel meal plan from the user's saved Dropbox link, parse it, and import meals."""
    db = get_db()
    today = date.today().isoformat()

    # Get Dropbox URL from settings
    row = db.execute("SELECT value FROM user_settings WHERE user_id = ? AND key = 'dropbox_meal_plan_url'",
                     (g.user_id,)).fetchone()
    if not row:
        return jsonify({"error": "No Dropbox link saved. Paste your Dropbox share link in Meals settings first."}), 400

    dropbox_url = row["value"].strip()

    # Convert share link to direct download (change dl=0 to dl=1)
    if "dropbox.com" in dropbox_url:
        if "dl=0" in dropbox_url:
            dropbox_url = dropbox_url.replace("dl=0", "dl=1")
        elif "dl=1" not in dropbox_url:
            dropbox_url += ("&" if "?" in dropbox_url else "?") + "dl=1"

    logger.info(f"[DROPBOX] Fetching meal plan for user {g.user_id} from: {dropbox_url[:80]}...")

    # Download the Excel file
    try:
        resp = http_requests.get(dropbox_url, timeout=30, allow_redirects=True)
        resp.raise_for_status()
    except Exception as e:
        logger.error(f"[DROPBOX] Download failed: {e}")
        return jsonify({"error": f"Failed to download from Dropbox: {str(e)}"}), 500

    # Parse Excel with openpyxl
    try:
        import openpyxl
        wb = openpyxl.load_workbook(BytesIO(resp.content), read_only=True, data_only=True)

        # Find today's sheet or the most relevant sheet
        # Sheets might be named by date, week, or be the first/active sheet
        target_sheet = None
        today_str = date.today().strftime("%m/%d")       # e.g., "05/21"
        today_str2 = date.today().strftime("%-m/%-d")    # e.g., "5/21"
        today_str3 = date.today().strftime("%Y-%m-%d")   # e.g., "2026-05-21"
        today_str4 = date.today().strftime("%b %d")      # e.g., "May 21"
        today_dow = date.today().strftime("%A")           # e.g., "Thursday"

        for ws in wb.worksheets:
            ws_name = ws.title.lower().strip()
            if today_str in ws_name or today_str2 in ws_name or today_str3 in ws_name or today_str4.lower() in ws_name or today_dow.lower() in ws_name:
                target_sheet = ws
                logger.info(f"[DROPBOX] Found today's sheet: '{ws.title}'")
                break

        if not target_sheet:
            # Use the active sheet or first sheet
            target_sheet = wb.active or wb.worksheets[0]
            logger.info(f"[DROPBOX] Using sheet: '{target_sheet.title}'")

        # Read all rows into a list
        rows = []
        for row in target_sheet.iter_rows(values_only=True):
            rows.append([str(cell) if cell is not None else "" for cell in row])

        wb.close()

    except Exception as e:
        logger.error(f"[DROPBOX] Excel parse failed: {e}\n{traceback.format_exc()}")
        return jsonify({"error": f"Failed to parse Excel file: {str(e)}"}), 500

    # Parse the meal plan from rows
    meals_imported = []
    current_section = "other"
    section_map = {
        "pre-swim": "pre_swim", "pre swim": "pre_swim",
        "post-swim": "post_swim", "post swim": "post_swim",
        "breakfast": "breakfast", "lunch": "lunch",
        "afternoon snack": "snack", "snack": "snack",
        "dinner": "dinner", "night": "night",
        "late night": "late_night",
    }

    # Find the header row to understand column positions
    header_idx = {}
    food_col = None
    for ri, row in enumerate(rows):
        row_lower = [c.lower().strip() for c in row]
        for ci, cell in enumerate(row_lower):
            if "food item" in cell or cell == "food":
                food_col = ci
                # Map columns by header
                for cj, h in enumerate(row_lower):
                    if "protein" in h:
                        header_idx["protein"] = cj
                    elif "calorie" in h:
                        header_idx["calories"] = cj
                    elif "carb" in h:
                        header_idx["carbs"] = cj
                    elif "consumed" in h:
                        header_idx["consumed"] = cj
                    elif "qty" in h or "quantity" in h:
                        header_idx["qty"] = cj
                    elif "fat" in h:
                        header_idx["fat"] = cj
                break
        if food_col is not None:
            break

    logger.info(f"[DROPBOX] Header mapping: food_col={food_col}, cols={header_idx}")

    # Delete existing meals for today
    db.execute("DELETE FROM meals WHERE user_id = ? AND date = ?", (g.user_id, today))

    # Parse food rows
    start_row = (ri + 1) if food_col is not None else 0
    for row in rows[start_row:]:
        if not any(c.strip() for c in row):
            continue

        # Join row for section detection
        row_text = " ".join(row).lower()

        # Skip non-food rows
        if "subtotal" in row_text or "totals" in row_text.split()[0:1] or "daily total" in row_text:
            continue
        if "target" in row_text.split()[0:1] or "remaining" in row_text or "consumed so far" in row_text:
            continue
        if "supplement" in row_text or "sauna" in row_text or "tdee" in row_text or "physioiq" in row_text:
            continue
        if "notes / changes" in row_text or "claude day grade" in row_text:
            continue

        # Section detection (look for section markers like ▸ PRE-SWIM or section name in first cells)
        found_section = False
        first_cells = " ".join(row[:3]).lower()
        for key, val in section_map.items():
            if key in first_cells and ("▸" in " ".join(row) or "subtotal" not in row_text):
                current_section = val
                found_section = True
                break
        if found_section:
            continue

        # Try to extract food data using header positions
        food_name = None
        protein = 0
        calories = 0
        carbs = 0
        fat = 0
        consumed = None

        if food_col is not None and food_col < len(row):
            food_name = row[food_col].strip()
            if not food_name or food_name.lower() in ("", "none", "food item"):
                continue

            def safe_float(val):
                try:
                    return float(val.replace(",", ""))
                except (ValueError, TypeError, AttributeError):
                    return 0

            if "protein" in header_idx and header_idx["protein"] < len(row):
                protein = safe_float(row[header_idx["protein"]])
            if "calories" in header_idx and header_idx["calories"] < len(row):
                calories = safe_float(row[header_idx["calories"]])
            if "carbs" in header_idx and header_idx["carbs"] < len(row):
                carbs = safe_float(row[header_idx["carbs"]])
            if "fat" in header_idx and header_idx["fat"] < len(row):
                fat = safe_float(row[header_idx["fat"]])
            if "consumed" in header_idx and header_idx["consumed"] < len(row):
                c_val = row[header_idx["consumed"]].strip().lower()
                if c_val == "yes":
                    consumed = True
                elif c_val == "no":
                    consumed = False

        else:
            # Fallback: tab-separated parsing (same as import endpoint)
            nums = []
            for c in row:
                try:
                    nums.append(float(c.replace(",", "")))
                except (ValueError, TypeError):
                    pass
            # Find food name (first non-empty non-numeric cell)
            for c in row:
                c_stripped = c.strip()
                if c_stripped and not c_stripped.replace(".", "").replace("-", "").isdigit() and c_stripped.lower() not in ("yes", "no"):
                    food_name = c_stripped
                    break
            if food_name and len(nums) >= 3:
                if len(nums) >= 4:
                    protein, calories, carbs = nums[1], nums[2], nums[3]
                else:
                    protein, calories, carbs = nums[0], nums[1], nums[2]

        # Skip empty / zero lines
        if not food_name or (calories == 0 and protein == 0 and carbs == 0):
            continue

        notes = f"Consumed: {'Yes' if consumed else 'Planned'}" if consumed is not None else "Planned"
        db.execute("""
            INSERT INTO meals (user_id, date, meal_type, description, calories, protein_g, carbs_g, fat_g, fiber_g, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (g.user_id, today, current_section, food_name, calories, protein, carbs, fat, 0, notes))

        meals_imported.append({
            "section": current_section,
            "food": food_name,
            "protein": protein,
            "calories": calories,
            "carbs": carbs,
            "consumed": consumed
        })

    db.commit()
    logger.info(f"[DROPBOX] Imported {len(meals_imported)} meals for user {g.user_id}")

    return jsonify({
        "success": True,
        "sheet": target_sheet.title if target_sheet else "unknown",
        "meals_imported": len(meals_imported),
        "meals": meals_imported
    })


# --- Workout Logs ---

@app.route("/api/workouts", methods=["GET"])
@require_auth
def get_workouts():
    db = get_db()
    target_date = request.args.get("date", date.today().isoformat())
    workouts = db.execute(
        "SELECT * FROM workout_logs WHERE user_id = ? AND date = ? ORDER BY created_at",
        (g.user_id, target_date)
    ).fetchall()
    return jsonify([dict(w) for w in workouts])

@app.route("/api/workouts", methods=["POST"])
@require_auth
def add_workout():
    data = request.json or {}
    log_text = data.get("log_text", "").strip()
    if not log_text:
        return jsonify({"error": "log_text is required"}), 400

    workout_date = data.get("date", date.today().isoformat())
    workout_type = data.get("workout_type", "lift")  # swim, lift, both
    notes = data.get("notes", "")

    db = get_db()
    db.execute("""
        INSERT INTO workout_logs (user_id, date, workout_type, log_text, notes)
        VALUES (?, ?, ?, ?, ?)
    """, (g.user_id, workout_date, workout_type, log_text, notes))
    db.commit()

    wid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    workout = db.execute("SELECT * FROM workout_logs WHERE id = ?", (wid,)).fetchone()
    logger.info("Workout logged: type=%s, date=%s, length=%d chars", workout_type, workout_date, len(log_text))
    return jsonify(dict(workout)), 201

@app.route("/api/workouts/<int:workout_id>", methods=["DELETE"])
@require_auth
def delete_workout(workout_id):
    db = get_db()
    db.execute("DELETE FROM workout_logs WHERE id = ? AND user_id = ?", (workout_id, g.user_id))
    db.commit()
    return jsonify({"success": True})

@app.route("/api/workouts/push", methods=["POST", "OPTIONS"])
def push_workout():
    """External push endpoint for lift tracker — uses APP_SECRET as simple auth token."""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    data = request.json or {}
    token = data.get("token", "")
    if token != app.config["APP_SECRET"]:
        return jsonify({"error": "Invalid token"}), 403

    log_text = data.get("log_text", "").strip()
    if not log_text:
        return jsonify({"error": "log_text is required"}), 400

    workout_date = data.get("date", date.today().isoformat())
    workout_type = data.get("workout_type", "lift")
    notes = data.get("notes", "Pushed from PhysioIQ Lift Tracker")

    db = get_db()
    # Find the first user (single-user app)
    user = db.execute("SELECT id FROM users LIMIT 1").fetchone()
    if not user:
        return jsonify({"error": "No user found"}), 404

    db.execute("""
        INSERT INTO workout_logs (user_id, date, workout_type, log_text, notes)
        VALUES (?, ?, ?, ?, ?)
    """, (user["id"], workout_date, workout_type, log_text, notes))
    db.commit()

    logger.info("Workout pushed from lift tracker: type=%s, date=%s, length=%d", workout_type, workout_date, len(log_text))
    return jsonify({"success": True, "message": "Lift log saved to PhysioIQ"}), 201

# --- Garmin ---

@app.route("/api/garmin/pull", methods=["POST"])
@require_auth
def garmin_pull():
    data = request.json or {}
    target = data.get("date")
    force = data.get("force", False)
    result = pull_garmin_data(g.user_id, target, force=force)
    return jsonify(result)

@app.route("/api/garmin/cooldown", methods=["GET", "DELETE"])
def garmin_cooldown():
    """Check or clear the Garmin rate-limit cooldown."""
    token_dir = os.path.join(os.path.dirname(app.config["DATABASE"]), ".garmin_tokens")
    cooldown_file = os.path.join(token_dir, "rate_limit_cooldown.txt")

    if request.method == "DELETE":
        if os.path.exists(cooldown_file):
            os.remove(cooldown_file)
            return jsonify({"status": "cooldown_cleared"})
        return jsonify({"status": "no_cooldown_active"})

    if os.path.exists(cooldown_file):
        try:
            with open(cooldown_file, "r") as f:
                last_fail = float(f.read().strip())
            elapsed = time.time() - last_fail
            remaining = max(0, 900 - elapsed)
            return jsonify({
                "cooldown_active": remaining > 0,
                "remaining_seconds": int(remaining),
                "last_failure": datetime.fromtimestamp(last_fail).isoformat()
            })
        except Exception:
            pass
    return jsonify({"cooldown_active": False})

@app.route("/api/garmin/test", methods=["GET"])
def garmin_test():
    """Debug endpoint — test Garmin connection without auth. Returns status info."""
    email = app.config["GARMIN_EMAIL"]
    password = app.config["GARMIN_PASSWORD"]
    info = {
        "email_configured": bool(email),
        "email_value": email[:3] + "***" if email else None,
        "password_configured": bool(password),
        "password_length": len(password) if password else 0,
        "timestamp": datetime.now().isoformat()
    }
    if not email or not password:
        info["status"] = "MISSING_CREDENTIALS"
        return jsonify(info)

    try:
        from garminconnect import Garmin
        info["garminconnect_installed"] = True
        import garminconnect as gc_module
        info["garminconnect_version"] = getattr(gc_module, "__version__", "unknown")
    except ImportError:
        info["garminconnect_installed"] = False
        info["status"] = "LIBRARY_NOT_INSTALLED"
        return jsonify(info)

    try:
        garmin = Garmin(email, password)
        # Patch garth's session with 429-aware retry
        try:
            from urllib3.util.retry import Retry
            from requests.adapters import HTTPAdapter
            retry_strategy = Retry(
                total=3,
                backoff_factor=10,
                status_forcelist=[429, 500, 502, 503, 504],
                respect_retry_after_header=True,
                allowed_methods=["GET", "POST"],
            )
            adapter = HTTPAdapter(max_retries=retry_strategy)
            if hasattr(garmin, 'garth') and hasattr(garmin.garth, 'sess'):
                garmin.garth.sess.mount("https://", adapter)
                garmin.garth.sess.mount("http://", adapter)
        except Exception:
            pass
        logger.info("[TEST] Attempting Garmin login for %s", email)
        garmin.login()
        info["login"] = "SUCCESS"
        info["display_name"] = garmin.get_full_name()

        # Try pulling today's stats
        today = date.today().isoformat()
        stats = garmin.get_stats(today)
        info["stats_keys"] = list(stats.keys()) if stats else []
        info["resting_hr"] = stats.get("restingHeartRate") if stats else None
        info["steps"] = stats.get("totalSteps") if stats else None
        info["body_battery"] = stats.get("bodyBatteryChargedValue") if stats else None
        info["status"] = "OK"
    except Exception as e:
        info["login"] = "FAILED"
        info["error"] = str(e)
        info["error_type"] = type(e).__name__
        info["traceback"] = traceback.format_exc()
        info["status"] = "LOGIN_FAILED"
        logger.error("[TEST] Garmin login failed: %s\n%s", str(e), traceback.format_exc())

    return jsonify(info)

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

def _background_generate_report(app_obj, user_id, report_type, report_id):
    """Run report generation in a background thread with its own app context."""
    logger.info("Background thread started for report %s (type=%s, user=%s)", report_id, report_type, user_id)
    try:
        with app_obj.app_context():
            result = generate_report(user_id, report_type, report_id=report_id)
            if result.get("error"):
                logger.error("Background report %s failed: %s", report_id, result["error"])
            else:
                logger.info("Background report %s completed successfully", report_id)
    except Exception as e:
        logger.error("Background thread CRASHED for report %s: %s", report_id, str(e))
        logger.error("Traceback: %s", traceback.format_exc())
        # Emergency: update status directly with standalone DB
        try:
            db = get_standalone_db()
            db.execute(
                "UPDATE reports SET html_content = ?, status = 'error' WHERE id = ?",
                (f"Background thread error: {str(e)}", report_id)
            )
            db.commit()
            db.close()
        except Exception as db_err:
            logger.error("Failed to update report status after crash: %s", str(db_err))

@app.route("/api/reports/generate", methods=["POST"])
@require_auth
def generate_report_endpoint():
    data = request.json or {}
    report_type = data.get("type", "morning")
    user_id = g.user_id
    today = date.today().isoformat()

    # Create a placeholder report row with status='generating'
    db = get_db()
    db.execute("""
        INSERT INTO reports (user_id, date, report_type, html_content, metrics_json, status)
        VALUES (?, ?, ?, '', '', 'generating')
    """, (user_id, today, report_type))
    db.commit()
    report_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]

    # Start background thread to generate the report
    thread = threading.Thread(
        target=_background_generate_report,
        args=(app, user_id, report_type, report_id),
        daemon=True
    )
    thread.start()

    # Return immediately — frontend will poll for completion
    return jsonify({"success": True, "report_id": report_id, "status": "generating"})

@app.route("/api/reports/<int:report_id>/status", methods=["GET"])
@require_auth
def report_status(report_id):
    db = get_db()
    report = db.execute(
        "SELECT id, status, html_content FROM reports WHERE id = ? AND user_id = ?",
        (report_id, g.user_id)
    ).fetchone()
    if not report:
        return jsonify({"error": "Report not found"}), 404
    result = {"report_id": report["id"], "status": report["status"]}
    if report["status"] == "complete":
        result["html"] = report["html_content"]
    elif report["status"] == "error":
        result["error"] = report["html_content"]
    return jsonify(result)

@app.route("/api/reports", methods=["GET"])
@require_auth
def get_reports():
    target_date = request.args.get("date", date.today().isoformat())
    db = get_db()
    reports = db.execute(
        "SELECT id, date, report_type, status, created_at FROM reports WHERE user_id = ? AND date = ? ORDER BY created_at DESC",
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
