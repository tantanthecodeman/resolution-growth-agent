import pytest
from decimal import Decimal

from app.ledger import AuditLedger, AuditRecord, GENESIS_HASH
from app.policy_gate import GateDecision, DecisionOutcome


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_decision(
    mandate_id: str = "mandate-1",
    action: str = "purchase",
    outcome: DecisionOutcome = DecisionOutcome.APPROVED,
    rule_fired: str = "test_rule",
    reason: str = "test decision",
    checked_values: dict | None = None,
) -> GateDecision:
    return GateDecision(
        outcome=outcome,
        rule_fired=rule_fired,
        reason=reason,
        mandate_id=mandate_id,
        action=action,
        checked_values=checked_values or {},
    )


def get_record(ledger: AuditLedger, record_id: int) -> AuditRecord:
    with ledger.Session() as session:
        return session.query(AuditRecord).filter(
            AuditRecord.id == record_id
        ).one()


# ---------------------------------------------------------------------------
# Append -- basic ledger behaviour
# ---------------------------------------------------------------------------

def test_append_stores_gate_decision_as_audit_record():
    ledger = AuditLedger()
    decision = make_decision(
        mandate_id="mandate-1",
        action="purchase",
        reason="purchase approved",
        checked_values={"amount": "500"},
    )

    record = ledger.append(decision)

    assert record.id is not None
    assert record.mandate_id == "mandate-1"
    assert record.action == "purchase"
    assert record.outcome == "approved"
    assert record.rule_fired == "test_rule"
    assert record.reason == "purchase approved"
    assert record.checked_values == {"amount": "500"}
    assert record.prev_hash == GENESIS_HASH
    assert len(record.row_hash) == 64


def test_second_record_points_to_first_record_hash():
    ledger = AuditLedger()

    first = ledger.append(
        make_decision(
            mandate_id="mandate-1",
            action="purchase",
            reason="first decision",
        )
    )

    second = ledger.append(
        make_decision(
            mandate_id="mandate-1",
            action="apply_discount",
            reason="second decision",
        )
    )

    assert first.prev_hash == GENESIS_HASH
    assert second.prev_hash == first.row_hash
    assert second.row_hash != first.row_hash


def test_multiple_decisions_form_a_hash_chain():
    ledger = AuditLedger()

    first = ledger.append(
        make_decision(
            mandate_id="mandate-1",
            action="purchase",
            reason="purchase approved",
        )
    )

    second = ledger.append(
        make_decision(
            mandate_id="mandate-1",
            action="apply_discount",
            reason="discount approved",
        )
    )

    third = ledger.append(
        make_decision(
            mandate_id="mandate-1",
            action="substitute_item",
            reason="substitution approved",
        )
    )

    assert first.prev_hash == GENESIS_HASH
    assert second.prev_hash == first.row_hash
    assert third.prev_hash == second.row_hash


# ---------------------------------------------------------------------------
# Chain verification -- intact ledger
# ---------------------------------------------------------------------------

def test_verify_chain_returns_intact_for_untampered_ledger():
    ledger = AuditLedger()

    ledger.append(
        make_decision(
            mandate_id="mandate-1",
            action="purchase",
            reason="purchase approved",
        )
    )

    ledger.append(
        make_decision(
            mandate_id="mandate-1",
            action="apply_discount",
            reason="discount approved",
        )
    )

    ledger.append(
        make_decision(
            mandate_id="mandate-2",
            action="purchase",
            reason="second purchase approved",
        )
    )

    valid, bad_row = ledger.verify_chain()

    assert valid is True
    assert bad_row is None


# ---------------------------------------------------------------------------
# Tamper detection -- direct SQLAlchemy mutation
# ---------------------------------------------------------------------------

def test_verify_chain_catches_tampered_reason_at_exact_row():
    ledger = AuditLedger()

    first = ledger.append(
        make_decision(
            mandate_id="mandate-1",
            action="purchase",
            reason="purchase approved",
        )
    )

    second = ledger.append(
        make_decision(
            mandate_id="mandate-1",
            action="apply_discount",
            reason="discount approved",
        )
    )

    third = ledger.append(
        make_decision(
            mandate_id="mandate-1",
            action="substitute_item",
            reason="substitution approved",
        )
    )

    # The chain should initially be valid.
    valid, bad_row = ledger.verify_chain()

    assert valid is True
    assert bad_row is None

    # Reach directly into SQLAlchemy and mutate the stored row.
    # We deliberately do NOT update row_hash.
    with ledger.Session() as session:
        row = session.query(AuditRecord).filter(
            AuditRecord.id == second.id
        ).one()

        row.reason = "TAMPERED REASON"
        session.commit()

    # The changed content no longer produces the stored hash.
    valid, bad_row = ledger.verify_chain()

    assert valid is False
    assert bad_row == second.id


# ---------------------------------------------------------------------------
# Tampering with the first row also breaks the chain
# ---------------------------------------------------------------------------

def test_tampering_with_first_row_is_detected_at_first_row():
    ledger = AuditLedger()

    first = ledger.append(
        make_decision(
            mandate_id="mandate-1",
            action="purchase",
            reason="original reason",
        )
    )

    ledger.append(
        make_decision(
            mandate_id="mandate-1",
            action="apply_discount",
            reason="second decision",
        )
    )

    with ledger.Session() as session:
        row = session.query(AuditRecord).filter(
            AuditRecord.id == first.id
        ).one()

        row.reason = "TAMPERED"
        session.commit()

    valid, bad_row = ledger.verify_chain()

    assert valid is False
    assert bad_row == first.id


# ---------------------------------------------------------------------------
# History and recent records
# ---------------------------------------------------------------------------

def test_history_for_mandate_returns_records_in_order():
    ledger = AuditLedger()

    first = ledger.append(
        make_decision(
            mandate_id="mandate-1",
            action="purchase",
            reason="first",
        )
    )

    second = ledger.append(
        make_decision(
            mandate_id="mandate-1",
            action="apply_discount",
            reason="second",
        )
    )

    ledger.append(
        make_decision(
            mandate_id="mandate-2",
            action="purchase",
            reason="different mandate",
        )
    )

    history = ledger.history_for_mandate("mandate-1")

    assert [row.id for row in history] == [first.id, second.id]
    assert [row.reason for row in history] == ["first", "second"]


def test_recent_returns_newest_records_first():
    ledger = AuditLedger()

    first = ledger.append(
        make_decision(
            mandate_id="mandate-1",
            action="purchase",
            reason="first",
        )
    )

    second = ledger.append(
        make_decision(
            mandate_id="mandate-2",
            action="purchase",
            reason="second",
        )
    )

    recent = ledger.recent(limit=2)

    assert [row.id for row in recent] == [second.id, first.id]


def test_recent_can_filter_by_outcome():
    ledger = AuditLedger()

    ledger.append(
        make_decision(
            mandate_id="mandate-1",
            outcome=DecisionOutcome.APPROVED,
            reason="approved decision",
        )
    )

    rejected = ledger.append(
        make_decision(
            mandate_id="mandate-2",
            outcome=DecisionOutcome.REJECTED,
            reason="rejected decision",
        )
    )

    recent = ledger.recent(
        limit=10,
        outcome="rejected",
    )

    assert len(recent) == 1
    assert recent[0].id == rejected.id
    assert recent[0].outcome == "rejected"


# ---------------------------------------------------------------------------
# Escalation tracking
# ---------------------------------------------------------------------------

def test_open_escalation_is_returned_until_resolved():
    ledger = AuditLedger()

    escalation = ledger.append(
        make_decision(
            mandate_id="mandate-1",
            outcome=DecisionOutcome.ESCALATED,
            rule_fired="discount_exceeds_policy",
            action="apply_discount",
            reason="discount requires approval",
        )
    )

    open_escalations = ledger.open_escalations()

    assert len(open_escalations) == 1
    assert open_escalations[0].id == escalation.id
    assert open_escalations[0].mandate_id == "mandate-1"


def test_resolving_escalation_creates_new_record():
    ledger = AuditLedger()

    escalation = ledger.append(
        make_decision(
            mandate_id="mandate-1",
            outcome=DecisionOutcome.ESCALATED,
            rule_fired="discount_exceeds_policy",
            action="apply_discount",
            reason="discount requires approval",
        )
    )

    resolution = ledger.resolve_escalation(
        mandate_id="mandate-1",
        decision="approve",
        note="Merchant approved the discount.",
    )

    assert resolution.id != escalation.id
    assert resolution.outcome == "approved"
    assert resolution.rule_fired == "merchant_resolved_escalation"
    assert resolution.action == "merchant_approve"
    assert resolution.checked_values["resolves_mandate_id"] == "mandate-1"


def test_resolved_escalation_is_no_longer_open():
    ledger = AuditLedger()

    ledger.append(
        make_decision(
            mandate_id="mandate-1",
            outcome=DecisionOutcome.ESCALATED,
            rule_fired="discount_exceeds_policy",
            action="apply_discount",
            reason="discount requires approval",
        )
    )

    ledger.resolve_escalation(
        mandate_id="mandate-1",
        decision="deny",
        note="Merchant rejected the discount.",
    )

    open_escalations = ledger.open_escalations()

    assert open_escalations == []


def test_invalid_escalation_resolution_decision_is_rejected():
    ledger = AuditLedger()

    with pytest.raises(ValueError, match="decision must be 'approve' or 'deny'"):
        ledger.resolve_escalation(
            mandate_id="mandate-1",
            decision="maybe",
            note="Invalid decision",
        )