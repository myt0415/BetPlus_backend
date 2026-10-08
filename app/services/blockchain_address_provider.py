from __future__ import annotations

import secrets
from decimal import Decimal


class BlockchainAddressProvider:
    """Abstraction for generating backend-owned blockchain deposit addresses.

    This project intentionally does not generate wallet private keys locally.
    Production deployments must integrate with a secure wallet service or HD/xpub
    provider; this default implementation is a safe stub for tests and local dev.
    """

    def __init__(self, *, provider_name: str = "mock"):
        self.provider_name = provider_name

    def create_deposit_address(self, currency: str, network: str, deposit_reference: str):
        normalized_currency = (currency or "").strip().upper()
        normalized_network = (network or "").strip().lower()
        if normalized_currency == "BTC":
            if normalized_network != "bitcoin":
                raise ValueError("Unsupported BTC network")
            return f"bc1q{deposit_reference.lower().replace('-', '')[:30]}{secrets.token_hex(5)}"
        if normalized_currency == "USDT":
            if normalized_network not in {"trc20", "erc20"}:
                raise ValueError("USDT network is required")
            if normalized_network == "trc20":
                prefix = "T"
            else:
                prefix = "0x"
            suffix = secrets.token_hex(12)
            return f"{prefix}{deposit_reference.lower().replace('-', '')[:18]}{suffix}"
        raise ValueError(f"Unsupported crypto currency: {currency}")

    def get_address(self, currency: str, network: str, deposit_reference: str):
        return self.create_deposit_address(currency, network, deposit_reference)


class ConfiguredBlockchainAddressProvider(BlockchainAddressProvider):
    def __init__(self):
        super().__init__(provider_name="configured")

    def create_deposit_address(self, currency: str, network: str, deposit_reference: str):
        # Real deployments should use a custodial or HD-wallet service here.
        return super().create_deposit_address(currency, network, deposit_reference)
