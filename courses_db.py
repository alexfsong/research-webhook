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
    # --- ask_threads (Ask-pivot: ODR /research replacement) ---
    """
    CREATE TABLE IF NOT EXISTS ask_threads (
      id TEXT PRIMARY KEY,
      title TEXT NOT NULL,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ask_turns (
      id TEXT PRIMARY KEY,
      thread_id TEXT NOT NULL REFERENCES ask_threads(id) ON DELETE CASCADE,
      idx INTEGER NOT NULL,
      question TEXT NOT NULL,
      route TEXT NOT NULL,
      run_id TEXT NOT NULL,
      ingested_doc_ids_json TEXT NOT NULL DEFAULT '[]',
      answer_md TEXT,
      citations_json TEXT NOT NULL DEFAULT '[]',
      depth TEXT NOT NULL DEFAULT 'standard',
      payload_shape TEXT NOT NULL DEFAULT 'flat',
      created_at TEXT NOT NULL,
      UNIQUE(thread_id, idx)
    )
    """,
    # --- ask_thread_parents (thread-branching join table; DAG-ready, v1 single-parent) ---
    """
    CREATE TABLE IF NOT EXISTS ask_thread_parents (
      thread_id TEXT NOT NULL REFERENCES ask_threads(id) ON DELETE CASCADE,
      parent_thread_id TEXT NOT NULL REFERENCES ask_threads(id) ON DELETE CASCADE,
      parent_turn_id TEXT NOT NULL REFERENCES ask_turns(id) ON DELETE CASCADE,
      parent_quote TEXT,
      created_at TEXT NOT NULL
    )
    """,
    # --- ask_deep_queue (deep-research subscription-quota queue) ---
    """
    CREATE TABLE IF NOT EXISTS ask_deep_queue (
      run_id TEXT PRIMARY KEY,
      bearer TEXT NOT NULL,
      thread_id TEXT,
      turn_id TEXT,
      payload_json TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'queued',
      depth TEXT NOT NULL DEFAULT 'deep',
      enqueued_at TEXT NOT NULL,
      started_at TEXT,
      finished_at TEXT,
      error TEXT
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
    "CREATE INDEX IF NOT EXISTS idx_ask_threads_updated ON ask_threads(updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_ask_turns_thread ON ask_turns(thread_id, idx)",
    "CREATE INDEX IF NOT EXISTS idx_ask_turns_run ON ask_turns(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_deep_queue_bearer ON ask_deep_queue(bearer, status, enqueued_at)",
    "CREATE INDEX IF NOT EXISTS idx_deep_queue_status ON ask_deep_queue(status, enqueued_at)",
    # Thread branching: forward (child -> parent), reverse (parent -> children),
    # and v1 single-parent invariant. Drop the UNIQUE index to enable DAG later.
    "CREATE INDEX IF NOT EXISTS idx_ask_thread_parents_thread ON ask_thread_parents(thread_id)",
    "CREATE INDEX IF NOT EXISTS idx_ask_thread_parents_parent ON ask_thread_parents(parent_thread_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_ask_thread_parents_single_v1 ON ask_thread_parents(thread_id)",
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
    ("ask_turns", "depth", "ALTER TABLE ask_turns ADD COLUMN depth TEXT NOT NULL DEFAULT 'standard'"),
    ("ask_turns", "payload_shape", "ALTER TABLE ask_turns ADD COLUMN payload_shape TEXT NOT NULL DEFAULT 'flat'"),
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


# ------------------------------------------------------------------------- #
# ask_threads / ask_turns DAO — backs POST /ask + Conversations tab.
# ------------------------------------------------------------------------- #

class SingleParentViolation(Exception):
    """Raised when a second ask_thread_parents row would be inserted for the same thread_id.

    v1 enforces single-parent via the UNIQUE INDEX on ask_thread_parents(thread_id);
    SQLite surfaces the violation as IntegrityError, which the DAO translates to this
    application-level exception for the webhook to convert into HTTP 400.
    """


def create_ask_thread(
    title: str,
    *,
    parent_thread_id: str | None = None,
    parent_turn_id: str | None = None,
    parent_quote: str | None = None,
) -> str:
    """Create a new ask_thread. When parent_* are provided, also insert one row
    into ask_thread_parents linking the new thread to the parent turn.

    Caller is responsible for validating that parent_thread_id and parent_turn_id
    exist + belong together; this DAO just enforces the single-parent invariant
    at the SQLite layer.
    """
    tid = _new_id("thr")
    now = _now()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO ask_threads(id,title,created_at,updated_at) VALUES (?,?,?,?)",
            (tid, title, now, now),
        )
        if parent_thread_id and parent_turn_id:
            try:
                conn.execute(
                    "INSERT INTO ask_thread_parents(thread_id,parent_thread_id,parent_turn_id,parent_quote,created_at) "
                    "VALUES (?,?,?,?,?)",
                    (tid, parent_thread_id, parent_turn_id, parent_quote, now),
                )
            except sqlite3.IntegrityError as e:
                # New thread can never collide on UNIQUE(thread_id) since `tid` is
                # freshly minted, so this branch is only reachable if a future caller
                # adapts this helper for re-parenting. Translate for the webhook.
                conn.execute("DELETE FROM ask_threads WHERE id=?", (tid,))
                raise SingleParentViolation(str(e)) from e
    return tid


def get_ask_thread_parents(thread_id: str) -> list[dict]:
    """Return parent link records for `thread_id`. Always ≤ 1 entry in v1.

    Each entry: `{thread_id, turn_id, quote?, created_at}`. Drop the v1 UNIQUE
    index to allow multiple entries (DAG); the consumer shape doesn't change.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT parent_thread_id, parent_turn_id, parent_quote, created_at "
            "FROM ask_thread_parents WHERE thread_id=? ORDER BY created_at",
            (thread_id,),
        ).fetchall()
    out: list[dict] = []
    for r in rows:
        entry = {
            "thread_id": r["parent_thread_id"],
            "turn_id": r["parent_turn_id"],
            "created_at": r["created_at"],
        }
        if r["parent_quote"]:
            entry["quote"] = r["parent_quote"]
        out.append(entry)
    return out


def get_ask_thread_children(thread_id: str) -> list[dict]:
    """Return immediate child thread summaries sorted by created_at DESC.

    Each: `{id, title, first_turn_question, created_at, has_quote}`.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT t.id, t.title, t.created_at, t.updated_at, p.parent_quote, "
            "       (SELECT question FROM ask_turns WHERE thread_id=t.id ORDER BY idx LIMIT 1) AS first_question "
            "FROM ask_thread_parents p "
            "JOIN ask_threads t ON t.id = p.thread_id "
            "WHERE p.parent_thread_id=? "
            "ORDER BY t.created_at DESC",
            (thread_id,),
        ).fetchall()
    return [
        {
            "id": r["id"],
            "title": r["title"],
            "first_turn_question": r["first_question"] or "",
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
            "has_quote": bool(r["parent_quote"]),
        }
        for r in rows
    ]


def get_ask_thread_descendants(thread_id: str) -> list[dict]:
    """Transitive children of `thread_id`. Each entry adds `depth` (1 = immediate child).

    Used by the Threads-index tree renderer in the PWA. Walks the join table
    iteratively in Python rather than via a recursive CTE so the implementation
    stays readable + cycle-safe (cycles are physically impossible in v1 anyway).
    """
    visited: set[str] = {thread_id}
    out: list[dict] = []
    queue: list[tuple[str, int]] = [(thread_id, 0)]
    while queue:
        cur, depth = queue.pop(0)
        children = get_ask_thread_children(cur)
        for c in children:
            cid = c["id"]
            if cid in visited:
                continue
            visited.add(cid)
            entry = dict(c)
            entry["depth"] = depth + 1
            entry["parent_thread_id"] = cur
            out.append(entry)
            queue.append((cid, depth + 1))
    return out


def add_ask_turn(
    thread_id: str,
    question: str,
    route: str,
    run_id: str,
    depth: str = "standard",
) -> str:
    """Append a new turn. Returns turn_id; bumps thread updated_at."""
    tnid = _new_id("turn")
    now = _now()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(idx), -1) AS m FROM ask_turns WHERE thread_id=?",
            (thread_id,),
        ).fetchone()
        next_idx = (row["m"] if row and row["m"] is not None else -1) + 1
        conn.execute(
            "INSERT INTO ask_turns(id,thread_id,idx,question,route,run_id,depth,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (tnid, thread_id, next_idx, question, route, run_id, depth, now),
        )
        conn.execute(
            "UPDATE ask_threads SET updated_at=? WHERE id=?", (now, thread_id),
        )
    return tnid


def update_ask_turn(
    turn_id: str,
    *,
    ingested_doc_ids: list | None = None,
    answer_md: str | None = None,
    citations: list | None = None,
    payload_shape: str | None = None,
) -> None:
    fields: dict = {}
    if ingested_doc_ids is not None:
        fields["ingested_doc_ids_json"] = json.dumps(ingested_doc_ids)
    if answer_md is not None:
        fields["answer_md"] = answer_md
    if citations is not None:
        fields["citations_json"] = json.dumps(citations)
    if payload_shape is not None:
        fields["payload_shape"] = payload_shape
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [turn_id]
    with get_conn() as conn:
        conn.execute(f"UPDATE ask_turns SET {cols} WHERE id=?", vals)


def update_ask_turn_route(turn_id: str, route: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE ask_turns SET route=? WHERE id=?", (route, turn_id))


def get_ask_turn(turn_id: str) -> dict | None:
    with get_conn() as conn:
        r = conn.execute("SELECT * FROM ask_turns WHERE id=?", (turn_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["ingested_doc_ids"] = json.loads(d.pop("ingested_doc_ids_json") or "[]")
    d["citations"] = json.loads(d.pop("citations_json") or "[]")
    return d


def get_ask_turn_question(turn_id: str) -> str | None:
    with get_conn() as conn:
        r = conn.execute(
            "SELECT question FROM ask_turns WHERE id=?", (turn_id,),
        ).fetchone()
    return r["question"] if r else None


def get_ask_thread(thread_id: str) -> dict | None:
    with get_conn() as conn:
        t = conn.execute(
            "SELECT * FROM ask_threads WHERE id=?", (thread_id,),
        ).fetchone()
        if not t:
            return None
        rows = conn.execute(
            "SELECT * FROM ask_turns WHERE thread_id=? ORDER BY idx", (thread_id,),
        ).fetchall()
    out = dict(t)
    turns = []
    for r in rows:
        d = dict(r)
        d["ingested_doc_ids"] = json.loads(d.pop("ingested_doc_ids_json") or "[]")
        d["citations"] = json.loads(d.pop("citations_json") or "[]")
        turns.append(d)
    out["turns"] = turns
    return out


def list_ask_threads(limit: int = 50) -> list[dict]:
    """Threads index summary. Each row includes parent linkage so the PWA can
    render the tree without an N+1 lookup.

    Fields: id, title, created_at, updated_at, turn_count, parent_thread_id
    (null when this thread is a root), has_quote (bool, true when a selection-
    level branch).
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT t.id, t.title, t.created_at, t.updated_at, "
            "       (SELECT COUNT(*) FROM ask_turns WHERE thread_id=t.id) AS turn_count, "
            "       p.parent_thread_id AS parent_thread_id, "
            "       CASE WHEN p.parent_quote IS NOT NULL AND p.parent_quote <> '' THEN 1 ELSE 0 END AS has_quote "
            "FROM ask_threads t "
            "LEFT JOIN ask_thread_parents p ON p.thread_id = t.id "
            "ORDER BY t.updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        d["has_quote"] = bool(d.get("has_quote"))
        out.append(d)
    return out


def delete_ask_thread(thread_id: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM ask_threads WHERE id=?", (thread_id,))
        return cur.rowcount > 0


# ------------------------------------------------------------------------- #
# ask_deep_queue DAO — subscription-quota queue for deep-research runs.
# ------------------------------------------------------------------------- #

def enqueue_deep(
    run_id: str,
    bearer: str,
    payload: dict,
    *,
    thread_id: str | None = None,
    turn_id: str | None = None,
    depth: str = "deep",
) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO ask_deep_queue(run_id,bearer,thread_id,turn_id,payload_json,status,depth,enqueued_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (run_id, bearer, thread_id, turn_id, json.dumps(payload), "queued", depth, _now()),
        )


def pop_next_for_bearer(bearer: str) -> dict | None:
    """Atomically claim the oldest queued row for this bearer (status queued → running).

    Returns the row dict with payload decoded, or None if nothing pending.
    """
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM ask_deep_queue "
            "WHERE bearer=? AND status='queued' "
            "ORDER BY enqueued_at ASC LIMIT 1",
            (bearer,),
        ).fetchone()
        if not row:
            conn.execute("COMMIT")
            return None
        conn.execute(
            "UPDATE ask_deep_queue SET status='running', started_at=? WHERE run_id=?",
            (_now(), row["run_id"]),
        )
        conn.execute("COMMIT")
    d = dict(row)
    d["payload"] = json.loads(d.pop("payload_json") or "{}")
    d["status"] = "running"
    return d


def mark_deep_done(run_id: str, *, error: str | None = None) -> None:
    status = "failed" if error else "done"
    with get_conn() as conn:
        conn.execute(
            "UPDATE ask_deep_queue SET status=?, finished_at=?, error=? WHERE run_id=?",
            (status, _now(), error, run_id),
        )


def count_deep_today(bearer: str) -> int:
    today_prefix = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with get_conn() as conn:
        r = conn.execute(
            "SELECT COUNT(*) AS n FROM ask_deep_queue "
            "WHERE bearer=? AND enqueued_at LIKE ?",
            (bearer, f"{today_prefix}%"),
        ).fetchone()
    return int(r["n"]) if r else 0


def queue_position(run_id: str) -> dict:
    """Return {status, queue_position, queue_total, started_at, finished_at, error} for a queued/running deep run.

    queue_position counts queued rows for the same bearer with strictly earlier enqueued_at.
    queue_total counts ALL queued rows for the bearer (incl. this one).
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM ask_deep_queue WHERE run_id=?", (run_id,),
        ).fetchone()
        if not row:
            return {}
        ahead = conn.execute(
            "SELECT COUNT(*) AS n FROM ask_deep_queue "
            "WHERE bearer=? AND status='queued' AND enqueued_at < ?",
            (row["bearer"], row["enqueued_at"]),
        ).fetchone()
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM ask_deep_queue "
            "WHERE bearer=? AND status='queued'",
            (row["bearer"],),
        ).fetchone()
    return {
        "status": row["status"],
        "queue_position": int(ahead["n"]),
        "queue_total": int(total["n"]),
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "error": row["error"],
    }


def bearers_with_queued() -> list[str]:
    """Bearers that currently have at least one queued (or running) deep row.

    Used at FastAPI startup to spawn one drainer per bearer with pending work.
    Includes 'running' rows so a webhook restart mid-flight resumes the drainer
    even if the previous process didn't finish the row (it will be retried by
    pop_next_for_bearer once we mark stale 'running' rows back to 'queued').
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT bearer FROM ask_deep_queue "
            "WHERE status IN ('queued','running')"
        ).fetchall()
    return [r["bearer"] for r in rows]


def revive_stale_running() -> int:
    """Flip any 'running' rows back to 'queued' so a restart picks them up.

    Webhook restart leaves any in-flight row stuck in 'running' until reaped. Run
    this once at startup before spawning drainers.
    """
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE ask_deep_queue SET status='queued', started_at=NULL "
            "WHERE status='running'"
        )
        return cur.rowcount


if __name__ == "__main__":
    print(init_db())
