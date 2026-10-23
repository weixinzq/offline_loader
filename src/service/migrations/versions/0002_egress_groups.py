"""Persist outbound account groups; existing accounts remain unassigned."""
from alembic import op
import sqlalchemy as sa

revision = "0002_egress_groups"
down_revision = "0001"
branch_labels = depends_on = None


def upgrade():
    op.create_table("egress_groups", sa.Column("group_id", sa.String(), primary_key=True),
                    sa.Column("config", sa.JSON(), nullable=False))


def downgrade():
    op.drop_table("egress_groups")
