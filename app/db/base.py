from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """业务模型与 Alembic 共用的 SQLAlchemy 声明式基类。"""
