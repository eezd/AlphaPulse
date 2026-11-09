"""Remove simulated trading and its dedicated data. This migration is irreversible."""
from alembic import op

revision = "f07890123456"
down_revision = "ef6789012345"
branch_labels = depends_on = None


def upgrade():
    # Drop children first. No CASCADE: an unexpected dependency must abort the transaction.
    for table in ("trading_orders", "trading_events", "trading_fills", "trading_symbols",
                  "trading_credentials", "trading_account", "trading_analyses", "news_signals"):
        op.drop_table(table)


def downgrade():
    raise RuntimeError("Simulated trading data and credentials were permanently removed; downgrade is unsupported")
