"""
Database backup / restore-rehearsal helper (Phase 1 recovery, Phase 5 routine).

    python scripts/backup_db.py                      # pg_dump -> backups/delta_prospect_<ts>.dump
    python scripts/backup_db.py --verify             # also restore into a scratch schema-less DB (needs CREATEDB) and count rows
    python scripts/backup_db.py --restore FILE --into delta_prospect_restore
                                                     # restore a dump into a NAMED database (never the live one)

Uses DATABASE_URL / DB_* like the app. Records a `backup_completed` ops event so
the Ops page can show when the last backup happened. On Railway run this from
your machine with DATABASE_URL pointing at the Railway Postgres.
"""

import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db import DB_CONFIG, connect  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
BACKUPS = ROOT / "backups"


def _tool(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    for candidate in sorted(Path("C:/Program Files/PostgreSQL").glob("*/bin/" + name + ".exe"), reverse=True):
        return str(candidate)
    raise SystemExit(f"{name} not found on PATH. Install PostgreSQL client tools or add them to PATH.")


def _env():
    env = dict(os.environ)
    env["PGPASSWORD"] = DB_CONFIG["password"]
    return env


def _conn_args(dbname: str | None = None) -> list[str]:
    return ["-h", DB_CONFIG["host"], "-p", str(DB_CONFIG["port"]), "-U", DB_CONFIG["user"], "-d", dbname or DB_CONFIG["dbname"]]


def backup() -> Path:
    BACKUPS.mkdir(exist_ok=True)
    out = BACKUPS / f"delta_prospect_{datetime.now():%Y%m%d-%H%M%S}.dump"
    subprocess.run([_tool("pg_dump"), *_conn_args(), "-F", "c", "-f", str(out)], check=True, env=_env())
    size = out.stat().st_size
    try:
        conn = connect()
        from alerts import notify
        notify(conn, "backup_completed", f"pg_dump written: {out.name} ({size / 1e6:.1f} MB)",
               payload={"file": str(out), "bytes": size}, severity="info")
        conn.close()
    except Exception as exc:  # the backup itself succeeded; event is best effort
        print("note: could not record ops event:", exc)
    print(f"Backup written: {out} ({size / 1e6:.1f} MB)")
    return out


def restore(dump: Path, into: str) -> None:
    if into == DB_CONFIG["dbname"]:
        raise SystemExit("Refusing to restore over the live database. Use --into <other_db_name>.")
    env = _env()
    subprocess.run([_tool("createdb"), *_conn_args("postgres")[:-2], into], check=False, env=env)
    subprocess.run([_tool("pg_restore"), *_conn_args(into), "--no-owner", "--clean", "--if-exists", str(dump)],
                   check=True, env=env)
    conn = connect(dbname=into)
    with conn.cursor() as cur:
        cur.execute("SELECT (SELECT COUNT(*) FROM prospect_matrix), (SELECT COUNT(*) FROM pressure_signals), (SELECT COUNT(*) FROM pitchbook_snapshots)")
        prospects, signals, snaps = cur.fetchone()
    conn.close()
    print(f"Restored into {into}: {prospects} prospects, {signals} signals, {snaps} snapshots")
    try:
        live = connect()
        from alerts import notify
        notify(live, "restore_rehearsal", f"Restored {dump.name} into {into}: {prospects} prospects, {signals} signals",
               payload={"file": str(dump), "into": into}, severity="info")
        live.close()
    except Exception:
        pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--restore", help="dump file to restore")
    ap.add_argument("--into", help="target database name for --restore / --verify", default="delta_prospect_restore")
    ap.add_argument("--verify", action="store_true", help="backup, then restore into --into to prove the file is usable")
    args = ap.parse_args(argv)
    if args.restore:
        restore(Path(args.restore), args.into)
        return 0
    out = backup()
    if args.verify:
        restore(out, args.into)
    return 0


if __name__ == "__main__":
    sys.exit(main())
