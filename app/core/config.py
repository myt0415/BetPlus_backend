from decimal import Decimal
from functools import lru_cache
import os

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from urllib.parse import urlparse

from app.core.db_url import (
    hosted_postgres_required,
    normalize_database_url,
    reject_sqlite_if_hosted,
    running_on_heroku,
)


def _private_callback(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").strip().lower()
    if parsed.scheme != "https" or not host or parsed.username or parsed.password:
        return True
    if host in {"localhost", "127.0.0.1", "0.0.0.0", "::1"} or host.endswith(".local"):
        return True
    if host.startswith("10.") or host.startswith("192.168.") or host.startswith("169.254."):
        return True
    if host.startswith("172."):
        parts = host.split(".")
        if len(parts) >= 2 and parts[1].isdigit() and 16 <= int(parts[1]) <= 31:
            return True
    return False


class Settings(BaseSettings):
    """Application configuration loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    database_url: str = Field(
        default="sqlite:///./betplus.db",
        validation_alias="DATABASE_URL",
    )
    secret_key: str = Field(
        default="dev-insecure-key-set-SECRET_KEY-in-production",
        validation_alias="SECRET_KEY",
    )
    jwt_algorithm: str = Field(default="HS256", validation_alias="JWT_ALGORITHM")
    access_token_expire_minutes: int = Field(
        default=60 * 24,
        validation_alias="ACCESS_TOKEN_EXPIRE_MINUTES",
    )
    cors_origins: str = Field(
        default="http://localhost:3000",
        validation_alias="CORS_ORIGINS",
    )
    seed_demo_data: bool = Field(default=True, validation_alias="SEED_DEMO_DATA")
    seed_demo_users: bool = Field(default=True, validation_alias="SEED_DEMO_USERS")
    allow_demo_seed: bool = Field(default=False, validation_alias="ALLOW_DEMO_SEED")
    environment: str = Field(default="development", validation_alias="ENVIRONMENT")
    port: int = Field(default=8000, validation_alias="PORT")
    settlement_poll_seconds: float = Field(
        default=10.0, validation_alias="SETTLEMENT_POLL_SECONDS"
    )

    rate_limit_enabled: bool = Field(
        default=True, validation_alias="RATE_LIMIT_ENABLED"
    )
    payments_mode: str = Field(default="simulated", validation_alias="PAYMENTS_MODE")
    allow_simulated_payments: bool = Field(
        default=False, validation_alias="ALLOW_SIMULATED_PAYMENTS"
    )
    payment_secret_key: str = Field(default="", validation_alias="PAYMENT_SECRET_KEY")
    payment_public_key: str = Field(default="", validation_alias="PAYMENT_PUBLIC_KEY")
    payment_webhook_secret: str = Field(
        default="", validation_alias="PAYMENT_WEBHOOK_SECRET"
    )
    payment_currency: str = Field(default="GHS", validation_alias="PAYMENT_CURRENCY")
    crypto_deposit_enabled: bool = Field(
        default=True, validation_alias="CRYPTO_DEPOSITS_ENABLED"
    )
    crypto_deposit_expiry_minutes: int = Field(
        default=60, validation_alias="CRYPTO_DEPOSIT_EXPIRY_MINUTES"
    )
    crypto_deposit_poll_interval_seconds: int = Field(
        default=15, validation_alias="CRYPTO_DEPOSIT_POLL_INTERVAL"
    )
    btc_rpc_url: str = Field(default="", validation_alias="BTC_RPC_URL")
    btc_indexer_url: str = Field(default="", validation_alias="BTC_INDEXER_URL")
    tron_rpc_url: str = Field(default="", validation_alias="TRON_RPC_URL")
    tron_indexer_url: str = Field(default="", validation_alias="TRON_INDEXER_URL")
    eth_rpc_url: str = Field(default="", validation_alias="ETH_RPC_URL")
    eth_indexer_url: str = Field(default="", validation_alias="ETH_INDEXER_URL")
    usdt_trc20_contract: str = Field(default="", validation_alias="USDT_TRC20_CONTRACT")
    usdt_erc20_contract: str = Field(default="", validation_alias="USDT_ERC20_CONTRACT")
    crypto_btc_ghs_rate: Decimal = Field(
        default=Decimal("22000"), validation_alias="CRYPTO_BTC_GHS_RATE"
    )
    crypto_usdt_ghs_rate: Decimal = Field(
        default=Decimal("1"), validation_alias="CRYPTO_USDT_GHS_RATE"
    )
    btc_confirmations_required: int = Field(
        default=2, validation_alias="BTC_CONFIRMATIONS_REQUIRED"
    )
    usdt_trc20_confirmations_required: int = Field(
        default=6, validation_alias="USDT_TRC20_CONFIRMATIONS_REQUIRED"
    )
    usdt_erc20_confirmations_required: int = Field(
        default=12, validation_alias="USDT_ERC20_CONFIRMATIONS_REQUIRED"
    )

    moolre_env: str = Field(default="sandbox", validation_alias="MOOLRE_ENV")
    moolre_api_base_url: str = Field(
        default="https://sandbox.moolre.com",
        validation_alias="MOOLRE_API_BASE_URL",
    )
    moolre_api_user: str = Field(default="", validation_alias="MOOLRE_API_USER")
    moolre_public_key: str = Field(default="", validation_alias="MOOLRE_PUBLIC_KEY")
    moolre_api_key: str = Field(default="", validation_alias="MOOLRE_API_KEY")
    moolre_account_number: str = Field(
        default="", validation_alias="MOOLRE_ACCOUNT_NUMBER"
    )
    moolre_webhook_secret: str = Field(
        default="", validation_alias="MOOLRE_WEBHOOK_SECRET"
    )
    moolre_callback_url: str = Field(default="", validation_alias="MOOLRE_CALLBACK_URL")

    nowpayments_enabled: bool = Field(
        default=False, validation_alias="NOWPAYMENTS_ENABLED"
    )
    nowpayments_api_key: str = Field(default="", validation_alias="NOWPAYMENTS_API_KEY")
    nowpayments_base_url: str = Field(
        default="https://api.nowpayments.io",
        validation_alias="NOWPAYMENTS_BASE_URL",
    )
    nowpayments_ipn_secret: str = Field(
        default="", validation_alias="NOWPAYMENTS_IPN_SECRET"
    )
    nowpayments_ipn_callback_url: str = Field(
        default="", validation_alias="NOWPAYMENTS_IPN_CALLBACK_URL"
    )
    nowpayments_price_currency: str = Field(
        default="", validation_alias="NOWPAYMENTS_PRICE_CURRENCY"
    )

    sportybet_facts_url: str = Field(
        default="https://www.sportybet.com/api/gh/factsCenter/importantEvents",
        validation_alias="SPORTYBET_FACTS_URL",
    )
    sportybet_live_url: str = Field(
        default="https://www.sportybet.com/api/gh/factsCenter/liveOrPrematchEvents",
        validation_alias="SPORTYBET_LIVE_URL",
    )
    sportybet_live_sport_id: str = Field(
        default="sr:sport:1",
        validation_alias="SPORTYBET_LIVE_SPORT_ID",
    )
    sportybet_live_timeout_seconds: float = Field(
        default=15.0,
        validation_alias="SPORTYBET_LIVE_TIMEOUT_SECONDS",
    )
    sportybet_live_retry_attempts: int = Field(
        default=2,
        validation_alias="SPORTYBET_LIVE_RETRY_ATTEMPTS",
    )
    sportybet_live_sync_stale_seconds: float = Field(
        default=600.0,
        validation_alias="SPORTYBET_LIVE_SYNC_STALE_SECONDS",
    )
    sportybet_live_sync_poll_seconds: float = Field(
        default=2.0,
        validation_alias="SPORTYBET_LIVE_SYNC_POLL_SECONDS",
    )
    sportybet_live_sync_interval_seconds: float = Field(
        default=30.0,
        validation_alias="SPORTYBET_LIVE_SYNC_INTERVAL_SECONDS",
    )
    sportybet_important_sync_interval_seconds: float = Field(
        default=300.0,
        validation_alias="SPORTYBET_IMPORTANT_SYNC_INTERVAL_SECONDS",
    )
    sportybet_live_sync_max_attempts: int = Field(
        default=3,
        validation_alias="SPORTYBET_LIVE_SYNC_MAX_ATTEMPTS",
    )
    sportybet_sport_id: str = Field(
        default="sr:sport:1",
        validation_alias="SPORTYBET_SPORT_ID",
    )
    sportybet_timeout_seconds: float = Field(
        default=15.0,
        validation_alias="SPORTYBET_TIMEOUT_SECONDS",
    )
    sportybet_retry_attempts: int = Field(
        default=2,
        validation_alias="SPORTYBET_RETRY_ATTEMPTS",
    )
    sportybet_client_id: str = Field(
        default="web",
        validation_alias="SPORTYBET_CLIENT_ID",
    )
    sportybet_oper_id: str = Field(
        default="3",
        validation_alias="SPORTYBET_OPER_ID",
    )
    sportybet_referer: str = Field(
        default="https://www.sportybet.com/gh/",
        validation_alias="SPORTYBET_REFERER",
    )
    sportybet_user_agent: str = Field(
        default=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/128.0.0.0 Safari/537.36"
        ),
        validation_alias="SPORTYBET_USER_AGENT",
    )

    @field_validator("environment")
    @classmethod
    def normalize_environment(cls, value: str) -> str:
        return (value or "development").strip().lower()

    @field_validator("payments_mode")
    @classmethod
    def normalize_payments_mode(cls, value: str) -> str:
        mode = (value or "simulated").strip().lower()
        if mode not in {"simulated", "moolre", "disabled"}:
            raise ValueError("PAYMENTS_MODE must be simulated, moolre, or disabled")
        return mode

    @field_validator("nowpayments_base_url")
    @classmethod
    def normalize_nowpayments_base_url(cls, value: str) -> str:
        raw = (value or "").strip().rstrip("/")
        if not raw:
            return "https://api.nowpayments.io"
        allowed = {
            "https://api.nowpayments.io",
            "https://api.sandbox.nowpayments.io",
        }
        if raw not in allowed:
            raise ValueError(
                "NOWPAYMENTS_BASE_URL must be https://api.nowpayments.io or "
                "https://api.sandbox.nowpayments.io"
            )
        return raw

    @field_validator("nowpayments_price_currency")
    @classmethod
    def normalize_nowpayments_price_currency(cls, value: str) -> str:
        raw = (value or "").strip().lower()
        if raw and not raw.isalnum():
            raise ValueError("NOWPAYMENTS_PRICE_CURRENCY must be a currency code")
        return raw

    @field_validator("moolre_env")
    @classmethod
    def normalize_moolre_env(cls, value: str) -> str:
        env = (value or "sandbox").strip().lower()
        if env not in {"sandbox", "production", "live"}:
            raise ValueError("MOOLRE_ENV must be sandbox or production")
        return "production" if env == "live" else env

    @field_validator("sportybet_timeout_seconds", "sportybet_live_timeout_seconds")
    @classmethod
    def clamp_sportybet_timeout(cls, value: float) -> float:
        return min(max(float(value), 1.0), 60.0)

    @field_validator("sportybet_retry_attempts", "sportybet_live_retry_attempts")
    @classmethod
    def clamp_sportybet_retries(cls, value: int) -> int:
        return min(max(int(value), 1), 3)

    @field_validator("sportybet_live_sync_stale_seconds")
    @classmethod
    def clamp_live_sync_stale(cls, value: float) -> float:
        return min(max(float(value), 30.0), 3600.0)

    @field_validator("sportybet_live_sync_poll_seconds")
    @classmethod
    def clamp_live_sync_poll(cls, value: float) -> float:
        return min(max(float(value), 0.25), 30.0)

    @field_validator(
        "sportybet_live_sync_interval_seconds",
        "sportybet_important_sync_interval_seconds",
    )
    @classmethod
    def clamp_sync_interval(cls, value: float) -> float:
        # 0 disables worker auto-enqueue for that sync type.
        return min(max(float(value), 0.0), 3600.0)

    @field_validator("sportybet_live_sync_max_attempts")
    @classmethod
    def clamp_live_sync_attempts(cls, value: int) -> int:
        return min(max(int(value), 1), 5)

    @field_validator("settlement_poll_seconds")
    @classmethod
    def clamp_settlement_poll(cls, value: float) -> float:
        return min(max(float(value), 1.0), 300.0)

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def is_test(self) -> bool:
        return self.environment == "test"

    @property
    def requires_postgres(self) -> bool:
        return hosted_postgres_required(environment=self.environment)

    @property
    def sqlalchemy_database_url(self) -> str:
        raw = (os.environ.get("DATABASE_URL") or "").strip() or self.database_url
        return normalize_database_url(
            raw,
            environment=self.environment,
            require_ssl=self.requires_postgres,
        )

    @property
    def cors_origin_list(self) -> list[str]:
        return [
            origin.strip() for origin in self.cors_origins.split(",") if origin.strip()
        ]

    @property
    def cookie_secure(self) -> bool:
        return self.environment not in {"development", "test"}

    def nowpayments_price_currency_code(self) -> str:
        raw = (self.nowpayments_price_currency or "").strip().lower()
        return raw or (self.payment_currency or "GHS").strip().lower()

    def moolre_request_base_url(self) -> str:
        configured = (self.moolre_api_base_url or "").strip().rstrip("/")
        sandbox = "https://sandbox.moolre.com"
        live = "https://api.moolre.com"
        if self.moolre_env == "sandbox":
            if configured == live:
                raise RuntimeError(
                    "MOOLRE_ENV=sandbox cannot use https://api.moolre.com; "
                    "set MOOLRE_ENV=production to use live APIs"
                )
            return configured or sandbox
        return configured or live

    @property
    def should_seed_demo(self) -> bool:
        if self.is_test:
            return False
        if self.requires_postgres and not self.allow_demo_seed:
            return False
        return self.seed_demo_data

    @property
    def should_seed_demo_users(self) -> bool:
        if not self.should_seed_demo:
            return False
        if self.requires_postgres:
            return False
        return self.seed_demo_users

    @property
    def allow_direct_wallet_funding(self) -> bool:
        if self.payments_mode == "simulated":
            return True
        if self.is_test:
            return True
        return False

    @property
    def effective_rate_limit_enabled(self) -> bool:
        if self.is_test:
            return False
        return self.rate_limit_enabled

    def validate_for_runtime(self) -> None:
        reject_sqlite_if_hosted(
            self.sqlalchemy_database_url, environment=self.environment
        )
        if not self.is_production and not running_on_heroku():
            return
        if self.secret_key.startswith("dev-insecure"):
            raise RuntimeError(
                "SECRET_KEY must be set to a strong secret in production"
            )
        if "*" in self.cors_origin_list:
            raise RuntimeError("CORS_ORIGINS must not include * in production")
        if not self.is_production:
            return
        if self.payments_mode == "simulated" and not self.allow_simulated_payments:
            raise RuntimeError(
                "PAYMENTS_MODE=simulated is not allowed in production unless "
                "ALLOW_SIMULATED_PAYMENTS=true (staging/demo only; not real-money)"
            )
        if self.payments_mode == "moolre":
            missing = [
                name
                for name, value in {
                    "MOOLRE_API_USER": self.moolre_api_user,
                    "MOOLRE_ACCOUNT_NUMBER": self.moolre_account_number,
                    "MOOLRE_WEBHOOK_SECRET": self.moolre_webhook_secret,
                }.items()
                if not value
            ]
            if self.moolre_env != "sandbox" and not self.moolre_public_key:
                missing.append("MOOLRE_PUBLIC_KEY")
            if missing:
                raise RuntimeError(
                    "Missing required Moolre production configuration: "
                    + ", ".join(missing)
                )
            self.moolre_request_base_url()
        if self.nowpayments_enabled:
            missing = [
                name
                for name, value in {
                    "NOWPAYMENTS_API_KEY": self.nowpayments_api_key,
                    "NOWPAYMENTS_IPN_SECRET": self.nowpayments_ipn_secret,
                    "NOWPAYMENTS_IPN_CALLBACK_URL": self.nowpayments_ipn_callback_url,
                }.items()
                if not (value or "").strip()
            ]
            if missing:
                raise RuntimeError(
                    "Missing required NOWPayments production configuration: "
                    + ", ".join(missing)
                )
            callback = (self.nowpayments_ipn_callback_url or "").strip()
            if not callback.lower().startswith("https://") or _private_callback(callback):
                raise RuntimeError(
                    "NOWPAYMENTS_IPN_CALLBACK_URL must be a public https URL"
                )


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
