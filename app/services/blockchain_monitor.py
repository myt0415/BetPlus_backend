from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.money import to_decimal
from app.models.payment import PaymentIntent
from app.services.crypto_pricing_service import CryptoPricingService
from app.services.wallet_service import WalletService


@dataclass
class BlockchainTransaction:
    hash: str
    network: str
    currency: str
    contract_address: str | None
    to_address: str
    amount: Decimal
    confirmations: int
    status: str = "confirmed"


class BlockchainProvider:
    def get_transactions(self, address: str) -> list[BlockchainTransaction]:
        raise NotImplementedError

    def get_transaction(self, tx_hash: str, *, network: str, currency: str, contract_address: str | None = None) -> BlockchainTransaction | None:
        raise NotImplementedError

    def get_confirmations(self, tx_hash: str, *, network: str, currency: str, contract_address: str | None = None) -> int:
        raise NotImplementedError


class MockBlockchainProvider(BlockchainProvider):
    def __init__(self, *, tx_hash: str, to_address: str, amount: str, confirmations: int, network: str, currency: str, contract_address: str | None = None):
        self.tx_hash = tx_hash
        self.to_address = to_address
        self.amount = Decimal(amount)
        self.confirmations = confirmations
        self.network = network
        self.currency = currency
        self.contract_address = contract_address

    def get_transaction(self, tx_hash: str, *, network: str, currency: str, contract_address: str | None = None) -> BlockchainTransaction | None:
        if tx_hash != self.tx_hash:
            return None
        return BlockchainTransaction(
            hash=self.tx_hash,
            network=network,
            currency=currency,
            contract_address=contract_address,
            to_address=self.to_address,
            amount=self.amount,
            confirmations=self.confirmations,
            status="confirmed" if self.confirmations > 0 else "pending",
        )

    def get_transactions(self, address: str) -> list[BlockchainTransaction]:
        if address and self.to_address == address:
            return [self.get_transaction(self.tx_hash, network=self.network, currency=self.currency, contract_address=self.contract_address)]
        return []

    def get_confirmations(self, tx_hash: str, *, network: str, currency: str, contract_address: str | None = None) -> int:
        if tx_hash == self.tx_hash:
            return self.confirmations
        return 0


class BlockchainMonitor:
    def __init__(self, provider: BlockchainProvider | None = None):
        self.provider = provider or MockBlockchainProvider(
            tx_hash="mock-tx",
            to_address="",
            amount="0",
            confirmations=0,
            network="bitcoin",
            currency="btc",
        )

    @staticmethod
    def _snapshot_intent(intent: PaymentIntent) -> PaymentIntent:
        snapshot = PaymentIntent()
        for field in (
            "id",
            "user_id",
            "provider",
            "kind",
            "provider_ref",
            "amount",
            "currency",
            "status",
            "channel",
            "authorization_url",
            "provider_txn_id",
            "provider_status",
            "pay_currency",
            "pay_amount",
            "pay_address",
            "network",
            "price_currency",
            "transaction_hash",
            "confirmations",
            "detected_amount",
            "confirmed_at",
            "expires_at",
            "extra",
            "created_at",
            "completed_at",
        ):
            setattr(snapshot, field, getattr(intent, field, None))
        return snapshot

    def _success_status(self, intent: PaymentIntent) -> str:
        network = (intent.network or "").lower()
        required = get_settings().btc_confirmations_required
        if intent.channel == "btc":
            required = get_settings().btc_confirmations_required
        elif network == "trc20":
            required = get_settings().usdt_trc20_confirmations_required
        elif network == "erc20":
            required = get_settings().usdt_erc20_confirmations_required
        if intent.confirmations is None:
            return "pending"
        if intent.confirmations < required:
            return "confirming"
        if intent.status == "partially_paid":
            return "partially_paid"
        return "completed"

    def reconcile_reference(self, reference: str, db: Session | None = None) -> PaymentIntent | None:
        session_provided = db is not None
        if not session_provided:
            from app.db.session import SessionLocal
            db = SessionLocal()

        intent = db.query(PaymentIntent).filter(PaymentIntent.provider_ref == reference).first()
        if not intent:
            if not session_provided:
                db.close()
            return None
        if intent.status in {"completed", "failed", "expired", "cancelled", "reversed"}:
            result = self._snapshot_intent(intent) if not session_provided else intent
            if not session_provided:
                db.close()
            return result
        tx = self.provider.get_transactions(intent.pay_address or "")
        if not tx:
            result = self._snapshot_intent(intent) if not session_provided else intent
            if not session_provided:
                db.close()
            return result
        tx = tx[0]
        if tx.to_address != (intent.pay_address or ""):
            intent.extra = {**(intent.extra or {}), "reconciliation_reason": "wrong_destination"}
            intent.status = "review_required"
            db.add(intent)
            db.commit()
            result = self._snapshot_intent(intent) if not session_provided else intent
            if not session_provided:
                db.close()
            return result
        if tx.network.lower() != (intent.network or "").lower():
            intent.extra = {**(intent.extra or {}), "reconciliation_reason": "wrong_network"}
            intent.status = "review_required"
            db.add(intent)
            db.commit()
            result = self._snapshot_intent(intent) if not session_provided else intent
            if not session_provided:
                db.close()
            return result
        if (intent.channel or "").lower() == "usdt" and intent.network == "trc20" and (tx.contract_address or "") != "TRC20_USDT_CONTRACT":
            intent.extra = {**(intent.extra or {}), "reconciliation_reason": "wrong_contract"}
            intent.status = "review_required"
            db.add(intent)
            db.commit()
            result = self._snapshot_intent(intent) if not session_provided else intent
            if not session_provided:
                db.close()
            return result
        amount = to_decimal(tx.amount)
        expected = to_decimal(intent.pay_amount or "0")
        intent.transaction_hash = tx.hash
        intent.confirmations = tx.confirmations
        intent.detected_amount = format(amount, "f")
        intent.extra = {
            **(intent.extra or {}),
            "provider_transaction_hash": tx.hash,
            "provider_confirmations": tx.confirmations,
            "provider_detected_amount": format(amount, "f"),
        }
        if amount < expected:
            intent.status = "partially_paid"
            db.add(intent)
            db.commit()
            result = self._snapshot_intent(intent) if not session_provided else intent
            if not session_provided:
                db.close()
            return result
        if amount > expected:
            intent.status = "review_required"
            intent.extra = {**(intent.extra or {}), "reconciliation_reason": "overpayment"}
            db.add(intent)
            db.commit()
            result = self._snapshot_intent(intent) if not session_provided else intent
            if not session_provided:
                db.close()
            return result
        if tx.confirmations < self._required_confirmations(intent):
            intent.status = "confirming"
            db.add(intent)
            db.commit()
            result = self._snapshot_intent(intent) if not session_provided else intent
            if not session_provided:
                db.close()
            return result
        intent.status = "completed"
        intent.completed_at = datetime.now(timezone.utc)
        intent.confirmed_at = datetime.now(timezone.utc)
        db.add(intent)
        WalletService.deposit(
            db,
            intent.user_id,
            float(intent.amount),
            f"Crypto deposit {intent.provider_ref}",
            track_referral=True,
            commit=False,
        )
        db.commit()
        result = self._snapshot_intent(intent) if not session_provided else intent
        if not session_provided:
            db.close()
        return result

    def _required_confirmations(self, intent: PaymentIntent) -> int:
        network = (intent.network or "").lower()
        if intent.channel == "btc":
            return get_settings().btc_confirmations_required
        if network == "trc20":
            return get_settings().usdt_trc20_confirmations_required
        if network == "erc20":
            return get_settings().usdt_erc20_confirmations_required
        return 1
