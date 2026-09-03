"""
fsm.py — Failure Recovery FSM.

This is the literal answer to "what broke, and what did you do about it." A payment
can succeed while the merchant-side fulfillment step downstream of it fails — stock
just ran out, the merchant's own system timed out, whatever it is. This file is what
decides, deterministically, what happens next: retry, refund, or hand it to a human.

Every transaction moves through an explicit, enumerated set of states, and a
transition that isn't in the allowed table raises rather than silently happening —
there is no "in-between" state this system can't name. Every transition is also
written to the SAME audit ledger everything else in the project writes to, using the
same GateDecision-shaped record, so the ledger reads as one continuous story instead
of two systems bolted together.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Callable, Optional

from app.mandate import Mandate
from app.policy_gate import PolicyGate, GateDecision, DecisionOutcome
from app.ledger import AuditLedger
from app.razorpay_client import RazorpayClient


class TxnState(str, Enum):
    PENDING_PAYMENT = "pending_payment"
    PAYMENT_CAPTURED = "payment_captured"
    FULFILLING = "fulfilling"
    FULFILLED = "fulfilled"
    FULFILLMENT_FAILED = "fulfillment_failed"
    RETRY_SCHEDULED = "retry_scheduled"
    COMPENSATING_REFUND = "compensating_refund"
    REFUNDED = "refunded"
    ESCALATED = "escalated"
    FAILED = "failed"


# The only guarantee that actually matters in a saga: no orphaned states. Anything
# not listed here as reachable from the current state is refused, not attempted.
_ALLOWED_TRANSITIONS: dict[TxnState, set[TxnState]] = {
    TxnState.PENDING_PAYMENT:     {TxnState.PAYMENT_CAPTURED, TxnState.FAILED},
    TxnState.PAYMENT_CAPTURED:    {TxnState.FULFILLING},
    TxnState.FULFILLING:          {TxnState.FULFILLED, TxnState.FULFILLMENT_FAILED},
    TxnState.FULFILLMENT_FAILED:  {TxnState.RETRY_SCHEDULED, TxnState.COMPENSATING_REFUND, TxnState.ESCALATED},
    TxnState.RETRY_SCHEDULED:     {TxnState.FULFILLING},
    TxnState.COMPENSATING_REFUND: {TxnState.REFUNDED, TxnState.ESCALATED, TxnState.FAILED},
    TxnState.FULFILLED:           set(),
    TxnState.REFUNDED:            set(),
    TxnState.ESCALATED:           set(),
    TxnState.FAILED:              set(),
}


class IllegalTransitionError(Exception):
    pass


class FailureReason(str, Enum):
    TRANSIENT_ERROR = "transient_error"              # timeout, network blip -> worth retrying
    MERCHANT_SYSTEM_DOWN = "merchant_system_down"      # transient -> worth retrying
    OUT_OF_STOCK = "out_of_stock"                       # permanent -> compensate
    UNKNOWN = "unknown"                                  # doesn't map cleanly -> escalate, don't guess


class CompensationStrategy(str, Enum):
    RETRY = "retry"
    COMPENSATE = "compensate"
    ESCALATE = "escalate"


# Deterministic default: failure reason -> what to do about it. A plain lookup table,
# not an LLM call — same "bounded" reasoning as the policy gate itself. UNKNOWN
# deliberately maps to ESCALATE rather than a guess. Swapping `classify_failure` for
# something that hands only the UNKNOWN case to the Resolution & Growth Agent for
# judgment is the intended extension point — everything else here stays deterministic.
_DEFAULT_CLASSIFICATION: dict[FailureReason, CompensationStrategy] = {
    FailureReason.TRANSIENT_ERROR: CompensationStrategy.RETRY,
    FailureReason.MERCHANT_SYSTEM_DOWN: CompensationStrategy.RETRY,
    FailureReason.OUT_OF_STOCK: CompensationStrategy.COMPENSATE,
    FailureReason.UNKNOWN: CompensationStrategy.ESCALATE,
}


class FulfillmentError(Exception):
    def __init__(self, reason: FailureReason, message: str = ""):
        self.reason = reason
        super().__init__(message or reason.value)


@dataclass
class TransactionRecord:
    transaction_id: str
    mandate: Mandate
    order_id: str
    payment_id: Optional[str] = None
    amount: Decimal = Decimal("0")
    state: TxnState = TxnState.PENDING_PAYMENT
    fulfillment_attempts: int = 0
    max_fulfillment_retries: int = 2
    history: list[str] = field(default_factory=list)


class FailureRecoveryFSM:
    def __init__(
        self,
        gate: PolicyGate,
        ledger: AuditLedger,
        razorpay: RazorpayClient,
        fulfill_order: Callable[[TransactionRecord], None],
        classify_failure: Optional[Callable[[FailureReason], CompensationStrategy]] = None,
    ):
        """`fulfill_order` is the merchant's own downstream step — create a shipment
        record, decrement warehouse stock, whatever it is — injected so this FSM has
        zero knowledge of any specific merchant's systems. It must raise
        FulfillmentError(reason) on failure and return normally on success.

        `classify_failure` defaults to the deterministic table above."""
        self.gate = gate
        self.ledger = ledger
        self.razorpay = razorpay
        self._fulfill_order = fulfill_order
        self._classify = classify_failure or (
            lambda r: _DEFAULT_CLASSIFICATION.get(r, CompensationStrategy.ESCALATE)
        )

    def _transition(self, record: TransactionRecord, target: TxnState, rule: str, reason: str, checked: dict):
        if target not in _ALLOWED_TRANSITIONS[record.state]:
            raise IllegalTransitionError(f"{record.state.value} -> {target.value} is not an allowed transition")
        decision = GateDecision(
            outcome=DecisionOutcome.APPROVED, rule_fired=rule, reason=reason,
            mandate_id=str(record.mandate.mandate_id), action=f"fsm_transition:{target.value}",
            checked_values={**checked, "from_state": record.state.value, "to_state": target.value},
        )
        self.ledger.append(decision, agent_reasoning=reason)
        record.history.append(f"{record.state.value} -> {target.value}: {reason}")
        record.state = target

    # ---- entry point: called once a verified payment.captured webhook arrives ----

    def on_payment_captured(self, record: TransactionRecord, payment_id: str):
        record.payment_id = payment_id
        self._transition(record, TxnState.PAYMENT_CAPTURED, "webhook_payment_captured",
                          "Razorpay confirmed payment capture", {"payment_id": payment_id})
        self._attempt_fulfillment(record)

    def _attempt_fulfillment(self, record: TransactionRecord):
        self._transition(record, TxnState.FULFILLING, "fulfillment_attempt_started",
                          f"attempt {record.fulfillment_attempts + 1}", {})
        try:
            self._fulfill_order(record)
        except FulfillmentError as e:
            record.fulfillment_attempts += 1
            self._transition(record, TxnState.FULFILLMENT_FAILED, "fulfillment_step_raised", str(e),
                              {"reason": e.reason.value, "attempt": record.fulfillment_attempts})
            self._handle_failure(record, e.reason)
            return
        self._transition(record, TxnState.FULFILLED, "fulfillment_succeeded",
                          "downstream fulfillment step completed", {})

    def _handle_failure(self, record: TransactionRecord, reason: FailureReason):
        strategy = self._classify(reason)

        if strategy == CompensationStrategy.RETRY and record.fulfillment_attempts < record.max_fulfillment_retries:
            self._transition(record, TxnState.RETRY_SCHEDULED, "retry_selected",
                              f"failure reason '{reason.value}' classified as retryable",
                              {"attempt": record.fulfillment_attempts, "max": record.max_fulfillment_retries})
            self._attempt_fulfillment(record)  # in production this is queued with backoff, not immediate
            return

        if strategy == CompensationStrategy.RETRY:
            strategy = CompensationStrategy.COMPENSATE  # retryable in principle, but out of attempts

        if strategy == CompensationStrategy.COMPENSATE:
            self._compensate_with_refund(record, reason)
            return

        decision = self.gate.escalate(record.mandate, f"fulfillment failure '{reason.value}' needs human review",
                                       {"transaction_id": record.transaction_id})
        self.ledger.append(decision, agent_reasoning=decision.reason)
        self._transition(record, TxnState.ESCALATED, decision.rule_fired, decision.reason, {})

    def _compensate_with_refund(self, record: TransactionRecord, reason: FailureReason):
        gate_decision = self.gate.issue_refund(record.mandate, record.amount,
                                                reason_code=f"fulfillment_failed:{reason.value}")
        self.ledger.append(gate_decision, agent_reasoning=gate_decision.reason)

        if gate_decision.outcome == DecisionOutcome.ESCALATED:
            self._transition(record, TxnState.ESCALATED, gate_decision.rule_fired, gate_decision.reason, {})
            return
        if gate_decision.outcome == DecisionOutcome.REJECTED:
            self._transition(record, TxnState.FAILED, gate_decision.rule_fired,
                              f"refund rejected by gate: {gate_decision.reason}", {})
            return

        self._transition(record, TxnState.COMPENSATING_REFUND, "compensation_started",
                          "gate approved refund; calling Razorpay", {})
        try:
            # deterministic idempotency key derived from the transaction id, not a
            # freshly random value -- a retried compensation must reuse the same key
            # or Razorpay would treat it as a brand new refund request
            result = self.razorpay.issue_refund(
                payment_id=record.payment_id, amount=record.amount,
                idempotency_key=f"refund-{record.transaction_id}"[:36],
                notes={"reason": reason.value},
            )
        except Exception as e:
            self._transition(record, TxnState.FAILED, "refund_api_call_failed", str(e), {})
            return
        self._transition(record, TxnState.REFUNDED, "refund_completed",
                          f"Razorpay refund {result.razorpay_refund_id} processed",
                          {"refund_id": result.razorpay_refund_id})
