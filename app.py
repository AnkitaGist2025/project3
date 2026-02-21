import os
from datetime import datetime, timezone

import psycopg2
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request
import plivo

load_dotenv()

PLIVO_AUTH_ID = os.getenv("PLIVO_AUTH_ID")
PLIVO_AUTH_TOKEN = os.getenv("PLIVO_AUTH_TOKEN")
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
DATABASE_URL = os.getenv("DATABASE_URL")

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

    if digit == "1":
        # Sub-menu for Sales
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
        log_call(caller, call_uuid, "Main > Support")
        response.add(
            plivo.xml.SpeakElement(
                "You pressed 2. Connecting you to Support. "
                "A representative will be with you shortly."
            )
        )

    elif digit == "3":
        log_call(caller, call_uuid, "Main > Hours")
        response.add(
            plivo.xml.SpeakElement(
                "You pressed 3. Our office hours are Monday through Friday, "
                "9 AM to 6 PM Eastern Time. "
                "Thank you for calling Acme Corp. Goodbye."
            )
        )

    else:
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

    if digit == "1":
        log_call(caller, call_uuid, "Main > Sales > New Customers")
        response.add(
            plivo.xml.SpeakElement(
                "Connecting you to our new-customer sales team. Please hold."
            )
        )

    elif digit == "2":
        log_call(caller, call_uuid, "Main > Sales > Existing Customers")
        response.add(
            plivo.xml.SpeakElement(
                "Connecting you to your account manager. Please hold."
            )
        )

    else:
        response.add(
            plivo.xml.SpeakElement("Invalid input. Please try again.")
        )
        # Send them back to the main menu
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
# Startup
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=True)
