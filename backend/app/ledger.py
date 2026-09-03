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