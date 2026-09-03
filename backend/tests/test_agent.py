from datetime import datetime, timedelta, timezone
from decimal import Decimal

import jwt as pyjwt

import app.mandate as m2
from app.mandate import ACPAdapter, ACPCartRequest, AllowedAction, ProtocolSource
from app.policy_gate import PolicyGate, PolicyConfig
from app.ledger import AuditLedger
from app.agent import (
    build_agent, initial_state, ScriptedReasoner, AgentProposal, ProposedAction,
)


def fresh_mandate(jti: str, sku: str = "SKU-9", ceiling: Decimal = Decimal("1000")):
    token = pyjwt.encode(
        {"iat": datetime.now(timezone.utc), "exp": datetime.now(timezone.utc) + timedelta(minutes=10), "jti": jti},
        m2._SIGNING_SECRETS[ProtocolSource.ACP], algorithm="HS256",
    )
    req = ACPCartRequest(
        agent_id="chatgpt-acp-v1", buyer_reference="user-77", line_items=[sku],
        cart_total=ceiling, session_expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        signed_cart_token=token,
    )
    mand = ACPAdapter.to_mandate(req, merchant_id="m-1")
    mand.allowed_actions = [AllowedAction.PURCHASE, AllowedAction.ACCEPT_DISCOUNT, AllowedAction.ACCEPT_SUBSTITUTE]
    return mand


def run_scenario(mandate, script, max_attempts=3):
    """Shared harness: runs the agent graph once, returns (final_state, ledger) so
    each test function below asserts on exactly what it cares about."""
    gate = PolicyGate(PolicyConfig(merchant_id="m-1"))
    ledger = AuditLedger("sqlite:///:memory:")
    reasoner = ScriptedReasoner(script)
    graph = build_agent(gate, ledger, reasoner)

    ctx = {"mandate": mandate, "sku": "SKU-9", "agent_seen_price": Decimal("500"),
           "live_price": Decimal("500"), "order_amount": Decimal("500")}
    final = graph.invoke(initial_state(ctx, max_attempts=max_attempts))
    return final, ledger


def test_reject_then_retry_then_success():
    """Agent mis-targets a SKU first, the gate rejects it, the agent self-corrects
    on the next attempt, and the second proposal succeeds."""
    final, ledger = run_scenario(
        fresh_mandate("jti-scn-1"),
        [
            AgentProposal(action=ProposedAction.APPLY_DISCOUNT, sku="WRONG-SKU", discount_pct=Decimal("3"),
                          rationale="Offering a small loyalty discount to close the cart."),
            AgentProposal(action=ProposedAction.APPLY_DISCOUNT, sku="SKU-9", discount_pct=Decimal("3"),
                          rationale="Retrying on the correct SKU with the same small discount."),
        ],
    )
    assert final["outcome"] == "success"
    assert final["attempts"] == 1, "should have taken exactly one retry to succeed"
    assert any("sku_out_of_scope" in h for h in final["history"]), \
        "the history should show the first attempt actually failed for the expected reason"

    ok, bad_row = ledger.verify_chain()
    assert ok, f"ledger chain broken at row {bad_row}"


def test_oversized_discount_escalates_without_retry():
    """A discount beyond the auto-approve ceiling gets escalated, not retried --
    escalation means a human is needed, not 'try again'."""
    final, ledger = run_scenario(
        fresh_mandate("jti-scn-2"),
        [
            AgentProposal(action=ProposedAction.APPLY_DISCOUNT, sku="SKU-9", discount_pct=Decimal("25"),
                          rationale="Buyer is price-sensitive; offering a larger discount to convert."),
        ],
    )
    assert final["outcome"] == "escalated"
    assert final["attempts"] == 0, "an escalation must not consume a retry attempt"

    ok, bad_row = ledger.verify_chain()
    assert ok, f"ledger chain broken at row {bad_row}"


def test_repeated_rejection_gives_up_cleanly():
    """The agent keeps proposing an invalid SKU and exhausts its attempt budget --
    the graph must terminate in 'failed', not hang or raise."""
    final, ledger = run_scenario(
        fresh_mandate("jti-scn-3"),
        [
            AgentProposal(action=ProposedAction.APPLY_DISCOUNT, sku="WRONG-SKU", discount_pct=Decimal("3"),
                          rationale="First attempt."),
            AgentProposal(action=ProposedAction.APPLY_DISCOUNT, sku="STILL-WRONG", discount_pct=Decimal("3"),
                          rationale="Second attempt."),
        ],
        max_attempts=2,
    )
    assert final["outcome"] == "failed"

    ok, bad_row = ledger.verify_chain()
    assert ok, f"ledger chain broken at row {bad_row}"
    rows = ledger.history_for_mandate(str(final["context"]["mandate"].mandate_id))
    assert len(rows) >= 2, "both rejected attempts should be individually logged"
