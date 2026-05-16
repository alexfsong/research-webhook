"""Parser + tool-use payload validation.

courses.py doesn't expose a typed parsing layer: validation is woven into
`_plan_course` / `_extract_claims`. These tests pin (a) the JSON helpers
directly and (b) the validation behaviour by driving the pipeline with
canned Anthropic responses through the StubAnthropic fixture.
"""
from __future__ import annotations

import asyncio

import pytest

import courses


# ---------------------------------------------------------------------------
# 4.1 _strip_code_fence
# ---------------------------------------------------------------------------

class TestStripCodeFence:
    def test_no_fence(self):
        assert courses._strip_code_fence('{"a": 1}') == '{"a": 1}'

    def test_json_fence(self):
        assert courses._strip_code_fence('```json\n{"a": 1}\n```') == '{"a": 1}'

    def test_generic_fence(self):
        assert courses._strip_code_fence('```\n{"a": 1}\n```') == '{"a": 1}'

    def test_trims_outer_whitespace(self):
        assert courses._strip_code_fence('   \n```json\n[1,2]\n```\n  ') == '[1,2]'

    def test_nested_backticks_in_body_preserved(self):
        """Inner ``` inside fenced block is left to JSON parser (regex non-greedy)."""
        src = '```json\n{"x":"a ``` b"}\n```'
        stripped = courses._strip_code_fence(src)
        # Whatever the regex picks must still be JSON-parseable (or raise downstream).
        assert stripped.startswith("{") and stripped.endswith("}")


# ---------------------------------------------------------------------------
# 4.2 _parse_json
# ---------------------------------------------------------------------------

class TestParseJson:
    def test_plain_object(self):
        assert courses._parse_json('{"a": 1}') == {"a": 1}

    def test_fenced_array(self):
        assert courses._parse_json('```json\n[1,2,3]\n```') == [1, 2, 3]

    def test_object_with_surrounding_prose_fallback(self):
        """Fallback regex extracts a JSON object embedded in commentary."""
        text = "Here is the plan: {\"title\": \"x\", \"lessons\": []} — good luck!"
        assert courses._parse_json(text) == {"title": "x", "lessons": []}

    def test_malformed_raises(self):
        with pytest.raises(Exception):
            courses._parse_json("not json at all, and { unbalanced")

    def test_empty_input_raises(self):
        with pytest.raises(Exception):
            courses._parse_json("")


# ---------------------------------------------------------------------------
# 4.3 Plan tool-use payload validation (via _plan_course)
# ---------------------------------------------------------------------------

class TestPlanPayload:
    async def test_valid_plan_returns_typed_object(self, mock_anthropic):
        plan_json = {
            "title": "FSRS Concurrency",
            "objective": "Compare FSRS, SM-2, anki-rs concurrency tradeoffs.",
            "lessons": [
                {"title": "L1", "objective": "Understand SM-2", "bloom_level": "understand", "retrieval_query": "SM-2 basics"},
                {"title": "L2", "objective": "Apply FSRS", "bloom_level": "apply", "retrieval_query": "FSRS internals"},
                {"title": "L3", "objective": "Analyze tradeoffs", "bloom_level": "analyze", "retrieval_query": "scheduler comparison"},
            ],
        }
        mock_anthropic.queue(json_obj=plan_json)
        plan = await courses._plan_course("query", corpus=[])
        assert plan["title"] == "FSRS Concurrency"
        assert len(plan["lessons"]) == 3
        assert plan["lessons"][0]["bloom_level"] == "understand"

    async def test_invalid_bloom_level_coerced_to_none(self, mock_anthropic):
        plan_json = {
            "title": "x", "objective": "y",
            "lessons": [
                {"title": "L1", "objective": "o", "bloom_level": "memorize", "retrieval_query": "q"},
            ],
        }
        mock_anthropic.queue(json_obj=plan_json)
        plan = await courses._plan_course("q", corpus=[])
        assert plan["lessons"][0]["bloom_level"] is None

    async def test_missing_lessons_field_raises(self, mock_anthropic):
        mock_anthropic.queue(json_obj={"title": "x", "objective": "y"})
        with pytest.raises(ValueError, match="missing 'lessons'"):
            await courses._plan_course("q", corpus=[])

    async def test_lesson_count_out_of_range(self, mock_anthropic):
        mock_anthropic.queue(json_obj={"title": "x", "objective": "y", "lessons": []})
        with pytest.raises(ValueError, match="lesson count out of range"):
            await courses._plan_course("q", corpus=[])

    async def test_too_many_lessons_rejected(self, mock_anthropic):
        too_many = {
            "title": "x", "objective": "y",
            "lessons": [
                {"title": f"L{i}", "objective": "o", "bloom_level": "understand", "retrieval_query": "q"}
                for i in range(8)
            ],
        }
        mock_anthropic.queue(json_obj=too_many)
        with pytest.raises(ValueError, match="lesson count out of range"):
            await courses._plan_course("q", corpus=[])

    async def test_plan_response_fenced_json_parses(self, mock_anthropic):
        plan_json = {
            "title": "x", "objective": "y",
            "lessons": [{"title": "L", "objective": "o", "bloom_level": "remember", "retrieval_query": "q"}],
        }
        import json as _json
        mock_anthropic.queue(text=f"```json\n{_json.dumps(plan_json)}\n```")
        plan = await courses._plan_course("q", corpus=[])
        assert plan["lessons"][0]["bloom_level"] == "remember"


# ---------------------------------------------------------------------------
# 4.4 Lesson body (Gagne 1-5) — generated text just round-trips. We verify the
# stub plumbing + that empty corpus is tolerated (corpus block is generated
# unconditionally).
# ---------------------------------------------------------------------------

class TestLessonBody:
    async def test_lesson_body_returned_verbatim(self, mock_anthropic):
        mock_anthropic.queue(text="## Hook\nReason.\n\nContent body. [1]")
        body = await courses._write_lesson(
            course_title="C", course_objective="O",
            lesson={"title": "L", "objective": "o", "bloom_level": "apply"},
            hits=[{"text": "snippet", "query": "q", "score": 0.5}],
        )
        assert body.startswith("## Hook")
        assert "[1]" in body


# ---------------------------------------------------------------------------
# 4.5 Claim list validation
# ---------------------------------------------------------------------------

class TestClaims:
    SAMPLE_HITS = [
        {"text": "snippet 1", "report_id": "r1", "thread_id": "t1", "query": "q1", "score": 0.9},
        {"text": "snippet 2", "report_id": "r2", "thread_id": "t1", "query": "q2", "score": 0.8},
    ]

    async def test_valid_claims_resolve_to_hits(self, mock_anthropic):
        mock_anthropic.queue(json_obj=[
            {"claim": "A is true", "citation_ns": [1, 2]},
            {"claim": "B is also true", "citation_ns": [2]},
        ])
        claims = await courses._extract_claims("body", self.SAMPLE_HITS)
        assert len(claims) == 2
        assert claims[0]["claim"] == "A is true"
        assert [c["report_id"] for c in claims[0]["citations"]] == ["r1", "r2"]
        assert [c["n"] for c in claims[0]["citations"]] == [1, 2]

    async def test_bad_citation_index_dropped(self, mock_anthropic):
        mock_anthropic.queue(json_obj=[
            {"claim": "Has bogus refs", "citation_ns": [1, 99, 0, -3]},
        ])
        claims = await courses._extract_claims("body", self.SAMPLE_HITS)
        # only n=1 survives the 1<=n<=len(hits) gate
        assert claims == [
            {
                "claim": "Has bogus refs",
                "citations": [
                    {"n": 1, "report_id": "r1", "thread_id": "t1", "query": "q1", "snippet": "snippet 1", "score": 0.9}
                ],
            }
        ]

    async def test_empty_claim_text_skipped(self, mock_anthropic):
        mock_anthropic.queue(json_obj=[
            {"claim": "   ", "citation_ns": [1]},
            {"claim": "Real claim", "citation_ns": [1]},
        ])
        claims = await courses._extract_claims("body", self.SAMPLE_HITS)
        assert [c["claim"] for c in claims] == ["Real claim"]

    async def test_truncates_at_eight(self, mock_anthropic):
        nine = [{"claim": f"c{i}", "citation_ns": [1]} for i in range(9)]
        mock_anthropic.queue(json_obj=nine)
        claims = await courses._extract_claims("body", self.SAMPLE_HITS)
        assert len(claims) == 8

    async def test_non_list_response_returns_empty(self, mock_anthropic):
        mock_anthropic.queue(json_obj={"oops": "object not list"})
        claims = await courses._extract_claims("body", self.SAMPLE_HITS)
        assert claims == []

    async def test_unparseable_response_returns_empty(self, mock_anthropic):
        mock_anthropic.queue(text="this is not json")
        claims = await courses._extract_claims("body", self.SAMPLE_HITS)
        assert claims == []
