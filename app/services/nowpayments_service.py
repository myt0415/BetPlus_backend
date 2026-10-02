"""NOWPayments crypto provider.

BetPlus remains authoritative for the wallet ledger. This module only talks to
NOWPayments, maps its currency codes, and checks IPN signatures.

Pay-currency tickers are the identifiers NOWPayments documents for these
networks (btc, usdttrc20, usdterc20). They are not accepted from the client.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

import httpx

from app.core.config import get_settings

logger = logging.getLogger("app.payments.nowpayments")

PROVIDER = "nowpayments"

PRODUCTION_BASE_URL = "https://api.nowpayments.io"
SANDBOX_BASE_URL = "https://api.sandbox.nowpayments.io"
ALLOWED_BASE_URLS = frozenset({PRODUCTION_BASE_URL, SANDBOX_BASE_URL})

# Backend-owned frontend currency/network → NOWPayments pay_currency.
# USDT without a network is intentionally absent.
_ASSETS: dict[tuple[str, str], str] = {
    ("btc", "bitcoin"): "btc",
    ("usdt", "trc20"): "usdttrc20",
    ("usdt", "erc20"): "usdterc20",
}

_CHANNEL_ALIASES = {
    "btc": "btc",
    "bitcoin": "btc",
    "usdt": "usdt",
    "tether": "usdt",
}

_COMBINED_CHANNELS = {
    "usdttrc20": ("usdt", "trc20"),
    "usdt-trc20": ("usdt", "trc20"),
    "usdt_trc20": ("usdt", "trc20"),
    "usdterc20": ("usdt", "erc20"),
    "usdt-erc20": ("usdt", "erc20"),
    "usdt_erc20": ("usdt", "erc20"),
}

# Application-level statuses. HTTP 200 is not one of them.
TERMINAL_PROVIDER_STATUSES = frozenset(
    {"finished", "failed", "expired", "refunded"}
)
PROVIDER_STATUS_RANK = {
    "waiting": 10,
    "confirming": 20,
    "confirmed": 30,
    "sending": 40,
    "partially_paid": 50,
    "finished": 60,
    "failed": 60,
    "expired": 60,
    "refunded": 60,
}

# waiting → pending, confirming/confirmed/sending → processing,
# finished → completed (credit), failed → failed, expired → expired.
BETPLUS_STATUS = {
    "waiting": "pending",
    "confirming": "processing",
    "confirmed": "processing",
    "sending": "processing",
    "partially_paid": "processing",
    "finished": "completed",
    "failed": "failed",
    "expired": "expired",
    "refunded": "failed",
}

_SECRETISH = ("key", "secret", "token", "password", "authorization", "api-key")


class NowPaymentsError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int = 400,
        timeout: bool = False,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.timeout = timeout


@dataclass(frozen=True)
class CryptoAsset:
    currency: str
    network: str
    pay_currency: str
    channel: str


@dataclass(frozen=True)
class NowPaymentSnapshot:
    payment_id: str
    payment_status: str
    order_id: str | None
    pay_address: str | None
    pay_amount: Decimal | None
    actually_paid: Decimal | None
    pay_currency: str | None
    price_amount: Decimal | None
    price_currency: str | None
    expires_at: datetime | None
    created_at: str | None


def _normalize_token(value: str | None) -> str:
    return (value or "").strip().lower().replace(" ", "")


def looks_like_crypto(channel: str | None, network: str | None = None) -> bool:
    token = _normalize_token(channel)
    if token in _CHANNEL_ALIASES or token in _COMBINED_CHANNELS:
        return True
    net = _normalize_token(network)
    return net in {"bitcoin", "trc20", "erc20"} and token in {"btc", "usdt"}


def resolve_asset(channel: str | None, network: str | None) -> CryptoAsset:
    """Map a BetPlus channel/network pair onto one allowlisted NOWPayments ticker."""
    token = _normalize_token(channel)
    net = _normalize_token(network)
    if token in _COMBINED_CHANNELS:
        currency, combined_net = _COMBINED_CHANNELS[token]
        if net and net not in {combined_net, "bitcoin"}:
            raise NowPaymentsError("Unsupported crypto network")
        if currency == "btc":
            net = "bitcoin"
        else:
            net = combined_net
        token = currency
    else:
        token = _CHANNEL_ALIASES.get(token, "")
        if token == "btc":
            if net in {"", "bitcoin", "btc"}:
                net = "bitcoin"
            else:
                raise NowPaymentsError("Unsupported BTC network")
        elif token == "usdt":
            if not net:
                raise NowPaymentsError(
                    "USDT network is required. Use trc20 or erc20."
                )
            if net not in {"trc20", "erc20"}:
                raise NowPaymentsError("Unsupported USDT network. Use trc20 or erc20.")
        else:
            raise NowPaymentsError("Unsupported crypto currency")

    pay_currency = _ASSETS.get((token, net))
    if not pay_currency:
        raise NowPaymentsError("Unsupported crypto currency")
    return CryptoAsset(
        currency="BTC" if token == "btc" else "USDT",
        network=net,
        pay_currency=pay_currency,
        channel=token,
    )


def betplus_status_for(provider_status: str) -> str | None:
    return BETPLUS_STATUS.get((provider_status or "").strip().lower())


def _decimal(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _parse_datetime(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _public_message(raw: object, fallback: str) -> str:
    text = " ".join(str(raw or "").split())[:180]
    lowered = text.lower()
    if not text or any(marker in lowered for marker in _SECRETISH):
        return fallback
    return text


def canonical_ipn_message(payload: dict[str, Any]) -> str:
    """Recursively sort keys and serialize like JSON.stringify.

    NOWPayments signs HMAC-SHA512 over the sorted JSON object, not the raw
    body bytes. Nested objects (for example fee) are sorted as well.
    """

    def encode(value: Any) -> str:
        if isinstance(value, dict):
            items = ",".join(
                f"{json.dumps(str(key), ensure_ascii=False)}:{encode(value[key])}"
                for key in sorted(value, key=lambda item: str(item))
            )
            return "{" + items + "}"
        if isinstance(value, list):
            return "[" + ",".join(encode(item) for item in value) + "]"
        if isinstance(value, Decimal):
            text = format(value, "f")
            if "." in text:
                text = text.rstrip("0").rstrip(".")
            return text or "0"
        if isinstance(value, bool):
            return "true" if value else "false"
        if value is None:
            return "null"
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
        if isinstance(value, float):
            return encode(Decimal(str(value)))
        return json.dumps(value, ensure_ascii=False)

    return encode(payload)


def verify_ipn_signature(raw_body: bytes, signature: str | None) -> bool:
    secret = (get_settings().nowpayments_ipn_secret or "").strip()
    if not secret or not signature or not str(signature).strip():
        return False
    try:
        payload = json.loads(raw_body.decode("utf-8"), parse_float=Decimal)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    digest = hmac.new(
        secret.encode("utf-8"),
        canonical_ipn_message(payload).encode("utf-8"),
        hashlib.sha512,
    ).hexdigest()
    return hmac.compare_digest(digest, str(signature).strip())


def parse_payment(body: dict[str, Any]) -> NowPaymentSnapshot:
    payment_id = body.get("payment_id")
    status = str(body.get("payment_status") or "").strip().lower()
    if payment_id is None or str(payment_id).strip() == "" or not status:
        raise NowPaymentsError("Crypto provider returned an invalid payment")
    payment_id_text = str(payment_id).strip()
    if not payment_id_text.isdigit():
        raise NowPaymentsError("Crypto provider returned an invalid payment")
    order_id = body.get("order_id")
    pay_currency = body.get("pay_currency")
    price_currency = body.get("price_currency")
    return NowPaymentSnapshot(
        payment_id=payment_id_text,
        payment_status=status,
        order_id=str(order_id).strip() if order_id not in (None, "") else None,
        pay_address=(str(body.get("pay_address")).strip() or None)
        if body.get("pay_address")
        else None,
        pay_amount=_decimal(body.get("pay_amount")),
        actually_paid=_decimal(body.get("actually_paid")),
        pay_currency=str(pay_currency).strip().lower() if pay_currency else None,
        price_amount=_decimal(body.get("price_amount")),
        price_currency=str(price_currency).strip().lower() if price_currency else None,
        expires_at=_parse_datetime(
            body.get("expiration_estimate_date") or body.get("valid_until")
        ),
        created_at=str(body.get("created_at")).strip()
        if body.get("created_at")
        else None,
    )


class NowPaymentsService:
    @staticmethod
    def assert_ready() -> None:
        settings = get_settings()
        if not settings.nowpayments_enabled:
            raise NowPaymentsError("Crypto payments are not enabled")
        if settings.payments_mode == "disabled":
            raise NowPaymentsError("Payments are disabled")
        if not (settings.nowpayments_api_key or "").strip():
            raise NowPaymentsError(
                "Crypto payments are not configured", status_code=503
            )
        if not (settings.nowpayments_ipn_secret or "").strip():
            raise NowPaymentsError(
                "Crypto payments are not configured", status_code=503
            )
        if not (settings.nowpayments_ipn_callback_url or "").strip():
            raise NowPaymentsError(
                "Crypto payment callbacks are not configured", status_code=503
            )
        price = settings.nowpayments_price_currency_code()
        wallet = (settings.payment_currency or "").strip().lower()
        if price != wallet:
            raise NowPaymentsError(
                "Crypto deposits require NOWPAYMENTS_PRICE_CURRENCY to match "
                "PAYMENT_CURRENCY. BetPlus does not convert currencies.",
                status_code=503,
            )

    @staticmethod
    def _headers() -> dict[str, str]:
        key = (get_settings().nowpayments_api_key or "").strip()
        if not key:
            raise NowPaymentsError(
                "Crypto payments are not configured", status_code=503
            )
        return {"x-api-key": key, "Content-Type": "application/json"}

    @staticmethod
    def _request(
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        settings = get_settings()
        base = settings.nowpayments_base_url
        parsed = urlparse(base)
        if parsed.scheme != "https" or base not in ALLOWED_BASE_URLS:
            raise NowPaymentsError(
                "Crypto provider URL is not allowed", status_code=503
            )
        url = f"{base}{path}"
        try:
            response = httpx.request(
                method,
                url,
                params=params,
                json=json_body,
                headers=NowPaymentsService._headers(),
                timeout=20.0,
                follow_redirects=False,
            )
        except httpx.TimeoutException as exc:
            logger.warning("nowpayments.timeout path=%s", path)
            raise NowPaymentsError(
                "Crypto provider timed out", status_code=503, timeout=True
            ) from exc
        except httpx.HTTPError as exc:
            logger.warning("nowpayments.http_error path=%s", path)
            raise NowPaymentsError(
                "Crypto provider is unavailable", status_code=503
            ) from exc

        if response.status_code in {401, 403}:
            logger.warning("nowpayments.auth_failed path=%s http=%s", path, response.status_code)
            raise NowPaymentsError(
                "Crypto payments are not configured", status_code=503
            )
        try:
            body = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise NowPaymentsError(
                "Crypto provider returned an invalid response", status_code=502
            ) from exc
        if not isinstance(body, dict):
            raise NowPaymentsError(
                "Crypto provider returned an invalid response", status_code=502
            )
        if response.status_code >= 500:
            logger.warning("nowpayments.unavailable path=%s http=%s", path, response.status_code)
            raise NowPaymentsError("Crypto provider is unavailable", status_code=503)
        if response.status_code >= 400:
            code = str(body.get("code") or body.get("statusCode") or "")
            message = _public_message(
                body.get("message"), "Crypto provider rejected the payment"
            )
            lowered = message.lower()
            if "minimal" in lowered or "min" in code.lower() or "amount" in lowered and "less" in lowered:
                raise NowPaymentsError(
                    "Deposit is below the crypto provider minimum", status_code=400
                )
            if "currenc" in lowered:
                raise NowPaymentsError(
                    "This deposit currency is not supported by the crypto provider",
                    status_code=400,
                )
            logger.info(
                "nowpayments.rejected path=%s http=%s code=%s",
                path,
                response.status_code,
                code or None,
            )
            raise NowPaymentsError(message, status_code=400)
        return body

    @staticmethod
    def estimate(amount: Decimal, price_currency: str, pay_currency: str) -> Decimal:
        body = NowPaymentsService._request(
            "GET",
            "/v1/estimate",
            params={
                "amount": format(amount, "f"),
                "currency_from": price_currency,
                "currency_to": pay_currency,
            },
        )
        estimated = _decimal(body.get("estimated_amount"))
        if estimated is None or estimated <= 0:
            raise NowPaymentsError(
                "Crypto provider could not estimate this deposit", status_code=502
            )
        return estimated

    @staticmethod
    def minimum_fiat(pay_currency: str, price_currency: str) -> tuple[Decimal | None, Decimal | None]:
        """Return (min crypto amount, min fiat equivalent) for the pair.

        NOWPayments GET /v1/min-amount uses currency_from as the pay currency
        and fiat_equivalent to express the minimum in the price currency.
        """
        body = NowPaymentsService._request(
            "GET",
            "/v1/min-amount",
            params={
                "currency_from": pay_currency,
                "currency_to": price_currency,
                "fiat_equivalent": price_currency,
            },
        )
        return _decimal(body.get("min_amount")), _decimal(body.get("fiat_equivalent"))

    @staticmethod
    def create_payment(
        *,
        price_amount: Decimal,
        price_currency: str,
        pay_currency: str,
        order_id: str,
        ipn_callback_url: str,
    ) -> NowPaymentSnapshot:
        payload = {
            "price_amount": float(price_amount),
            "price_currency": price_currency,
            "pay_currency": pay_currency,
            "ipn_callback_url": ipn_callback_url,
            "order_id": order_id,
            "order_description": "BetPlus wallet deposit",
            "is_fee_paid_by_user": False,
        }
        body = NowPaymentsService._request("POST", "/v1/payment", json_body=payload)
        snapshot = parse_payment(body)
        if snapshot.payment_status in {"failed", "expired", "refunded"}:
            raise NowPaymentsError("Crypto provider could not create the payment")
        if not snapshot.pay_address:
            raise NowPaymentsError(
                "Crypto provider did not return a payment address", status_code=502
            )
        return snapshot

    @staticmethod
    def get_payment(payment_id: str) -> NowPaymentSnapshot:
        if not str(payment_id).isdigit():
            raise NowPaymentsError("Unknown crypto payment", status_code=404)
        body = NowPaymentsService._request("GET", f"/v1/payment/{payment_id}")
        return parse_payment(body)
