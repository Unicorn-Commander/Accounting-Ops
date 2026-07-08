"""Global async engine + sessionmaker. Per-tenant search_path is set in
app/tenant_session.py — do NOT set server_settings={'search_path': ...} or
attach a connect event listener that pins search_path here.
"""
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, declared_attr

from app.config import get_settings


class Base(DeclarativeBase):
    @declared_attr.directive
    def __tablename__(cls) -> str:
        name = cls.__name__
        return "".join(["_" + c.lower() if c.isupper() else c for c in name]).lstrip("_")


engine = create_async_engine(
    get_settings().database_url,
    pool_size=20,
    max_overflow=10,
    pool_pre_ping=True,
    pool_recycle=1800,
)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """Public-schema session for tenant registry, admin, provisioning.

    For per-workspace queries, use `app.workspace_session.workspace_session(...)`.
    For per-entity queries within a workspace, use `entity_session(...)`.
    """
    async with SessionLocal() as session:
        yield session
