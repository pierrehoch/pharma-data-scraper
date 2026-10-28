from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

_engines: dict[str, Engine] = {}


def get_engine(url: str) -> Engine:
    if url not in _engines:
        kwargs: dict = {"future": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
        else:
            kwargs["pool_pre_ping"] = True
        engine = create_engine(url, **kwargs)
        if url.startswith("sqlite"):

            @event.listens_for(engine, "connect")
            def _sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001
                # pysqlite's implicit transactions break SAVEPOINT: let SQLAlchemy emit BEGIN itself.
                dbapi_conn.isolation_level = None
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA foreign_keys=ON")
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA synchronous=NORMAL")  # safe with WAL, avoids an fsync per commit
                cur.execute("PRAGMA busy_timeout=10000")
                cur.close()

            @event.listens_for(engine, "begin")
            def _sqlite_begin(conn):  # noqa: ANN001
                conn.exec_driver_sql("BEGIN")

        _engines[url] = engine
    return _engines[url]


def make_session_factory(url: str) -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(url), expire_on_commit=False, future=True)


def dispose_engines() -> None:
    for engine in _engines.values():
        engine.dispose()
    _engines.clear()
