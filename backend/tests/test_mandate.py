from datetime import datetime, timedelta, timezone
from decimal import Decimal

import jwt as pyjwt
import pytest
from pydantic import ValidationError

import app.mandate as m2
from app.mandate import (
    ACPAdapter, ACPCartRequest, AP2Adapter, AP2MandateEnvelope, UAPAdapter, UAPConsent,
    Mandate, AllowedAction, ProtocolSource,
)


def sign(source: ProtocolSource, claims: dict) -> str:
    return pyjwt.encode(claims, m2._SIGNING_SECRETS[source], algorithm="HS256")


def base_claims(**overrides) -> dict:
    now = datetime.now(timezone.utc)
    claims = {"iat": now, "exp": now + timedelta(minutes=10), "jti": "test-jti-001"}
    claims.update(overrides)
    return claims


def valid_mandate_kwargs(**overrides) -> dict:
    """A complete, valid set of Mandate constructor arguments -- used to test each
    validator in isolation by overriding exactly one field at a time."""
    now = datetime.now(timezone.utc)
    kwargs = dict(
        protocol_source=ProtocolSource.INTERNAL,
        buyer_agent_id="test-agent",
        principal_id="user-1",
        merchant_id="m-1",
        scope=["SKU-1"],
        allowed_actions=[AllowedAction.PURCHASE],
        spend_ceiling=Decimal("100"),
        valid_from=now,
        expires_at=now + timedelta(minutes=10),
        signature="sig",
        jti="jti-1",
        raw_source_payload={},
    )
    kwargs.update(overrides)
    return kwargs



def test_acp_adapter_produces_a_valid_mandate():
    token = sign(ProtocolSource.ACP, base_claims())
    req = ACPCartRequest(
        agent_id="chatgpt-acp-v1", buyer_reference="user-42", line_items=["SKU-1", "SKU-2"],
        cart_total=Decimal("499.00"), session_expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        signed_cart_token=token,
    )
    mandate = ACPAdapter.to_mandate(req, merchant_id="merchant-9")

    assert mandate.protocol_source == ProtocolSource.ACP
    assert mandate.buyer_agent_id == "chatgpt-acp-v1"
    assert mandate.merchant_id == "merchant-9"
    assert mandate.scope == ["SKU-1", "SKU-2"]
    assert mandate.spend_ceiling == Decimal("499.00")
    assert mandate.jti == "test-jti-001"
    assert AllowedAction.PURCHASE in mandate.allowed_actions
    assert AllowedAction.ACCEPT_SUBSTITUTE in mandate.allowed_actions
    assert mandate.remaining_budget == Decimal("499.00")


def test_ap2_adapter_reads_mandate_fields_from_jwt_claims():
    """AP2's actual payload IS the signed JWT -- everything the merchant needs comes
    out of its claims, not a separate envelope field."""
    token = sign(ProtocolSource.AP2, base_claims(
        iss="claude-ap2-agent", sub="user-77", aud="merchant-5",
        scope=["SKU-9"], allowed_actions=["purchase", "accept_discount"],
        max_amount=750, currency="INR",
    ))
    mandate = AP2Adapter.to_mandate(AP2MandateEnvelope(mandate_jwt=token))

    assert mandate.protocol_source == ProtocolSource.AP2
    assert mandate.buyer_agent_id == "claude-ap2-agent"
    assert mandate.principal_id == "user-77"
    assert mandate.merchant_id == "merchant-5"
    assert mandate.scope == ["SKU-9"]
    assert mandate.spend_ceiling == Decimal("750")
    assert AllowedAction.ACCEPT_DISCOUNT in mandate.allowed_actions


def test_uap_adapter_produces_a_valid_mandate():
    token = sign(ProtocolSource.UAP, base_claims())
    req = UAPConsent(
        consent_id="consent-1", payer_vpa="buyer@upi", merchant_vpa="merchant@upi",
        max_amount_per_txn=Decimal("2000"), category_scope=["groceries"],
        valid_upto=datetime.now(timezone.utc) + timedelta(minutes=10),
        consent_signature=token,
    )
    mandate = UAPAdapter.to_mandate(req)

    assert mandate.protocol_source == ProtocolSource.UAP
    assert mandate.buyer_agent_id == "buyer@upi"
    assert mandate.merchant_id == "merchant@upi"
    assert mandate.scope == ["groceries"]
    assert mandate.allowed_actions == [AllowedAction.PURCHASE]


def test_tampered_signature_is_rejected():
    """Signed with the WRONG secret -- simulates a forged or corrupted token."""
    bad_token = pyjwt.encode(base_claims(), "not-the-real-secret", algorithm="HS256")
    req = ACPCartRequest(
        agent_id="a", buyer_reference="b", line_items=["SKU-1"], cart_total=Decimal("100"),
        session_expires_at=datetime.now(timezone.utc) + timedelta(minutes=10), signed_cart_token=bad_token,
    )
    with pytest.raises(pyjwt.InvalidSignatureError):
        ACPAdapter.to_mandate(req, merchant_id="m-1")


def test_token_missing_required_claim_is_rejected():
    """No `jti` -- required for replay protection downstream, so a token without one
    must fail here rather than silently producing a Mandate with no replay key."""
    claims = base_claims()
    del claims["jti"]
    token = sign(ProtocolSource.ACP, claims)
    req = ACPCartRequest(
        agent_id="a", buyer_reference="b", line_items=["SKU-1"], cart_total=Decimal("100"),
        session_expires_at=datetime.now(timezone.utc) + timedelta(minutes=10), signed_cart_token=token,
    )
    with pytest.raises(pyjwt.MissingRequiredClaimError):
        ACPAdapter.to_mandate(req, merchant_id="m-1")



def test_valid_mandate_constructs_cleanly():
    mandate = Mandate(**valid_mandate_kwargs())
    assert mandate.remaining_budget == Decimal("100")


def test_empty_scope_is_rejected():
    with pytest.raises(ValidationError, match="scope cannot be empty"):
        Mandate(**valid_mandate_kwargs(scope=[]))


def test_zero_spend_ceiling_is_rejected():
    with pytest.raises(ValidationError, match="spend_ceiling must be > 0"):
        Mandate(**valid_mandate_kwargs(spend_ceiling=Decimal("0")))


def test_negative_spend_ceiling_is_rejected():
    with pytest.raises(ValidationError, match="spend_ceiling must be > 0"):
        Mandate(**valid_mandate_kwargs(spend_ceiling=Decimal("-50")))


def test_expiry_before_valid_from_is_rejected():
    now = datetime.now(timezone.utc)
    with pytest.raises(ValidationError, match="expires_at must be after valid_from"):
        Mandate(**valid_mandate_kwargs(valid_from=now, expires_at=now - timedelta(minutes=5)))


def test_already_expired_mandate_is_rejected_at_construction():
    """An expired mandate must fail to even become a Mandate object -- it should
    never reach the gate, the agent, or the ledger in the first place."""
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    with pytest.raises(ValidationError, match="already expired at ingestion"):
        Mandate(**valid_mandate_kwargs(valid_from=past - timedelta(minutes=10), expires_at=past))


def test_remaining_budget_accounts_for_spend_used():
    mandate = Mandate(**valid_mandate_kwargs(spend_ceiling=Decimal("500"), spend_used=Decimal("120")))
    assert mandate.remaining_budget == Decimal("380")