"""Phase 1: migration runner — fresh install, upgrade stamping, idempotent re-run.

Uses a throwaway database (delta_prospect_test_<rand>) when the dev role may
create databases; otherwise only the read-only checks against the dev DB run.
"""

import uuid

import psycopg2
import pytest

import migrations
from db import DB_CONFIG
from tests.conftest import requires_db

pytestmark = requires_db


@pytest.fixture
def scratch_db():
    """An empty place to run migrations from zero.

    Prefers a throwaway database. When the dev role cannot CREATE DATABASE
    (the local Windows install), falls back to an empty schema placed first on
    search_path — every unqualified CREATE in schema.sql/migrations lands there
    and the real tables in ``public`` are untouched."""
    name = f"zz_mig_{uuid.uuid4().hex[:8]}"
    try:
        admin = psycopg2.connect(**{**DB_CONFIG, "dbname": "postgres"})
        admin.autocommit = True
        with admin.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{name}"')
    except psycopg2.Error:
        admin = None
    if admin is not None:
        conn = psycopg2.connect(**{**DB_CONFIG, "dbname": name})
        try:
            yield conn
        finally:
            conn.close()
            with admin.cursor() as cur:
                cur.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
            admin.close()
        return

    conn = psycopg2.connect(**DB_CONFIG)
    with conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{name}"')
        cur.execute(f'SET search_path TO "{name}", public')
    conn.commit()
    try:
        yield conn
    finally:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA "{name}" CASCADE')
        conn.commit()
        conn.close()


def test_fresh_install_applies_everything_once(scratch_db):
    applied = migrations.run_all(scratch_db)
    assert applied[0] == "000_baseline"
    assert "010_phase1_background_jobs" in applied
    info = migrations.status(scratch_db)
    assert info["pending"] == []
    assert "002_pitchbook_snapshot" in info["applied"], "historical file stamped with the baseline"
    # second run is a no-op
    assert migrations.run_all(scratch_db) == []
    with scratch_db.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM schema_migrations")
        before = cur.fetchone()[0]
    migrations.run_all(scratch_db)
    with scratch_db.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM schema_migrations")
        assert cur.fetchone()[0] == before


def test_upgrade_path_from_schema_sql_only_database(scratch_db):
    """A database that only ever ran schema.sql (the old startup behaviour) must
    be stamped as baseline and receive just the numbered migrations."""
    with scratch_db.cursor() as cur:
        cur.execute(migrations.SCHEMA_FILE.read_text(encoding="utf-8"))
    scratch_db.commit()
    applied = migrations.run_all(scratch_db)
    assert applied[0] == "000_baseline"  # stamp (schema.sql is idempotent so re-running is harmless)
    assert all(v[:3].isdigit() for v in applied[1:])
    with scratch_db.cursor() as cur:
        cur.execute("SELECT 1 FROM information_schema.tables WHERE table_name = 'background_jobs'")
        assert cur.fetchone()


def test_failed_migration_rolls_back_and_stops(scratch_db, tmp_path, monkeypatch):
    migrations.run_all(scratch_db)
    bad_dir = tmp_path / "migrations"
    bad_dir.mkdir()
    (bad_dir / "900_bad.sql").write_text("CREATE TABLE zz_ok (id int); SELECT * FROM table_that_does_not_exist;")
    (bad_dir / "910_after.sql").write_text("CREATE TABLE zz_after (id int);")
    monkeypatch.setattr(migrations, "MIGRATIONS_DIR", bad_dir)
    with pytest.raises(Exception):
        migrations.run_all(scratch_db)
    with scratch_db.cursor() as cur:
        cur.execute("SELECT table_name FROM information_schema.tables WHERE table_name IN ('zz_ok','zz_after')")
        assert cur.fetchall() == [], "partial migration must be rolled back and later ones not attempted"
        cur.execute("SELECT version FROM schema_migrations WHERE version LIKE '9%'")
        assert cur.fetchall() == []


def test_dev_database_is_current(db_conn):
    assert migrations.status(db_conn)["pending"] == []
