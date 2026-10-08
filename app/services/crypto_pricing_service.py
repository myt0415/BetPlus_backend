from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from app.core.config import get_settings


class CryptoPricingService:
    """Backend-owned fiat/crypto pricing for blockchain deposits.

    Production deployments must set CRYPTO_BTC_GHS_RATE and/or
    CRYPTO_USDT_GHS_RATE from the configured exchange source. The defaults are
    intentionally conservative and are meant only for local/test use.
    """

    @staticmethod
    def get_rate(currency: str, fiat_currency: str = "GHS") -> Decimal:
        settings = get_settings()
        currency_key = (currency or "").strip().lower()
        fiat = (fiat_currency or "GHS").strip().upper()
        if currency_key == "btc":
            rate = Decimal(str(settings.crypto_btc_ghs_rate or "1"))
        elif currency_key == "usdt":
            rate = Decimal(str(settings.crypto_usdt_ghs_rate or "1"))
        else:
            raise ValueError(f"Unsupported crypto currency: {currency}")

        if fiat != "GHS":
            return rate
        return rate

    @staticmethod
    def calculate_expected_amount(
        fiat_amount: Decimal,
        *,
        crypto_currency: str,
        fiat_currency: str = "GHS",
    ) -> Decimal:
        if fiat_amount <= 0:
            return Decimal("0")
        rate = CryptoPricingService.get_rate(crypto_currency, fiat_currency)
        if rate <= 0:
            raise ValueError("Crypto conversion rate must be positive")
        amount = fiat_amount / rate
        if crypto_currency.lower() == "btc":
            quantum = Decimal("0.00000001")
        else:
            quantum = Decimal("0.000001")
        return amount.quantize(quantum, rounding=ROUND_HALF_UP)
