"""SQLite store for the course layer.

v1 endpoints use: courses, modules, lessons, follow_ups.
v2 tables (cards, reviews, progress) created now so the SRS layer can
ship without a migration later. Keep v2 columns unused until endpoints land.

Single-user for MVP: reviews.user_id / progress.user_id default 'local'.
"""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(os.path.expanduser("~/research-data/courses.db"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


SCHEMA = [
    # --- v1 (active) ---
    """
    CREATE TABLE IF NOT EXISTS courses (
      id TEXT PRIMARY KEY,
      title TEXT NOT NULL,
      objective TEXT NOT NULL,
      scope_json TEXT NOT NULL,
      status TEXT NOT NULL,
      error TEXT,
      model TEXT,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS modules (
      id TEXT PRIMARY KEY,
      course_id TEXT NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
      idx INTEGER NOT NULL,
      title TEXT NOT NULL,
      summary TEXT,
      UNIQUE(course_id, idx)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS lessons (
      id TEXT PRIMARY KEY,
      course_id TEXT NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
      module_id TEXT REFERENCES modules(id) ON DELETE SET NULL,
      idx INTEGER NOT NULL,
      title TEXT NOT NULL,
      objective TEXT NOT NULL,
      bloom_level TEXT,
      body_md TEXT NOT NULL,
      key_claims_json TEXT,
      citations_json TEXT,
      retrieval_json TEXT,
      source TEXT NOT NULL DEFAULT 'generated',
      edited_at TEXT,
      created_at TEXT NOT NULL,
      UNIQUE(course_id, idx)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS follow_ups (
      id TEXT PRIMARY KEY,
      lesson_id TEXT NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
      question TEXT NOT NULL,
      answer_md TEXT NOT NULL,
      citations_json TEXT,
      model TEXT,
      created_at TEXT NOT NULL
    )
    """,
    # --- v2 (schema-ready, no endpoints yet) ---
    """
    CREATE TABLE IF NOT EXISTS cards (
      id TEXT PRIMARY KEY,
      lesson_id TEXT NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
      course_id TEXT NOT NULL,
      kind TEXT NOT NULL,
      bloom_level TEXT,
      front TEXT NOT NULL,
      back TEXT NOT NULL,
      anchors_json TEXT,
      created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS reviews (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      card_id TEXT NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
      user_id TEXT NOT NULL DEFAULT 'local',
      rating INTEGER NOT NULL,
      reviewed_at TEXT NOT NULL,
      stability REAL,
      difficulty REAL,
      elapsed_days REAL,
      scheduled_days REAL,
      ease REAL,
      reps INTEGER,
      lapses INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS progress (
      user_id TEXT NOT NULL DEFAULT 'local',
      course_id TEXT NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
      lesson_id TEXT REFERENCES lessons(id) ON DELETE CASCADE,
      state TEXT NOT NULL,
      last_seen_at TEXT,
      completed_at TEXT,
      PRIMARY KEY(user_id, course_id, lesson_id)
    )
    """,
    # --- indexes ---
    "CREATE INDEX IF NOT EXISTS idx_lessons_course ON lessons(course_id, idx)",
    "CREATE INDEX IF NOT EXISTS idx_modules_course ON modules(course_id, idx)",
    "CREATE INDEX IF NOT EXISTS idx_followups_lesson ON follow_ups(lesson_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_courses_status ON courses(status, updated_at)",
    "CREATE INDEX IF NOT EXISTS idx_cards_lesson ON cards(lesson_id)",
    "CREATE INDEX IF NOT EXISTS idx_cards_course ON cards(course_id)",
    "CREATE INDEX IF NOT EXISTS idx_reviews_card ON reviews(card_id, reviewed_at)",
    "CREATE INDEX IF NOT EXISTS idx_reviews_user ON reviews(user_id, reviewed_at)",
]


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), isolation_level=None, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def get_conn():
    """Yield a sqlite3 connection with autocommit + FK + WAL enabled."""
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


_MIGRATIONS = [
    # (table, column, ddl) — idempotent; skipped if column already exists.
    ("lessons", "source", "ALTER TABLE lessons ADD COLUMN source TEXT NOT NULL DEFAULT 'generated'"),
    ("lessons", "edited_at", "ALTER TABLE lessons ADD COLUMN edited_at TEXT"),
]


def _existing_cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def init_db() -> dict:
    """Idempotent schema bootstrap + additive migrations. Safe on every startup."""
    with get_conn() as conn:
        for stmt in SCHEMA:
            conn.execute(stmt)
        for table, col, ddl in _MIGRATIONS:
            if col not in _existing_cols(conn, table):
                conn.execute(ddl)
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    return {"db_path": str(DB_PATH), "tables": [r["name"] for r in rows]}


# ------------------------------------------------------------------------- #
# DAO — thin helpers over the tables above. Keep SQL in this module so
# callers (webhook.py, courses.py) don't sprinkle raw queries around.
# ------------------------------------------------------------------------- #

def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def insert_course(
    title: str,
    objective: str,
    scope: dict,
    status: str = "pending",
    model: str | None = None,
) -> str:
    cid = _new_id("course")
    now = _now()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO courses(id,title,objective,scope_json,status,model,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (cid, title, objective, json.dumps(scope or {}), status, model, now, now),
        )
    return cid


def update_course(course_id: str, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = _now()
    cols = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [course_id]
    with get_conn() as conn:
        conn.execute(f"UPDATE courses SET {cols} WHERE id=?", vals)


def insert_lesson(
    course_id: str,
    idx: int,
    title: str,
    objective: str,
    bloom_level: str | None,
    body_md: str,
    key_claims: list | None = None,
    citations: list | None = None,
    retrieval: dict | None = None,
    module_id: str | None = None,
    source: str = "generated",
) -> str:
    lid = _new_id("lesson")
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO lessons(id,course_id,module_id,idx,title,objective,bloom_level,body_md,"
            "key_claims_json,citations_json,retrieval_json,source,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                lid, course_id, module_id, idx, title, objective, bloom_level, body_md,
                json.dumps(key_claims or []),
                json.dumps(citations or []),
                json.dumps(retrieval or {}),
                source, _now(),
            ),
        )
    return lid


def _lesson_row(r) -> dict:
    d = dict(r)
    d["key_claims"] = json.loads(d.pop("key_claims_json") or "[]")
    d["citations"] = json.loads(d.pop("citations_json") or "[]")
    d["retrieval"] = json.loads(d.pop("retrieval_json") or "{}")
    return d


def get_course(course_id: str, *, include_lessons: bool = True) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM courses WHERE id=?", (course_id,)).fetchone()
        if not row:
            return None
        c = dict(row)
        c["scope"] = json.loads(c.pop("scope_json") or "{}")
        if include_lessons:
            rows = conn.execute(
                "SELECT * FROM lessons WHERE course_id=? ORDER BY idx", (course_id,)
            ).fetchall()
            c["lessons"] = [_lesson_row(r) for r in rows]
    return c


def list_courses(limit: int = 50) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id,title,objective,status,model,created_at,updated_at "
            "FROM courses ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_course(course_id: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM courses WHERE id=?", (course_id,))
        return cur.rowcount > 0


def get_lesson(lesson_id: str) -> dict | None:
    with get_conn() as conn:
        r = conn.execute("SELECT * FROM lessons WHERE id=?", (lesson_id,)).fetchone()
    return _lesson_row(r) if r else None


_LESSON_WRITABLE = {
    "title", "objective", "bloom_level", "body_md",
    "key_claims_json", "citations_json", "retrieval_json",
    "idx", "module_id", "source", "edited_at",
}


def update_lesson(lesson_id: str, mark_edited: bool = True, **fields) -> bool:
    """Update lesson columns. mark_edited=True flips source='edited' + edited_at."""
    # transparently encode dict/list fields
    for k in ("key_claims", "citations", "retrieval"):
        if k in fields:
            fields[f"{k}_json"] = json.dumps(fields.pop(k) or ([] if k != "retrieval" else {}))
    fields = {k: v for k, v in fields.items() if k in _LESSON_WRITABLE}
    if not fields:
        return False
    if mark_edited:
        fields.setdefault("source", "edited")
        fields["edited_at"] = _now()
    cols = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [lesson_id]
    with get_conn() as conn:
        cur = conn.execute(f"UPDATE lessons SET {cols} WHERE id=?", vals)
        if cur.rowcount:
            row = conn.execute(
                "SELECT course_id FROM lessons WHERE id=?", (lesson_id,)
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE courses SET updated_at=? WHERE id=?", (_now(), row["course_id"]),
                )
        return cur.rowcount > 0


def append_lesson(
    course_id: str,
    title: str,
    objective: str,
    body_md: str = "",
    bloom_level: str | None = None,
    source: str = "hand_written",
) -> str | None:
    with get_conn() as conn:
        if not conn.execute("SELECT 1 FROM courses WHERE id=?", (course_id,)).fetchone():
            return None
        row = conn.execute(
            "SELECT COALESCE(MAX(idx), -1) AS m FROM lessons WHERE course_id=?",
            (course_id,),
        ).fetchone()
        next_idx = (row["m"] or -1) + 1
    return insert_lesson(
        course_id=course_id, idx=next_idx, title=title, objective=objective,
        bloom_level=bloom_level, body_md=body_md, source=source,
    )


def delete_lesson(lesson_id: str) -> str | None:
    """Delete + reindex siblings. Returns course_id on success, None if missing."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT course_id FROM lessons WHERE id=?", (lesson_id,)
        ).fetchone()
        if not row:
            return None
        course_id = row["course_id"]
        conn.execute("DELETE FROM lessons WHERE id=?", (lesson_id,))
        _reindex_lessons(conn, course_id)
        conn.execute("UPDATE courses SET updated_at=? WHERE id=?", (_now(), course_id))
    return course_id


def _reindex_lessons(conn: sqlite3.Connection, course_id: str) -> None:
    """Compact idx so (course_id, idx) stays gap-free 0..N-1."""
    rows = conn.execute(
        "SELECT id FROM lessons WHERE course_id=? ORDER BY idx", (course_id,),
    ).fetchall()
    # two-phase: shift to negative range to avoid UNIQUE clashes, then forward
    for i, r in enumerate(rows):
        conn.execute("UPDATE lessons SET idx=? WHERE id=?", (-1 - i, r["id"]))
    for i, r in enumerate(rows):
        conn.execute("UPDATE lessons SET idx=? WHERE id=?", (i, r["id"]))


def reorder_lessons(course_id: str, ordered_ids: list[str]) -> bool:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id FROM lessons WHERE course_id=?", (course_id,),
        ).fetchall()
        existing = {r["id"] for r in rows}
        if set(ordered_ids) != existing:
            return False  # caller passed an incomplete or mismatched list
        # two-phase reassignment via negative staging range
        for i, lid in enumerate(ordered_ids):
            conn.execute("UPDATE lessons SET idx=? WHERE id=?", (-1 - i, lid))
        for i, lid in enumerate(ordered_ids):
            conn.execute("UPDATE lessons SET idx=? WHERE id=?", (i, lid))
        conn.execute("UPDATE courses SET updated_at=? WHERE id=?", (_now(), course_id))
    return True


def add_follow_up(
    lesson_id: str,
    question: str,
    answer_md: str,
    citations: list | None = None,
    model: str | None = None,
) -> str:
    fid = _new_id("fu")
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO follow_ups(id,lesson_id,question,answer_md,citations_json,model,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (fid, lesson_id, question, answer_md, json.dumps(citations or []), model, _now()),
        )
    return fid


def list_follow_ups(lesson_id: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM follow_ups WHERE lesson_id=? ORDER BY created_at", (lesson_id,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["citations"] = json.loads(d.pop("citations_json") or "[]")
        out.append(d)
    return out


def get_status(course_id: str) -> dict | None:
    with get_conn() as conn:
        r = conn.execute(
            "SELECT id,status,error,model,updated_at FROM courses WHERE id=?",
            (course_id,),
        ).fetchone()
    return dict(r) if r else None


if __name__ == "__main__":
    print(init_db())
