"""
Versioned schema migrations.

    python migrations.py            # apply pending migrations
    python migrations.py --status   # show applied / pending
    python migrations.py --dry-run  # list what would run

How it works
------------
* `schema.sql` is the baseline (version ``000_baseline``). It is idempotent and
  is executed once per database, the first time the runner sees that database.
  Existing databases that already ran schema.sql at startup are simply stamped.
* Every later change lives in ``migrations/NNN_name.sql`` and is applied once,
  in filename order, inside its own transaction and recorded in
  ``schema_migrations`` with a checksum.
* A PostgreSQL advisory lock stops two processes (API + worker) migrating at
  the same time.
"""

import argparse
import hashlib
import logging
import sys
import time
from pathlib import Path

logger = logging.getLogger("migrations")

ROOT = Path(__file__).resolve().parent
SCHEMA_FILE = ROOT / "schema.sql"
MIGRATIONS_DIR = ROOT / "migrations"
BASELINE_VERSION = "000_baseline"
# Historical files that schema.sql already contains; stamped as applied with the baseline.
BASELINE_INCLUDES = {"002_pitchbook_snapshot"}
ADVISORY_LOCK_KEY = 7415_2026


def _checksum(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _ensure_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version     TEXT PRIMARY KEY,
                checksum    TEXT,
                applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                duration_ms INTEGER,
                note        TEXT
            )
        """)
    conn.commit()


def applied_versions(conn) -> dict:
    _ensure_table(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT version, checksum FROM schema_migrations")
        return {row[0]: row[1] for row in cur.fetchall()}


def migration_files() -> list[Path]:
    if not MIGRATIONS_DIR.is_dir():
        return []
    return sorted(p for p in MIGRATIONS_DIR.glob("*.sql") if p.stem[:3].isdigit())


def pending(conn) -> list[tuple[str, Path | None]]:
    """Return [(version, path)] still to apply. path is None for the baseline."""
    done = applied_versions(conn)
    items: list[tuple[str, Path | None]] = []
    if BASELINE_VERSION not in done:
        items.append((BASELINE_VERSION, None))
    for path in migration_files():
        if path.stem in done or path.stem in BASELINE_INCLUDES:
            continue
        items.append((path.stem, path))
    return items


def _record(conn, version: str, checksum: str | None, duration_ms: int, note: str | None = None):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO schema_migrations (version, checksum, duration_ms, note) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (version) DO NOTHING",
            (version, checksum, duration_ms, note),
        )


def _apply_sql(conn, version: str, sql: str, note: str | None = None):
    started = time.monotonic()
    with conn.cursor() as cur:
        cur.execute(sql)
        _record(conn, version, _checksum(sql), int((time.monotonic() - started) * 1000), note)
    conn.commit()
    logger.info("migration applied: %s", version)


def run_all(conn, dry_run: bool = False) -> list[str]:
    """Apply every pending migration. Returns the list of versions applied."""
    _ensure_table(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))
    try:
        todo = pending(conn)
        if dry_run:
            return [v for v, _ in todo]
        applied: list[str] = []
        for version, path in todo:
            try:
                if path is None:
                    _apply_sql(conn, version, SCHEMA_FILE.read_text(encoding="utf-8"), note="schema.sql baseline")
                    for stamped in sorted(BASELINE_INCLUDES):
                        _record(conn, stamped, None, 0, "included in baseline")
                    conn.commit()
                else:
                    _apply_sql(conn, version, path.read_text(encoding="utf-8"))
            except Exception:
                conn.rollback()
                logger.exception("migration FAILED: %s (rolled back; later migrations not attempted)", version)
                raise
            applied.append(version)
        _warn_on_drift(conn)
        return applied
    finally:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))
        conn.commit()


def _warn_on_drift(conn):
    done = applied_versions(conn)
    for path in migration_files():
        recorded = done.get(path.stem)
        if recorded and recorded != _checksum(path.read_text(encoding="utf-8")):
            logger.warning("migration file %s changed after it was applied — create a new migration instead", path.name)


def status(conn) -> dict:
    done = applied_versions(conn)
    return {
        "applied": sorted(done),
        "pending": [v for v, _ in pending(conn)],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    from db import connect

    conn = connect()
    try:
        if args.status:
            info = status(conn)
            print("Applied:", ", ".join(info["applied"]) or "(none)")
            print("Pending:", ", ".join(info["pending"]) or "(none)")
            return 0
        applied = run_all(conn, dry_run=args.dry_run)
        label = "Would apply" if args.dry_run else "Applied"
        print(f"{label}: {', '.join(applied) or '(nothing — schema is current)'}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
