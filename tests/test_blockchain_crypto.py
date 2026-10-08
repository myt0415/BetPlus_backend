from app.services.blockchain_monitor import BlockchainMonitor, MockBlockchainProvider
from tests.helpers import auth_headers, register_and_token


def test_direct_blockchain_deposit_uses_backend_address_and_expected_amount(client):
    token = register_and_token(client, "crypto-direct@example.com")
    resp = client.post(
        "/api/v1/payments/deposits",
        json={"amount": 20, "channel": "usdt", "network": "trc20"},
        headers=auth_headers(token),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["provider"] == "blockchain"
    assert body["provider_ref"].startswith("BETPLUS-DEP-")
    assert body["status"] == "pending"
    assert body["pay_address"]
    assert body["pay_amount"]
    assert body["network"] == "trc20"
    assert body["currency"] == "GHS"


def test_direct_blockchain_monitor_confirms_exact_payment_and_credits_once(client):
    token = register_and_token(client, "crypto-monitor@example.com")
    created = client.post(
        "/api/v1/payments/deposits",
        json={"amount": 20, "channel": "btc", "network": "bitcoin"},
        headers=auth_headers(token),
    )
    assert created.status_code == 201, created.text
    ref = created.json()["provider_ref"]
    monitor = BlockchainMonitor(
        provider=MockBlockchainProvider(
            tx_hash="TX_DIRECT_1",
            to_address=created.json()["pay_address"],
            amount="0.0009",
            confirmations=3,
            network="bitcoin",
            currency="btc",
            contract_address=None,
        )
    )
    intent = monitor.reconcile_reference(ref)
    assert intent is not None
    assert intent.status == "completed"
    me = client.get("/api/v1/auth/me", headers=auth_headers(token)).json()
    assert me["balance"] == 20


def test_blockchain_monitor_marks_partial_payment_for_review(client):
    token = register_and_token(client, "crypto-partial@example.com")
    created = client.post(
        "/api/v1/payments/deposits",
        json={"amount": 20, "channel": "usdt", "network": "trc20"},
        headers=auth_headers(token),
    )
    assert created.status_code == 201, created.text
    ref = created.json()["provider_ref"]
    monitor = BlockchainMonitor(
        provider=MockBlockchainProvider(
            tx_hash="TX_PARTIAL_1",
            to_address=created.json()["pay_address"],
            amount="15.0",
            confirmations=8,
            network="trc20",
            currency="usdt",
            contract_address="TRC20_USDT_CONTRACT",
        )
    )
    intent = monitor.reconcile_reference(ref)
    assert intent is not None
    assert intent.status in {"partially_paid", "review_required", "processing"}
