"""Added build_tags.

Revision ID: 41046ed4662f
Revises: 9d60d35786c1
Create Date: 2021-06-08 17:02:54.038001

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '41046ed4662f'
down_revision = '9d60d35786c1'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'build_tag',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('request_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['request_id'], ['request.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name'),
    )
    with op.batch_alter_table('build_tag', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_build_tag_request_id'), ['request_id'], unique=False)


def downgrade():
    with op.batch_alter_table('build_tag', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_build_tag_request_id'))

    op.drop_table('build_tag')
