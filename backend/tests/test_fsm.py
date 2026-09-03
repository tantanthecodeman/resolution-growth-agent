import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import jwt as pyjwt
import pytest

import app.mandate as m2
from app.mandate import ACPAdapter, ACPCartRequest, AllowedAction, ProtocolSource
from app.policy_gate import PolicyGate, PolicyConfig
from app.ledger import AuditLedger
from app.razorpay_client import RazorpayClient
from app.fsm import (
    FailureRecoveryFSM, TransactionRecord, FailureReason, FulfillmentError,
    TxnState, IllegalTransitionError,
)

os.environ.setdefault("RAZORPAY_KEY_ID", "rzp_test_fake")
os.environ.setdefault("RAZORPAY_KEY_SECRET", "fake_secret")


def fresh_mandate(jti, ceiling=Decimal("1000")):
    token = pyjwt.encode(
        {"iat": datetime.now(timezone.utc), "exp": datetime.now(timezone.utc) + timedelta(minutes=10), "jti": jti},
        m2._SIGNING_SECRETS[ProtocolSource.ACP], algorithm="HS256",
    )
    req = ACPCartRequest(agent_id="chatgpt-acp-v1", buyer_reference="user-77", line_items=["SKU-9"],
        cart_total=ceiling, session_expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        signed_cart_token=token)
    mand = ACPAdapter.to_mandate(req, merchant_id="m-1")
    mand.allowed_actions = [AllowedAction.PURCHASE, AllowedAction.INITIATE_REFUND]
    return mand


def make_fsm(fulfill_script):
    """fulfill_script: list of None (success) / FailureReason (raise) per call."""
    script = list(fulfill_script)

    def fulfill_order(record):
        outcome = script.pop(0)
        if outcome is not None:
            raise FulfillmentError(outcome, f"downstream step failed: {outcome.value}")

    gate = PolicyGate(PolicyConfig(merchant_id="m-1"))
    ledger = AuditLedger("sqlite:///:memory:")
    razorpay = RazorpayClient()
    fsm = FailureRecoveryFSM(gate, ledger, razorpay, fulfill_order)
    return fsm, ledger


def run_webhook(fsm, mandate, fulfill_script_len_marker, amount=Decimal("500")):
    """Drives one payment.captured event through the FSM with the Razorpay network
    boundary mocked, but every state transition and policy decision is real."""
    record = TransactionRecord(transaction_id=f"txn-{mandate.jti}", mandate=mandate,
                                order_id=f"order_{mandate.jti}", amount=amount)
    with patch.object(fsm.razorpay._client.order, "create") as mock_order, \
         patch("app.razorpay_client.requests.post") as mock_refund_post:
        mock_order.return_value = {"id": "order_LIVE1", "status": "created"}
        mock_refund_post.return_value.raise_for_status = lambda: None
        mock_refund_post.return_value.json = lambda: {"id": "rfnd_LIVE1", "status": "processed"}
        fsm.on_payment_captured(record, payment_id="pay_LIVE1")
    return record


def test_fulfillment_succeeds_first_try():
    fsm, ledger = make_fsm([None])
    record = run_webhook(fsm, fresh_mandate("fsm-a"), 1)
    assert record.state == TxnState.FULFILLED
    assert record.fulfillment_attempts == 0
    ok, bad_row = ledger.verify_chain()
    assert ok, f"ledger chain broken at row {bad_row}"


def test_transient_error_retries_then_succeeds():
    fsm, ledger = make_fsm([FailureReason.TRANSIENT_ERROR, None])
    record = run_webhook(fsm, fresh_mandate("fsm-b"), 2)
    assert record.state == TxnState.FULFILLED
    assert record.fulfillment_attempts == 1
    assert any("retry_scheduled" in h for h in record.history)
    ok, bad_row = ledger.verify_chain()
    assert ok, f"ledger chain broken at row {bad_row}"


def test_out_of_stock_triggers_automatic_refund():
    fsm, ledger = make_fsm([FailureReason.OUT_OF_STOCK])
    record = run_webhook(fsm, fresh_mandate("fsm-c"), 1)
    assert record.state == TxnState.REFUNDED
    ok, bad_row = ledger.verify_chain()
    assert ok, f"ledger chain broken at row {bad_row}"


def test_unknown_failure_escalates_rather_than_guessing():
    fsm, ledger = make_fsm([FailureReason.UNKNOWN])
    record = run_webhook(fsm, fresh_mandate("fsm-d"), 1)
    assert record.state == TxnState.ESCALATED
    ok, bad_row = ledger.verify_chain()
    assert ok, f"ledger chain broken at row {bad_row}"


def test_retries_exhausted_falls_through_to_refund():
    fsm, ledger = make_fsm([FailureReason.TRANSIENT_ERROR] * 3)
    record = run_webhook(fsm, fresh_mandate("fsm-e"), 3)
    assert record.state == TxnState.REFUNDED
    assert record.fulfillment_attempts == 2, "should stop retrying at the configured max"
    ok, bad_row = ledger.verify_chain()
    assert ok, f"ledger chain broken at row {bad_row}"


def test_illegal_transition_is_refused_not_silently_allowed():
    fsm, ledger = make_fsm([None])
    record = TransactionRecord(transaction_id="txn-illegal", mandate=fresh_mandate("fsm-f"),
                                order_id="order_x", amount=Decimal("100"))
    with patch.object(fsm.razorpay._client.order, "create"):
        fsm.on_payment_captured(record, payment_id="pay_X")  # ends in FULFILLED
    assert record.state == TxnState.FULFILLED

    with pytest.raises(IllegalTransitionError):
        fsm._transition(record, TxnState.REFUNDED, "manual_test",
                         "trying to skip straight to refunded", {})
