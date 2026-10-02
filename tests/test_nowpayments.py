"""NOWPayments deposits, status reconciliation, IPN, and wallet idempotency."""

from __future__ import annotations

import hashlib
import hmac
import json
from decimal import Decimal

from app.core.config import reset_settings_cache
from app.services.nowpayments_service import (
    NowPaymentSnapshot,
    NowPaymentsError,
    NowPaymentsService,
    canonical_ipn_message,
)
from tests.helpers import auth_headers, register_and_token

IPN_SECRET = "test-ipn-secret"


def _enable(monkeypatch):
    monkeypatch.setenv("NOWPAYMENTS_ENABLED", "true")
    monkeypatch.setenv("NOWPAYMENTS_API_KEY", "test-nowpayments-key")
    monkeypatch.setenv("NOWPAYMENTS_IPN_SECRET", IPN_SECRET)
    monkeypatch.setenv(
        "NOWPAYMENTS_IPN_CALLBACK_URL",
        "https://api.example.com/api/v1/payments/webhooks/nowpayments",
    )
    monkeypatch.setenv("NOWPAYMENTS_PRICE_CURRENCY", "ghs")
    monkeypatch.setenv("NOWPAYMENTS_BASE_URL", "https://api.sandbox.nowpayments.io")
    reset_settings_cache()


class _Provider:
    def __init__(self):
        self.creates = 0
        self.status = "waiting"
        self.pay_currency = "btc"
        self.price_amount = Decimal("25")
        self.actually_paid = Decimal("0.002")
        self.pay_amount = Decimal("0.002")
        self.min_fiat = Decimal("1")
        self.create_error: NowPaymentsError | None = None
        self.payment_id = "4242"

    def install(self, monkeypatch):
        provider = self

        def estimate(amount, price_currency, pay_currency):
            del amount, price_currency, pay_currency
            return Decimal("0.002")

        def minimum_fiat(pay_currency, price_currency):
            del pay_currency, price_currency
            return Decimal("0.0001"), provider.min_fiat

        def create_payment(**kwargs):
            provider.creates += 1
            if provider.create_error:
                raise provider.create_error
            provider.pay_currency = kwargs["pay_currency"]
            provider.price_amount = Decimal(str(kwargs["price_amount"]))
            return _snapshot(provider, order_id=kwargs["order_id"])

        def get_payment(payment_id):
            assert payment_id == provider.payment_id
            return _snapshot(provider, order_id=None)

        monkeypatch.setattr(NowPaymentsService, "estimate", staticmethod(estimate))
        monkeypatch.setattr(NowPaymentsService, "minimum_fiat", staticmethod(minimum_fiat))
        monkeypatch.setattr(NowPaymentsService, "create_payment", staticmethod(create_payment))
        monkeypatch.setattr(NowPaymentsService, "get_payment", staticmethod(get_payment))


def _snapshot(provider: _Provider, *, order_id: str | None) -> NowPaymentSnapshot:
    return NowPaymentSnapshot(
        payment_id=provider.payment_id,
        payment_status=provider.status,
        order_id=order_id,
        pay_address="bc1qexampleaddress",
        pay_amount=provider.pay_amount,
        actually_paid=provider.actually_paid if provider.status == "finished" else None,
        pay_currency=provider.pay_currency,
        price_amount=provider.price_amount,
        price_currency="ghs",
        expires_at=None,
        created_at="2026-09-23T22:00:00.000Z",
    )


def _deposit(client, token, **overrides):
    body = {
        "amount": 25,
        "channel": "btc",
        "network": "bitcoin",
        "provider": "nowpayments",
    }
    body.update(overrides)
    return client.post(
        "/api/v1/payments/deposits",
        json=body,
        headers=auth_headers(token),
    )


def _balance(client, token) -> float:
    return client.get("/api/v1/auth/me", headers=auth_headers(token)).json()["balance"]


def _deposit_count(client, token) -> int:
    txs = client.get("/api/v1/wallet/transactions", headers=auth_headers(token)).json()
    return sum(1 for tx in txs if tx["type"] == "deposit" and tx["amount"] > 0)


def _sign(payload: dict) -> tuple[bytes, str]:
    raw = json.dumps(payload).encode("utf-8")
    parsed = json.loads(raw.decode("utf-8"), parse_float=Decimal)
    digest = hmac.new(
        IPN_SECRET.encode("utf-8"),
        canonical_ipn_message(parsed).encode("utf-8"),
        hashlib.sha512,
    ).hexdigest()
    return raw, digest


def _ipn(client, payload: dict, *, signature: str | None = None):
    raw, digest = _sign(payload)
    return client.post(
        "/api/v1/payments/webhooks/nowpayments",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "x-nowpayments-sig": digest if signature is None else signature,
        },
    )


def _finished_payload(created: dict, **overrides) -> dict:
    payload = {
        "payment_id": int(created["payment_id"]),
        "payment_status": "finished",
        "pay_address": created["pay_address"],
        "price_amount": created["amount"],
        "price_currency": "ghs",
        "pay_amount": created["pay_amount"],
        "actually_paid": created["pay_amount"],
        "pay_currency": created["pay_currency"],
        "order_id": created["transaction_id"],
        "fee": {"currency": "btc", "depositFee": 0},
    }
    payload.update(overrides)
    return payload


def test_ipn_canonical_message_sorts_nested_keys():
    message = canonical_ipn_message({"b": 1, "a": {"z": 1, "y": "usdttrc20"}})
    assert message == '{"a":{"y":"usdttrc20","z":1},"b":1}'


def test_btc_deposit_is_pending_and_does_not_credit(client, monkeypatch):
    _enable(monkeypatch)
    provider = _Provider()
    provider.install(monkeypatch)
    token = register_and_token(client, "crypto-btc@example.com")
    resp = _deposit(client, token)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["provider"] == "nowpayments"
    assert body["status"] == "pending"
    assert body["channel"] == "btc"
    assert body["network"] == "bitcoin"
    assert body["pay_currency"] == "btc"
    assert body["pay_address"] == "bc1qexampleaddress"
    assert body["payment_id"] == "4242"
    assert body["transaction_id"] == body["id"]
    assert _balance(client, token) == 0
    assert _deposit_count(client, token) == 0


def test_usdt_trc20_and_erc20_deposits(client, monkeypatch):
    _enable(monkeypatch)
    provider = _Provider()
    provider.install(monkeypatch)
    token = register_and_token(client, "crypto-usdt@example.com")
    trc = _deposit(
        client, token, amount=30, channel="usdt", network="trc20", provider="nowpayments"
    )
    assert trc.status_code == 201, trc.text
    assert trc.json()["pay_currency"] == "usdttrc20"
    assert trc.json()["network"] == "trc20"
    erc = _deposit(
        client, token, amount=31, channel="usdt", network="erc20", provider="nowpayments"
    )
    assert erc.status_code == 201, erc.text
    assert erc.json()["pay_currency"] == "usdterc20"
    assert erc.json()["network"] == "erc20"
    assert _balance(client, token) == 0


def test_rejects_unsupported_currency_network_and_amount(client, monkeypatch):
    _enable(monkeypatch)
    provider = _Provider()
    provider.install(monkeypatch)
    token = register_and_token(client, "crypto-invalid@example.com")
    unsupported = _deposit(client, token, channel="eth", network="erc20")
    assert unsupported.status_code == 400
    bad_network = _deposit(client, token, channel="usdt", network="solana")
    assert bad_network.status_code == 400
    missing_network = _deposit(client, token, channel="usdt", network=None)
    assert missing_network.status_code == 400
    tiny = _deposit(client, token, amount=0.5)
    assert tiny.status_code == 400
    precise = _deposit(client, token, amount=10.999)
    assert precise.status_code == 400
    client.cookies.clear()
    unauth = client.post(
        "/api/v1/payments/deposits",
        json={"amount": 25, "channel": "btc", "provider": "nowpayments"},
    )
    assert unauth.status_code == 401
    assert provider.creates == 0
    assert _balance(client, token) == 0


def test_below_provider_minimum(client, monkeypatch):
    _enable(monkeypatch)
    provider = _Provider()
    provider.min_fiat = Decimal("50")
    provider.install(monkeypatch)
    token = register_and_token(client, "crypto-min@example.com")
    resp = _deposit(client, token, amount=10)
    assert resp.status_code == 400
    assert "minimum" in resp.text.lower()
    assert provider.creates == 0
    assert _balance(client, token) == 0


def test_duplicate_deposit_reuses_provider_payment(client, monkeypatch):
    _enable(monkeypatch)
    provider = _Provider()
    provider.install(monkeypatch)
    token = register_and_token(client, "crypto-dup@example.com")
    first = _deposit(client, token)
    second = _deposit(client, token)
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert provider.creates == 1


def test_provider_failure_and_timeout_do_not_credit(client, monkeypatch):
    _enable(monkeypatch)
    provider = _Provider()
    provider.create_error = NowPaymentsError(
        "Crypto provider is unavailable", status_code=503
    )
    provider.install(monkeypatch)
    token = register_and_token(client, "crypto-down@example.com")
    failed = _deposit(client, token)
    assert failed.status_code == 503
    assert provider.creates == 1
    assert _balance(client, token) == 0

    provider.create_error = NowPaymentsError(
        "Crypto provider timed out", status_code=503, timeout=True
    )
    provider.payment_id = "5252"
    timed_out = _deposit(client, token, amount=26)
    assert timed_out.status_code == 503
    assert provider.creates == 2
    assert _balance(client, token) == 0


def test_status_mapping_does_not_credit_until_finished(client, monkeypatch):
    _enable(monkeypatch)
    provider = _Provider()
    provider.install(monkeypatch)
    token = register_and_token(client, "crypto-status@example.com")
    created = _deposit(client, token).json()
    ref = created["provider_ref"]

    for provider_status, betplus_status in (
        ("waiting", "pending"),
        ("confirming", "processing"),
        ("confirmed", "processing"),
    ):
        provider.status = provider_status
        current = client.get(
            f"/api/v1/payments/{ref}", headers=auth_headers(token)
        )
        assert current.status_code == 200
        assert current.json()["status"] == betplus_status
        assert current.json()["provider_status"] == provider_status
        assert _balance(client, token) == 0

    provider.status = "finished"
    done = client.get(f"/api/v1/payments/{ref}", headers=auth_headers(token))
    assert done.status_code == 200
    assert done.json()["status"] == "completed"
    assert _balance(client, token) == 25
    assert _deposit_count(client, token) == 1

    provider.status = "finished"
    again = client.get(f"/api/v1/payments/{ref}", headers=auth_headers(token))
    assert again.json()["status"] == "completed"
    assert _balance(client, token) == 25
    assert _deposit_count(client, token) == 1


def test_failed_and_expired_status_do_not_credit(client, monkeypatch):
    _enable(monkeypatch)
    provider = _Provider()
    provider.install(monkeypatch)
    token = register_and_token(client, "crypto-terminal@example.com")
    created = _deposit(client, token).json()
    provider.status = "failed"
    failed = client.get(
        f"/api/v1/payments/{created['provider_ref']}", headers=auth_headers(token)
    )
    assert failed.json()["status"] == "failed"
    assert _balance(client, token) == 0

    token2 = register_and_token(client, "crypto-expired@example.com")
    provider.payment_id = "6262"
    provider.status = "waiting"
    created2 = _deposit(client, token2).json()
    provider.status = "expired"
    expired = client.get(
        f"/api/v1/payments/{created2['provider_ref']}", headers=auth_headers(token2)
    )
    assert expired.json()["status"] == "expired"
    assert _balance(client, token2) == 0


def test_unknown_provider_status_does_not_credit(client, monkeypatch):
    _enable(monkeypatch)
    provider = _Provider()
    provider.install(monkeypatch)
    token = register_and_token(client, "crypto-unknown@example.com")
    created = _deposit(client, token).json()
    provider.status = "not-a-real-status"
    current = client.get(
        f"/api/v1/payments/{created['provider_ref']}", headers=auth_headers(token)
    )
    assert current.status_code == 200
    assert current.json()["status"] == "pending"
    assert _balance(client, token) == 0


def test_payment_status_is_owner_scoped(client, monkeypatch):
    _enable(monkeypatch)
    provider = _Provider()
    provider.install(monkeypatch)
    owner = register_and_token(client, "crypto-owner@example.com")
    other = register_and_token(client, "crypto-other@example.com")
    created = _deposit(client, owner).json()
    hidden = client.get(
        f"/api/v1/payments/{created['provider_ref']}", headers=auth_headers(other)
    )
    assert hidden.status_code == 404


def test_webhook_finished_credits_once(client, monkeypatch):
    _enable(monkeypatch)
    provider = _Provider()
    provider.install(monkeypatch)
    token = register_and_token(client, "crypto-ipn@example.com")
    created = _deposit(client, token).json()
    payload = _finished_payload(created)
    first = _ipn(client, payload)
    second = _ipn(client, payload)
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["status"] == "completed"
    assert _balance(client, token) == 25
    assert _deposit_count(client, token) == 1


def test_webhook_rejects_bad_signature_and_unknown_payment(client, monkeypatch):
    _enable(monkeypatch)
    provider = _Provider()
    provider.install(monkeypatch)
    token = register_and_token(client, "crypto-badsig@example.com")
    created = _deposit(client, token).json()
    bad = _ipn(client, _finished_payload(created), signature="deadbeef")
    assert bad.status_code == 400
    assert _balance(client, token) == 0
    unknown = _ipn(
        client,
        _finished_payload(
            created,
            order_id="00000000-0000-0000-0000-000000000000",
            payment_id=999999,
        ),
    )
    assert unknown.status_code == 400
    assert _balance(client, token) == 0


def test_webhook_wrong_currency_or_amount_does_not_credit(client, monkeypatch):
    _enable(monkeypatch)
    provider = _Provider()
    provider.install(monkeypatch)
    token = register_and_token(client, "crypto-mismatch@example.com")
    created = _deposit(client, token).json()
    wrong_currency = _ipn(client, _finished_payload(created, pay_currency="eth"))
    assert wrong_currency.status_code == 200
    assert wrong_currency.json()["status"] != "completed"
    assert _balance(client, token) == 0

    token2 = register_and_token(client, "crypto-amount@example.com")
    provider.payment_id = "7272"
    created2 = _deposit(client, token2, amount=40).json()
    wrong_amount = _ipn(client, _finished_payload(created2, price_amount=1))
    assert wrong_amount.status_code == 200
    assert wrong_amount.json()["status"] != "completed"
    assert _balance(client, token2) == 0
    viewed = client.get(
        f"/api/v1/payments/{created2['provider_ref']}", headers=auth_headers(token2)
    )
    assert viewed.json()["review_required"] is True
    assert _balance(client, token2) == 0


def test_webhook_failed_and_expired_do_not_credit(client, monkeypatch):
    _enable(monkeypatch)
    provider = _Provider()
    provider.install(monkeypatch)
    token = register_and_token(client, "crypto-ipn-fail@example.com")
    created = _deposit(client, token).json()
    failed = _ipn(client, _finished_payload(created, payment_status="failed"))
    assert failed.status_code == 200
    assert failed.json()["status"] == "failed"
    assert _balance(client, token) == 0

    token2 = register_and_token(client, "crypto-ipn-expired@example.com")
    provider.payment_id = "8282"
    created2 = _deposit(client, token2).json()
    expired = _ipn(client, _finished_payload(created2, payment_status="expired"))
    assert expired.status_code == 200
    assert expired.json()["status"] == "expired"
    assert _balance(client, token2) == 0


def test_moolre_channel_is_not_captured_by_crypto(client):
    token = register_and_token(client, "crypto-moolre-path@example.com")
    resp = client.post(
        "/api/v1/payments/deposits",
        json={"amount": 15, "channel": "mobile_money"},
        headers=auth_headers(token),
    )
    assert resp.status_code == 201
    assert resp.json()["provider"] == "simulated"
    assert resp.json()["status"] == "completed"
    assert _balance(client, token) == 15
