"""Store NOWPayments pay-in details on payment intents."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "008_nowpayments"
down_revision: Union[str, None] = "007_webhook_events"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "payment_intents",
        sa.Column("provider_status", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "payment_intents",
        sa.Column("pay_currency", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "payment_intents",
        sa.Column("pay_amount", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "payment_intents",
        sa.Column("pay_address", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "payment_intents", sa.Column("network", sa.String(length=32), nullable=True)
    )
    op.add_column(
        "payment_intents",
        sa.Column("price_currency", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "payment_intents",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("payment_intents", "expires_at")
    op.drop_column("payment_intents", "price_currency")
    op.drop_column("payment_intents", "network")
    op.drop_column("payment_intents", "pay_address")
    op.drop_column("payment_intents", "pay_amount")
    op.drop_column("payment_intents", "pay_currency")
    op.drop_column("payment_intents", "provider_status")
