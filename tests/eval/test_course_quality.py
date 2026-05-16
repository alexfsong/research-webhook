"""Eval: end-to-end course quality on the fixture corpus.

Marked `eval` so it does NOT run in CI by default. Triggers:
  pytest -m eval

Requires:
  ANTHROPIC_API_KEY in env. The judge model is held out from the
  generation models so its scoring is not biased toward its own
  prompt habits.

Cost cap:
  EVAL_MAX_TOKENS env (default 200_000). The test fails if the
  combined input + output token count across all generation calls
  exceeds this, so a runaway prompt change can't quietly burn
  through a budget without the test going red.

Retrieval:
  Rather than spinning a real LlamaIndex over the fixture corpus
  (slow, downloads BGE embeddings) we substitute a simple lexical
  retriever that scores docs by token overlap with the query.
  This keeps eval focused on the course-generation prompts; an
  end-to-end test of LlamaIndex itself lives elsewhere.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from collections import Counter
from pathlib import Path

import pytest

import courses
import courses_db

pytestmark = pytest.mark.eval

CORPUS_DIR = Path(__file__).parent / "fixtures" / "corpus"
SEED = "Compare scheduler designs (FSRS, SM-2, anki-rs) and their concurrency tradeoffs."
JUDGE_MODEL = os.environ.get("EVAL_JUDGE_MODEL", "claude-opus-4-7")
MAX_TOKENS = int(os.environ.get("EVAL_MAX_TOKENS", "200000"))


# ---------------------------------------------------------------------------
# Fixture corpus loader + lexical retrieval shim
# ---------------------------------------------------------------------------

def _load_corpus() -> list[dict]:
    docs = []
    for p in sorted(CORPUS_DIR.glob("*.md")):
        raw = p.read_text(encoding="utf-8")
        front = {}
        body = raw
        m = re.match(r"^---\n(.*?)\n---\n(.*)$", raw, re.DOTALL)
        if m:
            for line in m.group(1).splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    front[k.strip()] = v.strip().strip("[]")
            body = m.group(2)
        docs.append({
            "path": str(p),
            "stem": p.stem,
            "url": front.get("url", ""),
            "title": front.get("title", p.stem),
            "tags": front.get("tags", ""),
            "body": body.strip(),
        })
    return docs


def _tokenize(s: str) -> list[str]:
    return [w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9\-]+", s)]


def _score(doc_tokens: Counter, q_tokens: list[str]) -> float:
    return sum(doc_tokens.get(t, 0) for t in q_tokens)


def _build_lexical_retriever():
    docs = _load_corpus()
    indexed = [(d, Counter(_tokenize(d["title"] + " " + d["body"]))) for d in docs]

    async def retrieve(query: str, k: int = 10, hybrid: bool = True, rerank: bool = True):
        qt = _tokenize(query)
        scored = [(d, _score(idx, qt)) for d, idx in indexed]
        scored.sort(key=lambda x: -x[1])
        out = []
        for i, (d, s) in enumerate(scored[:k]):
            if s <= 0:
                continue
            out.append({
                "text": d["body"],
                "score": float(s),
                "report_id": d["stem"],   # treat doc stem as the report_id
                "thread_id": "fixture",
                "query": d["url"],         # carry url through citations
                "node_id": f"{d['stem']}#0",
            })
        return out

    return retrieve, docs


# ---------------------------------------------------------------------------
# Usage tracker — wraps the real AsyncAnthropic to total tokens across calls
# ---------------------------------------------------------------------------

class _UsageTracker:
    def __init__(self, real_client):
        self._real = real_client
        self.input_tokens = 0
        self.output_tokens = 0
        self.calls = 0

    @property
    def messages(self):
        return self

    async def create(self, **kw):
        resp = await self._real.messages.create(**kw)
        self.calls += 1
        usage = getattr(resp, "usage", None)
        if usage is not None:
            self.input_tokens += getattr(usage, "input_tokens", 0) or 0
            self.output_tokens += getattr(usage, "output_tokens", 0) or 0
        return resp

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@pytest.fixture
def real_anthropic_tracked(monkeypatch):
    import anthropic
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY not set — skipping eval")
    real = anthropic.AsyncAnthropic()
    tracker = _UsageTracker(real)
    monkeypatch.setattr(courses, "_client", lambda: tracker)
    return tracker


@pytest.fixture
def fixture_retrieval(monkeypatch):
    retrieve, docs = _build_lexical_retriever()
    monkeypatch.setattr(courses.li, "retrieve", retrieve)
    return docs


# ---------------------------------------------------------------------------
# 7.1 + 7.2 + 7.4 — single eval run, multiple assertions
# ---------------------------------------------------------------------------

async def test_course_quality_end_to_end(
    mem_courses_db,
    real_anthropic_tracked,
    fixture_retrieval,
):
    cid = courses_db.insert_course(
        title="(pending)", objective=SEED, scope={"query_seed": SEED}, status="pending",
    )
    await courses.generate_course(cid, SEED, scope={"query_seed": SEED})

    # Hard token budget — fail fast before scoring.
    assert real_anthropic_tracked.total_tokens <= MAX_TOKENS, (
        f"eval run used {real_anthropic_tracked.total_tokens} tokens "
        f"({real_anthropic_tracked.calls} calls); budget {MAX_TOKENS}"
    )

    course = courses_db.get_course(cid)
    assert course["status"] == "draft", f"course failed: {course.get('error')}"

    # 7.2a — JSON shape valid
    assert 3 <= len(course["lessons"]) <= 7
    for lesson in course["lessons"]:
        assert lesson["body_md"].strip()
        assert lesson["citations"], f"lesson '{lesson['title']}' has no citations"
        assert 0 <= len(lesson["key_claims"]) <= 8

    # 7.2b — every cited n resolves to a fixture doc
    fixture_stems = {d["stem"] for d in fixture_retrieval}
    for lesson in course["lessons"]:
        for c in lesson["citations"]:
            assert c["report_id"] in fixture_stems, (
                f"citation report_id {c['report_id']!r} not in fixture corpus"
            )

    # 7.2c — Bloom-tag diversity ≥ 2
    bloom_levels = {l.get("bloom_level") for l in course["lessons"] if l.get("bloom_level")}
    assert len(bloom_levels) >= 2, f"Bloom-tag diversity too low: {bloom_levels}"

    # Capture cost figure for docs before the judge runs so a judge
    # failure still leaves the token figure in the output (printed on -s).
    print(
        f"\n[eval] course-gen tokens in/out/total="
        f"{real_anthropic_tracked.input_tokens}/"
        f"{real_anthropic_tracked.output_tokens}/"
        f"{real_anthropic_tracked.total_tokens} "
        f"calls={real_anthropic_tracked.calls}"
    )

    # 7.3 — LLM-as-judge rubric. Skip non-numeric fields like "notes".
    scores = await _judge(course)
    print(f"[eval] judge scores: {scores}")
    for key, value in scores.items():
        if not isinstance(value, (int, float)):
            continue
        assert value >= 3, f"judge gave {key}={value} (< 3); full scores: {scores}"


# ---------------------------------------------------------------------------
# Judge — single Anthropic call against a held-out model
# ---------------------------------------------------------------------------

JUDGE_PROMPT = """You are scoring a short auto-generated self-study course.

Score each criterion 1-5 (1=fails, 3=passable, 5=excellent). Respond ONLY as JSON:
{"coverage": N, "atomicity": N, "coherence": N, "notes": "one sentence"}

Criteria:
- coverage:  Do the lessons collectively cover the course objective?
- atomicity: Are key_claims atomic, single-fact assertions (not paragraphs)?
- coherence: Does each lesson body flow logically and stay on-topic for that lesson's objective?

Course:
"""


async def _judge(course: dict) -> dict:
    import anthropic
    summary = {
        "title": course["title"],
        "objective": course["objective"],
        "lessons": [
            {
                "title": l["title"],
                "objective": l["objective"],
                "bloom_level": l.get("bloom_level"),
                "body_md": (l["body_md"] or "")[:2000],
                "key_claims": [c["claim"] for c in l.get("key_claims", [])],
            }
            for l in course["lessons"]
        ],
    }
    prompt = JUDGE_PROMPT + json.dumps(summary, indent=2)
    client = anthropic.AsyncAnthropic()
    resp = await client.messages.create(
        model=JUDGE_MODEL,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    text = ""
    for b in resp.content or []:
        if getattr(b, "type", None) == "text":
            text = b.text or ""
            break
    try:
        return courses._parse_json(text)
    except Exception as e:  # noqa: BLE001
        pytest.fail(f"judge returned unparseable JSON: {e}\n---\n{text}")
