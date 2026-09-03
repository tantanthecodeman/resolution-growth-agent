from __future__ import annotations
 
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from uuid import UUID, uuid4
 
import jwt  # PyJWT  # type: ignore[reportMissingImports]
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
    
    @field_validator("scope")
    @classmethod
    def scope_not_empty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError(
                "mandate scope cannot be empty — an unscoped mandate is unbounded by "
                "definition, and this system does not accept unbounded mandates"
            )
        return v
 
    @field_validator("spend_ceiling")
    @classmethod
    def ceiling_positive(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError("spend_ceiling must be > 0")
        return v
 
    @model_validator(mode="after")
    def expiry_after_start(self) -> "Mandate":
        if self.expires_at <= self.valid_from:
            raise ValueError("expires_at must be after valid_from")
        return self
 
    @model_validator(mode="after")
    def not_already_expired(self) -> "Mandate":
        if self.expires_at <= datetime.now(timezone.utc):
            raise ValueError("mandate is already expired at ingestion — reject, don't normalize")
        return self
 
    @property
    def remaining_budget(self) -> Decimal:
        return self.spend_ceiling - self.spend_used
 
class ACPCartRequest(BaseModel):
    """Shape modeled on the OpenAI/Stripe Agentic Commerce Protocol cart object."""
    agent_id: str
    buyer_reference: str
    line_items: list[str]           # SKU ids
    cart_total: Decimal
    currency: str = "INR"
    session_expires_at: datetime
    signed_cart_token: str           # JWT signed by the ACP-compliant agent platform
 
 
class AP2MandateEnvelope(BaseModel):
    """Shape modeled on Google's AP2 signed-mandate concept. AP2's actual payload is the
    JWT itself — everything the merchant needs is inside its claims, not the envelope."""
    mandate_jwt: str
 
 
class UAPConsent(BaseModel):
    """Shape modeled on NPCI UAP / UPI Reserve-Pay style consent object."""
    consent_id: str
    payer_vpa: str
    merchant_vpa: str
    max_amount_per_txn: Decimal
    category_scope: list[str]
    valid_upto: datetime
    consent_signature: str
 
 
_SIGNING_SECRETS: dict[ProtocolSource, str] = {
    ProtocolSource.ACP: "acp-shared-secret-placeholder",
    ProtocolSource.AP2: "ap2-shared-secret-placeholder",
    ProtocolSource.UAP: "uap-shared-secret-placeholder",
}
 
 
class ReplayedMandateError(Exception):
    """Raised when a jti has been seen before. In the real service this checks a
    'seen_jti' table/cache with a TTL matching the token's own expiry — a signed
    mandate is only valid proof of authorization ONCE."""
 
 
def _verify_and_decode(token: str, source: ProtocolSource) -> dict:
    claims = jwt.decode(
        token,
        _SIGNING_SECRETS[source],
        algorithms=["HS256"],
        options={
            "require": ["exp", "iat", "jti"],
            "verify_aud": False,
        },
    )
    return claims
 
class ACPAdapter:
    @staticmethod
    def to_mandate(req: ACPCartRequest, merchant_id: str) -> Mandate:
        claims = _verify_and_decode(req.signed_cart_token, ProtocolSource.ACP)
        return Mandate(
            protocol_source=ProtocolSource.ACP,
            buyer_agent_id=req.agent_id,
            principal_id=req.buyer_reference,
            merchant_id=merchant_id,
            scope=req.line_items,
            allowed_actions=[AllowedAction.PURCHASE, AllowedAction.ACCEPT_SUBSTITUTE],
            spend_ceiling=req.cart_total,
            currency=req.currency,
            valid_from=datetime.now(timezone.utc),
            expires_at=req.session_expires_at,
            signature=req.signed_cart_token,
            jti=claims["jti"],
            raw_source_payload=req.model_dump(mode="json"),
        )
 
 
class AP2Adapter:
    @staticmethod
    def to_mandate(req: AP2MandateEnvelope) -> Mandate:
        claims = _verify_and_decode(req.mandate_jwt, ProtocolSource.AP2)
        return Mandate(
            protocol_source=ProtocolSource.AP2,
            buyer_agent_id=claims["iss"],
            principal_id=claims["sub"],
            merchant_id=claims["aud"],
            scope=claims.get("scope", []),
            allowed_actions=[AllowedAction(a) for a in claims.get("allowed_actions", ["purchase"])],
            spend_ceiling=Decimal(str(claims["max_amount"])),
            currency=claims.get("currency", "INR"),
            valid_from=datetime.fromtimestamp(claims["iat"], tz=timezone.utc),
            expires_at=datetime.fromtimestamp(claims["exp"], tz=timezone.utc),
            signature=req.mandate_jwt,
            jti=claims["jti"],
            raw_source_payload=claims,
        )
 
 
class UAPAdapter:
    @staticmethod
    def to_mandate(req: UAPConsent) -> Mandate:
        claims = _verify_and_decode(req.consent_signature, ProtocolSource.UAP)
        return Mandate(
            protocol_source=ProtocolSource.UAP,
            buyer_agent_id=req.payer_vpa,
            principal_id=req.payer_vpa,
            merchant_id=req.merchant_vpa,
            scope=req.category_scope,
            allowed_actions=[AllowedAction.PURCHASE],
            spend_ceiling=req.max_amount_per_txn,
            currency="INR",
            valid_from=datetime.now(timezone.utc),
            expires_at=req.valid_upto,
            signature=req.consent_signature,
            jti=claims["jti"],
            raw_source_payload=req.model_dump(mode="json"),
        )