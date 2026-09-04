import hmac
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

os.environ["RAZORPAY_KEY_ID"] = "rzp_test_fake"
os.environ["RAZORPAY_KEY_SECRET"] = "fake_secret"
os.environ["RAZORPAY_WEBHOOK_SECRET"] = "whsec_e2e_test"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

import jwt as pyjwt
from fastapi.testclient import TestClient

import app.mandate as m2
from app.mandate import ProtocolSource
from app.agent import ScriptedReasoner, AgentProposal, ProposedAction
from app.main import create_app


def sign(body: str, secret: str) -> str:
    return hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


def acp_signed_cart_payload(jti, sku="SKU-9", cart_total="500.00"):
    token = pyjwt.encode(
        {"iat": datetime.now(timezone.utc), "exp": datetime.now(timezone.utc) + timedelta(minutes=10), "jti": jti},
        m2._SIGNING_SECRETS[ProtocolSource.ACP], algorithm="HS256",
    )
    return {
        "agent_id": "chatgpt-acp-v1", "buyer_reference": "user-e2e", "line_items": [sku],
        "cart_total": cart_total, "currency": "INR",
        "session_expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
        "signed_cart_token": token,
    }


def test_full_pipeline_buyer_request_to_order_to_webhook_to_fulfillment():
    """The capstone test: a buyer agent's ACP request goes through mandate
    normalization, the LangGraph agent, the deterministic gate, a real (idempotent,
    mocked-network) Razorpay order, then a genuinely HMAC-signed webhook drives the
    FSM to fulfillment -- and every step across all of it lands in one continuous,
    tamper-evident ledger.

    The ACP adapter only grants PURCHASE + ACCEPT_SUBSTITUTE by default (see
    mandate.py), so the scripted proposal here confirms the cart via a
    same-item "substitution" at its own unchanged price -- a realistic shape for
    "agent re-confirms the cart is still valid" rather than a discount, which
    would need a mandate scope this ACP request doesn't grant.
    """
    reasoner = ScriptedReasoner([
        AgentProposal(action=ProposedAction.SUBSTITUTE_ITEM, sku="SKU-9", substitute_sku="SKU-9",
                      substitute_price=Decimal("500.00"),
                      rationale="Confirming the cart at its current, unchanged price."),
    ])
    app = create_app(reasoner=reasoner)
    client = TestClient(app)
    state = app.state.rga

    with patch.object(state.razorpay._client.order, "create") as mock_order:
        mock_order.return_value = {"id": "order_E2E1", "status": "created"}
        resp = client.post("/agent/resolve", json={
            "protocol": "acp", "sku": "SKU-9", "agent_seen_price": "500.00", "live_price": "500.00",
            "order_amount": "500.00", "merchant_id": "m-1", "payload": acp_signed_cart_payload("e2e-jti-1"),
        })

    assert resp.status_code == 200
    body = resp.json()
    assert body["outcome"] == "success"
    order_id = body["razorpay_order_id"]
    assert order_id == "order_E2E1"

    record = state.store.get_by_order_id(order_id)
    assert record is not None
    assert record.state.value == "pending_payment"

    webhook_payload = {
        "event": "payment.captured",
        "payload": {"payment": {"entity": {"id": "pay_E2E1", "order_id": order_id,
                                            "amount": 50000, "status": "captured"}}},
    }
    wh_body = json.dumps(webhook_payload)
    wh_sig = sign(wh_body, os.environ["RAZORPAY_WEBHOOK_SECRET"])

    wh_resp = client.post("/webhooks/razorpay", content=wh_body, headers={"X-Razorpay-Signature": wh_sig})
    assert wh_resp.status_code == 200
    assert record.state.value == "fulfilled"

    ok, bad_row = state.ledger.verify_chain()
    assert ok, f"ledger chain broken at row {bad_row}"

    rows = state.ledger.history_for_mandate(record.transaction_id)
    assert len(rows) >= 4, "admission, substitution, order creation context, and fulfillment " \
                            "should all have left a distinct trail"
