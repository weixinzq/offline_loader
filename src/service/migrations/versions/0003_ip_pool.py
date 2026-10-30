"""Replace stored account routes with a shared, explicitly enabled IP pool."""
from alembic import op
import sqlalchemy as sa

revision = "0003_ip_pool"
down_revision = "0002_egress_groups"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    for name, key in (("accounts", "account_id"), ("egress_groups", "group_id")):
        table = sa.table(name, sa.column(key, sa.String), sa.column("config", sa.JSON))
        for identifier, config in connection.execute(sa.select(table)).all():
            config = dict(config)
            if name == "accounts":
                config.pop("egress_group_id", None)
            else:
                config.update(max_online=25, enabled=False)
            connection.execute(table.update().where(table.c[key] == identifier).values(config=config))


def downgrade():
    # Account route bindings cannot be recovered after dynamic allocation.
    pass
