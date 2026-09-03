from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional

from app.mandate import Mandate, AllowedAction


@dataclass
class PolicyConfig:
    merchant_id: str
    max_auto_discount_pct: Decimal = Decimal("5")          
    price_drift_tolerance_pct: Decimal = Decimal("3")        
    max_substitution_price_diff_pct: Decimal = Decimal("10")
    max_auto_refund_amount: Decimal = Decimal("2000")        
    replay_window_seconds: int = 900                          


class DecisionOutcome(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"
    ESCALATED = "escalated"


@dataclass
class GateDecision:
    outcome: DecisionOutcome
    rule_fired: str
    reason: str
    mandate_id: str
    action: str
    checked_values: dict
    decided_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))



class ReplayGuard:
    def __init__(self):
        self._seen: dict[str, datetime] = {}

    def check_and_record(self, jti: str, window_seconds: int) -> bool:
        now = datetime.now(timezone.utc)
        self._seen = {k: v for k, v in self._seen.items() if (now - v).total_seconds() < window_seconds}
        if jti in self._seen:
            return False  # this signed token has already started a session once
        self._seen[jti] = now
        return True



class PolicyGate:
    def __init__(self, policy: PolicyConfig, replay_guard: Optional[ReplayGuard] = None):
        self.policy = policy
        self.replay_guard = replay_guard or ReplayGuard()

    

    def _deny(self, mandate: Mandate, rule: str, reason: str, action: str, checked: dict) -> GateDecision:
        return GateDecision(DecisionOutcome.REJECTED, rule, reason, str(mandate.mandate_id), action, checked)

    def _approve(self, mandate: Mandate, rule: str, reason: str, action: str, checked: dict) -> GateDecision:
        return GateDecision(DecisionOutcome.APPROVED, rule, reason, str(mandate.mandate_id), action, checked)

    def _escalate(self, mandate: Mandate, rule: str, reason: str, action: str, checked: dict) -> GateDecision:
        return GateDecision(DecisionOutcome.ESCALATED, rule, reason, str(mandate.mandate_id), action, checked)

    def _mandate_live(self, mandate: Mandate) -> bool:
        now = datetime.now(timezone.utc)
        return mandate.valid_from <= now <= mandate.expires_at


    def admit(self, mandate: Mandate) -> GateDecision:
        if not self._mandate_live(mandate):
            return self._deny(mandate, "mandate_expired", "mandate is not currently valid",
                               "admit", {"now": datetime.now(timezone.utc).isoformat(),
                                         "valid_from": mandate.valid_from.isoformat(),
                                         "expires_at": mandate.expires_at.isoformat()})
        if not self.replay_guard.check_and_record(mandate.jti, self.policy.replay_window_seconds):
            return self._deny(mandate, "replay_detected",
                               "this signed mandate has already been used to start a session",
                               "admit", {"jti": mandate.jti})
        return self._approve(mandate, "session_admitted", "mandate is live and unused", "admit", {"jti": mandate.jti})


    def apply_discount(self, mandate: Mandate, sku: str, discount_pct: Decimal, order_amount: Decimal) -> GateDecision:
        if not self._mandate_live(mandate):
            return self._deny(mandate, "mandate_expired", "mandate expired mid-session", "apply_discount", {})
        if AllowedAction.ACCEPT_DISCOUNT not in mandate.allowed_actions:
            return self._deny(mandate, "action_not_in_mandate_scope",
                               "mandate does not authorize accepting a discount", "apply_discount",
                               {"allowed": [a.value for a in mandate.allowed_actions]})
        if sku not in mandate.scope:
            return self._deny(mandate, "sku_out_of_scope", f"'{sku}' is not covered by this mandate",
                               "apply_discount", {"sku": sku, "scope": mandate.scope})
        if discount_pct > self.policy.max_auto_discount_pct:
            return self._escalate(mandate, "discount_exceeds_policy",
                                   f"{discount_pct}% exceeds the {self.policy.max_auto_discount_pct}% auto-approve ceiling",
                                   "apply_discount",
                                   {"requested_pct": str(discount_pct), "ceiling_pct": str(self.policy.max_auto_discount_pct)})
        discounted = order_amount * (1 - discount_pct / 100)
        if discounted > mandate.remaining_budget:
            return self._deny(mandate, "spend_ceiling_exceeded",
                               "discounted amount still exceeds remaining mandate budget", "apply_discount",
                               {"discounted": str(discounted), "remaining_budget": str(mandate.remaining_budget)})
        return self._approve(mandate, "discount_within_policy",
                              f"{discount_pct}% is within the {self.policy.max_auto_discount_pct}% ceiling",
                              "apply_discount", {"discount_pct": str(discount_pct), "final_amount": str(discounted)})

    def substitute_item(self, mandate: Mandate, original_sku: str, substitute_sku: str,
                         original_price: Decimal, substitute_price: Decimal) -> GateDecision:
        if not self._mandate_live(mandate):
            return self._deny(mandate, "mandate_expired", "mandate expired mid-session", "substitute_item", {})
        if AllowedAction.ACCEPT_SUBSTITUTE not in mandate.allowed_actions:
            return self._deny(mandate, "action_not_in_mandate_scope",
                               "mandate does not authorize accepting a substitution", "substitute_item",
                               {"allowed": [a.value for a in mandate.allowed_actions]})
        if original_sku not in mandate.scope:
            return self._deny(mandate, "sku_out_of_scope", f"'{original_sku}' is not covered by this mandate",
                               "substitute_item", {"sku": original_sku, "scope": mandate.scope})
        diff_pct = abs(substitute_price - original_price) / original_price * 100
        checked = {"original_price": str(original_price), "substitute_price": str(substitute_price),
                   "diff_pct": f"{diff_pct:.2f}"}
        if diff_pct > self.policy.max_substitution_price_diff_pct:
            return self._escalate(mandate, "substitution_price_diff_exceeds_policy",
                                   f"price difference of {diff_pct:.1f}% exceeds the "
                                   f"{self.policy.max_substitution_price_diff_pct}% ceiling",
                                   "substitute_item", checked)
        if substitute_price > mandate.remaining_budget:
            return self._deny(mandate, "spend_ceiling_exceeded",
                               "substitute item price exceeds remaining mandate budget",
                               "substitute_item", checked)
        return self._approve(mandate, "substitution_within_policy",
                              f"price diff of {diff_pct:.1f}% is within the "
                              f"{self.policy.max_substitution_price_diff_pct}% ceiling",
                              "substitute_item", checked)

    def check_price_drift(self, mandate: Mandate, sku: str, agent_seen_price: Decimal, live_price: Decimal) -> GateDecision:
        """Read-only / advisory: doesn't move money, decides whether a price change can
        be silently absorbed or must be escalated to a substitution/human decision.
        Callable any number of times within a session — it's a check, not a spend."""
        if sku not in mandate.scope:
            return self._deny(mandate, "sku_out_of_scope", f"'{sku}' is not covered by this mandate",
                               "check_price_drift", {"sku": sku})
        drift_pct = abs(live_price - agent_seen_price) / agent_seen_price * 100
        checked = {"agent_seen_price": str(agent_seen_price), "live_price": str(live_price),
                   "drift_pct": f"{drift_pct:.2f}"}
        if drift_pct <= self.policy.price_drift_tolerance_pct:
            return self._approve(mandate, "drift_within_tolerance",
                                  f"{drift_pct:.1f}% drift is within the {self.policy.price_drift_tolerance_pct}% tolerance",
                                  "check_price_drift", checked)
        return self._escalate(mandate, "drift_exceeds_tolerance",
                               f"{drift_pct:.1f}% drift exceeds the {self.policy.price_drift_tolerance_pct}% "
                               f"tolerance — requires substitution or human approval",
                               "check_price_drift", checked)

    def escalate(self, mandate: Mandate, reason: str, context: Optional[dict] = None) -> GateDecision:
        """The agent itself deciding a situation needs a human, as opposed to the gate
        capping a proposal that exceeded policy. Different trigger, same outcome type,
        same logging — an agent-requested escalation is still a deterministic, auditable
        event, not a free-text dead end."""
        return self._escalate(mandate, "agent_requested_escalation", reason, "escalate", context or {})

    def issue_refund(self, mandate: Mandate, amount: Decimal, reason_code: str) -> GateDecision:
        if AllowedAction.INITIATE_REFUND not in mandate.allowed_actions:
            return self._deny(mandate, "action_not_in_mandate_scope",
                               "mandate does not authorize initiating a refund", "issue_refund",
                               {"allowed": [a.value for a in mandate.allowed_actions]})
        if amount > self.policy.max_auto_refund_amount:
            return self._escalate(mandate, "refund_exceeds_auto_ceiling",
                                   f"refund of {amount} exceeds the auto-approve ceiling of "
                                   f"{self.policy.max_auto_refund_amount}",
                                   "issue_refund", {"amount": str(amount), "ceiling": str(self.policy.max_auto_refund_amount)})
        return self._approve(mandate, "refund_within_policy", f"refund of {amount} is within the auto-approve ceiling",
                              "issue_refund", {"amount": str(amount), "reason_code": reason_code})
