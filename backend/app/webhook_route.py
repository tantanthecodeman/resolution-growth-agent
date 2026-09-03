"""
webhook_route.py — The single HTTP entrypoint where Razorpay talks to this system.

Two rules govern everything in this file:

  1. The raw request body is read and verified BEFORE anything else touches it. See
     the note in razorpay_client.py: a re-parsed/re-serialized body can differ in
     whitespace from what was actually signed and fail verification even when its
     content is identical, so `await request.body()` happens first and the same raw
     bytes are what get both verified and parsed.

  2. This route responds fast. Signature verification and a duplicate-event check
     happen inline (both are cheap — local HMAC comparison and a dict lookup); the
     actual FSM processing (which can be slow, since it may call Razorpay again for
     a refund) is handed to a background task so Razorpay never times out waiting on
     us and retries an event we already received. At larger scale, the background
     task becomes a queue consumer instead — same function signature, different
     execution model, nothing else in this file changes.
"""

from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Request, HTTPException

from app.policy_gate import ReplayGuard  # reused as-is: "have I seen this key before,
                                      # within this window" is exactly what mandate
                                      # replay protection AND webhook delivery dedup
                                      # both need — no reason to write it twice
from app.razorpay_client import RazorpayClient
from app.fsm import FailureRecoveryFSM, TransactionRecord

# Razorpay retries failed/unacknowledged webhooks for up to 24 hours, so the dedup
# window has to cover that whole span or a legitimate retry after a slow first
# response could be wrongly treated as a fresh event.
WEBHOOK_EVENT_DEDUP_WINDOW_SECONDS = 24 * 60 * 60


class TransactionStore:
    """In-memory for this demo — indexed both by Razorpay order_id (for webhook
    lookups) and by our own transaction_id (for the idempotent-order-creation check
    in main.py, BEFORE an order_id even exists). In production this is two indexes
    on the same Postgres table, not two separate stores."""

    def __init__(self):
        self._by_order_id: dict[str, TransactionRecord] = {}
        self._by_transaction_id: dict[str, TransactionRecord] = {}

    def save(self, record: TransactionRecord):
        self._by_order_id[record.order_id] = record
        self._by_transaction_id[record.transaction_id] = record

    def get_by_order_id(self, order_id: str) -> Optional[TransactionRecord]:
        return self._by_order_id.get(order_id)

    def get_by_transaction_id(self, transaction_id: str) -> Optional[TransactionRecord]:
        return self._by_transaction_id.get(transaction_id)


def process_captured_payment(fsm: FailureRecoveryFSM, store: TransactionStore,
                              order_id: str, payment_id: str) -> None:
    """Runs in the background, after the HTTP response has already gone out. If the
    order_id isn't recognized, log-and-drop rather than raise — there's no one left
    to catch an exception raised after the response is already on its way."""
    record = store.get_by_order_id(order_id)
    if record is None:
        return
    fsm.on_payment_captured(record, payment_id)


def build_webhook_router(
    razorpay: RazorpayClient,
    fsm: FailureRecoveryFSM,
    store: TransactionStore,
    event_dedup: ReplayGuard,
    webhook_secret: str,
) -> APIRouter:
    """Factory, not a module-level router: the shared instances (Razorpay client,
    FSM, transaction store, dedup guard) are constructed once at app startup and
    closed over here, rather than re-fetched per request via FastAPI dependencies —
    simpler to test, and avoids re-instantiating anything stateful per request."""

    router = APIRouter()

    @router.post("/webhooks/razorpay")
    async def razorpay_webhook(request: Request, background_tasks: BackgroundTasks):
        raw_body = await request.body()
        signature = request.headers.get("X-Razorpay-Signature", "")

        if not razorpay.verify_webhook(raw_body.decode("utf-8"), signature, webhook_secret):
            # Deliberately 400, not 200 — an unverified request must NOT be
            # acknowledged, or a forged POST could be used to silently suppress a
            # real retry Razorpay would otherwise keep sending.
            raise HTTPException(status_code=400, detail="invalid webhook signature")

        payload = json.loads(raw_body)
        event = payload.get("event", "")

        if event != "payment.captured":
            # Verified, but not something this route acts on. 200 here specifically
            # to stop Razorpay retrying an event we're intentionally ignoring.
            return {"status": "ignored", "event": event}

        payment_entity = payload["payload"]["payment"]["entity"]
        payment_id = payment_entity["id"]
        order_id = payment_entity["order_id"]

        dedup_key = f"payment.captured:{payment_id}"
        if not event_dedup.check_and_record(dedup_key, WEBHOOK_EVENT_DEDUP_WINDOW_SECONDS):
            # Already processed this exact event once. Still 200 — this is expected,
            # correct behavior under at-least-once delivery, not an error.
            return {"status": "duplicate_ignored", "payment_id": payment_id}

        background_tasks.add_task(process_captured_payment, fsm, store, order_id, payment_id)
        return {"status": "accepted", "payment_id": payment_id}

    return router
