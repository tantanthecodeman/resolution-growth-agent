from __future__ import annotations
 
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from uuid import UUID, uuid4
 
import jwt  # PyJWT
from pydantic import BaseModel, Field, field_validator, model_validator
 
 
class ProtocolSource(str, Enum):
    ACP = "acp"            # OpenAI / Stripe Agentic Commerce Protocol
    AP2 = "ap2"             # Google Agent Payments Protocol
    UAP = "uap"             # NPCI Unified/UPI Agentic Protocol
    INTERNAL = "internal"   # synthetic agent simulator / test harness
 
 
class AllowedAction(str, Enum):
    """What the buyer's mandate lets the merchant-side agent do autonomously.
    Anything not listed here must be escalated, no matter how small."""
    PURCHASE = "purchase"
    ACCEPT_SUBSTITUTE = "accept_substitute"
    ACCEPT_DISCOUNT = "accept_discount"
    INITIATE_REFUND = "initiate_refund"