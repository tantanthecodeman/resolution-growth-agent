"""
simulate_webhook.py — Signs and sends a real payment.captured webhook, exactly the
way Razorpay's servers would, against your deployed backend.

This only does something meaningful if the order_id you pass in already exists in
the backend's TransactionStore, which right now is IN-MEMORY, not Neon-backed (see
the docstring on TransactionStore in webhook_route.py). That means:
  1. Run simulate_buyer_agent.py first and get a "success" outcome with a real
     razorpay_order_id.
  2. Run THIS script immediately after, against the same live deployment, passing
     that exact order_id, before anything causes the Render process to restart
     (a redeploy, a crash, or -- less likely, but possible on free tier -- a
     platform-level restart). If the process restarts between the two calls, the
     in-memory record is gone and this webhook will be correctly, harmlessly
     dropped (see process_captured_payment in webhook_route.py) rather than error.

Also worth knowing: the currently deployed _fulfill_order (in AppState, main.py)
always succeeds unconditionally. So this script will always show a clean
payment_captured -> fulfilling -> fulfilled path in the ledger, never the retry,
refund, or escalate branches of the FSM -- those are proven separately and
deliberately in tests/test_fsm.py, which controls failure on purpose.

Usage:
    python3 simulate_webhook.py https://<your-render-app>.onrender.com <razorpay_order_id>
"""

import hashlib
import hmac
import json
import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()  # reads backend/.env directly -- this script doesn't import app.main,
                # so it needs its own read of RAZORPAY_WEBHOOK_SECRET


def sign(body: str, secret: str) -> str:
    return hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


def main():
    if len(sys.argv) != 3:
        print("Usage: python3 simulate_webhook.py https://<your-render-app>.onrender.com <razorpay_order_id>")
        sys.exit(1)

    base_url = sys.argv[1].rstrip("/")
    order_id = sys.argv[2]

    secret = os.environ.get("RAZORPAY_WEBHOOK_SECRET")
    if not secret:
        print("RAZORPAY_WEBHOOK_SECRET not found in your local .env -- this must be the")
        print("SAME value you set on Render, or the signature check will correctly reject it.")
        sys.exit(1)

    payload = {
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {
                    "id": f"pay_manual_test_{order_id[-8:]}",
                    "order_id": order_id,
                    "amount": 50000,
                    "status": "captured",
                }
            }
        },
    }
    body = json.dumps(payload)
    signature = sign(body, secret)

    print(f"POST {base_url}/webhooks/razorpay")
    print(body)
    print()

    resp = requests.post(
        f"{base_url}/webhooks/razorpay",
        data=body,
        headers={"X-Razorpay-Signature": signature, "Content-Type": "application/json"},
        timeout=30,
    )
    print(f"Status: {resp.status_code}")
    print(resp.json())


if __name__ == "__main__":
    main()