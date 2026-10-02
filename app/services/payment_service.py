import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.money import to_decimal
from app.models.payment import PaymentIntent, PaymentWebhookEvent
from app.services.moolre_service import (
    MoolreError,
    MoolreService,
    is_tx_failed,
    is_tx_pending,
    is_tx_success,
)
from app.services.nowpayments_service import (
    NowPaymentsError,
    NowPaymentsService,
    NowPaymentSnapshot,
    PROVIDER_STATUS_RANK,
    betplus_status_for,
    looks_like_crypto,
    parse_payment,
    resolve_asset,
)
from app.services.wallet_service import InsufficientBalanceError, WalletService

logger = logging.getLogger("app.payments")

TERMINAL_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "expired", "reversed"}
)
PENDING_STATUSES = frozenset({"pending", "processing"})


class PaymentError(Exception):
    def __init__(
        self,
        message: str,
        *,
        reference: str | None = None,
        status_code: int = 400,
    ):
        super().__init__(message)
        self.reference = reference
        self.status_code = status_code


def _new_ref() -> str:
    return "bp_" + secrets.token_hex(12)


def _now():
    return datetime.now(timezone.utc)


def _extra(intent: PaymentIntent) -> dict[str, Any]:
    return dict(intent.extra or {})


class PaymentService:
    @staticmethod
    def initiate_deposit(
        db: Session,
        *,
        user_id: str,
        amount: float,
        channel: str | None = None,
        email: str | None = None,
        payer_phone: str | None = None,
        network: str | None = None,
        provider: str | None = None,
    ) -> PaymentIntent:
        del email
        if PaymentService._requests_crypto(channel, network, provider):
            return PaymentService.initiate_crypto_deposit(
                db,
                user_id=user_id,
                amount=amount,
                channel=channel,
                network=network,
            )
        settings = get_settings()
        if settings.payments_mode == "disabled":
            raise PaymentError("Payments are disabled")

        dec_amount = to_decimal(amount)
        if dec_amount <= 0:
            raise PaymentError("Amount must be positive")

        if settings.payments_mode == "moolre":
            try:
                MoolreService.collection_channel(channel or "mtn")
                MoolreService.normalize_phone(payer_phone)
            except MoolreError as exc:
                raise PaymentError(str(exc)) from exc

        intent = PaymentIntent(
            user_id=user_id,
            provider=settings.payments_mode,
            kind="deposit",
            provider_ref=_new_ref(),
            amount=dec_amount,
            currency=settings.payment_currency,
            status="pending",
            channel=channel or "mtn",
        )
        db.add(intent)
        db.flush()

        if settings.payments_mode == "moolre":
            try:
                body = PaymentService._initiate_collection_with_retry(
                    intent, payer_phone=payer_phone
                )
            except MoolreError as exc:
                intent.status = "failed"
                intent.completed_at = _now()
                intent.extra = {"error": str(exc), "code": exc.code}
                db.add(intent)
                db.commit()
                raise PaymentError(str(exc), reference=intent.provider_ref) from exc
            intent.extra = PaymentService._collection_extra(body, payer_phone)
            db.add(intent)
            db.commit()
            db.refresh(intent)
            logger.info(
                "payment.deposit_pending id=%s ref=%s user=%s otp_required=%s",
                intent.id,
                intent.provider_ref,
                user_id,
                bool((intent.extra or {}).get("otp_required")),
            )
            return intent

        db.commit()
        db.refresh(intent)
        if settings.payments_mode == "simulated":
            intent = PaymentService.complete_intent(db, intent)
        db.refresh(intent)
        return intent

    @staticmethod
    def _initiate_collection_with_retry(
        intent: PaymentIntent,
        *,
        payer_phone: str | None,
        otpcode: str | None = None,
    ) -> dict[str, Any]:
        try:
            return MoolreService.initiate_collection(
                intent, payer_phone=payer_phone, otpcode=otpcode
            )
        except MoolreError as exc:
            if exc.code != "TP13":
                raise
            intent.provider_ref = _new_ref()
            return MoolreService.initiate_collection(
                intent, payer_phone=payer_phone, otpcode=otpcode
            )

    @staticmethod
    def _collection_extra(
        body: dict[str, Any], payer_phone: str | None
    ) -> dict[str, Any]:
        extra = dict(body)
        extra["payer_phone"] = MoolreService.normalize_phone(payer_phone)
        extra["otp_required"] = str(body.get("code") or "").upper() == "TP14"
        extra["provider_response_code"] = body.get("code")
        return extra

    @staticmethod
    def confirm_deposit_otp(
        db: Session,
        *,
        reference: str,
        user_id: str,
        otpcode: str,
        is_admin: bool = False,
    ) -> PaymentIntent:
        settings = get_settings()
        if settings.payments_mode != "moolre":
            raise PaymentError("Phone verification is not required")

        intent = PaymentService.get_by_ref(db, reference)
        if not intent or (intent.user_id != user_id and not is_admin):
            raise PaymentError("not_found")
        extra = _extra(intent)
        if intent.kind != "deposit" or intent.status != "pending" or not extra.get(
            "otp_required"
        ):
            raise PaymentError("This payment is not waiting for a verification code")

        digits = "".join(ch for ch in str(otpcode) if ch.isdigit())
        if len(digits) < 4 or len(digits) > 10:
            raise PaymentError(
                "Enter the SMS verification code", reference=intent.provider_ref
            )

        phone = str(extra.get("payer_phone") or "")
        try:
            body = PaymentService._initiate_collection_with_retry(
                intent, payer_phone=phone, otpcode=digits
            )
        except MoolreError as exc:
            extra["code"] = exc.code
            extra["otp_required"] = True
            extra["error"] = str(exc)
            intent.extra = extra
            db.add(intent)
            db.commit()
            raise PaymentError(str(exc), reference=intent.provider_ref) from exc

        code = str(body.get("code") or "").upper()
        if code == "TP14":
            extra.update(PaymentService._collection_extra(body, phone))
            extra["otp_required"] = True
            intent.extra = extra
            db.add(intent)
            db.commit()
            db.refresh(intent)
            raise PaymentError(
                "That verification code is incorrect or expired. Try again.",
                reference=intent.provider_ref,
            )

        extra.update(PaymentService._collection_extra(body, phone))
        extra["otp_required"] = False
        extra["otp_verified"] = True
        if code == "TP17":
            try:
                follow = PaymentService._initiate_collection_with_retry(
                    intent, payer_phone=phone
                )
            except MoolreError as exc:
                extra["otp_required"] = True
                extra["error"] = str(exc)
                extra["code"] = exc.code
                intent.extra = extra
                db.add(intent)
                db.commit()
                raise PaymentError(str(exc), reference=intent.provider_ref) from exc
            extra.update(PaymentService._collection_extra(follow, phone))
            if extra.get("otp_required"):
                intent.extra = extra
                db.add(intent)
                db.commit()
                db.refresh(intent)
                return intent
        intent.extra = extra
        db.add(intent)
        db.commit()
        db.refresh(intent)
        logger.info(
            "payment.deposit_otp_verified id=%s ref=%s code=%s",
            intent.id,
            intent.provider_ref,
            extra.get("provider_response_code"),
        )
        return intent

    @staticmethod
    def complete_intent(
        db: Session,
        intent: PaymentIntent,
        *,
        commit: bool = True,
        provider_txn_id: str | None = None,
    ) -> PaymentIntent:
        locked = (
            db.query(PaymentIntent)
            .filter(PaymentIntent.id == intent.id)
            .with_for_update()
            .first()
        )
        if not locked:
            raise PaymentError("Payment not found")
        if locked.status == "completed":
            return locked
        if locked.status in {"failed", "cancelled", "expired", "reversed"}:
            raise PaymentError("Payment already failed")
        if locked.status != "pending":
            return locked

        locked.status = "processing"
        db.flush()

        if locked.kind == "deposit":
            WalletService.deposit(
                db,
                locked.user_id,
                float(locked.amount),
                f"Payment {locked.provider_ref}",
                track_referral=True,
                commit=False,
            )
        elif locked.kind == "withdrawal":
            pass
        else:
            raise PaymentError("Unknown payment kind")

        locked.status = "completed"
        locked.completed_at = _now()
        if provider_txn_id:
            locked.provider_txn_id = str(provider_txn_id)[:64]
        db.add(locked)
        db.flush()
        logger.info(
            "payment.completed id=%s ref=%s kind=%s",
            locked.id,
            locked.provider_ref,
            locked.kind,
        )
        if commit:
            db.commit()
            db.refresh(locked)
        return locked

    @staticmethod
    def _lock_by_ref(db: Session, reference: str) -> PaymentIntent:
        intent = (
            db.query(PaymentIntent)
            .filter(PaymentIntent.provider_ref == reference)
            .with_for_update()
            .first()
        )
        if not intent:
            raise PaymentError("Unknown payment reference")
        return intent

    @staticmethod
    def _fail_intent(
        db: Session,
        intent: PaymentIntent,
        *,
        status: str = "failed",
        restore_withdrawal: bool = True,
        commit: bool = True,
    ) -> PaymentIntent:
        if intent.status == "completed" and status == "reversed":
            return PaymentService._reverse_completed_withdrawal(
                db, intent, commit=commit
            )
        if intent.status in TERMINAL_STATUSES:
            return intent
        if (
            restore_withdrawal
            and intent.kind == "withdrawal"
            and not _extra(intent).get("funds_restored")
        ):
            WalletService.deposit(
                db,
                intent.user_id,
                float(intent.amount),
                f"Withdrawal reversal {intent.provider_ref}",
                tx_type="withdraw_reversal",
                ledger_type="withdraw_reversal",
                track_referral=False,
                commit=False,
            )
            extra = _extra(intent)
            extra["funds_restored"] = True
            intent.extra = extra
        intent.status = status
        intent.completed_at = _now()
        db.add(intent)
        db.flush()
        logger.info(
            "payment.failed id=%s ref=%s status=%s",
            intent.id,
            intent.provider_ref,
            status,
        )
        if commit:
            db.commit()
            db.refresh(intent)
        return intent

    @staticmethod
    def _reverse_completed_withdrawal(
        db: Session, intent: PaymentIntent, *, commit: bool = True
    ) -> PaymentIntent:
        if intent.kind != "withdrawal":
            return intent
        if intent.status == "reversed":
            return intent
        if _extra(intent).get("funds_restored"):
            intent.status = "reversed"
            db.add(intent)
            if commit:
                db.commit()
                db.refresh(intent)
            return intent
        WalletService.deposit(
            db,
            intent.user_id,
            float(intent.amount),
            f"Withdrawal reversal {intent.provider_ref}",
            tx_type="withdraw_reversal",
            ledger_type="withdraw_reversal",
            track_referral=False,
            commit=False,
        )
        extra = _extra(intent)
        extra["funds_restored"] = True
        intent.extra = extra
        intent.status = "reversed"
        intent.completed_at = _now()
        db.add(intent)
        db.flush()
        if commit:
            db.commit()
            db.refresh(intent)
        return intent

    @staticmethod
    def verify_payment_reference(
        db: Session,
        reference: str,
        *,
        commit: bool = True,
        allow_transfer_status: bool = False,
    ) -> PaymentIntent:
        settings = get_settings()
        intent = PaymentService._lock_by_ref(db, reference)
        if intent.status in TERMINAL_STATUSES:
            return intent
        if settings.payments_mode != "moolre":
            return intent

        try:
            body = MoolreService.get_payment_status(
                reference, private=allow_transfer_status or intent.kind == "withdrawal"
            )
        except MoolreError as exc:
            raise PaymentError(str(exc)) from exc

        return PaymentService._apply_provider_data(
            db, intent, body, commit=commit, require_verified_success=True
        )

    @staticmethod
    def _apply_provider_data(
        db: Session,
        intent: PaymentIntent,
        body: dict[str, Any],
        *,
        commit: bool,
        require_verified_success: bool,
    ) -> PaymentIntent:
        settings = get_settings()
        data = body.get("data") if isinstance(body.get("data"), dict) else None
        if data is None:
            return intent
        if is_tx_pending(data) and not is_tx_success(data):
            return intent
        if is_tx_failed(data):
            return PaymentService._fail_intent(db, intent, commit=commit)
        if not is_tx_success(data):
            return intent

        try:
            verified = MoolreService.verified_success_data(
                body,
                expected_ref=intent.provider_ref,
                expected_amount=to_decimal(intent.amount),
                expected_account=settings.moolre_account_number,
            )
        except MoolreError as exc:
            if require_verified_success:
                raise PaymentError(str(exc)) from exc
            logger.warning(
                "payment.verify_mismatch ref=%s reason=%s",
                intent.provider_ref,
                str(exc),
            )
            return intent

        txn_id = verified.get("transactionid")
        return PaymentService.complete_intent(
            db,
            intent,
            commit=commit,
            provider_txn_id=str(txn_id) if txn_id is not None else None,
        )

    @staticmethod
    def initiate_withdrawal(
        db: Session,
        *,
        user_id: str,
        amount: float,
        channel: str | None = None,
        destination: str | None = None,
    ) -> PaymentIntent:
        settings = get_settings()
        if settings.payments_mode == "disabled":
            raise PaymentError("Payments are disabled")
        dec_amount = to_decimal(amount)
        if dec_amount <= 0:
            raise PaymentError("Amount must be positive")

        receiver = destination
        if settings.payments_mode == "moolre":
            try:
                MoolreService.transfer_channel(channel or "mtn")
                receiver = MoolreService.normalize_phone(destination)
            except MoolreError as exc:
                raise PaymentError(str(exc)) from exc

        intent = PaymentIntent(
            user_id=user_id,
            provider=settings.payments_mode,
            kind="withdrawal",
            provider_ref=_new_ref(),
            amount=dec_amount,
            currency=settings.payment_currency,
            status="pending",
            channel=channel or "mtn",
            extra={"destination": receiver} if receiver else None,
        )
        db.add(intent)
        db.flush()

        try:
            WalletService.withdraw(
                db,
                user_id,
                float(dec_amount),
                f"Withdrawal {intent.provider_ref}",
                commit=False,
            )
        except InsufficientBalanceError as exc:
            db.rollback()
            raise PaymentError(str(exc)) from exc

        if settings.payments_mode == "simulated":
            intent.status = "completed"
            intent.completed_at = _now()
            db.add(intent)
            db.commit()
            db.refresh(intent)
            return intent

        if settings.payments_mode == "moolre":
            try:
                body = MoolreService.initiate_transfer(intent, receiver_phone=receiver)
            except MoolreError as exc:
                PaymentService._fail_intent(db, intent, commit=True)
                raise PaymentError(str(exc), reference=intent.provider_ref) from exc
            extra = _extra(intent)
            extra["provider_response_code"] = body.get("code")
            intent.extra = extra
            data = body.get("data") if isinstance(body.get("data"), dict) else None
            if data and is_tx_success(data):
                try:
                    verified = MoolreService.verified_success_data(
                        body,
                        expected_ref=intent.provider_ref,
                        expected_amount=dec_amount,
                        expected_account=settings.moolre_account_number,
                    )
                except MoolreError:
                    db.add(intent)
                    db.commit()
                    db.refresh(intent)
                    return intent
                txn_id = verified.get("transactionid")
                if txn_id is not None:
                    intent.provider_txn_id = str(txn_id)[:64]
                intent.status = "completed"
                intent.completed_at = _now()
            elif data and is_tx_failed(data):
                return PaymentService._fail_intent(db, intent, commit=True)
            db.add(intent)
            db.commit()
            db.refresh(intent)
            logger.info(
                "payment.withdrawal_pending id=%s ref=%s user=%s",
                intent.id,
                intent.provider_ref,
                user_id,
            )
            return intent

        db.commit()
        db.refresh(intent)
        return intent

    @staticmethod
    def verify_webhook_signature(raw_body: bytes, signature: str | None) -> bool:
        settings = get_settings()
        secret = settings.payment_webhook_secret or settings.payment_secret_key
        if not secret:
            if settings.payments_mode == "simulated" and not settings.is_production:
                return True
            return False
        if not signature:
            return False
        digest_sha512 = hmac.new(
            secret.encode("utf-8"), raw_body, hashlib.sha512
        ).hexdigest()
        digest_sha256 = hmac.new(
            secret.encode("utf-8"), raw_body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(digest_sha512, signature) or hmac.compare_digest(
            digest_sha256, signature
        )

    @staticmethod
    def verify_moolre_callback_secret(payload: dict[str, Any]) -> bool:
        secret = (get_settings().moolre_webhook_secret or "").strip()
        if not secret:
            return False
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        provided = str(data.get("secret") or payload.get("secret") or "")
        if not provided:
            return False
        return hmac.compare_digest(provided, secret)

    @staticmethod
    def _webhook_event_key(payload: dict[str, Any], reference: str) -> str:
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        txn_id = data.get("transactionid")
        if txn_id is not None and str(txn_id).strip():
            return str(txn_id).strip()[:128]
        ts = str(data.get("ts") or "")
        txstatus = str(data.get("txstatus") or payload.get("status") or "")
        digest = hashlib.sha256(
            f"{reference}:{ts}:{txstatus}".encode("utf-8")
        ).hexdigest()
        return digest[:128]

    @staticmethod
    def _claim_webhook_event(
        db: Session, *, provider: str, event_key: str, intent_id: str
    ) -> bool:
        try:
            with db.begin_nested():
                db.add(
                    PaymentWebhookEvent(
                        provider=provider,
                        event_key=event_key,
                        payment_intent_id=intent_id,
                    )
                )
                db.flush()
            return True
        except IntegrityError:
            return False

    @staticmethod
    def handle_webhook(db: Session, payload: dict[str, Any]) -> PaymentIntent | None:
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        reference = str(
            data.get("externalref")
            or data.get("reference")
            or payload.get("externalref")
            or payload.get("reference")
            or ""
        )
        if not reference:
            raise PaymentError("Missing payment reference")

        intent = PaymentService._lock_by_ref(db, reference)
        event_key = PaymentService._webhook_event_key(payload, reference)
        claimed = PaymentService._claim_webhook_event(
            db,
            provider=intent.provider or "moolre",
            event_key=event_key,
            intent_id=intent.id,
        )
        if not claimed:
            logger.info("payment.webhook_duplicate ref=%s event=%s", reference, event_key)
            return intent

        settings = get_settings()
        if settings.payments_mode == "moolre":
            if is_tx_failed(data):
                return PaymentService._fail_intent(db, intent, commit=True)
            try:
                verified = PaymentService.verify_payment_reference(
                    db, reference, commit=False
                )
            except PaymentError as exc:
                message = str(exc).lower()
                logger.warning("payment.webhook_verify_failed ref=%s", reference)
                if "mismatch" in message:
                    db.commit()
                    return intent
                db.rollback()
                raise
            db.commit()
            db.refresh(verified)
            return verified

        event = str(payload.get("event") or data.get("status") or "")
        success = event in {
            "charge.success",
            "success",
            "successful",
            "completed",
            "transfer.success",
        }
        failed = event in {"charge.failed", "failed", "transfer.failed"}
        if failed:
            return PaymentService._fail_intent(db, intent, commit=True)
        if not success and intent.provider != "simulated":
            db.commit()
            return intent
        return PaymentService.complete_intent(db, intent)

    @staticmethod
    def get_by_ref(db: Session, reference: str) -> PaymentIntent | None:
        return (
            db.query(PaymentIntent)
            .filter(PaymentIntent.provider_ref == reference)
            .first()
        )

    @staticmethod
    def get_owned_and_refresh(
        db: Session, *, reference: str, user_id: str, is_admin: bool = False
    ) -> PaymentIntent:
        intent = PaymentService.get_by_ref(db, reference)
        if not intent or (intent.user_id != user_id and not is_admin):
            raise PaymentError("not_found")
        if _extra(intent).get("otp_required"):
            return intent
        if intent.provider == "nowpayments" and intent.status in PENDING_STATUSES:
            try:
                intent = PaymentService.reconcile_nowpayments(db, intent)
            except PaymentError:
                db.rollback()
                intent = PaymentService.get_by_ref(db, reference) or intent
            return intent
        if (
            get_settings().payments_mode == "moolre"
            and intent.status in PENDING_STATUSES
        ):
            try:
                intent = PaymentService.verify_payment_reference(db, reference)
            except PaymentError:
                db.rollback()
                intent = PaymentService.get_by_ref(db, reference) or intent
        return intent

    @staticmethod
    def _requests_crypto(
        channel: str | None, network: str | None, provider: str | None
    ) -> bool:
        prov = (provider or "").strip().lower()
        if prov and prov not in {"moolre", "nowpayments"}:
            raise PaymentError("Unsupported payment provider")
        crypto = looks_like_crypto(channel, network)
        if prov == "moolre" and crypto:
            raise PaymentError("Mobile money cannot process a crypto deposit")
        if prov == "nowpayments":
            return True
        return crypto

    @staticmethod
    def initiate_crypto_deposit(
        db: Session,
        *,
        user_id: str,
        amount: float,
        channel: str | None,
        network: str | None,
    ) -> PaymentIntent:
        settings = get_settings()
        if settings.payments_mode == "disabled":
            raise PaymentError("Payments are disabled")
        try:
            NowPaymentsService.assert_ready()
            asset = resolve_asset(channel, network)
        except NowPaymentsError as exc:
            raise PaymentError(str(exc), status_code=exc.status_code) from exc

        try:
            raw_amount = Decimal(str(amount))
        except Exception as exc:
            raise PaymentError("Enter a valid amount") from exc
        if raw_amount != raw_amount.quantize(Decimal("0.01")):
            raise PaymentError("Amount must have at most 2 decimal places")
        dec_amount = to_decimal(amount)
        if dec_amount < Decimal("1.00"):
            raise PaymentError("Minimum deposit is 1.00")
        if dec_amount > Decimal("50000.00"):
            raise PaymentError("Maximum deposit is 50000.00")

        price_currency = settings.nowpayments_price_currency_code()
        WalletService._lock_user(db, user_id)
        existing = PaymentService._reusable_crypto_intent(
            db,
            user_id=user_id,
            amount=dec_amount,
            channel=asset.channel,
            network=asset.network,
        )
        if existing:
            db.commit()
            db.refresh(existing)
            return existing

        intent = PaymentIntent(
            user_id=user_id,
            provider="nowpayments",
            kind="deposit",
            provider_ref=_new_ref(),
            amount=dec_amount,
            currency=settings.payment_currency,
            status="pending",
            channel=asset.channel,
            network=asset.network,
            pay_currency=asset.pay_currency,
            price_currency=price_currency,
            provider_status="waiting",
        )
        db.add(intent)
        db.flush()

        try:
            estimated = NowPaymentsService.estimate(
                dec_amount, price_currency, asset.pay_currency
            )
            min_crypto, min_fiat = NowPaymentsService.minimum_fiat(
                asset.pay_currency, price_currency
            )
            if min_fiat is not None and dec_amount < min_fiat:
                raise NowPaymentsError("Deposit is below the crypto provider minimum")
            if min_crypto is not None and estimated < min_crypto and min_fiat is None:
                raise NowPaymentsError("Deposit is below the crypto provider minimum")
            snapshot = NowPaymentsService.create_payment(
                price_amount=dec_amount,
                price_currency=price_currency,
                pay_currency=asset.pay_currency,
                order_id=intent.id,
                ipn_callback_url=settings.nowpayments_ipn_callback_url.strip(),
            )
        except NowPaymentsError as exc:
            intent.status = "failed"
            intent.completed_at = _now()
            intent.provider_status = "failed" if exc.timeout else intent.provider_status
            intent.extra = {
                "error": str(exc),
                "provider_timeout": exc.timeout,
            }
            db.add(intent)
            db.commit()
            raise PaymentError(
                str(exc), reference=intent.provider_ref, status_code=exc.status_code
            ) from exc

        if snapshot.order_id and snapshot.order_id != intent.id:
            intent.status = "failed"
            intent.completed_at = _now()
            intent.extra = {"error": "Provider order id mismatch"}
            db.add(intent)
            db.commit()
            raise PaymentError(
                "Crypto provider returned an unexpected order",
                reference=intent.provider_ref,
                status_code=502,
            )
        if (
            snapshot.price_currency
            and snapshot.price_currency != price_currency
        ) or (
            snapshot.price_amount is not None
            and to_decimal(snapshot.price_amount) != dec_amount
        ):
            intent.status = "failed"
            intent.completed_at = _now()
            intent.extra = {"error": "Provider price mismatch", "reconciliation_required": True}
            db.add(intent)
            db.commit()
            raise PaymentError(
                "Crypto provider returned an unexpected amount",
                reference=intent.provider_ref,
            )
        if snapshot.pay_currency and snapshot.pay_currency != asset.pay_currency:
            intent.status = "failed"
            intent.completed_at = _now()
            intent.extra = {"error": "Provider currency mismatch"}
            db.add(intent)
            db.commit()
            raise PaymentError(
                "Crypto provider returned an unexpected currency",
                reference=intent.provider_ref,
            )

        PaymentService._store_crypto_snapshot(intent, snapshot)
        mapped = betplus_status_for(snapshot.payment_status) or "pending"
        if mapped in {"failed", "expired"}:
            intent.status = mapped
            intent.completed_at = _now()
        elif mapped == "processing":
            intent.status = "processing"
        else:
            intent.status = "pending"
        db.add(intent)
        db.commit()
        db.refresh(intent)
        logger.info(
            "payment.crypto_pending id=%s ref=%s currency=%s network=%s",
            intent.id,
            intent.provider_ref,
            asset.currency,
            asset.network,
        )
        return intent

    @staticmethod
    def _reusable_crypto_intent(
        db: Session,
        *,
        user_id: str,
        amount,
        channel: str,
        network: str,
    ) -> PaymentIntent | None:
        rows = (
            db.query(PaymentIntent)
            .filter(
                PaymentIntent.user_id == user_id,
                PaymentIntent.provider == "nowpayments",
                PaymentIntent.kind == "deposit",
                PaymentIntent.status.in_(tuple(PENDING_STATUSES)),
                PaymentIntent.amount == amount,
                PaymentIntent.channel == channel,
                PaymentIntent.network == network,
            )
            .order_by(PaymentIntent.created_at.desc())
            .all()
        )
        now = _now()
        for intent in rows:
            extra = _extra(intent)
            if extra.get("reconciliation_required"):
                continue
            if not intent.provider_txn_id or not intent.pay_address:
                raise PaymentError(
                    "A crypto deposit is already being created",
                    status_code=409,
                    reference=intent.provider_ref,
                )
            expires = intent.expires_at
            if expires is not None:
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=timezone.utc)
                if expires <= now:
                    continue
            else:
                created = intent.created_at
                if created is not None:
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=timezone.utc)
                    if created <= now - timedelta(hours=1):
                        continue
            return intent
        return None

    @staticmethod
    def _store_crypto_snapshot(intent: PaymentIntent, snapshot: NowPaymentSnapshot) -> None:
        intent.provider_txn_id = snapshot.payment_id[:64]
        intent.provider_status = snapshot.payment_status[:32]
        if snapshot.pay_address and not intent.pay_address:
            intent.pay_address = snapshot.pay_address[:128]
        if snapshot.pay_amount is not None and (
            not intent.pay_amount or snapshot.payment_status == "finished"
        ):
            intent.pay_amount = format(snapshot.pay_amount, "f")[:64]
        if snapshot.pay_currency and not intent.pay_currency:
            intent.pay_currency = snapshot.pay_currency[:32]
        if snapshot.price_currency and not intent.price_currency:
            intent.price_currency = snapshot.price_currency[:16]
        if snapshot.expires_at:
            intent.expires_at = snapshot.expires_at
        extra = _extra(intent)
        extra.update(
            {
                "provider_payment_id": snapshot.payment_id,
                "provider_status": snapshot.payment_status,
                "provider_status_rank": PROVIDER_STATUS_RANK.get(
                    snapshot.payment_status, 0
                ),
                "provider_currency": snapshot.pay_currency,
                "provider_network": intent.network,
                "provider_pay_address": snapshot.pay_address,
                "provider_pay_amount": (
                    format(snapshot.pay_amount, "f") if snapshot.pay_amount is not None else None
                ),
                "provider_price_amount": (
                    format(snapshot.price_amount, "f")
                    if snapshot.price_amount is not None
                    else None
                ),
                "provider_price_currency": snapshot.price_currency,
                "provider_created_at": snapshot.created_at,
                "actually_paid": (
                    format(snapshot.actually_paid, "f")
                    if snapshot.actually_paid is not None
                    else None
                ),
            }
        )
        extra.pop("error", None)
        intent.extra = extra

    @staticmethod
    def reconcile_nowpayments(db: Session, intent: PaymentIntent) -> PaymentIntent:
        if intent.provider != "nowpayments":
            return intent
        if intent.status in TERMINAL_STATUSES:
            return intent
        if not intent.provider_txn_id:
            return intent
        try:
            snapshot = NowPaymentsService.get_payment(intent.provider_txn_id)
        except NowPaymentsError as exc:
            raise PaymentError(str(exc), status_code=exc.status_code) from exc
        return PaymentService._apply_nowpayments_snapshot(db, intent, snapshot, commit=True)

    @staticmethod
    def handle_nowpayments_ipn(db: Session, payload: dict[str, Any]) -> PaymentIntent:
        try:
            snapshot = parse_payment(payload)
        except NowPaymentsError as exc:
            raise PaymentError("Invalid crypto callback") from exc

        intent = PaymentService._find_nowpayments_intent(db, snapshot)
        if intent.status in TERMINAL_STATUSES:
            return intent
        event_key = f"{snapshot.payment_id}:{snapshot.payment_status}"[:128]
        claimed = PaymentService._claim_webhook_event(
            db,
            provider="nowpayments",
            event_key=event_key,
            intent_id=intent.id,
        )
        if not claimed:
            logger.info(
                "payment.crypto_webhook_duplicate id=%s status=%s",
                intent.id,
                snapshot.payment_status,
            )
            return intent
        return PaymentService._apply_nowpayments_snapshot(
            db, intent, snapshot, commit=True
        )

    @staticmethod
    def _find_nowpayments_intent(db: Session, snapshot: NowPaymentSnapshot) -> PaymentIntent:
        intent = None
        if snapshot.order_id:
            intent = (
                db.query(PaymentIntent)
                .filter(PaymentIntent.id == snapshot.order_id)
                .with_for_update()
                .first()
            )
        if intent is None:
            intent = (
                db.query(PaymentIntent)
                .filter(
                    PaymentIntent.provider == "nowpayments",
                    PaymentIntent.provider_txn_id == snapshot.payment_id,
                )
                .with_for_update()
                .first()
            )
        if not intent or intent.provider != "nowpayments":
            raise PaymentError("Unknown payment reference")
        if snapshot.order_id and snapshot.order_id != intent.id:
            raise PaymentError("Payment reference mismatch")
        if intent.provider_txn_id and intent.provider_txn_id != snapshot.payment_id:
            raise PaymentError("Payment reference mismatch")
        return intent

    @staticmethod
    def _apply_nowpayments_snapshot(
        db: Session,
        intent: PaymentIntent,
        snapshot: NowPaymentSnapshot,
        *,
        commit: bool,
    ) -> PaymentIntent:
        locked = (
            db.query(PaymentIntent)
            .filter(PaymentIntent.id == intent.id)
            .with_for_update()
            .first()
        )
        if not locked:
            raise PaymentError("Unknown payment reference")
        if locked.status in TERMINAL_STATUSES:
            return locked

        extra = _extra(locked)
        previous_rank = int(extra.get("provider_status_rank") or 0)
        incoming_rank = PROVIDER_STATUS_RANK.get(snapshot.payment_status, 0)
        if incoming_rank and previous_rank and incoming_rank < previous_rank:
            logger.info(
                "payment.crypto_status_replay id=%s status=%s",
                locked.id,
                snapshot.payment_status,
            )
            if commit:
                db.commit()
            return locked

        if snapshot.pay_currency and locked.pay_currency and snapshot.pay_currency != locked.pay_currency:
            return PaymentService._mark_crypto_review(
                db,
                locked,
                reason="currency_mismatch",
                snapshot=snapshot,
                commit=commit,
            )
        if snapshot.order_id and snapshot.order_id != locked.id:
            return PaymentService._mark_crypto_review(
                db,
                locked,
                reason="order_mismatch",
                snapshot=snapshot,
                commit=commit,
            )

        PaymentService._store_crypto_snapshot(locked, snapshot)
        mapped = betplus_status_for(snapshot.payment_status)
        if mapped is None:
            extra = _extra(locked)
            extra["unknown_provider_status"] = snapshot.payment_status
            locked.extra = extra
            db.add(locked)
            if commit:
                db.commit()
                db.refresh(locked)
            return locked

        if snapshot.payment_status == "partially_paid":
            return PaymentService._mark_crypto_review(
                db,
                locked,
                reason="partially_paid",
                snapshot=snapshot,
                commit=commit,
            )
        if mapped in {"failed", "expired"}:
            return PaymentService._fail_intent(db, locked, status=mapped, commit=commit)
        if mapped == "completed":
            return PaymentService._credit_crypto_if_amounts_match(
                db, locked, snapshot, commit=commit
            )
        if mapped == "processing" and locked.status == "pending":
            locked.status = "processing"
        db.add(locked)
        if commit:
            db.commit()
            db.refresh(locked)
        return locked

    @staticmethod
    def _credit_crypto_if_amounts_match(
        db: Session,
        intent: PaymentIntent,
        snapshot: NowPaymentSnapshot,
        *,
        commit: bool,
    ) -> PaymentIntent:
        """Credit the wallet only for a finished payment whose fiat price matches."""
        if snapshot.payment_status != "finished":
            return intent
        price_currency = (intent.price_currency or "").strip().lower()
        wallet_currency = (intent.currency or "").strip().lower()
        mismatch = None
        if snapshot.price_currency != price_currency or price_currency != wallet_currency:
            mismatch = "price_currency"
        elif snapshot.price_amount is None or to_decimal(snapshot.price_amount) != to_decimal(
            intent.amount
        ):
            mismatch = "price_amount"
        elif not snapshot.pay_currency or snapshot.pay_currency != (intent.pay_currency or ""):
            mismatch = "pay_currency"
        elif snapshot.pay_amount is None or snapshot.actually_paid is None:
            mismatch = "crypto_amount_missing"
        elif snapshot.actually_paid < snapshot.pay_amount:
            mismatch = "underpaid"
        if mismatch:
            return PaymentService._mark_crypto_review(
                db, intent, reason=mismatch, snapshot=snapshot, commit=commit
            )
        return PaymentService._credit_crypto_deposit(
            db, intent, provider_txn_id=snapshot.payment_id, commit=commit
        )

    @staticmethod
    def _credit_crypto_deposit(
        db: Session,
        intent: PaymentIntent,
        *,
        provider_txn_id: str,
        commit: bool,
    ) -> PaymentIntent:
        locked = (
            db.query(PaymentIntent)
            .filter(PaymentIntent.id == intent.id)
            .with_for_update()
            .first()
        )
        if not locked:
            raise PaymentError("Payment not found")
        if locked.status == "completed":
            return locked
        if locked.status in {"failed", "cancelled", "expired", "reversed"}:
            return locked
        if locked.status not in PENDING_STATUSES:
            return locked

        extra = _extra(locked)
        extra["reconciliation_required"] = False
        locked.extra = extra
        locked.status = "processing"
        db.flush()
        WalletService.deposit(
            db,
            locked.user_id,
            float(locked.amount),
            f"Crypto deposit {locked.provider_ref}",
            track_referral=True,
            commit=False,
        )
        extra = _extra(locked)
        extra["wallet_credited"] = True
        extra["reconciliation_required"] = False
        locked.extra = extra
        locked.status = "completed"
        locked.completed_at = _now()
        locked.provider_status = "finished"
        locked.provider_txn_id = str(provider_txn_id)[:64]
        db.add(locked)
        db.flush()
        logger.info(
            "payment.crypto_completed id=%s ref=%s",
            locked.id,
            locked.provider_ref,
        )
        if commit:
            db.commit()
            db.refresh(locked)
        return locked

    @staticmethod
    def _mark_crypto_review(
        db: Session,
        intent: PaymentIntent,
        *,
        reason: str,
        snapshot: NowPaymentSnapshot,
        commit: bool,
    ) -> PaymentIntent:
        PaymentService._store_crypto_snapshot(intent, snapshot)
        extra = _extra(intent)
        extra["reconciliation_required"] = True
        extra["reconciliation_reason"] = reason
        intent.extra = extra
        if intent.status == "pending":
            intent.status = "processing"
        db.add(intent)
        db.flush()
        logger.warning(
            "payment.crypto_review id=%s reason=%s provider_status=%s",
            intent.id,
            reason,
            snapshot.payment_status,
        )
        if commit:
            db.commit()
            db.refresh(intent)
        return intent
