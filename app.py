import json
import os
from datetime import datetime, timezone

import psycopg2
import redis
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request
import plivo

load_dotenv()

PLIVO_AUTH_ID = os.getenv("PLIVO_AUTH_ID")
PLIVO_AUTH_TOKEN = os.getenv("PLIVO_AUTH_TOKEN")
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
DATABASE_URL = os.getenv("NEON_POSTGRES_URL") or os.getenv("POSTGRES_URL") or os.getenv("DATABASE_URL")

REDIS_URL = os.getenv("REDIS_URL")
redis_client = redis.from_url(REDIS_URL, decode_responses=True)

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS call_logs (
            id          SERIAL PRIMARY KEY,
            caller_number TEXT NOT NULL,
            call_uuid   TEXT,
            menu_path   TEXT NOT NULL,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """)
    conn.commit()
    cur.close()
    conn.close()


def log_call(caller_number, call_uuid, menu_path):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO call_logs (caller_number, call_uuid, menu_path, created_at) "
        "VALUES (%s, %s, %s, %s)",
        (caller_number, call_uuid, menu_path, datetime.now(timezone.utc)),
    )
    conn.commit()
    cur.close()
    conn.close()


def _caller_info(req):
    """Extract caller number and call UUID from the Plivo request."""
    caller = req.form.get("From") or req.args.get("From", "unknown")
    call_uuid = req.form.get("CallUUID") or req.args.get("CallUUID", "")
    return caller, call_uuid


# ---------------------------------------------------------------------------
# IVR routes
# ---------------------------------------------------------------------------

@app.route("/answer/", methods=["GET", "POST"])
def answer_call():
    """Initial IVR greeting — plays the main menu and waits for a digit."""
    caller, call_uuid = _caller_info(request)

    # Create a Redis session for this caller
    session_data = {
        "step": "main_menu",
        "call_uuid": call_uuid,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    redis_client.setex(f"session:{caller}", SESSION_TTL, json.dumps(session_data))
    app.logger.info(f"Session created for caller={caller}")

    response = plivo.xml.ResponseElement()

    get_input = plivo.xml.GetDigitsElement(
        action=f"{BASE_URL}/handle-input/",
        method="POST",
        timeout=10,
        num_digits=1,
        retries=2,
    )
    get_input.add(
        plivo.xml.SpeakElement(
            "Welcome to Acme Corp. "
            "Press 1 for Sales. "
            "Press 2 for Support. "
            "Press 3 for Hours."
        )
    )
    response.add(get_input)

    response.add(
        plivo.xml.SpeakElement("We didn't receive any input. Goodbye.")
    )

    return Response(response.to_string(), content_type="text/xml")


@app.route("/handle-input/", methods=["GET", "POST"])
def handle_input():
    """Handles the digit pressed at the main menu."""
    digit = request.form.get("Digits") or request.args.get("Digits", "")
    caller, call_uuid = _caller_info(request)
    app.logger.info(f"Main menu — caller={caller} digit='{digit}'")

    response = plivo.xml.ResponseElement()

    # Update Redis session with the menu selection
    key = f"session:{caller}"
    raw = redis_client.get(key)
    session_data = json.loads(raw) if raw else {}

    if digit == "1":
        session_data["step"] = "sales_menu"
        redis_client.setex(key, SESSION_TTL, json.dumps(session_data))

        get_input = plivo.xml.GetDigitsElement(
            action=f"{BASE_URL}/handle-sales-input/",
            method="POST",
            timeout=10,
            num_digits=1,
            retries=2,
        )
        get_input.add(
            plivo.xml.SpeakElement(
                "Sales department. "
                "Press 1 for new customers. "
                "Press 2 for existing customers."
            )
        )
        response.add(get_input)
        response.add(
            plivo.xml.SpeakElement("We didn't receive any input. Goodbye.")
        )

    elif digit == "2":
        session_data["step"] = "routed_support"
        redis_client.setex(key, SESSION_TTL, json.dumps(session_data))
        log_call(caller, call_uuid, "Main > Support")
        response.add(
            plivo.xml.SpeakElement(
                "You pressed 2. Connecting you to Support. "
                "A representative will be with you shortly."
            )
        )

    elif digit == "3":
        session_data["step"] = "routed_hours"
        redis_client.setex(key, SESSION_TTL, json.dumps(session_data))
        log_call(caller, call_uuid, "Main > Hours")
        response.add(
            plivo.xml.SpeakElement(
                "You pressed 3. Our office hours are Monday through Friday, "
                "9 AM to 6 PM Eastern Time. "
                "Thank you for calling Acme Corp. Goodbye."
            )
        )

    else:
        session_data["step"] = "invalid_input"
        redis_client.setex(key, SESSION_TTL, json.dumps(session_data))
        response.add(
            plivo.xml.SpeakElement("Invalid input. Please try again.")
        )
        response.add(
            plivo.xml.RedirectElement(f"{BASE_URL}/answer/")
        )

    return Response(response.to_string(), content_type="text/xml")


@app.route("/handle-sales-input/", methods=["GET", "POST"])
def handle_sales_input():
    """Handles the digit pressed in the Sales sub-menu."""
    digit = request.form.get("Digits") or request.args.get("Digits", "")
    caller, call_uuid = _caller_info(request)
    app.logger.info(f"Sales sub-menu — caller={caller} digit='{digit}'")

    response = plivo.xml.ResponseElement()

    key = f"session:{caller}"
    raw = redis_client.get(key)
    session_data = json.loads(raw) if raw else {}

    if digit == "1":
        session_data["step"] = "routed_sales_new"
        redis_client.setex(key, SESSION_TTL, json.dumps(session_data))
        log_call(caller, call_uuid, "Main > Sales > New Customers")
        response.add(
            plivo.xml.SpeakElement(
                "Connecting you to our new-customer sales team. Please hold."
            )
        )

    elif digit == "2":
        session_data["step"] = "routed_sales_existing"
        redis_client.setex(key, SESSION_TTL, json.dumps(session_data))
        log_call(caller, call_uuid, "Main > Sales > Existing Customers")
        response.add(
            plivo.xml.SpeakElement(
                "Connecting you to your account manager. Please hold."
            )
        )

    else:
        session_data["step"] = "invalid_input"
        redis_client.setex(key, SESSION_TTL, json.dumps(session_data))
        response.add(
            plivo.xml.SpeakElement("Invalid input. Please try again.")
        )
        response.add(
            plivo.xml.RedirectElement(f"{BASE_URL}/answer/")
        )

    return Response(response.to_string(), content_type="text/xml")


# ---------------------------------------------------------------------------
# Call-log API
# ---------------------------------------------------------------------------

@app.route("/logs", methods=["GET"])
def get_logs():
    """Return all call logs as JSON."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, caller_number, call_uuid, menu_path, created_at "
        "FROM call_logs ORDER BY created_at DESC"
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()

    logs = [
        {
            "id": row[0],
            "caller_number": row[1],
            "call_uuid": row[2],
            "menu_path": row[3],
            "timestamp": row[4].isoformat(),
        }
        for row in rows
    ]
    return jsonify(logs)


# ---------------------------------------------------------------------------
# Postgres API routes
# ---------------------------------------------------------------------------

@app.route("/api/setup-db", methods=["GET"])
def setup_db():
    """Create the call_logs table (run once)."""
    init_db()
    return jsonify({"message": "Table created successfully"})


@app.route("/api/log-call", methods=["POST"])
def api_log_call():
    """Insert a call record into the database."""
    data = request.get_json() or {}
    caller_number = data.get("caller_number")
    call_uuid = data.get("call_uuid", "")
    menu_path = data.get("menu_path", "")

    if not caller_number:
        return jsonify({"error": "caller_number is required"}), 400

    log_call(caller_number, call_uuid, menu_path)
    return jsonify({"message": "Call logged", "caller_number": caller_number, "menu_path": menu_path})


@app.route("/api/call-logs", methods=["GET"])
def api_call_logs():
    """Return all call logs as JSON."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, caller_number, call_uuid, menu_path, created_at "
        "FROM call_logs ORDER BY created_at DESC"
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()

    logs = [
        {
            "id": row[0],
            "caller_number": row[1],
            "call_uuid": row[2],
            "menu_path": row[3],
            "timestamp": row[4].isoformat(),
        }
        for row in rows
    ]
    return jsonify(logs)


@app.route("/api/call-history/<phone>", methods=["GET"])
def api_call_history(phone):
    """Return call logs for a specific phone number."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, caller_number, call_uuid, menu_path, created_at "
        "FROM call_logs WHERE caller_number = %s ORDER BY created_at DESC",
        (phone,),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()

    logs = [
        {
            "id": row[0],
            "caller_number": row[1],
            "call_uuid": row[2],
            "menu_path": row[3],
            "timestamp": row[4].isoformat(),
        }
        for row in rows
    ]
    return jsonify(logs)


# ---------------------------------------------------------------------------
# Redis session routes
# ---------------------------------------------------------------------------

SESSION_TTL = 1800  # 30 minutes in seconds


@app.route("/api/start-session", methods=["POST"])
def start_session():
    """Create a new caller session in Redis with a 30-minute TTL."""
    caller_id = request.args.get("caller_id")
    if not caller_id:
        return jsonify({"error": "caller_id is required"}), 400

    session_data = {
        "step": "greeting",
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    redis_client.setex(f"session:{caller_id}", SESSION_TTL, json.dumps(session_data))
    return jsonify({"message": "Session created", "caller_id": caller_id, "session": session_data})


@app.route("/api/get-session", methods=["GET"])
def get_session():
    """Retrieve a caller session from Redis."""
    caller_id = request.args.get("caller_id")
    if not caller_id:
        return jsonify({"error": "caller_id is required"}), 400

    data = redis_client.get(f"session:{caller_id}")
    if data is None:
        return jsonify({"error": "Session not found or expired"}), 404

    session_data = json.loads(data)
    ttl = redis_client.ttl(f"session:{caller_id}")
    return jsonify({"caller_id": caller_id, "session": session_data, "ttl_seconds": ttl})


@app.route("/api/update-session", methods=["POST"])
def update_session():
    """Update the step value of an existing caller session."""
    caller_id = request.args.get("caller_id")
    step = request.args.get("step")
    if not caller_id or not step:
        return jsonify({"error": "caller_id and step are required"}), 400

    key = f"session:{caller_id}"
    data = redis_client.get(key)
    if data is None:
        return jsonify({"error": "Session not found or expired"}), 404

    session_data = json.loads(data)
    session_data["step"] = step
    ttl = redis_client.ttl(key)
    redis_client.setex(key, ttl if ttl > 0 else SESSION_TTL, json.dumps(session_data))
    return jsonify({"message": "Session updated", "caller_id": caller_id, "session": session_data})


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=True)
