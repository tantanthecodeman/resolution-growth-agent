from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.mandate import Mandate, AllowedAction, ProtocolSource
from app.policy_gate import (
    PolicyConfig,
    DecisionOutcome,
    GateDecision,
    ReplayGuard,
    PolicyGate,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def valid_mandate_kwargs(**overrides) -> dict:
    """A complete, valid Mandate constructor argument set used to create
    mandates for testing individual policy rules."""
    now = datetime.now(timezone.utc)
    kwargs = dict(
        protocol_source=ProtocolSource.INTERNAL,
        buyer_agent_id="test-agent",
        principal_id="user-1",
        merchant_id="merchant-1",
        scope=["SKU-1", "SKU-2"],
        allowed_actions=[
            AllowedAction.PURCHASE,
            AllowedAction.ACCEPT_SUBSTITUTE,
            AllowedAction.ACCEPT_DISCOUNT,
            AllowedAction.INITIATE_REFUND,
        ],
        spend_ceiling=Decimal("1000"),
        spend_used=Decimal("0"),
        currency="INR",
        valid_from=now - timedelta(minutes=5),
        expires_at=now + timedelta(minutes=10),
        signature="sig",
        jti="jti-1",
        raw_source_payload={},
    )
    kwargs.update(overrides)
    return kwargs


def make_mandate(**overrides) -> Mandate:
    return Mandate(**valid_mandate_kwargs(**overrides))


def make_gate(**overrides) -> PolicyGate:
    config = PolicyConfig(
        merchant_id="merchant-1",
        **overrides,
    )
    return PolicyGate(config)


# ---------------------------------------------------------------------------
# Admit -- happy path and replay protection
# ---------------------------------------------------------------------------

def test_live_unused_mandate_is_admitted():
    gate = make_gate()
    mandate = make_mandate()

    decision = gate.admit(mandate)

    assert decision.outcome == DecisionOutcome.APPROVED
    assert decision.rule_fired == "session_admitted"
    assert decision.action == "admit"
    assert decision.mandate_id == str(mandate.mandate_id)
    assert decision.checked_values["jti"] == mandate.jti


def test_mandate_not_yet_valid_is_rejected_at_admission():
    now = datetime.now(timezone.utc)
    mandate = make_mandate(
        valid_from=now + timedelta(minutes=5),
        expires_at=now + timedelta(minutes=15),
    )
    gate = make_gate()

    decision = gate.admit(mandate)

    assert decision.outcome == DecisionOutcome.REJECTED
    assert decision.rule_fired == "mandate_expired"


def test_same_jti_cannot_start_two_sessions():
    gate = make_gate()

    first_mandate = make_mandate(jti="replay-jti")
    second_mandate = make_mandate(jti="replay-jti")

    first_decision = gate.admit(first_mandate)
    second_decision = gate.admit(second_mandate)

    assert first_decision.outcome == DecisionOutcome.APPROVED
    assert second_decision.outcome == DecisionOutcome.REJECTED
    assert second_decision.rule_fired == "replay_detected"


def test_different_jti_can_start_separate_sessions():
    gate = make_gate()

    first_mandate = make_mandate(jti="jti-1")
    second_mandate = make_mandate(jti="jti-2")

    first_decision = gate.admit(first_mandate)
    second_decision = gate.admit(second_mandate)

    assert first_decision.outcome == DecisionOutcome.APPROVED
    assert second_decision.outcome == DecisionOutcome.APPROVED


# ---------------------------------------------------------------------------
# Discount policy -- happy path and rejection/escalation rules
# ---------------------------------------------------------------------------

def test_discount_within_policy_is_approved():
    gate = make_gate(max_auto_discount_pct=Decimal("5"))
    mandate = make_mandate()

    decision = gate.apply_discount(
        mandate,
        sku="SKU-1",
        discount_pct=Decimal("5"),
        order_amount=Decimal("500"),
    )

    assert decision.outcome == DecisionOutcome.APPROVED
    assert decision.rule_fired == "discount_within_policy"
    assert decision.action == "apply_discount"
    assert decision.checked_values["discount_pct"] == "5"
    assert decision.checked_values["final_amount"] == "475.00"


def test_discount_exceeding_policy_is_escalated():
    gate = make_gate(max_auto_discount_pct=Decimal("5"))
    mandate = make_mandate()

    decision = gate.apply_discount(
        mandate,
        sku="SKU-1",
        discount_pct=Decimal("8"),
        order_amount=Decimal("500"),
    )

    assert decision.outcome == DecisionOutcome.ESCALATED
    assert decision.rule_fired == "discount_exceeds_policy"


def test_discount_without_mandate_permission_is_rejected():
    gate = make_gate()
    mandate = make_mandate(
        allowed_actions=[AllowedAction.PURCHASE],
    )

    decision = gate.apply_discount(
        mandate,
        sku="SKU-1",
        discount_pct=Decimal("5"),
        order_amount=Decimal("500"),
    )

    assert decision.outcome == DecisionOutcome.REJECTED
    assert decision.rule_fired == "action_not_in_mandate_scope"


def test_discount_for_out_of_scope_sku_is_rejected():
    gate = make_gate()
    mandate = make_mandate(scope=["SKU-1"])

    decision = gate.apply_discount(
        mandate,
        sku="SKU-99",
        discount_pct=Decimal("5"),
        order_amount=Decimal("500"),
    )

    assert decision.outcome == DecisionOutcome.REJECTED
    assert decision.rule_fired == "sku_out_of_scope"


def test_discount_that_exceeds_remaining_budget_is_rejected():
    gate = make_gate()
    mandate = make_mandate(
        spend_ceiling=Decimal("500"),
        spend_used=Decimal("450"),
    )

    decision = gate.apply_discount(
        mandate,
        sku="SKU-1",
        discount_pct=Decimal("5"),
        order_amount=Decimal("100"),
    )

    assert decision.outcome == DecisionOutcome.REJECTED
    assert decision.rule_fired == "spend_ceiling_exceeded"


# ---------------------------------------------------------------------------
# Substitution policy -- happy path and rejection/escalation rules
# ---------------------------------------------------------------------------

def test_substitution_within_price_difference_is_approved():
    gate = make_gate(max_substitution_price_diff_pct=Decimal("10"))
    mandate = make_mandate()

    decision = gate.substitute_item(
        mandate,
        original_sku="SKU-1",
        substitute_sku="SKU-2",
        original_price=Decimal("100"),
        substitute_price=Decimal("110"),
    )

    assert decision.outcome == DecisionOutcome.APPROVED
    assert decision.rule_fired == "substitution_within_policy"
    assert decision.action == "substitute_item"
    assert decision.checked_values["original_price"] == "100"
    assert decision.checked_values["substitute_price"] == "110"
    assert decision.checked_values["diff_pct"] == "10.00"


def test_substitution_exceeding_price_difference_is_escalated():
    gate = make_gate(max_substitution_price_diff_pct=Decimal("10"))
    mandate = make_mandate()

    decision = gate.substitute_item(
        mandate,
        original_sku="SKU-1",
        substitute_sku="SKU-2",
        original_price=Decimal("100"),
        substitute_price=Decimal("120"),
    )

    assert decision.outcome == DecisionOutcome.ESCALATED
    assert decision.rule_fired == "substitution_price_diff_exceeds_policy"


def test_substitution_without_mandate_permission_is_rejected():
    gate = make_gate()
    mandate = make_mandate(
        allowed_actions=[AllowedAction.PURCHASE],
    )

    decision = gate.substitute_item(
        mandate,
        original_sku="SKU-1",
        substitute_sku="SKU-2",
        original_price=Decimal("100"),
        substitute_price=Decimal("105"),
    )

    assert decision.outcome == DecisionOutcome.REJECTED
    assert decision.rule_fired == "action_not_in_mandate_scope"


def test_substitution_for_out_of_scope_sku_is_rejected():
    gate = make_gate()
    mandate = make_mandate(scope=["SKU-1"])

    decision = gate.substitute_item(
        mandate,
        original_sku="SKU-99",
        substitute_sku="SKU-2",
        original_price=Decimal("100"),
        substitute_price=Decimal("105"),
    )

    assert decision.outcome == DecisionOutcome.REJECTED
    assert decision.rule_fired == "sku_out_of_scope"


def test_substitution_exceeding_remaining_budget_is_rejected():
    gate = make_gate()
    mandate = make_mandate(
        spend_ceiling=Decimal("500"),
        spend_used=Decimal("450"),
    )

    decision = gate.substitute_item(
        mandate,
        original_sku="SKU-1",
        substitute_sku="SKU-2",
        original_price=Decimal("100"),
        substitute_price=Decimal("100"),
    )

    assert decision.outcome == DecisionOutcome.REJECTED
    assert decision.rule_fired == "spend_ceiling_exceeded"


# ---------------------------------------------------------------------------
# Price drift -- read-only policy checks
# ---------------------------------------------------------------------------

def test_price_drift_within_tolerance_is_approved():
    gate = make_gate(price_drift_tolerance_pct=Decimal("3"))
    mandate = make_mandate()

    decision = gate.check_price_drift(
        mandate,
        sku="SKU-1",
        agent_seen_price=Decimal("100"),
        live_price=Decimal("102"),
    )

    assert decision.outcome == DecisionOutcome.APPROVED
    assert decision.rule_fired == "drift_within_tolerance"
    assert decision.action == "check_price_drift"
    assert decision.checked_values["drift_pct"] == "2.00"


def test_price_drift_exceeding_tolerance_is_escalated():
    gate = make_gate(price_drift_tolerance_pct=Decimal("3"))
    mandate = make_mandate()

    decision = gate.check_price_drift(
        mandate,
        sku="SKU-1",
        agent_seen_price=Decimal("100"),
        live_price=Decimal("105"),
    )

    assert decision.outcome == DecisionOutcome.ESCALATED
    assert decision.rule_fired == "drift_exceeds_tolerance"


def test_price_drift_for_out_of_scope_sku_is_rejected():
    gate = make_gate()
    mandate = make_mandate(scope=["SKU-1"])

    decision = gate.check_price_drift(
        mandate,
        sku="SKU-99",
        agent_seen_price=Decimal("100"),
        live_price=Decimal("102"),
    )

    assert decision.outcome == DecisionOutcome.REJECTED
    assert decision.rule_fired == "sku_out_of_scope"


def test_price_drift_can_be_checked_multiple_times():
    gate = make_gate()
    mandate = make_mandate()

    first_decision = gate.check_price_drift(
        mandate,
        sku="SKU-1",
        agent_seen_price=Decimal("100"),
        live_price=Decimal("101"),
    )

    second_decision = gate.check_price_drift(
        mandate,
        sku="SKU-1",
        agent_seen_price=Decimal("100"),
        live_price=Decimal("102"),
    )

    assert first_decision.outcome == DecisionOutcome.APPROVED
    assert second_decision.outcome == DecisionOutcome.APPROVED
    assert first_decision.rule_fired == "drift_within_tolerance"
    assert second_decision.rule_fired == "drift_within_tolerance"


# ---------------------------------------------------------------------------
# Agent-requested escalation
# ---------------------------------------------------------------------------

def test_agent_can_request_escalation():
    gate = make_gate()
    mandate = make_mandate()

    decision = gate.escalate(
        mandate,
        reason="Customer requested a product outside the authorized scope",
        context={"sku": "SKU-99"},
    )

    assert decision.outcome == DecisionOutcome.ESCALATED
    assert decision.rule_fired == "agent_requested_escalation"
    assert decision.action == "escalate"
    assert decision.reason == "Customer requested a product outside the authorized scope"
    assert decision.checked_values["sku"] == "SKU-99"


def test_agent_escalation_without_context_uses_empty_dictionary():
    gate = make_gate()
    mandate = make_mandate()

    decision = gate.escalate(
        mandate,
        reason="Human approval required",
    )

    assert decision.outcome == DecisionOutcome.ESCALATED
    assert decision.rule_fired == "agent_requested_escalation"
    assert decision.checked_values == {}


# ---------------------------------------------------------------------------
# Refund policy -- happy path and rejection/escalation rules
# ---------------------------------------------------------------------------

def test_refund_within_auto_ceiling_is_approved():
    gate = make_gate(max_auto_refund_amount=Decimal("2000"))
    mandate = make_mandate()

    decision = gate.issue_refund(
        mandate,
        amount=Decimal("1500"),
        reason_code="customer_request",
    )

    assert decision.outcome == DecisionOutcome.APPROVED
    assert decision.rule_fired == "refund_within_policy"
    assert decision.action == "issue_refund"
    assert decision.checked_values["amount"] == "1500"
    assert decision.checked_values["reason_code"] == "customer_request"


def test_refund_exceeding_auto_ceiling_is_escalated():
    gate = make_gate(max_auto_refund_amount=Decimal("2000"))
    mandate = make_mandate()

    decision = gate.issue_refund(
        mandate,
        amount=Decimal("2500"),
        reason_code="customer_request",
    )

    assert decision.outcome == DecisionOutcome.ESCALATED
    assert decision.rule_fired == "refund_exceeds_auto_ceiling"


def test_refund_without_mandate_permission_is_rejected():
    gate = make_gate()
    mandate = make_mandate(
        allowed_actions=[AllowedAction.PURCHASE],
    )

    decision = gate.issue_refund(
        mandate,
        amount=Decimal("500"),
        reason_code="customer_request",
    )

    assert decision.outcome == DecisionOutcome.REJECTED
    assert decision.rule_fired == "action_not_in_mandate_scope"


# ---------------------------------------------------------------------------
# ReplayGuard -- isolated tests
# ---------------------------------------------------------------------------

def test_replay_guard_accepts_first_use_of_jti():
    guard = ReplayGuard()

    assert guard.check_and_record("jti-1", window_seconds=900) is True


def test_replay_guard_rejects_second_use_of_same_jti():
    guard = ReplayGuard()

    assert guard.check_and_record("jti-1", window_seconds=900) is True
    assert guard.check_and_record("jti-1", window_seconds=900) is False


def test_replay_guard_accepts_different_jtis():
    guard = ReplayGuard()

    assert guard.check_and_record("jti-1", window_seconds=900) is True
    assert guard.check_and_record("jti-2", window_seconds=900) is True