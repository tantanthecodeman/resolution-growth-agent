import hashlib
import hmac
from decimal import Decimal
from unittest.mock import Mock, patch

from app.razorpay_client import (
    OrderResult,
    RazorpayClient,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_client():
    return RazorpayClient(
        key_id="test_key_id",
        key_secret="test_key_secret",
    )


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

def test_create_order_returns_existing_order_without_calling_razorpay():
    client = make_client()

    existing = OrderResult(
        razorpay_order_id="order_existing",
        amount_paise=1000,
        currency="INR",
        receipt="txn-test-123",
        status="created",
    )

    lookup = Mock(return_value=existing)

    with patch.object(client._client.order, "create") as mock_create:
        result = client.create_order_idempotent(
            transaction_id="test-123",
            amount=Decimal("10.00"),
            currency="INR",
            existing_order_lookup=lookup,
        )

    assert result == existing
    lookup.assert_called_once_with("txn-test-123")
    mock_create.assert_not_called()


def test_create_order_calls_razorpay_when_no_existing_order():
    client = make_client()

    lookup = Mock(return_value=None)

    razorpay_response = {
        "id": "order_new123",
        "status": "created",
    }

    with patch.object(
        client._client.order,
        "create",
        return_value=razorpay_response,
    ) as mock_create:

        result = client.create_order_idempotent(
            transaction_id="test-456",
            amount=Decimal("25.50"),
            currency="INR",
            existing_order_lookup=lookup,
            notes={"source": "pytest"},
        )

    assert result.razorpay_order_id == "order_new123"
    assert result.amount_paise == 2550
    assert result.currency == "INR"
    assert result.receipt == "txn-test-456"
    assert result.status == "created"

    lookup.assert_called_once_with("txn-test-456")

    mock_create.assert_called_once_with(
        data={
            "amount": 2550,
            "currency": "INR",
            "receipt": "txn-test-456",
            "payment_capture": 1,
            "notes": {"source": "pytest"},
        }
    )


def test_create_order_is_idempotent_on_retry():
    client = make_client()

    existing = OrderResult(
        razorpay_order_id="order_existing",
        amount_paise=1000,
        currency="INR",
        receipt="txn-retry-1",
        status="created",
    )

    lookup = Mock(
        side_effect=[
            None,
            existing,
        ]
    )

    razorpay_response = {
        "id": "order_first",
        "status": "created",
    }

    with patch.object(
        client._client.order,
        "create",
        return_value=razorpay_response,
    ) as mock_create:

        first = client.create_order_idempotent(
            transaction_id="retry-1",
            amount=Decimal("10.00"),
            currency="INR",
            existing_order_lookup=lookup,
        )

        second = client.create_order_idempotent(
            transaction_id="retry-1",
            amount=Decimal("10.00"),
            currency="INR",
            existing_order_lookup=lookup,
        )

    assert first.razorpay_order_id == "order_first"
    assert second.razorpay_order_id == "order_existing"

    # The second attempt must NOT create another Razorpay order.
    mock_create.assert_called_once()


# ---------------------------------------------------------------------------
# Refunds
# ---------------------------------------------------------------------------

def test_issue_refund_sends_idempotency_header():
    client = make_client()

    response = Mock()
    response.json.return_value = {
        "id": "rfnd_test123",
        "status": "processed",
    }
    response.raise_for_status.return_value = None

    with patch(
        "app.razorpay_client.requests.post",
        return_value=response,
    ) as mock_post:

        result = client.issue_refund(
            payment_id="pay_test123",
            amount=Decimal("15.00"),
            idempotency_key="ledger-row-123",
            notes={"reason": "test"},
        )

    assert result.razorpay_refund_id == "rfnd_test123"
    assert result.payment_id == "pay_test123"
    assert result.amount_paise == 1500
    assert result.status == "processed"

    mock_post.assert_called_once_with(
        "https://api.razorpay.com/v1/payments/pay_test123/refund",
        auth=("test_key_id", "test_key_secret"),
        headers={
            "X-Refund-Idempotency": "ledger-row-123",
        },
        json={
            "amount": 1500,
            "notes": {"reason": "test"},
        },
        timeout=15,
    )


# ---------------------------------------------------------------------------
# Webhook signature verification
# ---------------------------------------------------------------------------

def test_verify_webhook_accepts_valid_signature():
    client = make_client()

    raw_body = '{"event":"payment.captured","id":"evt_test123"}'
    webhook_secret = "webhook-secret"

    signature = hmac.new(
        webhook_secret.encode(),
        raw_body.encode(),
        hashlib.sha256,
    ).hexdigest()

    assert client.verify_webhook(
        raw_body=raw_body,
        signature=signature,
        webhook_secret=webhook_secret,
    ) is True


def test_verify_webhook_rejects_invalid_signature():
    client = make_client()

    raw_body = '{"event":"payment.captured","id":"evt_test123"}'

    assert client.verify_webhook(
        raw_body=raw_body,
        signature="definitely-not-valid",
        webhook_secret="webhook-secret",
    ) is False


def test_verify_webhook_rejects_modified_body():
    client = make_client()

    original_body = '{"event":"payment.captured","amount":1000}'
    modified_body = '{"event":"payment.captured","amount":9999}'
    webhook_secret = "webhook-secret"

    signature = hmac.new(
        webhook_secret.encode(),
        original_body.encode(),
        hashlib.sha256,
    ).hexdigest()

    assert client.verify_webhook(
        raw_body=modified_body,
        signature=signature,
        webhook_secret=webhook_secret,
    ) is False