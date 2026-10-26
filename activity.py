"""Prospect activity timeline helpers (Phase 4)."""

from psycopg2.extras import Json, RealDictCursor


def log_activity(conn, prospect_id: str, activity_type: str, *, actor: str = "api", field: str | None = None,
                 old_value=None, new_value=None, note: str | None = None, payload: dict | None = None) -> None:
    """Insert one activity row inside the caller's transaction (no commit)."""
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO prospect_activity
               (prospect_id, activity_type, actor, field, old_value, new_value, note, payload)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (prospect_id, activity_type, actor, field,
             None if old_value is None else str(old_value)[:2000],
             None if new_value is None else str(new_value)[:2000],
             note, Json(payload or {})),
        )


def recent_activity(conn, prospect_id: str, limit: int = 100) -> list[dict]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM prospect_activity WHERE prospect_id = %s ORDER BY created_at DESC LIMIT %s",
            (prospect_id, limit),
        )
        return [dict(r) for r in cur.fetchall()]
