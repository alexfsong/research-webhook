"""End-to-end pipeline tests with mocked Anthropic + retrieval.

Drives `courses.generate_course` against the in-memory courses_db and a
stubbed `li.retrieve`. Verifies that:
  - one course row + N lesson rows land on success,
  - claim FKs point at the right lesson,
  - retry path (transient error followed by success) reaches `draft`,
  - empty-retrieval path lands the course in `failed` with a reason.
"""
from __future__ import annotations

import asyncio
import json

import pytest

import courses
import courses_db


@pytest.fixture
def patch_db(mem_courses_db, monkeypatch):
    """Bind `courses` to the test instance of `courses_db`.

    mem_courses_db monkeypatches DB_PATH; that's enough for module-level calls
    inside courses.generate_course because it imports courses_db as a module
    (no re-binding needed). This fixture is a no-op alias kept for readability
    so each pipeline test reads as a 3-fixture composition.
    """
    return mem_courses_db


@pytest.fixture
def fake_retrieve(monkeypatch, sample_retrieval_results):
    """Patch `courses.li.retrieve` to return canned hits.

    The default returns the deterministic 3-hit sample. Tests can override per
    call by stuffing `fake_retrieve.queue` with lists of hits.
    """

    class _Retrieve:
        def __init__(self) -> None:
            self.queue: list[list[dict]] = []
            self.default = sample_retrieval_results
            self.calls: list[dict] = []

        async def __call__(self, query, k=10, hybrid=True, rerank=True):
            self.calls.append({"query": query, "k": k, "hybrid": hybrid, "rerank": rerank})
            if self.queue:
                return self.queue.pop(0)
            return list(self.default)

    fr = _Retrieve()
    monkeypatch.setattr(courses.li, "retrieve", fr)
    return fr


def _plan_payload(n_lessons: int = 3) -> dict:
    return {
        "title": "FSRS Concurrency",
        "objective": "Compare FSRS, SM-2, anki-rs on high-throughput review.",
        "lessons": [
            {
                "title": f"Lesson {i+1}",
                "objective": f"Objective {i+1}",
                "bloom_level": ["understand", "apply", "analyze", "evaluate"][i % 4],
                "retrieval_query": f"sub-query {i+1}",
            }
            for i in range(n_lessons)
        ],
    }


def _queue_full_course(mock, n_lessons: int = 3) -> None:
    """Queue plan + per-lesson (body + claims) responses in order."""
    mock.queue(json_obj=_plan_payload(n_lessons))
    for i in range(n_lessons):
        mock.queue(text=f"## Hook\nLesson {i+1} body with citation [1].\n")
        mock.queue(json_obj=[
            {"claim": f"Claim {i+1}.A", "citation_ns": [1]},
            {"claim": f"Claim {i+1}.B", "citation_ns": [2]},
        ])


# ---------------------------------------------------------------------------
# 5.1 Happy path: plan + N lessons land in the DB
# ---------------------------------------------------------------------------

async def test_generate_course_writes_course_and_lessons(patch_db, mock_anthropic, fake_retrieve):
    cid = courses_db.insert_course(title="(pending)", objective="seed", scope={}, status="pending")
    _queue_full_course(mock_anthropic, n_lessons=3)

    await courses.generate_course(cid, "seed query", scope={})

    course = courses_db.get_course(cid)
    assert course["status"] == "draft"
    assert course["title"] == "FSRS Concurrency"
    assert course["objective"].startswith("Compare FSRS")
    assert len(course["lessons"]) == 3
    assert [l["idx"] for l in course["lessons"]] == [0, 1, 2]
    assert [l["bloom_level"] for l in course["lessons"]] == ["understand", "apply", "analyze"]


async def test_generate_course_attaches_claims_and_citations(patch_db, mock_anthropic, fake_retrieve):
    cid = courses_db.insert_course(title="(pending)", objective="seed", scope={}, status="pending")
    _queue_full_course(mock_anthropic, n_lessons=2)
    await courses.generate_course(cid, "seed", scope={})
    course = courses_db.get_course(cid)
    for lesson in course["lessons"]:
        assert len(lesson["key_claims"]) == 2
        # Every claim resolves to ≥1 citation whose report_id is from the hits set.
        for claim in lesson["key_claims"]:
            assert claim["citations"]
            assert all("report_id" in c for c in claim["citations"])
        # Citations array uses 1-based n matching the retrieval order.
        assert [c["n"] for c in lesson["citations"]] == [1, 2, 3]


async def test_generate_course_call_count(patch_db, mock_anthropic, fake_retrieve):
    """One plan call + (body + claims) per lesson."""
    cid = courses_db.insert_course(title="(pending)", objective="seed", scope={}, status="pending")
    _queue_full_course(mock_anthropic, n_lessons=4)
    await courses.generate_course(cid, "seed", scope={})
    # 1 plan + 4 * (body + claims) = 9
    assert len(mock_anthropic.calls) == 9
    # First call is the plan call, subsequent alternate body/claims.
    assert "Seed question" in mock_anthropic.calls[0]["messages"][0]["content"]


# ---------------------------------------------------------------------------
# 5.2 Lesson claim FK correctness (each claim row sits under the right lesson)
# ---------------------------------------------------------------------------

async def test_lesson_claims_attached_to_correct_lesson(patch_db, mock_anthropic, fake_retrieve):
    cid = courses_db.insert_course(title="(pending)", objective="seed", scope={}, status="pending")
    mock_anthropic.queue(json_obj=_plan_payload(n_lessons=2))
    # Lesson 1 gets one canned claim, lesson 2 gets another — verify they don't cross.
    mock_anthropic.queue(text="Body A")
    mock_anthropic.queue(json_obj=[{"claim": "ONLY-LESSON-1", "citation_ns": [1]}])
    mock_anthropic.queue(text="Body B")
    mock_anthropic.queue(json_obj=[{"claim": "ONLY-LESSON-2", "citation_ns": [1]}])

    await courses.generate_course(cid, "seed", scope={})
    lessons = courses_db.get_course(cid)["lessons"]
    assert lessons[0]["key_claims"][0]["claim"] == "ONLY-LESSON-1"
    assert lessons[1]["key_claims"][0]["claim"] == "ONLY-LESSON-2"


# ---------------------------------------------------------------------------
# 5.3 Retry / transient-error path
# ---------------------------------------------------------------------------

async def test_plan_failure_marks_course_failed(patch_db, mock_anthropic, fake_retrieve):
    """The pipeline does not implement a retry layer above the SDK — a hard
    error on the plan call must surface as `status=failed` rather than crash."""
    cid = courses_db.insert_course(title="(pending)", objective="seed", scope={}, status="pending")
    mock_anthropic.queue(raises=RuntimeError("upstream 429"))
    await courses.generate_course(cid, "seed", scope={})
    status = courses_db.get_status(cid)
    assert status["status"] == "failed"
    assert "upstream 429" in status["error"]


async def test_lesson_failure_aborts_remaining_and_marks_failed(patch_db, mock_anthropic, fake_retrieve):
    """If one of the lesson calls raises mid-loop, the whole course fails."""
    cid = courses_db.insert_course(title="(pending)", objective="seed", scope={}, status="pending")
    mock_anthropic.queue(json_obj=_plan_payload(n_lessons=3))
    # Lesson 1 OK
    mock_anthropic.queue(text="Body 1")
    mock_anthropic.queue(json_obj=[{"claim": "c", "citation_ns": [1]}])
    # Lesson 2 blows up on the body call
    mock_anthropic.queue(raises=RuntimeError("model timeout"))
    await courses.generate_course(cid, "seed", scope={})
    status = courses_db.get_status(cid)
    assert status["status"] == "failed"
    # Only the first lesson should have persisted before the abort.
    assert len(courses_db.get_course(cid)["lessons"]) == 1


# ---------------------------------------------------------------------------
# 5.4 Empty-retrieval degradation
# ---------------------------------------------------------------------------

async def test_empty_corpus_fails_with_reason(patch_db, mock_anthropic, fake_retrieve):
    fake_retrieve.queue = [[]]  # first call returns nothing
    cid = courses_db.insert_course(title="(pending)", objective="seed", scope={}, status="pending")
    await courses.generate_course(cid, "no-hits query", scope={})
    status = courses_db.get_status(cid)
    assert status["status"] == "failed"
    assert "0 hits" in status["error"]
    # No model calls happened — retrieval gate ran before any LLM work.
    assert mock_anthropic.calls == []


async def test_lesson_empty_retrieval_falls_back_to_corpus_slice(patch_db, mock_anthropic, fake_retrieve):
    """If a per-lesson retrieve returns nothing, courses.py reuses the
    top-K corpus slice and continues. Lesson body must still get written."""
    cid = courses_db.insert_course(title="(pending)", objective="seed", scope={}, status="pending")
    # Course-level retrieve returns hits; lesson-level retrieve returns []
    fake_retrieve.queue = [
        list(fake_retrieve.default),  # plan-stage corpus
        [],                            # lesson 1 sub-query → empty
    ]
    mock_anthropic.queue(json_obj=_plan_payload(n_lessons=1))
    mock_anthropic.queue(text="Body 1 with [1].")
    mock_anthropic.queue(json_obj=[{"claim": "c", "citation_ns": [1]}])
    await courses.generate_course(cid, "seed", scope={})
    course = courses_db.get_course(cid)
    assert course["status"] == "draft"
    assert len(course["lessons"]) == 1
    # Fallback used corpus_hits[:LESSON_TOP_K] — so citation count matches the
    # default sample (3 hits) rather than 0.
    assert len(course["lessons"][0]["citations"]) == 3


# ---------------------------------------------------------------------------
# regenerate_lesson + ask_lesson — wired through the same parsers
# ---------------------------------------------------------------------------

async def test_regenerate_lesson_marks_edited(patch_db, mock_anthropic, fake_retrieve):
    cid = courses_db.insert_course(title="c", objective="o", scope={}, status="draft")
    lid = courses_db.insert_lesson(
        course_id=cid, idx=0, title="L1", objective="o",
        bloom_level="understand", body_md="old body",
        retrieval={"query": "sub-query"},
    )
    mock_anthropic.queue(text="rewritten body")
    mock_anthropic.queue(json_obj=[{"claim": "rewritten", "citation_ns": [1]}])
    out = await courses.regenerate_lesson(lid, feedback="make it crisper")
    assert out["lesson_id"] == lid
    lesson = courses_db.get_lesson(lid)
    assert lesson["body_md"] == "rewritten body"
    assert lesson["source"] == "edited"
    assert lesson["edited_at"] is not None


async def test_ask_lesson_persists_follow_up(patch_db, mock_anthropic, fake_retrieve):
    cid = courses_db.insert_course(title="c", objective="o", scope={}, status="draft")
    lid = courses_db.insert_lesson(
        course_id=cid, idx=0, title="L1", objective="o",
        bloom_level="understand", body_md="body",
        retrieval={"query": "sub-query"},
    )
    mock_anthropic.queue(text="cited answer [1]")
    result = await courses.ask_lesson(lid, "What is FSRS?")
    assert result["answer_md"] == "cited answer [1]"
    follow_ups = courses_db.list_follow_ups(lid)
    assert len(follow_ups) == 1
    assert follow_ups[0]["question"] == "What is FSRS?"
