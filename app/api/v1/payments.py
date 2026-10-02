from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
import json
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.config import get_settings
from app.db.session import get_db
from app.schemas import PaymentInitiateIn, PaymentIntentOut, PaymentOtpIn
from app.services.idempotency_service import IdempotencyService
from app.services.nowpayments_service import verify_ipn_signature
from app.services.payment_service import PaymentError, PaymentService
from app.services.rate_limit import enforce_rate_limit

router = APIRouter()


def _intent_out(intent) -> PaymentIntentOut:
    extra = dict(intent.extra or {})
    otp_required = bool(extra.get("otp_required"))
    return PaymentIntentOut.model_validate(intent).model_copy(
        update={
            "otp_required": otp_required,
            "next_action": "otp" if otp_required else None,
            "transaction_id": intent.id,
            "payment_id": intent.provider_txn_id,
            "review_required": bool(extra.get("reconciliation_required")),
        }
    )


def _payment_error_detail(exc: PaymentError) -> dict[str, str]:
    detail = {"message": str(exc), "code": "PAYMENT_FAILED"}
    if exc.reference:
        detail["reference"] = exc.reference
    return detail


@router.post("/webhooks/nowpayments")
async def nowpayments_webhook(request: Request, db: Session = Depends(get_db)):
    """NOWPayments IPN. Signature is checked before any wallet credit."""
    enforce_rate_limit(bucket="nowpayments-ipn", limit=120, window_seconds=60)
    raw = await request.body()
    signature = request.headers.get("x-nowpayments-sig")
    if not verify_ipn_signature(raw, signature):
        raise HTTPException(status_code=400, detail="Invalid webhook signature")
    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON")
    try:
        intent = PaymentService.handle_nowpayments_ipn(db, payload)
    except PaymentError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return Response(
        content=json.dumps(
            {
                "ok": True,
                "reference": intent.provider_ref,
                "status": intent.status,
            }
        ),
        media_type="application/json",
        status_code=200,
    )


@router.post("/deposits", response_model=PaymentIntentOut, status_code=201)
def initiate_deposit(
    payload: PaymentInitiateIn,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    enforce_rate_limit(bucket=f"pay:{current_user.id}", limit=20, window_seconds=60)
    cached = IdempotencyService.replay_or_begin(
        db,
        user_id=current_user.id,
        key=idempotency_key,
        method="POST",
        path="/api/v1/payments/deposits",
        payload=payload.model_dump(),
    )
    if cached:
        if not cached.status_code:
            raise HTTPException(status_code=409, detail="Request already in progress")
        return cached.response_body
    try:
        intent = PaymentService.initiate_deposit(
            db,
            user_id=current_user.id,
            amount=payload.amount,
            channel=payload.channel,
            email=current_user.email,
            payer_phone=payload.phone or payload.payer_phone or current_user.phone,
            network=payload.network,
            provider=payload.provider,
        )
        out = _intent_out(intent)
        IdempotencyService.store(
            db,
            user_id=current_user.id,
            key=idempotency_key,
            method="POST",
            path="/api/v1/payments/deposits",
            payload=payload.model_dump(),
            status_code=201,
            response_body=out,
        )
        db.commit()
        return out
    except PaymentError as exc:
        IdempotencyService.clear_in_progress(
            db, user_id=current_user.id, key=idempotency_key
        )
        db.commit()
        raise HTTPException(
            status_code=exc.status_code, detail=_payment_error_detail(exc)
        ) from exc


@router.post("/withdrawals", response_model=PaymentIntentOut, status_code=201)
def initiate_withdrawal(
    payload: PaymentInitiateIn,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    enforce_rate_limit(bucket=f"pay:{current_user.id}", limit=20, window_seconds=60)
    cached = IdempotencyService.replay_or_begin(
        db,
        user_id=current_user.id,
        key=idempotency_key,
        method="POST",
        path="/api/v1/payments/withdrawals",
        payload=payload.model_dump(),
    )
    if cached:
        if not cached.status_code:
            raise HTTPException(status_code=409, detail="Request already in progress")
        return cached.response_body
    try:
        intent = PaymentService.initiate_withdrawal(
            db,
            user_id=current_user.id,
            amount=payload.amount,
            channel=payload.channel,
            destination=payload.destination or payload.phone or payload.payer_phone,
        )
        out = _intent_out(intent)
        IdempotencyService.store(
            db,
            user_id=current_user.id,
            key=idempotency_key,
            method="POST",
            path="/api/v1/payments/withdrawals",
            payload=payload.model_dump(),
            status_code=201,
            response_body=out,
        )
        db.commit()
        return out
    except PaymentError as exc:
        IdempotencyService.clear_in_progress(
            db, user_id=current_user.id, key=idempotency_key
        )
        db.commit()
        raise HTTPException(
            status_code=exc.status_code, detail=_payment_error_detail(exc)
        ) from exc
    except Exception as exc:
        IdempotencyService.clear_in_progress(
            db, user_id=current_user.id, key=idempotency_key
        )
        db.commit()
        raise HTTPException(status_code=400, detail="Withdrawal could not be processed") from exc


@router.post("/{reference}/otp", response_model=PaymentIntentOut)
def confirm_deposit_otp(
    reference: str,
    payload: PaymentOtpIn,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    enforce_rate_limit(bucket=f"pay:{current_user.id}", limit=20, window_seconds=60)
    try:
        intent = PaymentService.confirm_deposit_otp(
            db,
            reference=reference,
            user_id=current_user.id,
            otpcode=payload.otpcode,
            is_admin=bool(current_user.is_admin),
        )
    except PaymentError as exc:
        if str(exc) == "not_found":
            raise HTTPException(status_code=404, detail="Payment not found") from exc
        raise HTTPException(
            status_code=exc.status_code, detail=_payment_error_detail(exc)
        ) from exc
    return _intent_out(intent)


@router.get("/{reference}", response_model=PaymentIntentOut)
def get_payment(
    reference: str,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    enforce_rate_limit(
        bucket=f"paystatus:{current_user.id}", limit=60, window_seconds=60
    )
    try:
        intent = PaymentService.get_owned_and_refresh(
            db,
            reference=reference,
            user_id=current_user.id,
            is_admin=bool(current_user.is_admin),
        )
    except PaymentError as exc:
        if str(exc) == "not_found":
            raise HTTPException(status_code=404, detail="Payment not found") from exc
        raise HTTPException(
            status_code=exc.status_code, detail=_payment_error_detail(exc)
        ) from exc
    return _intent_out(intent)


@router.post("/webhook")
async def payment_webhook(
    request: Request,
    db: Session = Depends(get_db),
    x_paystack_signature: str | None = Header(default=None),
    x_webhook_signature: str | None = Header(default=None),
):
    raw = await request.body()
    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON")

    settings = get_settings()
    if settings.payments_mode == "moolre":
        if not PaymentService.verify_moolre_callback_secret(payload):
            raise HTTPException(status_code=400, detail="Invalid webhook signature")
    else:
        signature = x_paystack_signature or x_webhook_signature
        if not PaymentService.verify_webhook_signature(raw, signature):
            raise HTTPException(status_code=400, detail="Invalid webhook signature")
    try:
        intent = PaymentService.handle_webhook(db, payload)
    except PaymentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Response(
        content=json.dumps(
            {
                "ok": True,
                "reference": intent.provider_ref if intent else None,
                "status": intent.status if intent else None,
            }
        ),
        media_type="application/json",
        status_code=200,
    )
