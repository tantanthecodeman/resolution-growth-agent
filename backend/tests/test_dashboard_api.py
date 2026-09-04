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
from app.policy_gate import PolicyGate, PolicyConfig
from app.ledger import AuditLedger
from app.dashboard_api import build_dashboard_router


def fresh_mandate(jti):
    token = pyjwt.encode(
        {"iat": datetime.now(timezone.utc), "exp": datetime.now(timezone.utc) + timedelta(minutes=10), "jti": jti},
        m2._SIGNING_SECRETS[ProtocolSource.ACP], algorithm="HS256",
    )
    req = ACPCartRequest(agent_id="chatgpt-acp-v1", buyer_reference="user-1", line_items=["SKU-1"],
        cart_total=Decimal("1000"), session_expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        signed_cart_token=token)
    mand = ACPAdapter.to_mandate(req, merchant_id="m-1")
    mand.allowed_actions = [AllowedAction.PURCHASE, AllowedAction.ACCEPT_DISCOUNT]
    return mand


@pytest.fixture
def dashboard_test_app():
    gate = PolicyGate(PolicyConfig(merchant_id="m-1"))
    ledger = AuditLedger("sqlite:///:memory:")

    mandate = fresh_mandate("dash-1")
    admit_decision = gate.admit(mandate)
    ledger.append(admit_decision, agent_reasoning="Session admission check.")
    escalate_decision = gate.apply_discount(mandate, "SKU-1", Decimal("30"), Decimal("500"))
    ledger.append(escalate_decision, agent_reasoning="Buyer requested a large discount.")

    app = FastAPI()
    app.include_router(build_dashboard_router(ledger))
    client = TestClient(app)
    return {"client": client, "ledger": ledger, "mandate_id": str(mandate.mandate_id)}


def test_ledger_endpoint_returns_recent_rows(dashboard_test_app):
    resp = dashboard_test_app["client"].get("/api/ledger")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 2
    assert rows[0]["outcome"] == "escalated"  # newest first
    assert rows[1]["outcome"] == "approved"


def test_ledger_endpoint_filters_by_outcome(dashboard_test_app):
    resp = dashboard_test_app["client"].get("/api/ledger", params={"outcome": "escalated"})
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["rule"] == "discount_exceeds_policy"


def test_escalation_appears_in_open_queue(dashboard_test_app):
    resp = dashboard_test_app["client"].get("/api/escalations")
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    assert items[0]["mandate"] == dashboard_test_app["mandate_id"]


def test_resolving_escalation_removes_it_from_the_open_queue_without_editing_the_original_row(dashboard_test_app):
    ctx = dashboard_test_app
    original_rows = ctx["client"].get("/api/ledger").json()
    original_escalated_row = next(r for r in original_rows if r["outcome"] == "escalated")

    resp = ctx["client"].post("/api/escalations/resolve", json={
        "mandate_id": ctx["mandate_id"], "decision": "approve", "note": "Approved by merchant on call.",
    })
    assert resp.status_code == 200

    # the queue is now empty
    assert ctx["client"].get("/api/escalations").json() == []

    # the ORIGINAL escalated row is untouched -- resolution appended, never edited
    rows_after = ctx["client"].get("/api/ledger").json()
    same_row = next(r for r in rows_after if r["id"] == original_escalated_row["id"])
    assert same_row == original_escalated_row
    assert same_row["hash"] == original_escalated_row["hash"], "hash must be unchanged -- proves no mutation"

    # a NEW row exists recording the resolution
    assert len(rows_after) == 3
    ok, bad_row = ctx["ledger"].verify_chain()
    assert ok, f"ledger chain broken at row {bad_row}"


def test_resolve_rejects_invalid_decision(dashboard_test_app):
    resp = dashboard_test_app["client"].post("/api/escalations/resolve", json={
        "mandate_id": dashboard_test_app["mandate_id"], "decision": "maybe", "note": "unsure",
    })
    assert resp.status_code == 400
