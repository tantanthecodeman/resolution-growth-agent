import hmac
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import jwt as pyjwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("RAZORPAY_KEY_ID", "rzp_test_fake")
os.environ.setdefault("RAZORPAY_KEY_SECRET", "fake_secret")

import app.mandate as m2
from app.mandate import ACPAdapter, ACPCartRequest, AllowedAction, ProtocolSource
from app.policy_gate import PolicyGate, PolicyConfig, ReplayGuard
from app.ledger import AuditLedger
from app.razorpay_client import RazorpayClient
from app.fsm import FailureRecoveryFSM, TransactionRecord
from app.webhook_route import build_webhook_router, TransactionStore

WEBHOOK_SECRET = "whsec_test_abc123"


def fresh_mandate(jti):
    token = pyjwt.encode(
        {"iat": datetime.now(timezone.utc), "exp": datetime.now(timezone.utc) + timedelta(minutes=10), "jti": jti},
        m2._SIGNING_SECRETS[ProtocolSource.ACP], algorithm="HS256",
    )
    req = ACPCartRequest(agent_id="chatgpt-acp-v1", buyer_reference="user-1", line_items=["SKU-1"],
        cart_total=Decimal("1000"), session_expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        signed_cart_token=token)
    mand = ACPAdapter.to_mandate(req, merchant_id="m-1")
    mand.allowed_actions = [AllowedAction.PURCHASE, AllowedAction.INITIATE_REFUND]
    return mand


def sign(body: str, secret: str) -> str:
    return hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


@pytest.fixture
def webhook_test_app():
    """Fresh app + client + a saved TransactionRecord ready to receive a webhook
    for it, rebuilt for every test so tests don't leak state into each other."""
    calls = []
    gate = PolicyGate(PolicyConfig(merchant_id="m-1"))
    ledger = AuditLedger("sqlite:///:memory:")
    razorpay = RazorpayClient()

    def fulfill_order(record):
        calls.append(record.transaction_id)  # succeeds every time in this test

    fsm = FailureRecoveryFSM(gate, ledger, razorpay, fulfill_order)
    store = TransactionStore()
    dedup = ReplayGuard()
    router = build_webhook_router(razorpay, fsm, store, dedup, WEBHOOK_SECRET)

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    mand = fresh_mandate("wh-1")
    record = TransactionRecord(transaction_id="txn-wh-1", mandate=mand, order_id="order_LIVE9", amount=Decimal("500"))
    store.save(record)

    payload = {
        "event": "payment.captured",
        "payload": {"payment": {"entity": {"id": "pay_WH1", "order_id": "order_LIVE9",
                                            "amount": 50000, "status": "captured"}}},
    }
    body = json.dumps(payload)

    return {"client": client, "record": record, "ledger": ledger, "calls": calls,
            "body": body, "good_sig": sign(body, WEBHOOK_SECRET)}


def test_valid_webhook_drives_fsm_via_background_task(webhook_test_app):
    ctx = webhook_test_app
    resp = ctx["client"].post("/webhooks/razorpay", content=ctx["body"],
                               headers={"X-Razorpay-Signature": ctx["good_sig"]})
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"
    # TestClient runs BackgroundTasks synchronously before returning, so the FSM
    # has genuinely already run by the time we get here -- not just "was scheduled"
    assert ctx["calls"] == ["txn-wh-1"]
    assert ctx["record"].state.value == "fulfilled"


def test_invalid_signature_is_rejected_with_400(webhook_test_app):
    ctx = webhook_test_app
    resp = ctx["client"].post("/webhooks/razorpay", content=ctx["body"],
                               headers={"X-Razorpay-Signature": "not-the-real-signature"})
    assert resp.status_code == 400
    assert ctx["calls"] == [], "an unverified webhook must never reach the FSM"


def test_duplicate_delivery_is_acknowledged_but_not_reprocessed(webhook_test_app):
    ctx = webhook_test_app
    first = ctx["client"].post("/webhooks/razorpay", content=ctx["body"],
                                headers={"X-Razorpay-Signature": ctx["good_sig"]})
    assert first.status_code == 200
    assert ctx["calls"] == ["txn-wh-1"]

    second = ctx["client"].post("/webhooks/razorpay", content=ctx["body"],
                                 headers={"X-Razorpay-Signature": ctx["good_sig"]})
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate_ignored"
    assert ctx["calls"] == ["txn-wh-1"], "fulfillment must not run a second time for the same event"


def test_unhandled_event_type_is_acknowledged_without_action(webhook_test_app):
    ctx = webhook_test_app
    other_body = json.dumps({"event": "payment.failed", "payload": {}})
    resp = ctx["client"].post("/webhooks/razorpay", content=other_body,
                               headers={"X-Razorpay-Signature": sign(other_body, WEBHOOK_SECRET)})
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"
    assert ctx["calls"] == []


def test_ledger_stays_intact_across_all_webhook_traffic(webhook_test_app):
    ctx = webhook_test_app
    ctx["client"].post("/webhooks/razorpay", content=ctx["body"], headers={"X-Razorpay-Signature": ctx["good_sig"]})
    ctx["client"].post("/webhooks/razorpay", content=ctx["body"], headers={"X-Razorpay-Signature": "bad"})
    ctx["client"].post("/webhooks/razorpay", content=ctx["body"], headers={"X-Razorpay-Signature": ctx["good_sig"]})

    ok, bad_row = ctx["ledger"].verify_chain()
    assert ok, f"ledger chain broken at row {bad_row}"
