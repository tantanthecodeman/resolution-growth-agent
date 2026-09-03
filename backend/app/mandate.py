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
    
class Mandate(BaseModel):
    mandate_id: UUID = Field(default_factory=uuid4)
    protocol_source: ProtocolSource
 
    buyer_agent_id: str      
    principal_id: str       
    merchant_id: str
 
    scope: list[str]                     
    allowed_actions: list[AllowedAction]
 
    spend_ceiling: Decimal
    spend_used: Decimal = Decimal("0")   
    currency: str = "INR"
 
    valid_from: datetime
    expires_at: datetime
 
    signature: str    
    jti: str          
    raw_source_payload: dict   
 
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))