"""add district-scoped ballots and candidates

Revision ID: 8f3d2a1c7b49
Revises: 6c212c0249e0
Create Date: 2026-08-15 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "8f3d2a1c7b49"
down_revision: str | None = "6c212c0249e0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing rows are all federal party-list ballots. Keeping the default makes
    # the existing importer backward-compatible while single-member importers must
    # provide the stable OIK code explicitly.
    with op.batch_alter_table("ballots") as batch_op:
        batch_op.add_column(
            sa.Column(
                "scope_key",
                sa.String(length=120),
                server_default="federal",
                nullable=False,
            )
        )
        batch_op.add_column(sa.Column("oik_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_ballots_oik_id_geographies", "geographies", ["oik_id"], ["id"]
        )
        batch_op.create_unique_constraint(
            "uq_ballot_election_kind_scope", ["election_id", "kind", "scope_key"]
        )
        batch_op.create_unique_constraint(
            "uq_ballot_election_kind_oik", ["election_id", "kind", "oik_id"]
        )
        # MySQL uses the old unique index to support the election_id foreign
        # key. Create a replacement with the same leading column before
        # dropping it, or MySQL rejects the DROP INDEX operation.
        batch_op.drop_constraint("uq_ballot_election_kind", type_="unique")
        batch_op.create_check_constraint(
            "ck_ballot_kind_scope",
            "(kind = 'PARTY_LIST' AND oik_id IS NULL AND scope_key = 'federal') OR "
            "(kind = 'SINGLE_MEMBER' AND oik_id IS NOT NULL "
            "AND scope_key <> 'federal' AND scope_key <> '')",
        )

    op.create_table(
        "candidates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ballot_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("full_name", sa.String(length=500), nullable=False),
        sa.Column("party_affiliation", sa.String(length=500), nullable=True),
        sa.Column("is_self_nominated", sa.Boolean(), nullable=False),
        sa.Column("registration_status", sa.String(length=120), nullable=True),
        sa.Column("source_artifact_id", sa.Integer(), nullable=True),
        sa.Column("source_record_id", sa.String(length=120), nullable=True),
        sa.Column("gas_vybory_id", sa.String(length=120), nullable=True),
        sa.Column("raw_json", sa.JSON(), nullable=False),
        sa.CheckConstraint("position > 0", name="ck_candidate_position_positive"),
        sa.ForeignKeyConstraint(["ballot_id"], ["ballots.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_artifact_id"], ["source_artifacts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ballot_id", "position", name="uq_candidate_ballot_position"),
        sa.UniqueConstraint(
            "ballot_id",
            "source_artifact_id",
            "source_record_id",
            name="uq_candidate_ballot_source_record",
        ),
    )
    op.create_index(
        "ix_candidates_ballot_name", "candidates", ["ballot_id", "full_name"], unique=False
    )
    op.create_index("ix_candidates_gas_id", "candidates", ["gas_vybory_id"], unique=False)

    op.create_index(
        "ix_results_ballot_uik",
        "result_records",
        ["ballot_id", "uik_number"],
        unique=False,
    )

    with op.batch_alter_table("votes") as batch_op:
        batch_op.alter_column("option_id", existing_type=sa.Integer(), nullable=True)
        batch_op.add_column(sa.Column("candidate_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_votes_candidate_id_candidates", "candidates", ["candidate_id"], ["id"]
        )
        batch_op.create_unique_constraint(
            "uq_vote_result_candidate", ["result_record_id", "candidate_id"]
        )
        batch_op.create_check_constraint(
            "ck_vote_exactly_one_target",
            "(option_id IS NOT NULL AND candidate_id IS NULL) OR "
            "(option_id IS NULL AND candidate_id IS NOT NULL)",
        )

    op.create_index(
        "ix_votes_candidate_result",
        "votes",
        ["candidate_id", "result_record_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_votes_candidate_result", table_name="votes")
    with op.batch_alter_table("votes") as batch_op:
        batch_op.drop_constraint("ck_vote_exactly_one_target", type_="check")
        batch_op.drop_constraint("uq_vote_result_candidate", type_="unique")
        batch_op.drop_constraint("fk_votes_candidate_id_candidates", type_="foreignkey")
        batch_op.drop_column("candidate_id")
        batch_op.alter_column("option_id", existing_type=sa.Integer(), nullable=False)

    op.drop_index("ix_results_ballot_uik", table_name="result_records")
    op.drop_index("ix_candidates_gas_id", table_name="candidates")
    op.drop_index("ix_candidates_ballot_name", table_name="candidates")
    op.drop_table("candidates")

    with op.batch_alter_table("ballots") as batch_op:
        batch_op.drop_constraint("ck_ballot_kind_scope", type_="check")
        batch_op.drop_constraint("uq_ballot_election_kind_oik", type_="unique")
        batch_op.drop_constraint("uq_ballot_election_kind_scope", type_="unique")
        batch_op.drop_constraint("fk_ballots_oik_id_geographies", type_="foreignkey")
        batch_op.drop_column("oik_id")
        batch_op.drop_column("scope_key")
        batch_op.create_unique_constraint(
            "uq_ballot_election_kind", ["election_id", "kind"]
        )
