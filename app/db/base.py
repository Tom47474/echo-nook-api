from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Shared metadata base imported by application models and Alembic."""
