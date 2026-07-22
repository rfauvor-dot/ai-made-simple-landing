"""Standalone health check for the AI Made Simple 40+ funnel. Intended to be
run by a scheduled Claude session (not a Task Scheduler cron job itself --
Claude decides whether to notify Rick based on this script's output).

Checks:
1. Stripe for new real customer payments beyond the two known manual test
   sessions (both already refunded) -- and if found, whether the portal's
   webhook actually enrolled them and sent the welcome email successfully.
2. The local FacebookAutoPoster Windows Task Scheduler task's last run
   result and the local log for new errors since the last check.

State (last-seen session IDs, last-checked FB run time) persists in
funnel_health_state.json so repeat runs don't re-flag the same thing twice.
Prints a clear summary; exits 0 if all-clear, 1 if something needs attention.
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "funnel_health_state.json")
FB_LOG_FILE = os.path.join(BASE_DIR, "facebook_poster_log.txt")

LANDING_SERVICE_ID = "srv-d9f8sv6rnols73aa415g"
PORTAL_SERVICE_ID = "srv-d9en4vjtqb8s73aqq3i0"
OWNER_ID = "tea-d8i49vjtqb8s73aolc8g"

# Both already refunded -- manual verification tests, not real customers.
KNOWN_TEST_SESSION_IDS = {
    "cs_live_a1kIRG8AMUxTxXKpriVBGUv2BWfvJP2wOJ7YBzsxBk6hidd13RwqamnJqN",
    "cs_live_a1irN0ytwSwovesutujxRyVmQ3hMIcQNOBxn4djV3yJT0Mcub0wYPDnXyI",
}

RENDER_API_KEY = os.environ.get("RENDER_API_KEY")


def load_state():
    if not os.path.exists(STATE_FILE):
        return {"reported_session_ids": [], "fb_last_run_time": None, "fb_log_lines_seen": 0}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def render_headers():
    return {"Authorization": f"Bearer {RENDER_API_KEY}", "Accept": "application/json"}


def get_render_env_vars(service_id):
    resp = requests.get(
        f"https://api.render.com/v1/services/{service_id}/env-vars",
        headers=render_headers(),
        timeout=30,
    )
    resp.raise_for_status()
    env = {}
    for item in resp.json():
        ev = item.get("envVar", item)
        env[ev["key"]] = ev.get("value", "")
    return env


def get_render_logs(service_id, limit=100):
    resp = requests.get(
        "https://api.render.com/v1/logs",
        headers=render_headers(),
        params={"resource": service_id, "ownerId": OWNER_ID, "limit": limit, "direction": "backward"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("logs", [])


def check_stripe(state, issues):
    landing_env = get_render_env_vars(LANDING_SERVICE_ID)
    stripe_key = landing_env.get("STRIPE_SECRET_KEY")
    if not stripe_key or not stripe_key.startswith("sk_"):
        issues.append(f"STRIPE_SECRET_KEY on landing service looks wrong (starts with {stripe_key[:6]!r}) -- check Render env vars.")
        return

    r = requests.get(
        "https://api.stripe.com/v1/checkout/sessions",
        headers={"Authorization": f"Bearer {stripe_key}"},
        params={"limit": 20},
        timeout=30,
    )
    if r.status_code != 200:
        issues.append(f"Stripe API call failed: {r.status_code} {r.text[:200]}")
        return

    sessions = r.json().get("data", [])
    already_reported = set(state.get("reported_session_ids", []))
    new_paid = [
        s for s in sessions
        if s["id"] not in KNOWN_TEST_SESSION_IDS
        and s["id"] not in already_reported
        and s["payment_status"] == "paid"
    ]

    if not new_paid:
        return

    portal_logs = get_render_logs(PORTAL_SERVICE_ID, limit=200)
    log_text = "\n".join(entry.get("message", "") for entry in portal_logs)

    for s in new_paid:
        email = (s.get("customer_details") or {}).get("email", "unknown")
        summary = f"NEW REAL CUSTOMER: {email}, ${(s.get('amount_total') or 0) / 100:.2f}, session {s['id']}"
        if f"Enrolled new user {email}" in log_text or f"already-enrolled user {email}" in log_text:
            if f"Welcome email to {email} failed to send" in log_text:
                summary += " -- ACCOUNT CREATED BUT WELCOME EMAIL FAILED TO SEND. Customer has no password."
            else:
                summary += " -- webhook fired, account created, email appears to have sent OK."
        else:
            summary += " -- WARNING: no matching webhook log entry found yet (may just be propagation delay, or the webhook didn't fire)."
        issues.append(summary)
        state.setdefault("reported_session_ids", []).append(s["id"])


def check_facebook_poster(state, issues):
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-ScheduledTaskInfo -TaskName 'FacebookAutoPoster' | ConvertTo-Json"],
            capture_output=True, text=True, timeout=30,
        )
    except Exception as exc:
        issues.append(f"Could not check FacebookAutoPoster scheduled task: {exc}")
        return

    if result.returncode != 0:
        issues.append(f"Could not query FacebookAutoPoster task (may not exist on this machine): {result.stderr.strip()[:200]}")
        return

    try:
        info = json.loads(result.stdout)
    except json.JSONDecodeError:
        issues.append("Could not parse FacebookAutoPoster task info.")
        return

    last_run_time = info.get("LastRunTime")
    last_task_result = info.get("LastTaskResult")
    # 267011 = SCHED_S_TASK_HAS_NOT_RUN -- not a failure, just means the task
    # hasn't fired yet (expected until the first Mon/Wed/Fri 9am occurs).
    BENIGN_RESULT_CODES = (0, "0", None, 267011, "267011")

    if last_run_time and last_run_time != state.get("fb_last_run_time"):
        state["fb_last_run_time"] = last_run_time
        if last_task_result not in BENIGN_RESULT_CODES:
            issues.append(f"FacebookAutoPoster's last run ({last_run_time}) failed with result code {last_task_result}.")

    if os.path.exists(FB_LOG_FILE):
        with open(FB_LOG_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
        seen = state.get("fb_log_lines_seen", 0)
        new_lines = lines[seen:]
        state["fb_log_lines_seen"] = len(lines)
        error_lines = [l.strip() for l in new_lines if "ERROR" in l or "WARNING" in l]
        if error_lines:
            issues.append("New FacebookAutoPoster log warnings/errors:\n  " + "\n  ".join(error_lines))


def main():
    state = load_state()
    issues = []

    try:
        check_stripe(state, issues)
    except Exception as exc:
        issues.append(f"Stripe/portal check itself failed: {exc}")

    try:
        check_facebook_poster(state, issues)
    except Exception as exc:
        issues.append(f"Facebook poster check itself failed: {exc}")

    save_state(state)

    if issues:
        print(f"ISSUES FOUND ({datetime.now(timezone.utc).isoformat()}):")
        for issue in issues:
            print(f"- {issue}")
        sys.exit(1)
    else:
        print(f"ALL CLEAR ({datetime.now(timezone.utc).isoformat()}) -- no new customers, no Facebook poster errors.")
        sys.exit(0)


if __name__ == "__main__":
    main()
