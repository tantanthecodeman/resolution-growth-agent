
from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Optional

import razorpay
import requests


class WebhookSignatureError(Exception):
    pass


@dataclass
class OrderResult:
    razorpay_order_id: str
    amount_paise: int
    currency: str
    receipt: str
    status: str


@dataclass
class RefundResult:
    razorpay_refund_id: str
    payment_id: str
    amount_paise: int
    status: str


class RazorpayClient:
    def __init__(self, key_id: Optional[str] = None, key_secret: Optional[str] = None,
                 base_url: str = "https://api.razorpay.com/v1"):
        self._key_id = key_id or os.environ["RAZORPAY_KEY_ID"]
        self._key_secret = key_secret or os.environ["RAZORPAY_KEY_SECRET"]
        self._client = razorpay.Client(auth=(self._key_id, self._key_secret))
        self._base_url = base_url

    # ---- orders ----

    def create_order_idempotent(
        self,
        transaction_id: str,
        amount: Decimal,
        currency: str,
        existing_order_lookup: Callable[[str], Optional[OrderResult]],
        notes: Optional[dict] = None,
    ) -> OrderResult:
        """`existing_order_lookup` is injected rather than hardcoded to a specific store,
        so this stays testable and storage-agnostic. In the real service it queries the
        same Postgres store everything else in the project writes to."""
        receipt = f"txn-{transaction_id}"
        existing = existing_order_lookup(receipt)
        if existing is not None:
            return existing  # a retry after a crash lands here, not on a duplicate order

        amount_paise = int(amount * 100)
        response = self._client.order.create(data={
            "amount": amount_paise,
            "currency": currency,
            "receipt": receipt,
            "payment_capture": 1,
            "notes": notes or {},
        })
        return OrderResult(
            razorpay_order_id=response["id"],
            amount_paise=amount_paise,
            currency=currency,
            receipt=receipt,
            status=response["status"],
        )

    # ---- refunds ----

    def issue_refund(self, payment_id: str, amount: Decimal, idempotency_key: str,
                      notes: Optional[dict] = None) -> RefundResult:
        """Idempotency key must be >=10 chars, alnum/hyphen/underscore — derive it from
        the audit ledger row id for the refund decision, never a freshly random value,
        or a retry would generate a NEW key and defeat the point."""
        amount_paise = int(amount * 100)
        response = requests.post(
            f"{self._base_url}/payments/{payment_id}/refund",
            auth=(self._key_id, self._key_secret),
            headers={"X-Refund-Idempotency": idempotency_key},
            json={"amount": amount_paise, "notes": notes or {}},
            timeout=15,
        )
        response.raise_for_status()
        body = response.json()
        return RefundResult(
            razorpay_refund_id=body["id"],
            payment_id=payment_id,
            amount_paise=amount_paise,
            status=body.get("status", "processed"),
        )

    # ---- webhooks ----

    def verify_webhook(self, raw_body: str, signature: str, webhook_secret: str) -> bool:
        """Pure local HMAC-SHA256 comparison against Razorpay's documented scheme — no
        network call. `raw_body` MUST be the exact, unparsed request body string; a
        round-tripped/re-serialized JSON body will have different whitespace and will
        fail verification even for a genuine webhook."""
        try:
            self._client.utility.verify_webhook_signature(raw_body, signature, webhook_secret)
            return True
        except razorpay.errors.SignatureVerificationError:
            return False
