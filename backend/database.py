import os

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, declarative_base


DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "sqlite:///./railblock.db"
)


# ===========================================================================
# SQLite lock-timeout hardening
# ===========================================================================
#
# Why this exists:
#
#   /generate-block-plan was reported to hang indefinitely with NO
#   corresponding access-log line and NO exception, even though the
#   endpoint's own logic (scheduling_service.py + cp_sat_optimizer.py)
#   was verified to complete in well under a second for the same data.
#
#   That symptom - a synchronous DB call that neither returns nor raises -
#   is the signature of a SQLite file lock: if another connection (a second
#   Uvicorn worker, a leftover --reload process, DB Browser for SQLite, a
#   previous unclosed session, etc.) is holding a write lock on
#   railblock.db, Python's sqlite3 driver will silently retry the request
#   in a loop for up to "timeout" seconds before giving up. The stdlib
#   default is only 5 seconds, so a genuine lock would normally surface as
#   an "OperationalError: database is locked" fairly quickly - but with
#   the default rollback/journal settings, a long-running writer (or a
#   stuck transaction that never commits/rolls back) can make later
#   requests queue far longer than that, and on some Windows setups a
#   held lock combined with reload-triggered duplicate processes can
#   appear to block forever from the client's point of view.
#
#   RAILBLOCK_SQLITE_TIMEOUT_SECONDS below is an explicit, generous busy
#   timeout: the driver will keep retrying for this many seconds and then
#   raise a clear "database is locked" error instead of hanging silently
#   or effectively forever. WAL mode is also enabled, which lets readers
#   and a single writer operate concurrently instead of blocking each
#   other outright - this is the single biggest fix for
#   "database is locked" symptoms with SQLite + FastAPI.
# ===========================================================================

SQLITE_TIMEOUT_SECONDS = float(
    os.getenv("RAILBLOCK_SQLITE_TIMEOUT_SECONDS", "15")
)


engine_kwargs = {}

if DATABASE_URL.startswith("sqlite"):
    engine_kwargs["connect_args"] = {
        "check_same_thread": False,
        # sqlite3-level busy timeout (seconds). If a lock can't be
        # acquired within this window, sqlite3 raises OperationalError
        # instead of retrying silently.
        "timeout": SQLITE_TIMEOUT_SECONDS,
    }


engine = create_engine(
    DATABASE_URL,
    **engine_kwargs
)


if DATABASE_URL.startswith("sqlite"):

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, connection_record):
        """
        Applied to every new sqlite3 connection in the pool.

        - WAL journal mode: allows concurrent readers + one writer instead
          of exclusive-locking the whole file for every write, which is
          the most common cause of "hangs" under FastAPI + SQLite.
        - busy_timeout (milliseconds): a second, driver-independent
          backstop matching SQLITE_TIMEOUT_SECONDS above, so SQLite itself
          also retries-then-raises instead of hanging.
        - foreign_keys=ON: SQLite does not enforce FK constraints unless
          explicitly told to; this keeps referential integrity consistent
          with what the ORM's ForeignKey/relationship definitions expect.
        """
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute(
                f"PRAGMA busy_timeout={int(SQLITE_TIMEOUT_SECONDS * 1000)};"
            )
            cursor.execute("PRAGMA foreign_keys=ON;")
        finally:
            cursor.close()


SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)


Base = declarative_base()