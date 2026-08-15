"""add exact GAS commission ID resolution provenance

Revision ID: c9a31f6d0e72
Revises: 8f3d2a1c7b49
Create Date: 2026-08-15 15:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c9a31f6d0e72"
down_revision: str | None = "8f3d2a1c7b49"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("commissions") as batch_op:
        batch_op.create_unique_constraint(
            "uq_commission_snapshot_gas_id",
            ["source_artifact_id", "gas_vybory_id"],
        )
    op.create_index("ix_results_gas_id", "result_records", ["gas_vybory_id"], unique=False)
    op.create_table(
        "gas_id_resolutions",
        sa.Column("result_record_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("reason_code", sa.String(length=80), nullable=True),
        sa.Column("gas_vybory_id", sa.String(length=120), nullable=True),
        sa.Column("parent_source_artifact_id", sa.Integer(), nullable=True),
        sa.Column("detail_source_artifact_id", sa.Integer(), nullable=True),
        sa.Column("commission_source_artifact_id", sa.Integer(), nullable=True),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column(
            "resolved_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["result_record_id"], ["result_records.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["parent_source_artifact_id"], ["source_artifacts.id"]),
        sa.ForeignKeyConstraint(["detail_source_artifact_id"], ["source_artifacts.id"]),
        sa.ForeignKeyConstraint(["commission_source_artifact_id"], ["source_artifacts.id"]),
        sa.PrimaryKeyConstraint("result_record_id"),
    )
    op.create_index(
        "ix_gas_resolutions_status_reason",
        "gas_id_resolutions",
        ["status", "reason_code"],
        unique=False,
    )
    op.create_index(
        "ix_gas_resolutions_gas_id",
        "gas_id_resolutions",
        ["gas_vybory_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_gas_resolutions_gas_id", table_name="gas_id_resolutions")
    op.drop_index("ix_gas_resolutions_status_reason", table_name="gas_id_resolutions")
    op.drop_table("gas_id_resolutions")
    op.drop_index("ix_results_gas_id", table_name="result_records")
    with op.batch_alter_table("commissions") as batch_op:
        batch_op.drop_constraint("uq_commission_snapshot_gas_id", type_="unique")
