import logging
import time

from sqlalchemy.exc import OperationalError

logger = logging.getLogger(__name__)

# Per-turn Anthropic usage, logged onto the assistant Message row that
# turn produced (see app.agent.loop.TurnUsage / app.routes.chat) — added
# after `messages` already existed in production, so db.create_all()
# (CREATE TABLE IF NOT EXISTS, never ALTER) can't add these on its own;
# see add_missing_columns_safely below.
_COLUMNS_BY_TABLE = {
    "messages": {
        "input_tokens": "INTEGER",
        "output_tokens": "INTEGER",
        "cache_creation_input_tokens": "INTEGER",
        "cache_read_input_tokens": "INTEGER",
    },
}


def create_all_tables_safely(db, retries: int = 3, retry_delay: float = 0.5) -> None:
    """db.create_all() is SELECT-then-CREATE per table (checkfirst=True by
    default), which races when it runs concurrently in more than one
    process against the same fresh database — e.g. gunicorn workers each
    importing wsgi:app without --preload all see no tables yet and all
    issue CREATE TABLE; every process but the first then crashes with
    "table X already exists" ("Worker failed to boot").

    Schema creation should really happen exactly once, before any worker
    exists (see docker-entrypoint.sh / scripts/init_db.py, which run this
    as a separate step ahead of `gunicorn`). This wrapper is a
    defense-in-depth fallback for if that step is ever skipped:

    - "already exists" means the raced process already reached the
      actually-intended end state (the tables exist) — swallowed as a
      no-op rather than crashing the process.
    - "database is locked" (seen under heavier concurrency — 8+ processes
      hitting a brand-new file at once can outlast SQLite's busy_timeout)
      is NOT swallowed outright: unlike "already exists" it doesn't tell us
      this process's own tables actually got created, so silently ignoring
      it could leave a half-created schema. Instead, retry — create_all()
      is idempotent (checkfirst skips tables that already exist), so a
      retry safely finishes creating whatever this process didn't get to.
    - anything else still raises.
    """
    for attempt in range(retries):
        try:
            db.create_all()
            return
        except OperationalError as exc:
            message = str(exc).lower()
            if "already exists" in message:
                logger.info(
                    "db.create_all() raced with another process/worker creating the same table(s) — continuing."
                )
                return
            if "database is locked" in message and attempt < retries - 1:
                logger.info("db.create_all() found the database locked by another process/worker — retrying.")
                time.sleep(retry_delay)
                continue
            raise


def add_missing_columns_safely(db, retries: int = 3, retry_delay: float = 0.5) -> None:
    """ALTER TABLE ADD COLUMN for any column in _COLUMNS_BY_TABLE that an
    existing table doesn't have yet.

    db.create_all() only CREATEs a table that doesn't exist at all — it
    never ALTERs one that already does, so a column added to a model
    after its table was first created in production (the usage columns
    above are the first case of this) needs its own one-time migration.
    Checked via PRAGMA table_info rather than a schema-version marker —
    simple, and trivially correct for a handful of nullable columns on a
    small SQLite file. SQLite-only (this app's only database); a no-op on
    anything else.

    Called from the exact same place as create_all_tables_safely (see
    app.create_app / scripts/init_db.py), right after it — but on a brand
    new database, several concurrently-booting workers each running both
    calls in sequence can still interleave ACROSS threads/processes: one
    worker's create_all() may not have created this table yet when
    another worker's PRAGMA/ALTER TABLE here runs ("no such table"), since
    each is a separate CREATE TABLE statement, not one atomic step. Same
    idempotent, race-tolerant pattern as create_all_tables_safely:
    "duplicate column" means another process/worker already added it
    (swallowed); "database is locked" or "no such table" (the table is
    still being created elsewhere) retries; anything else raises.
    """
    if db.engine.dialect.name != "sqlite":
        return

    for table_name, columns in _COLUMNS_BY_TABLE.items():
        for attempt in range(retries):
            try:
                existing = {row[1] for row in db.session.execute(db.text(f"PRAGMA table_info({table_name})")).fetchall()}
                if not existing:
                    # PRAGMA table_info silently returns nothing for a
                    # table that doesn't exist yet, rather than erroring —
                    # if create_all() hasn't created it on THIS connection's
                    # view of the database yet, wait rather than ALTER a
                    # table that isn't there.
                    raise OperationalError(f"no such table: {table_name}", {}, Exception())
                for column_name, column_type in columns.items():
                    if column_name in existing:
                        continue
                    db.session.execute(db.text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}"))
                    logger.info("added column %s.%s", table_name, column_name)
                db.session.commit()
                break
            except OperationalError as exc:
                db.session.rollback()
                message = str(exc).lower()
                if "duplicate column" in message:
                    logger.info(
                        "add_missing_columns_safely raced with another process/worker adding the same column — continuing."
                    )
                    break
                if ("database is locked" in message or "no such table" in message) and attempt < retries - 1:
                    logger.info("add_missing_columns_safely found %s — retrying.", table_name)
                    time.sleep(retry_delay)
                    continue
                raise
