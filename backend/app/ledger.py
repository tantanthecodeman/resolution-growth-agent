from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Optional

from sqlalchemy import Column, Integer, String, DateTime, JSON, Text, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from sqlalchemy.pool import StaticPool

from app.policy_gate import GateDecision, DecisionOutcome

Base = declarative_base()

GENESIS_HASH = "0" * 64


class AuditRecord(Base):
    __tablename__ = "audit_ledger"

    id = Column(Integer, primary_key=True, autoincrement=True)
    mandate_id = Column(String, nullable=False, index=True)
    action = Column(String, nullable=False)
    outcome = Column(String, nullable=False)
    rule_fired = Column(String, nullable=False)
    reason = Column(Text, nullable=False)
    checked_values = Column(JSON, nullable=False)
    agent_reasoning = Column(Text, nullable=True)
    decided_at = Column(String, nullable=False)
    prev_hash = Column(String(64), nullable=False)
    row_hash = Column(String(64), nullable=False)

    def canonical_payload(self) -> dict:
        """Exactly what goes into this row's hash. `id` is a DB-assigned surrogate key,
        not semantic content, so it's deliberately excluded — everything a human would
        actually care about verifying is included."""
        return {
            "mandate_id": self.mandate_id,
            "action": self.action,
            "outcome": self.outcome,
            "rule_fired": self.rule_fired,
            "reason": self.reason,
            "checked_values": self.checked_values,
            "agent_reasoning": self.agent_reasoning,
            "decided_at": self.decided_at,
            "prev_hash": self.prev_hash,
        }


def _compute_hash(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class AuditLedger:
    def __init__(self, engine_url: str = "sqlite:///:memory:"):
        """Postgres in production (postgresql://...); sqlite:///:memory: for local
        tests/CI. Only portable SQLAlchemy types are used, so the URL is the only
        thing that changes between the two.

        The StaticPool/check_same_thread handling below matters specifically for
        SQLite's :memory: mode: by default each new connection gets its own,
        separate empty in-memory database, and FastAPI's BackgroundTasks run on a
        worker thread that opens a NEW connection — so without this, a webhook
        handler's background task would see "no such table" even though the ledger
        was created moments earlier on the request thread. Postgres has no such
        issue (it's a real server, not a per-connection in-process database), so
        this only activates for the sqlite:///:memory: case."""
        connect_args = {}
        kwargs = {}
        if engine_url.startswith("sqlite") and ":memory:" in engine_url:
            connect_args = {"check_same_thread": False}
            kwargs = {"poolclass": StaticPool}
        self.engine = create_engine(engine_url, connect_args=connect_args, **kwargs)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def _last_hash(self, session: Session) -> str:
        last = session.query(AuditRecord).order_by(AuditRecord.id.desc()).first()
        return last.row_hash if last else GENESIS_HASH

    def append(self, decision: GateDecision, agent_reasoning: Optional[str] = None) -> AuditRecord:
        with self.Session() as session:
            prev_hash = self._last_hash(session)
            record = AuditRecord(
                mandate_id=decision.mandate_id,
                action=decision.action,
                outcome=decision.outcome.value,
                rule_fired=decision.rule_fired,
                reason=decision.reason,
                checked_values=decision.checked_values,
                agent_reasoning=agent_reasoning,
                decided_at=decision.decided_at.isoformat(),
                prev_hash=prev_hash,
                row_hash="",
            )
            record.row_hash = _compute_hash(record.canonical_payload())
            session.add(record)
            session.commit()
            session.refresh(record)
            return record

    def verify_chain(self) -> tuple[bool, Optional[int]]:
        """Walks the whole ledger, recomputes every hash from scratch. Returns
        (True, None) if intact, or (False, row_id) for the first row where the
        stored hash no longer matches its content or its predecessor's hash."""
        with self.Session() as session:
            rows = session.query(AuditRecord).order_by(AuditRecord.id.asc()).all()
            expected_prev = GENESIS_HASH
            for row in rows:
                if row.prev_hash != expected_prev:
                    return False, row.id
                if _compute_hash(row.canonical_payload()) != row.row_hash:
                    return False, row.id
                expected_prev = row.row_hash
            return True, None

    def history_for_mandate(self, mandate_id: str) -> list[AuditRecord]:
        with self.Session() as session:
            return (session.query(AuditRecord)
                    .filter(AuditRecord.mandate_id == mandate_id)
                    .order_by(AuditRecord.id.asc()).all())

    def recent(self, limit: int = 50, outcome: Optional[str] = None) -> list[AuditRecord]:
        """For the dashboard's register view. Read-only, newest first."""
        with self.Session() as session:
            q = session.query(AuditRecord)
            if outcome:
                q = q.filter(AuditRecord.outcome == outcome)
            return q.order_by(AuditRecord.id.desc()).limit(limit).all()

    def open_escalations(self) -> list[AuditRecord]:
        """An escalated row is 'open' until a LATER row resolves it. Resolution is
        never a mutation of the original row -- see resolve_escalation() below --
        so 'open' just means: no resolving row exists yet for this mandate_id."""
        with self.Session() as session:
            all_rows = session.query(AuditRecord).order_by(AuditRecord.id.asc()).all()
            resolved_mandate_ids = {
                row.checked_values.get("resolves_mandate_id")
                for row in all_rows
                if row.rule_fired == "merchant_resolved_escalation"
            }
            return [
                row for row in all_rows
                if row.outcome == "escalated" and row.mandate_id not in resolved_mandate_ids
            ]

    def resolve_escalation(self, mandate_id: str, decision: str, note: str) -> AuditRecord:
        """Records a merchant's human decision on an open escalation as a NEW,
        appended row -- never by editing the original escalated row. Mutating a
        past ledger entry, even to add a resolution, would break the exact
        tamper-evidence guarantee the hash chain exists to provide. The link back
        to what's being resolved lives in `checked_values`, not in an edit."""
        if decision not in ("approve", "deny"):
            raise ValueError(f"decision must be 'approve' or 'deny', got {decision!r}")
        gate_decision = GateDecision(
            outcome=DecisionOutcome.APPROVED if decision == "approve" else DecisionOutcome.REJECTED,
            rule_fired="merchant_resolved_escalation",
            reason=note,
            mandate_id=mandate_id,
            action=f"merchant_{decision}",
            checked_values={"resolves_mandate_id": mandate_id, "decision": decision},
        )
        return self.append(gate_decision, agent_reasoning=note)
