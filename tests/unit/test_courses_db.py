"""DAO round-trips + cascades + idempotent migration."""
from __future__ import annotations

import pytest


def _new_course(db, **overrides):
    return db.insert_course(
        title=overrides.get("title", "Test Course"),
        objective=overrides.get("objective", "Demonstrate testing"),
        scope=overrides.get("scope", {"query_seed": "test"}),
        status=overrides.get("status", "pending"),
        model=overrides.get("model"),
    )


def _new_lesson(db, course_id, idx=0, **overrides):
    return db.insert_lesson(
        course_id=course_id,
        idx=idx,
        title=overrides.get("title", f"Lesson {idx}"),
        objective=overrides.get("objective", "Learn it"),
        bloom_level=overrides.get("bloom_level", "understand"),
        body_md=overrides.get("body_md", "Body."),
        key_claims=overrides.get("key_claims", [{"claim": "x", "citations": []}]),
        citations=overrides.get("citations", [{"n": 1, "report_id": "r1"}]),
        retrieval=overrides.get("retrieval", {"k": 10}),
    )


# ---------------------------------------------------------------------------
# 3.1 Round-trip every public DAO
# ---------------------------------------------------------------------------

def test_course_insert_get_roundtrip(mem_courses_db):
    db = mem_courses_db
    cid = _new_course(db, model="claude-sonnet-4-6")
    got = db.get_course(cid)
    assert got["id"] == cid
    assert got["title"] == "Test Course"
    assert got["objective"] == "Demonstrate testing"
    assert got["scope"] == {"query_seed": "test"}
    assert got["status"] == "pending"
    assert got["model"] == "claude-sonnet-4-6"
    assert got["lessons"] == []


def test_course_update_fields(mem_courses_db):
    db = mem_courses_db
    cid = _new_course(db)
    db.update_course(cid, status="draft", title="Renamed")
    got = db.get_course(cid)
    assert got["status"] == "draft"
    assert got["title"] == "Renamed"


def test_lesson_insert_get_roundtrip(mem_courses_db):
    db = mem_courses_db
    cid = _new_course(db)
    lid = _new_lesson(db, cid, idx=0, body_md="Hello [1]")
    lesson = db.get_lesson(lid)
    assert lesson["id"] == lid
    assert lesson["course_id"] == cid
    assert lesson["idx"] == 0
    assert lesson["bloom_level"] == "understand"
    assert lesson["body_md"] == "Hello [1]"
    assert lesson["key_claims"] == [{"claim": "x", "citations": []}]
    assert lesson["citations"] == [{"n": 1, "report_id": "r1"}]
    assert lesson["retrieval"] == {"k": 10}
    assert lesson["source"] == "generated"


def test_lesson_update_marks_edited(mem_courses_db):
    db = mem_courses_db
    cid = _new_course(db)
    lid = _new_lesson(db, cid)
    assert db.update_lesson(lid, body_md="Edited body") is True
    lesson = db.get_lesson(lid)
    assert lesson["body_md"] == "Edited body"
    assert lesson["source"] == "edited"
    assert lesson["edited_at"] is not None


def test_lesson_update_without_mark_edited(mem_courses_db):
    db = mem_courses_db
    cid = _new_course(db)
    lid = _new_lesson(db, cid)
    db.update_lesson(lid, mark_edited=False, body_md="Silent rewrite")
    lesson = db.get_lesson(lid)
    assert lesson["source"] == "generated"
    assert lesson["edited_at"] is None


def test_list_courses_orders_by_updated_at_desc(mem_courses_db):
    db = mem_courses_db
    cid1 = _new_course(db, title="A")
    cid2 = _new_course(db, title="B")
    # Bump cid1 most recent
    db.update_course(cid1, status="draft")
    rows = db.list_courses()
    titles = [r["title"] for r in rows]
    assert titles[0] == "A"
    assert "B" in titles


def test_append_lesson_increments_idx(mem_courses_db):
    db = mem_courses_db
    cid = _new_course(db)
    _new_lesson(db, cid, idx=0)
    _new_lesson(db, cid, idx=1)
    lid_appended = db.append_lesson(cid, title="Third", objective="...")
    assert lid_appended is not None
    lesson = db.get_lesson(lid_appended)
    assert lesson["idx"] == 2


def test_delete_lesson_reindexes(mem_courses_db):
    db = mem_courses_db
    cid = _new_course(db)
    lids = [_new_lesson(db, cid, idx=i, title=f"L{i}") for i in range(3)]
    assert db.delete_lesson(lids[1]) == cid
    remaining = db.get_course(cid)["lessons"]
    assert [l["idx"] for l in remaining] == [0, 1]
    assert [l["title"] for l in remaining] == ["L0", "L2"]


def test_reorder_lessons(mem_courses_db):
    db = mem_courses_db
    cid = _new_course(db)
    lids = [_new_lesson(db, cid, idx=i, title=f"L{i}") for i in range(3)]
    assert db.reorder_lessons(cid, [lids[2], lids[0], lids[1]]) is True
    lessons = db.get_course(cid)["lessons"]
    assert [l["title"] for l in lessons] == ["L2", "L0", "L1"]


def test_reorder_rejects_mismatched_set(mem_courses_db):
    db = mem_courses_db
    cid = _new_course(db)
    lids = [_new_lesson(db, cid, idx=i) for i in range(2)]
    assert db.reorder_lessons(cid, [lids[0]]) is False  # missing one
    assert db.reorder_lessons(cid, [lids[0], lids[1], "lesson_bogus"]) is False


def test_follow_ups_roundtrip(mem_courses_db):
    db = mem_courses_db
    cid = _new_course(db)
    lid = _new_lesson(db, cid)
    fid = db.add_follow_up(
        lesson_id=lid, question="Q?", answer_md="A.",
        citations=[{"n": 1, "report_id": "rep_x"}], model="haiku",
    )
    rows = db.list_follow_ups(lid)
    assert len(rows) == 1
    assert rows[0]["id"] == fid
    assert rows[0]["question"] == "Q?"
    assert rows[0]["citations"] == [{"n": 1, "report_id": "rep_x"}]


def test_status_endpoint_minimal_view(mem_courses_db):
    db = mem_courses_db
    cid = _new_course(db, model="claude-haiku-4-5")
    st = db.get_status(cid)
    assert st["status"] == "pending"
    assert st["model"] == "claude-haiku-4-5"
    assert "error" in st


# ---------------------------------------------------------------------------
# 3.2 FK cascade
# ---------------------------------------------------------------------------

def test_delete_course_cascades_lessons_and_followups(mem_courses_db):
    db = mem_courses_db
    cid = _new_course(db)
    lid = _new_lesson(db, cid)
    db.add_follow_up(lesson_id=lid, question="?", answer_md=".")
    assert db.delete_course(cid) is True
    assert db.get_course(cid) is None
    assert db.get_lesson(lid) is None
    assert db.list_follow_ups(lid) == []


# ---------------------------------------------------------------------------
# 3.3 Idempotent migration
# ---------------------------------------------------------------------------

def test_init_db_idempotent(mem_courses_db):
    db = mem_courses_db
    info1 = db.init_db()
    info2 = db.init_db()
    assert info1["tables"] == info2["tables"]
    # Adding fresh data after the second init still works.
    cid = _new_course(db)
    assert db.get_course(cid) is not None


def test_migration_columns_present(mem_courses_db):
    """Additive migrations in _MIGRATIONS apply on init_db."""
    db = mem_courses_db
    with db.get_conn() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(ask_turns)").fetchall()}
    assert {"depth", "payload_shape"}.issubset(cols)
    with db.get_conn() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(lessons)").fetchall()}
    assert {"source", "edited_at"}.issubset(cols)


# ---------------------------------------------------------------------------
# 3.4 ask_threads / ask_turns DAO
# ---------------------------------------------------------------------------

def test_ask_thread_and_turns_roundtrip(mem_courses_db):
    db = mem_courses_db
    tid = db.create_ask_thread("FSRS deep dive")
    t1 = db.add_ask_turn(tid, question="Q1", route="local", run_id="run_1")
    t2 = db.add_ask_turn(tid, question="Q2", route="cloud", run_id="run_2", depth="deep")

    db.update_ask_turn(
        t1, answer_md="A1", citations=[{"n": 1, "report_id": "r_1"}],
        ingested_doc_ids=["doc_a"], payload_shape="flat",
    )
    db.update_ask_turn(
        t2, answer_md='{"shape":"report","toc":[],"sections":[]}',
        payload_shape="report",
    )

    thread = db.get_ask_thread(tid)
    assert thread["title"] == "FSRS deep dive"
    assert len(thread["turns"]) == 2
    assert thread["turns"][0]["idx"] == 0
    assert thread["turns"][0]["answer_md"] == "A1"
    assert thread["turns"][0]["citations"] == [{"n": 1, "report_id": "r_1"}]
    assert thread["turns"][0]["ingested_doc_ids"] == ["doc_a"]
    assert thread["turns"][0]["depth"] == "standard"
    assert thread["turns"][0]["payload_shape"] == "flat"
    assert thread["turns"][1]["depth"] == "deep"
    assert thread["turns"][1]["payload_shape"] == "report"


def test_ask_turn_question_lookup(mem_courses_db):
    db = mem_courses_db
    tid = db.create_ask_thread("t")
    turn = db.add_ask_turn(tid, question="What is FSRS?", route="local", run_id="r")
    assert db.get_ask_turn_question(turn) == "What is FSRS?"
    assert db.get_ask_turn_question("turn_bogus") is None


def test_list_ask_threads_includes_turn_count(mem_courses_db):
    db = mem_courses_db
    tid = db.create_ask_thread("alpha")
    db.add_ask_turn(tid, question="q1", route="local", run_id="r1")
    db.add_ask_turn(tid, question="q2", route="local", run_id="r2")
    threads = db.list_ask_threads()
    row = next(t for t in threads if t["id"] == tid)
    assert row["turn_count"] == 2


def test_delete_ask_thread_cascades_turns(mem_courses_db):
    db = mem_courses_db
    tid = db.create_ask_thread("doomed")
    turn = db.add_ask_turn(tid, question="q", route="local", run_id="r")
    assert db.delete_ask_thread(tid) is True
    assert db.get_ask_thread(tid) is None
    assert db.get_ask_turn(turn) is None


# ---------------------------------------------------------------------------
# Deep queue DAO — covers §9 plumbing from deep-research-mode.
# ---------------------------------------------------------------------------

def test_enqueue_pop_mark_done(mem_courses_db):
    db = mem_courses_db
    db.enqueue_deep("run_a", "bearer_x", {"question": "Q"}, depth="deep")
    row = db.pop_next_for_bearer("bearer_x")
    assert row is not None
    assert row["run_id"] == "run_a"
    assert row["payload"] == {"question": "Q"}
    assert row["status"] == "running"
    db.mark_deep_done("run_a")
    pos = db.queue_position("run_a")
    assert pos["status"] == "done"


def test_count_deep_today(mem_courses_db):
    db = mem_courses_db
    db.enqueue_deep("r1", "b1", {})
    db.enqueue_deep("r2", "b1", {})
    db.enqueue_deep("r3", "b2", {})
    assert db.count_deep_today("b1") == 2
    assert db.count_deep_today("b2") == 1
    assert db.count_deep_today("b_other") == 0


def test_queue_position_ordering(mem_courses_db):
    db = mem_courses_db
    db.enqueue_deep("r1", "b", {})
    db.enqueue_deep("r2", "b", {})
    db.enqueue_deep("r3", "b", {})
    p1 = db.queue_position("r1")
    p3 = db.queue_position("r3")
    assert p1["queue_position"] == 0
    assert p3["queue_position"] == 2
    assert p1["queue_total"] == 3


def test_revive_stale_running(mem_courses_db):
    db = mem_courses_db
    db.enqueue_deep("r1", "b", {})
    db.pop_next_for_bearer("b")  # → running
    assert db.revive_stale_running() == 1
    row = db.pop_next_for_bearer("b")
    assert row is not None and row["run_id"] == "r1"


def test_bearers_with_queued(mem_courses_db):
    db = mem_courses_db
    db.enqueue_deep("r1", "alice", {})
    db.enqueue_deep("r2", "bob", {})
    bearers = set(db.bearers_with_queued())
    assert bearers == {"alice", "bob"}
