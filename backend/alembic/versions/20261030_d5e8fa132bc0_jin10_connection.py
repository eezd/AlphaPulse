"""Add encrypted Jin10 QR connection; preserve existing calendar records."""
from alembic import op
import sqlalchemy as sa

revision = "d5e8fa132bc0"
down_revision = "c4d7e9f021ab"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("jin10_connection",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("token_encrypted", sa.LargeBinary()),
        sa.Column("connected_at", sa.DateTime(timezone=True)),
        sa.Column("task_id", sa.String(64)),
        sa.Column("owner", sa.String(64)),
        sa.Column("qr_encrypted", sa.LargeBinary()),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("next_poll_at", sa.DateTime(timezone=True)),
        sa.Column("error", sa.Text()))
    op.execute("INSERT INTO jin10_connection (id) VALUES (1)")


def downgrade():
    op.drop_table("jin10_connection")
