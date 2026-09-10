"""Hash chain over the audit log.

Revision ID: 0002
Revises: 0001
Created: 2026-09-10

Adds prev_hash and entry_hash to audit_log so each entry commits to its
predecessor. Editing or deleting a row then breaks every link after it, making
tampering with the record of who approved a freeze detectable.

Both columns are nullable and existing rows are deliberately NOT backfilled:
computing a hash for a historical entry would assert it had been verified when
it never was. They report as unverifiable instead, which is accurate.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("audit_log", sa.Column("prev_hash", sa.Text(), nullable=True))
    op.add_column("audit_log", sa.Column("entry_hash", sa.Text(), nullable=True))
    op.create_index(
        "idx_audit_log_entry_hash",
        "audit_log",
        [sa.text("id DESC")],
        postgresql_where=sa.text("entry_hash IS NOT NULL"),
    )


def downgrade() -> None:
    # Reversible, unlike the baseline: this drops derived verification data,
    # not the audit entries themselves. The log survives; only the ability to
    # prove it has not been altered is lost.
    op.drop_index("idx_audit_log_entry_hash", table_name="audit_log")
    op.drop_column("audit_log", "entry_hash")
    op.drop_column("audit_log", "prev_hash")
