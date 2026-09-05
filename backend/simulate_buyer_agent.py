"""
simulate_buyer_agent.py — Plays the role of an AI buyer agent hitting your REAL
deployed backend, end to end. Run this from inside backend/, with your venv
active, since it imports app.mandate to sign a token with the same secret your
live server verifies against.

Usage:
    python3 simulate_buyer_agent.py https://<your-render-app>.onrender.com
"""

import sys
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import jwt as pyjwt
import requests

import app.mandate as m2
from app.mandate import ProtocolSource


def build_acp_payload(sku="SKU-9", cart_total="500.00"):
    token = pyjwt.encode(
        {
            "iat": datetime.now(timezone.utc),
            "exp": datetime.now(timezone.utc) + timedelta(minutes=10),
            "jti": f"manual-test-{datetime.now(timezone.utc).timestamp()}",  # unique every run
        },
        m2._SIGNING_SECRETS[ProtocolSource.ACP],
        algorithm="HS256",
    )
    return {
        "agent_id": "manual-test-agent",
        "buyer_reference": "manual-tester",
        "line_items": [sku],
        "cart_total": cart_total,
        "currency": "INR",
        "session_expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
        "signed_cart_token": token,
    }


def main():
    if len(sys.argv) != 2:
        print("Usage: python3 simulate_buyer_agent.py https://<your-render-app>.onrender.com")
        sys.exit(1)

    base_url = sys.argv[1].rstrip("/")
    body = {
        "protocol": "acp",
        "sku": "SKU-9",
        "agent_seen_price": "500.00",
        "live_price": "500.00",
        "order_amount": "500.00",
        "merchant_id": "m-1",
        "payload": build_acp_payload(),
    }

    print(f"POST {base_url}/agent/resolve")
    print(json.dumps(body, indent=2))
    print()

    resp = requests.post(f"{base_url}/agent/resolve", json=body, timeout=30)
    print(f"Status: {resp.status_code}")
    try:
        print(json.dumps(resp.json(), indent=2))
    except json.JSONDecodeError:
        # A non-JSON body (a bare 500, an HTML error page, a proxy timeout page)
        # means something crashed server-side before it could even form a
        # response -- print the raw text so the real error is visible, instead
        # of masking it behind a confusing secondary parse failure.
        print("Response was not JSON. Raw body:")
        print(resp.text[:2000])
        print()
        print("Check your Render service's Logs tab for the actual traceback "
              "around this timestamp.")


if __name__ == "__main__":
    main()