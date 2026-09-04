"""
dashboard_api.py — Read/resolve surface for the merchant dashboard.

Deliberately separate from webhook_route.py and the /agent/resolve handler in
main.py: this router only ever READS the ledger and APPENDS merchant-decision rows
to it (see AuditLedger.resolve_escalation). It never calls the gate, the agent, or
Razorpay directly -- a human resolving an escalation here is recorded as a decision,
not wired back into automatically re-running the original transaction. Actually
completing an approved-by-human transaction is a deliberate scope boundary, not an
oversight: it would mean re-entering the whole resolve pipeline with elevated,
human-granted authority, which is its own piece of design, not a checkbox to add
here casually.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.ledger import AuditLedger


class LedgerRowOut(BaseModel):
    id: int
    time: str
    mandate: str
    action: str
    outcome: str
    rule: str
    reason: str
    amount: Optional[str] = None
    hash: str


class EscalationOut(BaseModel):
    mandate: str
    title: str
    detail: str
    amount: Optional[str] = None


class ResolveEscalationRequest(BaseModel):
    mandate_id: str
    decision: str   # "approve" | "deny"
    note: str


def _row_amount(checked_values: dict) -> Optional[str]:
    for key in ("final_amount", "substitute_price", "amount"):
        if key in checked_values:
            return f"₹{checked_values[key]}"
    return None


def _to_row_out(row) -> LedgerRowOut:
    return LedgerRowOut(
        id=row.id,
        time=row.decided_at.split("T")[1][:8] if "T" in row.decided_at else row.decided_at,
        mandate=row.mandate_id,   # full id -- truncate for DISPLAY in the frontend,
                                  # never in the API, or resolve_escalation can't match it back
        action=row.action,
        outcome=row.outcome,
        rule=row.rule_fired,
        reason=row.reason,
        amount=_row_amount(row.checked_values or {}),
        hash=row.row_hash[:8],
    )


def build_dashboard_router(ledger: AuditLedger) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.get("/ledger", response_model=list[LedgerRowOut])
    def get_ledger(outcome: Optional[str] = Query(default=None), limit: int = Query(default=50, le=200)):
        rows = ledger.recent(limit=limit, outcome=outcome)
        return [_to_row_out(r) for r in rows]

    @router.get("/escalations", response_model=list[EscalationOut])
    def get_escalations():
        rows = ledger.open_escalations()
        out = []
        for r in rows:
            out.append(EscalationOut(
                mandate=r.mandate_id,   # full id -- same reasoning as _to_row_out above
                title=f"{r.action.replace('_', ' ')} needs review",
                detail=r.reason,
                amount=_row_amount(r.checked_values or {}),
            ))
        return out

    @router.post("/escalations/resolve")
    def resolve_escalation(req: ResolveEscalationRequest):
        if req.decision not in ("approve", "deny"):
            raise HTTPException(status_code=400, detail="decision must be 'approve' or 'deny'")
        record = ledger.resolve_escalation(req.mandate_id, req.decision, req.note)
        return {"status": "recorded", "ledger_row_id": record.id}

    return router
