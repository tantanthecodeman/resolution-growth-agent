"""
agent.py — The Resolution & Growth Agent, built as a LangGraph state graph.

This is the "agent" the track asks for: it perceives a situation (a price drift, a
cart needing resolution), reasons about what to propose, and acts by calling gated
tools — but it never executes anything itself. Every action it proposes is translated
into a call against policy_gate.PolicyGate, which is the only thing with authority to
approve, escalate, or reject. If the gate rejects a proposal, the agent sees why and
gets to try a different approach; if the gate escalates, the agent stops and a human
takes over. This file only ever decides WHAT to propose — never whether it's allowed.

Graph shape:

    admit --(ok)--> reason --> act --(approved)-----> success --> END
      |                          |
      | (rejected)               |--(escalated)-----> escalated --> END
      v                          |
    give_up --> END              |--(rejected, attempts left)--> retry_prep --> reason
                                  |
                                  |--(rejected, out of attempts)--> give_up --> END
"""

from __future__ import annotations

from decimal import Decimal
from enum import Enum
from typing import Optional, Protocol, TypedDict

from pydantic import BaseModel, Field

from langgraph.graph import StateGraph, END

from app.mandate import Mandate
from app.policy_gate import PolicyGate, GateDecision, DecisionOutcome
from app.ledger import AuditLedger


# ---------------------------------------------------------------------------
# What the agent is allowed to propose — this IS its tool schema. The LLM's job
# on every reasoning step is to fill in one of these, nothing more.
# ---------------------------------------------------------------------------

class ProposedAction(str, Enum):
    ACCEPT_AS_IS = "accept_as_is"
    APPLY_DISCOUNT = "apply_discount"
    SUBSTITUTE_ITEM = "substitute_item"
    ESCALATE_TO_MERCHANT = "escalate_to_merchant"


class AgentProposal(BaseModel):
    """Structured output the reasoning step must produce. This is a PROPOSAL, not an
    instruction — node_act is the only thing that turns it into a real gate call, and
    the gate is the only thing that can turn a gate call into a real Razorpay action."""
    action: ProposedAction
    sku: Optional[str] = None
    discount_pct: Optional[Decimal] = None
    substitute_sku: Optional[str] = None
    substitute_price: Optional[Decimal] = None
    rationale: str = Field(
        ..., description="One or two sentences on why this action was chosen. "
                          "Becomes the agent_reasoning field in the audit ledger."
    )


SYSTEM_PROMPT = """\
You are the Resolution & Growth Agent for a merchant on Razorpay. You handle checkout
requests and exceptions coming from AI buyer agents (ChatGPT/ACP, Claude/UAP, or other
AP2-speaking agents), already normalized into a single Mandate.

Your job is narrow: given the situation, propose exactly ONE next action from the set
you have been given (accept as-is, apply a discount, substitute an item, or escalate
to the merchant). You do not decide whether an action is ALLOWED — a separate,
deterministic policy gate does that, and it will approve, escalate, or reject your
proposal. If it rejects your proposal, you will be told why, and should propose a
different action, not repeat the same one. If it escalates, stop — a human is now
handling it. You never have the ability to move money directly; every proposal you
make is a request, not an action.

Always give a short, concrete rationale for your proposal — it becomes part of a
permanent audit record a merchant or auditor may read later.
"""


# ---------------------------------------------------------------------------
# Reasoner — the LLM call is behind this narrow interface so the graph doesn't care
# whether it's talking to Groq or a scripted stand-in for tests.
# ---------------------------------------------------------------------------

class SessionContext(TypedDict):
    mandate: Mandate
    sku: str
    agent_seen_price: Decimal
    live_price: Decimal
    order_amount: Decimal


class Reasoner(Protocol):
    def propose(self, context: SessionContext, history: list[str]) -> AgentProposal: ...


class GroqReasoner:
    """Production reasoner. Requires langchain-groq and a GROQ_API_KEY — this is the
    only class in the whole project that makes a network call to an LLM."""

    def __init__(self, model: str = "llama-3.3-70b-versatile", api_key: Optional[str] = None):
        from langchain_groq import ChatGroq  # imported lazily so the rest of this
        # module (and its tests) don't require the package or a network call at all
        self._llm = ChatGroq(model=model, api_key=api_key).with_structured_output(AgentProposal)

    def propose(self, context: SessionContext, history: list[str]) -> AgentProposal:
        user_msg = (
            f"SKU: {context['sku']}\n"
            f"Agent's last-seen price: {context['agent_seen_price']}\n"
            f"Current live price: {context['live_price']}\n"
            f"Order amount: {context['order_amount']}\n"
            f"What has happened so far this session:\n" + "\n".join(f"- {h}" for h in history)
        )
        return self._llm.invoke([("system", SYSTEM_PROMPT), ("human", user_msg)])


class GeminiReasoner:
    """Fallback reasoner for when Groq access is blocked (org/role restrictions on
    the console are a known, common snag). Requires langchain-google-genai and a
    GOOGLE_API_KEY from aistudio.google.com. Identical shape to GroqReasoner
    deliberately -- same SYSTEM_PROMPT, same structured-output contract via
    AgentProposal, same propose() signature -- so swapping between them is a
    one-line change in AppState._build_default_reasoner, never a graph change."""

    def __init__(self, model: str = "gemini-2.0-flash", api_key: Optional[str] = None):
        from langchain_google_genai import ChatGoogleGenerativeAI  # lazy import,
        # same reasoning as GroqReasoner above
        self._llm = ChatGoogleGenerativeAI(model=model, google_api_key=api_key).with_structured_output(AgentProposal)

    def propose(self, context: SessionContext, history: list[str]) -> AgentProposal:
        user_msg = (
            f"SKU: {context['sku']}\n"
            f"Agent's last-seen price: {context['agent_seen_price']}\n"
            f"Current live price: {context['live_price']}\n"
            f"Order amount: {context['order_amount']}\n"
            f"What has happened so far this session:\n" + "\n".join(f"- {h}" for h in history)
        )
        return self._llm.invoke([("system", SYSTEM_PROMPT), ("human", user_msg)])


class ScriptedReasoner:
    """Deterministic stand-in for local tests, CI, and this sandbox — anywhere no LLM
    endpoint is reachable. Swapping `GroqReasoner()` for this in `build_agent()` is a
    one-line change; nothing about the graph's structure or the gate depends on which
    reasoner is plugged in, which is the whole point of keeping this behind a Protocol."""

    def __init__(self, script: list[AgentProposal]):
        self._script = list(script)

    def propose(self, context: SessionContext, history: list[str]) -> AgentProposal:
        if self._script:
            return self._script.pop(0)
        return AgentProposal(action=ProposedAction.ESCALATE_TO_MERCHANT,
                              rationale="Out of scripted responses — escalating rather than guessing.")


# ---------------------------------------------------------------------------
# Graph state
# ---------------------------------------------------------------------------

class AgentState(TypedDict):
    context: SessionContext
    attempts: int
    max_attempts: int
    history: list[str]
    last_decision: Optional[GateDecision]
    last_proposal: Optional[AgentProposal]
    outcome: Optional[str]   # "success" | "escalated" | "failed", set at the end


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

def node_admit(state: AgentState, gate: PolicyGate, ledger: AuditLedger) -> AgentState:
    mandate = state["context"]["mandate"]
    decision = gate.admit(mandate)
    ledger.append(decision, agent_reasoning="Session admission check — mandate liveness and replay status.")
    state["last_decision"] = decision
    if decision.outcome != DecisionOutcome.APPROVED:
        state["outcome"] = "failed"
        state["history"].append(f"admission rejected: {decision.reason}")
    else:
        state["history"].append("session admitted")
    return state


def node_reason(state: AgentState, reasoner: Reasoner) -> AgentState:
    try:
        proposal = reasoner.propose(state["context"], state["history"])
    except Exception as e:
        # A flaky or misconfigured LLM call must never take the whole request
        # down with it -- this is the same "never let a transaction block on a
        # flaky model call" principle applied to a real failure mode, not just
        # documented as an intention. Escalate honestly rather than crash or guess.
        proposal = AgentProposal(
            action=ProposedAction.ESCALATE_TO_MERCHANT,
            rationale=f"Reasoner call failed ({type(e).__name__}: {e}); escalating rather than "
                      f"blocking the transaction or guessing.",
        )
    state["last_proposal"] = proposal
    state["history"].append(f"agent proposes: {proposal.action.value} — {proposal.rationale}")
    return state


def node_act(state: AgentState, gate: PolicyGate, ledger: AuditLedger) -> AgentState:
    mandate = state["context"]["mandate"]
    ctx = state["context"]
    proposal = state["last_proposal"]
    assert proposal is not None

    if proposal.action == ProposedAction.ACCEPT_AS_IS:
        decision = gate.check_price_drift(mandate, ctx["sku"], ctx["agent_seen_price"], ctx["live_price"])
    elif proposal.action == ProposedAction.APPLY_DISCOUNT:
        decision = gate.apply_discount(
            mandate, proposal.sku or ctx["sku"], proposal.discount_pct or Decimal("0"), ctx["order_amount"]
        )
    elif proposal.action == ProposedAction.SUBSTITUTE_ITEM:
        decision = gate.substitute_item(
            mandate, ctx["sku"], proposal.substitute_sku or "", ctx["agent_seen_price"],
            proposal.substitute_price or Decimal("0"),
        )
    elif proposal.action == ProposedAction.ESCALATE_TO_MERCHANT:
        decision = gate.escalate(mandate, proposal.rationale, {"sku": ctx["sku"]})
    else:
        raise ValueError(f"unhandled proposed action: {proposal.action}")

    ledger.append(decision, agent_reasoning=proposal.rationale)
    state["last_decision"] = decision
    state["history"].append(f"gate outcome: {decision.outcome.value} ({decision.rule_fired}: {decision.reason})")
    return state


def node_retry_prep(state: AgentState) -> AgentState:
    state["attempts"] += 1
    state["history"].append(f"retrying — attempt {state['attempts'] + 1} of {state['max_attempts']}")
    return state


def node_success(state: AgentState) -> AgentState:
    state["outcome"] = "success"
    return state


def node_escalated(state: AgentState) -> AgentState:
    state["outcome"] = "escalated"
    # In production this is where a row lands in the merchant dashboard's approval
    # queue — the escalation is already logged in the ledger by node_act; this node
    # is the hook point for notifying a human, not for making any further decision.
    return state


def node_give_up(state: AgentState) -> AgentState:
    if state["outcome"] is None:
        state["outcome"] = "failed"
    return state


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def route_after_admit(state: AgentState) -> str:
    return "reason" if state["outcome"] is None else "give_up"


def route_after_act(state: AgentState) -> str:
    decision = state["last_decision"]
    assert decision is not None
    if decision.outcome == DecisionOutcome.APPROVED:
        return "success"
    if decision.outcome == DecisionOutcome.ESCALATED:
        return "escalate"
    # REJECTED — either the agent gets another shot, or it doesn't
    if state["attempts"] + 1 >= state["max_attempts"]:
        return "give_up"
    return "retry"


# ---------------------------------------------------------------------------
# Graph assembly
# ---------------------------------------------------------------------------

def build_agent(gate: PolicyGate, ledger: AuditLedger, reasoner: Reasoner):
    graph = StateGraph(AgentState)

    graph.add_node("admit", lambda s: node_admit(s, gate, ledger))
    graph.add_node("reason", lambda s: node_reason(s, reasoner))
    graph.add_node("act", lambda s: node_act(s, gate, ledger))
    graph.add_node("retry_prep", node_retry_prep)
    graph.add_node("success", node_success)
    graph.add_node("escalated", node_escalated)
    graph.add_node("give_up", node_give_up)

    graph.set_entry_point("admit")
    graph.add_conditional_edges("admit", route_after_admit, {"reason": "reason", "give_up": "give_up"})
    graph.add_edge("reason", "act")
    graph.add_conditional_edges(
        "act", route_after_act,
        {"success": "success", "escalate": "escalated", "retry": "retry_prep", "give_up": "give_up"},
    )
    graph.add_edge("retry_prep", "reason")
    graph.add_edge("success", END)
    graph.add_edge("escalated", END)
    graph.add_edge("give_up", END)

    return graph.compile()


def initial_state(context: SessionContext, max_attempts: int = 3) -> AgentState:
    return AgentState(context=context, attempts=0, max_attempts=max_attempts,
                       history=[], last_decision=None, last_proposal=None, outcome=None)