from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class UserRegister(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    email: EmailStr
    phone: str | None = Field(default=None, max_length=32)
    password: str = Field(min_length=6, max_length=256)
    referral_code: str | None = None


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    email: str
    phone: str | None = None
    balance: float = 0.0
    is_admin: bool = False
    is_manager: bool = False
    referral_code: str | None = None
    referred_by_manager_id: str | None = None
    settings: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None


class Token(BaseModel):
    access_token: str
    token_type: str


class SportOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    slug: str


class LeagueOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    sport_id: int
    name: str
    slug: str


class SportyBetSyncSkip(BaseModel):
    event_id: str | None = None
    game_id: str | None = None
    reason: str


class SportyBetSyncOut(BaseModel):
    success: bool
    source: str
    fetched: int
    created: int
    updated: int
    skipped_existing: int
    skipped_invalid: int
    skipped_protected: int = 0
    failed: int
    unsupported_markets: int = 0
    skipped: list[SportyBetSyncSkip] = Field(default_factory=list)


class SportyBetLiveSyncQueuedOut(BaseModel):
    success: bool
    status: str
    job_id: str


class SportyBetLiveSyncJobOut(BaseModel):
    job_id: str | None = None
    status: str
    sync_type: str = "live_or_prematch"
    fetched: int = 0
    processed: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    skipped_invalid: int = 0
    skipped_protected: int = 0
    failed: int = 0
    live_updated: int = 0
    ended_updated: int = 0
    attempt_count: int = 0
    error_message: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


class GameListOut(BaseModel):
    """Catalog list row.

    Omits ``markets``: a full markets blob per row makes list responses large
    enough to exhaust the web dyno's memory budget. Use ``GameOut`` (the
    single-game detail response) when markets are needed.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    external_id: str
    league_id: int
    league_name: str | None = None
    sport: str | None = None
    home: str
    away: str
    home_abbr: str | None = None
    away_abbr: str | None = None
    starts_at: datetime | None = None
    status: str
    is_live: bool = False
    live_minute: int | None = None
    home_score: int | None = None
    away_score: int | None = None
    odds_home: float | None = None
    odds_draw: float | None = None
    odds_away: float | None = None


class GameOut(GameListOut):
    markets: list[dict[str, Any]] = Field(default_factory=list)


class TransactionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: str
    bet_id: str | None = None
    type: str
    amount: float
    description: str | None = None
    created_at: datetime


class WalletOp(BaseModel):
    amount: float = Field(gt=0)
    description: str | None = None


class BetSelectionIn(BaseModel):
    match_id: str
    home_team: str
    away_team: str
    selection: str
    selection_label: str
    odds: float | None = Field(default=None, gt=0)
    league: str = ""
    market_id: str | None = None
    market_name: str | None = None


class BetSelectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    leg_index: int
    match_id: str
    home_team: str
    away_team: str
    selection: str
    selection_label: str
    odds: float
    league: str
    market_id: str | None = None
    market_name: str | None = None
    outcome_label: str | None = None
    manager_ft_score: dict[str, Any] | None = None


class BetCreate(BaseModel):
    stake: float = Field(gt=0)
    odds: float = Field(gt=0)


class BetPlaceIn(BaseModel):
    stake: float = Field(gt=0)
    selections: list[BetSelectionIn] = Field(min_length=1)
    flex_cut: int | None = Field(default=None, ge=0)
    accept_odds_change: bool = False


class BetBatchItemIn(BaseModel):
    stake: float = Field(gt=0)
    selections: list[BetSelectionIn] = Field(min_length=1)
    flex_cut: int | None = Field(default=None, ge=0)


class BetBatchPlaceIn(BaseModel):
    bets: list[BetBatchItemIn] = Field(min_length=1, max_length=20)
    accept_odds_change: bool = False


class BetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: str
    booking_code: str
    ticket_id: str | None = None
    verify_code: str | None = None
    stake: float
    total_odds: float
    potential_win: float
    bonus: float = 0.0
    flex_cut: int | None = None
    status: str
    payout: float | None = None
    leg_results: list[dict[str, Any]] | None = None
    placed_at: datetime
    settled_at: datetime | None = None
    selections: list[BetSelectionOut] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    detail: str


class UserProfileUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=32)


class PasswordChangeIn(BaseModel):
    current_password: str = Field(min_length=1)
    new_password: str = Field(min_length=6)


class UserSettingsUpdate(BaseModel):
    notifications: bool | None = None
    oddsFormat: str | None = None
    language: str | None = None
    managerMode: bool | None = None


class AdminUserPatch(BaseModel):
    is_manager: bool | None = None
    balance: float | None = Field(default=None, ge=0)


class AdminCreditIn(BaseModel):
    amount: float = Field(gt=0)
    description: str | None = None


class AdminSettleIn(BaseModel):
    status: str = Field(pattern="^(won|lost|void)$")


class AdminBetPatch(BaseModel):
    stake: float | None = Field(default=None, gt=0)
    selections: list[BetSelectionIn] | None = None


class AdminStatsOut(BaseModel):
    users: int
    bets: int
    open_bets: int
    total_user_balance: float
    platform_balance: float
    net_position: float


class LedgerEntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    entry_type: str
    amount: float
    description: str
    user_id: str | None = None
    bet_id: str | None = None
    created_at: datetime | None = None


class AuditLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    actor_id: str | None = None
    role: str
    action: str
    detail: str
    bet_id: str | None = None
    match_id: str | None = None
    booking_code: str | None = None
    created_at: datetime | None = None


class ManagerMatchCreate(BaseModel):
    home_team: str = Field(min_length=1, max_length=255)
    away_team: str = Field(min_length=1, max_length=255)
    league: str = Field(default="Manual League", max_length=255)
    sport: str = Field(default="football", max_length=32)
    kickoff: datetime | None = None
    note: str | None = None


class ManagerMatchPatch(BaseModel):
    status: str | None = None
    home_score: int | None = Field(default=None, ge=0)
    away_score: int | None = Field(default=None, ge=0)
    home_team: str | None = Field(default=None, max_length=255)
    away_team: str | None = Field(default=None, max_length=255)
    league: str | None = None
    kickoff: datetime | None = None
    note: str | None = None


class ManagerLegPatch(BaseModel):
    selection: str | None = None
    selection_label: str | None = None
    odds: float | None = Field(default=None, gt=0)
    market_id: str | None = None
    market_name: str | None = None
    outcome_label: str | None = None
    outcome_status: str | None = None
    ft_home_score: int | None = Field(default=None, ge=0)
    ft_away_score: int | None = Field(default=None, ge=0)


class PaymentInitiateIn(BaseModel):
    amount: float = Field(gt=0)
    channel: str | None = Field(default="mobile_money", max_length=32)
    phone: str | None = Field(default=None, max_length=32)
    payer_phone: str | None = Field(default=None, max_length=32)
    destination: str | None = Field(default=None, max_length=255)
    network: str | None = Field(default=None, max_length=32)
    provider: str | None = Field(default=None, max_length=32)


class PaymentOtpIn(BaseModel):
    otpcode: str = Field(min_length=4, max_length=16)


class PaymentIntentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: str
    provider: str
    kind: str
    provider_ref: str
    amount: float
    currency: str
    status: str
    channel: str | None = None
    authorization_url: str | None = None
    otp_required: bool = False
    next_action: str | None = None
    created_at: datetime | None = None
    completed_at: datetime | None = None
    transaction_id: str | None = None
    network: str | None = None
    pay_address: str | None = None
    pay_amount: str | None = None
    pay_currency: str | None = None
    payment_id: str | None = None
    provider_status: str | None = None
    expires_at: datetime | None = None
    review_required: bool = False
