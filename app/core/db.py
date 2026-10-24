from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import settings


def normalize_database_url(url: str) -> tuple[str, dict]:
    """Turn a Neon/Heroku-style URL into an asyncpg URL plus connect_args.

    - postgres:// and postgresql:// -> postgresql+asyncpg://
    - sslmode=require -> ssl="require" (asyncpg does not understand sslmode)
    - channel_binding is dropped (unsupported by asyncpg)
    - Neon's pooled host (-pooler, PgBouncer) needs the prepared-statement cache disabled
    """
    connect_args: dict = {}
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://"):]
    if url.startswith("postgresql+asyncpg://"):
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query))
        sslmode = query.pop("sslmode", None)
        query.pop("channel_binding", None)
        if sslmode and sslmode != "disable":
            connect_args["ssl"] = "require"
        if "-pooler" in (parts.hostname or ""):
            connect_args["statement_cache_size"] = 0
        url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
    return url, connect_args


def make_engine(url: str):
    url, connect_args = normalize_database_url(url)
    kwargs = {"connect_args": connect_args, "pool_pre_ping": True}
    if not url.startswith("sqlite"):
        kwargs.update(pool_size=5, max_overflow=5, pool_recycle=300)  # Neon suspends idle connections
    return create_async_engine(url, **kwargs)


engine = make_engine(settings.database_url)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_session():
    # Routes commit explicitly; leaving the context rolls back anything uncommitted.
    async with SessionLocal() as session:
        yield session


__all__ = ["engine", "SessionLocal", "get_session", "AsyncSession", "make_engine", "normalize_database_url"]
