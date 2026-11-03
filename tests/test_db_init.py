import sqlite3
import threading

import pytest
from sqlalchemy.exc import OperationalError

from app import create_app
from app.config import AppConfig
from app.db_init import add_missing_columns_safely, create_all_tables_safely
from app.models import Message


class _FakeDB:
    def __init__(self, exc=None):
        self._exc = exc
        self.create_all_called = False

    def create_all(self):
        self.create_all_called = True
        if self._exc is not None:
            raise self._exc


def _operational_error(message):
    return OperationalError("CREATE TABLE x", {}, Exception(message))


def test_create_all_tables_safely_swallows_already_exists_race():
    """The actual reported bug: a worker that loses the create_all() race
    used to crash with this exact error ("Worker failed to boot").
    """
    db = _FakeDB(_operational_error('table "handoffs" already exists'))
    create_all_tables_safely(db)  # must not raise
    assert db.create_all_called is True


def test_create_all_tables_safely_reraises_other_operational_errors():
    db = _FakeDB(_operational_error("no such table: foo"))
    with pytest.raises(OperationalError):
        create_all_tables_safely(db)


class _LocksThenSucceeds:
    """Fails with "database is locked" a fixed number of times, then succeeds."""

    def __init__(self, fail_times):
        self._remaining_failures = fail_times
        self.call_count = 0

    def create_all(self):
        self.call_count += 1
        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            raise _operational_error("database is locked")


def test_create_all_tables_safely_retries_on_database_locked():
    """Unlike "already exists", "database is locked" doesn't mean this
    process's own tables got created — it must retry (create_all() is
    idempotent) rather than silently swallow the error.
    """
    db = _LocksThenSucceeds(fail_times=2)
    create_all_tables_safely(db, retry_delay=0)
    assert db.call_count == 3


def test_create_all_tables_safely_gives_up_after_max_retries():
    db = _LocksThenSucceeds(fail_times=10)
    with pytest.raises(OperationalError):
        create_all_tables_safely(db, retries=3, retry_delay=0)
    assert db.call_count == 3


def test_create_all_tables_safely_passes_through_when_no_error():
    db = _FakeDB(None)
    create_all_tables_safely(db)  # must not raise


def test_concurrent_create_app_against_fresh_db_does_not_crash(tmp_path):
    """Regression: gunicorn's default (no --preload) fork model has each
    worker import wsgi:app independently, and create_app() calls
    db.create_all(). On a fresh database, several workers booting at once
    used to race that call and crash with "table X already exists" ("Worker
    failed to boot") — recovering only because the tables existed by the
    next restart. Building several app instances concurrently against the
    same brand-new SQLite file must never raise.
    """
    db_path = tmp_path / "race.db"
    stores_dir = tmp_path / "stores"
    stores_dir.mkdir()
    errors = []

    def _build():
        try:
            config = AppConfig()
            config.database_uri = f"sqlite:///{db_path}"
            config.stores_dir = stores_dir
            create_app(config)
        except Exception as exc:  # pragma: no cover - failure path under test
            errors.append(exc)

    threads = [threading.Thread(target=_build) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"concurrent create_app() raised: {errors!r}"


# ---------- add_missing_columns_safely ----------


class _FakeEngine:
    def __init__(self, dialect_name):
        self.dialect = type("D", (), {"name": dialect_name})()


class _FakeDBWithEngine:
    def __init__(self, dialect_name):
        self.engine = _FakeEngine(dialect_name)
        self.session = None  # must never be touched for a non-sqlite engine

    def text(self, s):  # pragma: no cover - should never be called
        raise AssertionError("db.text() called for a non-sqlite engine")


def test_add_missing_columns_safely_is_a_noop_on_non_sqlite():
    db = _FakeDBWithEngine("postgresql")
    add_missing_columns_safely(db)  # must not raise or touch db.session


def _create_legacy_messages_table(db_path):
    """A `messages` table shaped like production's did BEFORE the usage
    columns existed — and the `sessions` table it foreign-keys to, so a
    real Message row can be inserted afterwards.
    """
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE sessions (id VARCHAR(64) PRIMARY KEY, store_id VARCHAR(64) NOT NULL, "
        "created_at DATETIME NOT NULL, last_active_at DATETIME NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id VARCHAR(64) NOT NULL, "
        "role VARCHAR(16) NOT NULL, content TEXT NOT NULL, ip_address VARCHAR(64), "
        "created_at DATETIME NOT NULL)"
    )
    conn.commit()
    conn.close()


def test_add_missing_columns_safely_adds_usage_columns_to_an_existing_table(tmp_path):
    """The real-world case this exists for: a database created before the
    per-turn usage columns were added to the Message model. create_all()
    alone would leave `messages` exactly as it was (CREATE TABLE IF NOT
    EXISTS never alters an existing table) — add_missing_columns_safely
    must add them, and the table must still work for a normal insert
    afterwards.
    """
    db_path = tmp_path / "legacy.db"
    _create_legacy_messages_table(db_path)

    config = AppConfig()
    config.database_uri = f"sqlite:///{db_path}"
    config.stores_dir = tmp_path / "stores"
    config.stores_dir.mkdir()
    app = create_app(config)

    with app.app_context():
        from app.extensions import db

        columns = {row[1] for row in db.session.execute(db.text("PRAGMA table_info(messages)")).fetchall()}
        assert {"input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"} <= columns

        from app.models import Session

        db.session.add(Session(id="s1", store_id="acme"))
        db.session.add(Message(session_id="s1", role="assistant", content="hi", input_tokens=10, cache_read_input_tokens=5))
        db.session.commit()

        row = db.session.get(Message, 1)
        assert row.input_tokens == 10
        assert row.cache_read_input_tokens == 5
        assert row.output_tokens is None  # untouched column stays NULL, not 0


def test_add_missing_columns_safely_is_idempotent(tmp_path):
    """Running it twice (e.g. two gunicorn workers both booting against the
    same not-yet-migrated database) must not raise or double-add a column.
    """
    db_path = tmp_path / "legacy.db"
    _create_legacy_messages_table(db_path)

    config = AppConfig()
    config.database_uri = f"sqlite:///{db_path}"
    config.stores_dir = tmp_path / "stores"
    config.stores_dir.mkdir()
    create_app(config)
    app2 = create_app(config)  # second "worker boot" against the now-migrated db

    with app2.app_context():
        from app.extensions import db

        columns = [row[1] for row in db.session.execute(db.text("PRAGMA table_info(messages)")).fetchall()]
        assert columns.count("input_tokens") == 1
