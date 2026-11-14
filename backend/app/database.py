from collections.abc import Generator
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from .config import settings


class Base(DeclarativeBase):
    pass


def make_engine(database_url: str):
    if database_url.startswith("sqlite:///") and ":memory:" not in database_url:
        Path(database_url.removeprefix("sqlite:/// ").replace("sqlite:///", "")).parent.mkdir(
            parents=True, exist_ok=True
        )
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite:") else {}
    return create_engine(database_url, connect_args=connect_args, pool_pre_ping=True)


engine = make_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    with SessionLocal() as db:
        yield db
