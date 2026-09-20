"""Per-test database isolation for the pytest suites.

Every DB-backed test used to `create_all()` / `drop_all()` the whole schema on
a fresh engine — ~250 ms of DDL per test, which is where most of the suite's
wall-clock went once the collaboration and share-link tests landed. This module
is the standard replacement (the SQLAlchemy "join a Session into an external
transaction" recipe; what Django's `TestCase` and Rails' transactional
fixtures do):

* the schema is created **once** per pytest session, on a shared engine;
* each test runs inside one outer transaction on a dedicated connection, and
  the Session joins it with ``join_transaction_mode="create_savepoint"``, so
  the code under test keeps calling ``commit()`` / ``rollback()`` /
  ``begin_nested()`` unchanged — they release / roll back to a savepoint — and
  the outer transaction is rolled back at teardown, leaving no trace.

That covers everything except what only a real ``COMMIT`` can show: a second
connection (row-lock races, code running on another thread) sees nothing of an
uncommitted transaction. Tests of that shape opt into
:func:`committed_session` via the ``committed_db`` marker; they get an ordinary
session with real commits and the tables are truncated afterwards.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import MetaData, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

COMMITTED_DB_MARKER = "committed_db"


@contextmanager
def transactional_session(engine: Engine) -> Iterator[Session]:
    """A Session whose every commit is a savepoint inside one rolled-back
    transaction — the default isolation for a test."""
    connection = engine.connect()
    outer = connection.begin()
    session = Session(
        bind=connection,
        autoflush=False,
        join_transaction_mode="create_savepoint",
    )
    try:
        yield session
    finally:
        session.close()
        # A failed statement (an IntegrityError the test provoked, say) may
        # already have ended the outer transaction; rolling back is then a no-op.
        if outer.is_active:
            outer.rollback()
        connection.close()


@contextmanager
def committed_session(engine: Engine, metadata: MetaData) -> Iterator[Session]:
    """A Session with real commits; every table in ``metadata`` is truncated at
    exit so the next test still starts from an empty database."""
    session = sessionmaker(bind=engine, autoflush=False)()
    try:
        yield session
    finally:
        session.close()
        truncate_all(engine, metadata)


def truncate_all(engine: Engine, metadata: MetaData) -> None:
    """Empty every table in ``metadata`` in one statement."""
    preparer = engine.dialect.identifier_preparer
    names = ", ".join(preparer.format_table(t) for t in metadata.sorted_tables)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))


def session_factory_for(session: Session) -> sessionmaker[Session]:
    """A ``SessionLocal`` stand-in that sees what ``session`` sees.

    Code that opens its own session (background tasks, seeds) is normally
    bound to the settings database; tests monkeypatch it with this. Under
    :func:`transactional_session` the factory shares the test's connection and
    joins the same transaction, so uncommitted rows are visible both ways; under
    :func:`committed_session` it is bound to the engine like production and
    hands out independent connections, which is what a thread needs.
    """
    return sessionmaker(
        bind=session.get_bind(),
        autoflush=False,
        join_transaction_mode="create_savepoint",
    )
