"""
main.py — Wires every component built so far into one FastAPI application.

`create_app()` builds all shared, stateful singletons exactly once: the audit
ledger, the policy gate, the Razorpay client, the transaction store, two
INDEPENDENT ReplayGuards (one for mandate replay at session admission, one for
webhook delivery dedup — same class, deliberately separate instances, since they
guard two different keyspaces and must never share state), and the compiled
LangGraph agent. It's a factory function rather than a bare module-level `app`
specifically so tests can inject a scripted reasoner instead of a real Groq call —
the same dependency-injection discipline used throughout every other file in this
project (fulfill_order into the FSM, existing_order_lookup into the Razorpay
client, etc.), applied one level up.

Two HTTP surfaces exist:
  POST /agent/resolve      — where an AI buyer agent's request enters the system
  POST /webhooks/razorpay  — where Razorpay's async payment events enter the system

Everything between them (the gate, the ledger, the FSM) is code already built and
tested in isolation in earlier files — this file's only job is wiring, not new logic.
"""

from __future__ import annotations

import os
from decimal import Decimal
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app.mandate import (
    ACPAdapter, ACPCartRequest, AP2Adapter, AP2MandateEnvelope, UAPAdapter, UAPConsent, Mandate,
)
from app.policy_gate import PolicyGate, PolicyConfig, ReplayGuard
from app.ledger import AuditLedger
from app.razorpay_client import RazorpayClient, OrderResult
from app.fsm import FailureRecoveryFSM, TransactionRecord
from app.webhook_route import build_webhook_router, TransactionStore
from app.dashboard_api import build_dashboard_router
from app.agent import build_agent, initial_state, ScriptedReasoner, GroqReasoner, GeminiReasoner, AgentProposal, ProposedAction, Reasoner, SessionContext

# Must run BEFORE any os.environ.get() call below -- including the ones inside
# AppState.__init__, which is why this sits at module level, right after the
# imports it depends on, rather than inside create_app(). load_dotenv() reads
# backend/.env (NOT .env.example) into the process's environment; it's a no-op
# if the file doesn't exist, which is exactly what you want in production, where
# real env vars are injected by Render/Docker instead of a local .env file.
load_dotenv()


class AppState:
    """One instance of every shared component, built once. Everything downstream
    (route handlers) closes over this rather than constructing anything per-request."""

    def __init__(self, reasoner: Optional[Reasoner] = None):
        self.ledger = AuditLedger(os.environ.get("DATABASE_URL", "sqlite:///./ledger.db"))
        self.gate = PolicyGate(PolicyConfig(merchant_id=os.environ.get("MERCHANT_ID", "m-1")))
        self.razorpay = RazorpayClient()
        self.store = TransactionStore()
        self.webhook_dedup = ReplayGuard()  # separate keyspace from the gate's own
                                             # mandate-replay ReplayGuard — never shared
        self.reasoner = reasoner or self._build_default_reasoner()
        self.agent_app = build_agent(self.gate, self.ledger, self.reasoner)
        self.fsm = FailureRecoveryFSM(self.gate, self.ledger, self.razorpay,
                                       fulfill_order=self._fulfill_order)

    @staticmethod
    def _build_default_reasoner() -> Reasoner:
        # Groq checked first (fast, and the original choice) -- falls through to
        # Gemini if no Groq key is configured, since Groq's console has a known,
        # common access-restriction snag (org/role gating) that Gemini's signup
        # doesn't share. Either path produces the same Reasoner shape, so nothing
        # downstream of this function needs to know or care which one is live.
        if os.environ.get("GROQ_API_KEY"):
            return GroqReasoner(api_key=os.environ["GROQ_API_KEY"])
        if os.environ.get("GOOGLE_API_KEY"):
            return GeminiReasoner(api_key=os.environ["GOOGLE_API_KEY"])
        # No key configured — fall back to a reasoner that always escalates rather
        # than silently guessing. The app stays runnable without a key; it just
        # can't make autonomous judgment calls, which is the honest behavior.
        return ScriptedReasoner([AgentProposal(
            action=ProposedAction.ESCALATE_TO_MERCHANT,
            rationale="No LLM configured for this deployment — escalating rather than guessing.",
        )])

    @staticmethod
    def _fulfill_order(record: TransactionRecord) -> None:
        """Stand-in for a merchant's real downstream fulfillment step (create a
        shipment record, decrement warehouse stock, call their own OMS). Swap this
        for a real integration — the FSM only requires that it raise
        FulfillmentError(reason) on failure and return normally on success; nothing
        else in the FSM depends on what this function actually does."""
        pass  # succeeds unconditionally in this scaffold


class ResolveRequest(BaseModel):
    """Module-level, deliberately: with `from __future__ import annotations` active,
    FastAPI resolves string type hints against the module's global namespace. A
    Pydantic model defined INSIDE create_app() would never be found there, and
    FastAPI would silently fall back to reading `req` as a query parameter instead
    of a request body — a real, easy-to-miss bug caught while wiring this up.
    Only the route handlers need to close over per-app state; the schema doesn't."""
    protocol: str          # "acp" | "ap2" | "uap"
    sku: str
    agent_seen_price: Decimal
    live_price: Decimal
    order_amount: Decimal
    merchant_id: str
    payload: dict            # the raw protocol-shaped body, dispatched by `protocol`


def create_app(reasoner: Optional[Reasoner] = None) -> FastAPI:
    state = AppState(reasoner=reasoner)

    app = FastAPI(title="Resolution & Growth Agent")
    app.state.rga = state  # exposed for tests; route handlers below use the closure instead

    # The dashboard (Vercel) and this API (Render) are different origins by design
    # -- this is what makes cross-origin fetches from the dashboard work at all.
    # CORS_ALLOWED_ORIGINS is a comma-separated env var; defaults to "*" for local
    # dev only. Lock this down to the real Vercel URL before treating this as a
    # production deployment, not just a demo.
    allowed_origins = os.environ.get("CORS_ALLOWED_ORIGINS", "*").split(",")
    app.add_middleware(
        CORSMiddleware, allow_origins=allowed_origins, allow_methods=["*"], allow_headers=["*"],
    )

    app.include_router(build_webhook_router(
        state.razorpay, state.fsm, state.store, state.webhook_dedup,
        os.environ.get("RAZORPAY_WEBHOOK_SECRET", ""),
    ))
    app.include_router(build_dashboard_router(state.ledger))

    @app.get("/health")
    def health():
        """Deliberately does nothing but confirm the process is alive: no DB
        query, no gate, no ledger read. This exists specifically so an uptime
        pinger (UptimeRobot or similar) can keep Render's free tier from cold-
        starting before a judge opens the dashboard, without generating load
        against Postgres or polluting the ledger with synthetic traffic."""
        return {"status": "ok"}

    # -----------------------------------------------------------------------
    # Buyer-agent entrypoint
    # -----------------------------------------------------------------------

    def normalize_mandate(req: ResolveRequest) -> Mandate:
        if req.protocol == "acp":
            return ACPAdapter.to_mandate(ACPCartRequest(**req.payload), merchant_id=req.merchant_id)
        if req.protocol == "ap2":
            return AP2Adapter.to_mandate(AP2MandateEnvelope(**req.payload))
        if req.protocol == "uap":
            return UAPAdapter.to_mandate(UAPConsent(**req.payload))
        raise HTTPException(status_code=400, detail=f"unrecognized protocol: {req.protocol}")

    def existing_order_for(transaction_id: str, receipt: str) -> Optional[OrderResult]:
        record = state.store.get_by_transaction_id(transaction_id)
        if record is None:
            return None
        return OrderResult(razorpay_order_id=record.order_id, amount_paise=int(record.amount * 100),
                            currency=record.mandate.currency, receipt=receipt, status="already_created")

    @app.post("/agent/resolve")
    def resolve(req: ResolveRequest):
        try:
            mandate = normalize_mandate(req)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"mandate validation failed: {e}")

        ctx: SessionContext = {
            "mandate": mandate, "sku": req.sku, "agent_seen_price": req.agent_seen_price,
            "live_price": req.live_price, "order_amount": req.order_amount,
        }
        final_state = state.agent_app.invoke(initial_state(ctx))

        if final_state["outcome"] != "success":
            # escalated or failed -- nothing gets charged. The full reasoning trail
            # is already in the ledger via node_act/node_admit; this response just
            # tells the calling agent what happened, it doesn't duplicate the record.
            return {"outcome": final_state["outcome"], "history": final_state["history"]}

        # This is the ONE place an approved resolution turns into a real Razorpay
        # order. The amount comes from the GATE's own decision record, never
        # recomputed from the agent's raw proposal -- the gate already validated it,
        # re-deriving it here would reopen exactly the gap the gate exists to close.
        decision = final_state["last_decision"]
        final_amount = Decimal(str(
            decision.checked_values.get("final_amount")
            or decision.checked_values.get("substitute_price")
            or req.order_amount
        ))
        transaction_id = str(mandate.mandate_id)
        receipt = f"txn-{transaction_id}"

        order = state.razorpay.create_order_idempotent(
            transaction_id=transaction_id, amount=final_amount, currency=mandate.currency,
            existing_order_lookup=lambda r: existing_order_for(transaction_id, r),
            notes={"mandate_id": transaction_id, "sku": req.sku},
        )

        record = TransactionRecord(transaction_id=transaction_id, mandate=mandate,
                                    order_id=order.razorpay_order_id, amount=final_amount)
        state.store.save(record)

        return {
            "outcome": "success",
            "razorpay_order_id": order.razorpay_order_id,
            "amount": str(final_amount),
            "currency": mandate.currency,
            "history": final_state["history"],
        }

    return app


# Production entrypoint: `uvicorn app.main:app`
app = create_app()