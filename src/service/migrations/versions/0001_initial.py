"""Account configuration, task summaries and structured logs."""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("accounts", sa.Column("account_id", sa.String(), primary_key=True), sa.Column("config", sa.JSON(), nullable=False))
    op.create_table("tasks", sa.Column("task_id", sa.String(), primary_key=True), sa.Column("record", sa.JSON(), nullable=False))
    op.create_table("logs", sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
                    sa.Column("task_id", sa.String()), sa.Column("account_id", sa.String()),
                    sa.Column("timestamp", sa.String(), nullable=False), sa.Column("message", sa.String(), nullable=False))
    op.create_index("ix_logs_task_id", "logs", ["task_id"])


def downgrade():
    op.drop_table("logs")
    op.drop_table("tasks")
    op.drop_table("accounts")
