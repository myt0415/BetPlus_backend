import uuid

from sqlalchemy import Column, DateTime, ForeignKey, Integer, JSON, Numeric, String, UniqueConstraint
from sqlalchemy.sql import func

from app.db.base import Base


def new_uuid() -> str:
    return str(uuid.uuid4())


class PaymentIntent(Base):
    """Provider-backed deposit or withdrawal. Wallet credits happen only after verification."""

    __tablename__ = "payment_intents"
    __table_args__ = (
        UniqueConstraint("provider", "provider_ref", name="uq_payment_provider_ref"),
    )

    id = Column(String(36), primary_key=True, default=new_uuid)
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    provider = Column(String(32), nullable=False, default="simulated")
    kind = Column(String(16), nullable=False, default="deposit")
    provider_ref = Column(String(64), nullable=False, index=True)
    amount = Column(Numeric(12, 2), nullable=False)
    currency = Column(String(8), nullable=False, default="GHS")
    status = Column(String(16), nullable=False, default="pending", index=True)
    channel = Column(String(32), nullable=True)
    authorization_url = Column(String(512), nullable=True)
    provider_txn_id = Column(String(64), nullable=True, index=True)
    provider_status = Column(String(32), nullable=True)
    pay_currency = Column(String(32), nullable=True)
    pay_amount = Column(String(64), nullable=True)
    pay_address = Column(String(128), nullable=True)
    network = Column(String(32), nullable=True)
    price_currency = Column(String(16), nullable=True)
    transaction_hash = Column(String(128), nullable=True, index=True)
    confirmations = Column(Integer, nullable=True, default=0)
    detected_amount = Column(String(64), nullable=True)
    confirmed_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    extra = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)


class PaymentWebhookEvent(Base):
    """Durable Moolre callback idempotency (transactionid / event key)."""

    __tablename__ = "payment_webhook_events"
    __table_args__ = (
        UniqueConstraint("provider", "event_key", name="uq_payment_webhook_event"),
    )

    id = Column(String(36), primary_key=True, default=new_uuid)
    provider = Column(String(32), nullable=False, index=True)
    event_key = Column(String(128), nullable=False)
    payment_intent_id = Column(
        String(36), ForeignKey("payment_intents.id"), nullable=True, index=True
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (
        UniqueConstraint("user_id", "key_value", name="uq_user_idempotency_key"),
    )

    id = Column(String(36), primary_key=True, default=new_uuid)
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    key_value = Column(String(128), nullable=False)
    method = Column(String(8), nullable=False)
    path = Column(String(255), nullable=False)
    request_hash = Column(String(64), nullable=False)
    status_code = Column(Integer, nullable=False)
    response_body = Column(JSON, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class RateLimitHit(Base):
    __tablename__ = "rate_limit_hits"

    id = Column(String(36), primary_key=True, default=new_uuid)
    bucket = Column(String(128), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
