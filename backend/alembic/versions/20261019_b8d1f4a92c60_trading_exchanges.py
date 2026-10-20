"""模拟盘支持按交易所隔离凭证、计划、订单和成交。

Revision ID: b8d1f4a92c60
Revises: c5e8a1b3d207
"""

from alembic import op
import sqlalchemy as sa

revision = "b8d1f4a92c60"
down_revision = "c5e8a1b3d207"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("trading_account", sa.Column("exchange", sa.String(16), server_default="binance", nullable=False))
    op.create_table(
        "trading_credentials",
        sa.Column("exchange", sa.String(16), primary_key=True),
        sa.Column("api_key_encrypted", sa.LargeBinary()),
        sa.Column("api_secret_encrypted", sa.LargeBinary()),
        sa.Column("api_key_hint", sa.String(8)),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.execute("""INSERT INTO trading_credentials
        (exchange, api_key_encrypted, api_secret_encrypted, api_key_hint, updated_at)
        SELECT 'binance', api_key_encrypted, api_secret_encrypted, api_key_hint, updated_at FROM trading_account""")
    for column in ("api_key_encrypted", "api_secret_encrypted", "api_key_hint"):
        op.drop_column("trading_account", column)
    for table in ("trading_analyses", "trading_orders", "trading_events", "trading_fills"):
        op.add_column(table, sa.Column("exchange", sa.String(16), server_default="binance", nullable=False))
        if table != "trading_fills":
            op.create_index(f"ix_{table}_exchange", table, ["exchange"])
    op.alter_column("trading_orders", "exchange_id", type_=sa.String(64), existing_type=sa.String(32))
    for column in ("trade_id", "order_id"):
        op.alter_column("trading_fills", column, type_=sa.String(64), existing_type=sa.BigInteger(), postgresql_using=f"{column}::text")
    op.drop_constraint("trading_fills_pkey", "trading_fills", type_="primary")
    op.create_primary_key("trading_fills_pkey", "trading_fills", ["exchange", "symbol", "trade_id"])


def downgrade() -> None:
    # Bybit ID 无法转回整数；已有 Bybit 数据时明确拒绝回退，避免丢失历史。
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM trading_fills WHERE exchange != 'binance')
           OR EXISTS (SELECT 1 FROM trading_orders WHERE exchange != 'binance')
           OR EXISTS (SELECT 1 FROM trading_analyses WHERE exchange != 'binance')
           OR EXISTS (SELECT 1 FROM trading_credentials WHERE exchange != 'binance') THEN
            RAISE EXCEPTION '已有其他交易所数据，不能回退模拟盘迁移';
        END IF;
    END $$""")
    op.drop_constraint("trading_fills_pkey", "trading_fills", type_="primary")
    for column in ("trade_id", "order_id"):
        op.alter_column("trading_fills", column, type_=sa.BigInteger(), postgresql_using=f"{column}::bigint")
    op.create_primary_key("trading_fills_pkey", "trading_fills", ["symbol", "trade_id"])
    op.alter_column("trading_orders", "exchange_id", type_=sa.String(32))
    for table in ("trading_analyses", "trading_orders", "trading_events", "trading_fills"):
        if table != "trading_fills":
            op.drop_index(f"ix_{table}_exchange", table_name=table)
        op.drop_column(table, "exchange")
    for column, kind in (("api_key_encrypted", sa.LargeBinary()), ("api_secret_encrypted", sa.LargeBinary()), ("api_key_hint", sa.String(8))):
        op.add_column("trading_account", sa.Column(column, kind))
    op.execute("""UPDATE trading_account SET api_key_encrypted = c.api_key_encrypted,
        api_secret_encrypted = c.api_secret_encrypted, api_key_hint = c.api_key_hint
        FROM trading_credentials c WHERE c.exchange = 'binance'""")
    op.drop_table("trading_credentials")
    op.drop_column("trading_account", "exchange")
