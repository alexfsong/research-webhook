"""Course generation pipeline (backward-design + Bloom + Gagne 1-5).

Stages:
  1. Corpus slice — LI retrieve top-K against query_seed.
  2. Plan (Sonnet) — course title/objective + 3-5 Bloom-tagged lesson plans.
  3. Per-lesson write (Sonnet) — Gagne 1-5 scaffolded body using lesson-scoped retrieve.
  4. Key-claims (Haiku) — atomic cite-able claims extracted from the body.

Everything persists with status='draft'. Edit + regenerate endpoints (task #10)
promote lessons to source='edited'.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re

import anthropic

import courses_db
import llamaindex_store as li

log = logging.getLogger("courses")

PLAN_MODEL = os.environ.get("COURSE_PLAN_MODEL", "claude-sonnet-4-6")
LESSON_MODEL = os.environ.get("COURSE_LESSON_MODEL", "claude-sonnet-4-6")
CLAIMS_MODEL = os.environ.get("COURSE_CLAIMS_MODEL", "claude-haiku-4-5-20251001")

CORPUS_TOP_K = int(os.environ.get("COURSE_CORPUS_K", "30"))
LESSON_TOP_K = int(os.environ.get("COURSE_LESSON_K", "10"))
PLAN_MAX_TOKENS = int(os.environ.get("COURSE_PLAN_MAX_TOKENS", "2048"))
LESSON_MAX_TOKENS = int(os.environ.get("COURSE_LESSON_MAX_TOKENS", "3072"))
CLAIMS_MAX_TOKENS = int(os.environ.get("COURSE_CLAIMS_MAX_TOKENS", "1024"))

BLOOM_LEVELS = {"remember", "understand", "apply", "analyze", "evaluate", "create"}


def _client() -> anthropic.AsyncAnthropic:
    return anthropic.AsyncAnthropic()


def _strip_code_fence(s: str) -> str:
    s = s.strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", s, re.DOTALL)
    return m.group(1).strip() if m else s


def _parse_json(text: str) -> dict | list:
    raw = _strip_code_fence(text or "")
    try:
        return json.loads(raw)
    except Exception:
        m = re.search(r"\{.*\}|\[.*\]", raw, re.DOTALL)
        if not m:
            raise
        return json.loads(m.group(0))


def _corpus_block(hits: list[dict], per_snippet: int = 500) -> str:
    return "\n\n".join(
        f"[{i+1}] query={h.get('query','')!r} score={h.get('score',0):.3f}\n"
        f"{(h.get('text') or '')[:per_snippet]}"
        for i, h in enumerate(hits)
    )


async def _ant_text(resp) -> str:
    for block in resp.content or []:
        if getattr(block, "type", None) == "text":
            return block.text or ""
    return ""


async def _plan_course(query_seed: str, corpus: list[dict]) -> dict:
    prompt = (
        "You are designing a short self-study course from a private research corpus.\n\n"
        f"Seed question: {query_seed}\n\n"
        "Relevant excerpts from the corpus:\n---\n"
        f"{_corpus_block(corpus[:CORPUS_TOP_K])}\n---\n\n"
        "Follow backward design:\n"
        "1. Pick ONE measurable course-level objective the learner should achieve.\n"
        "2. Pick 3-5 lesson objectives that build toward it, each tagged with a Bloom level\n"
        "   (remember, understand, apply, analyze, evaluate, create). Early lessons low-Bloom,\n"
        "   later lessons higher-Bloom.\n"
        "3. Give each lesson a short title and a retrieval_query (search string we'll use\n"
        "   to pull evidence for that lesson's body).\n\n"
        "Respond ONLY as JSON (no prose):\n"
        "{\n"
        '  "title": "...",\n'
        '  "objective": "...",\n'
        '  "lessons": [\n'
        '    {"title":"...","objective":"...","bloom_level":"understand","retrieval_query":"..."}\n'
        "  ]\n"
        "}"
    )
    resp = await _client().messages.create(
        model=PLAN_MODEL, max_tokens=PLAN_MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )
    plan = _parse_json(await _ant_text(resp))
    if not isinstance(plan, dict) or "lessons" not in plan:
        raise ValueError(f"plan missing 'lessons': {plan!r}")
    lessons = plan.get("lessons") or []
    if not (1 <= len(lessons) <= 7):
        raise ValueError(f"plan lesson count out of range: {len(lessons)}")
    for l in lessons:
        bl = (l.get("bloom_level") or "").lower()
        l["bloom_level"] = bl if bl in BLOOM_LEVELS else None
    return plan


async def _write_lesson(course_title: str, course_objective: str, lesson: dict, hits: list[dict]) -> str:
    prompt = (
        "Write one lesson body for a short self-study course.\n\n"
        f"Course title: {course_title}\n"
        f"Course objective: {course_objective}\n\n"
        f"Lesson title: {lesson.get('title','')}\n"
        f"Lesson objective: {lesson.get('objective','')}\n"
        f"Bloom level: {lesson.get('bloom_level') or 'unspecified'}\n\n"
        "Ground your content ONLY in the excerpts below. If an excerpt supports a claim,\n"
        "cite it inline as [n] matching the bracketed excerpt number.\n\n"
        "Excerpts:\n---\n"
        f"{_corpus_block(hits)}\n---\n\n"
        "Structure (Gagne 1-5):\n"
        "1. Hook — one sentence that frames why this matters.\n"
        "2. Objective — restate what the learner will be able to do.\n"
        "3. Prior-knowledge bridge — one short paragraph connecting to likely prior knowledge.\n"
        "4. Content — 3-6 short paragraphs explaining the material with inline [n] citations.\n"
        "5. Guidance — a brief worked example or mental model, where applicable.\n\n"
        "Return Markdown. No course-level preamble. No lesson title heading (caller adds it)."
    )
    resp = await _client().messages.create(
        model=LESSON_MODEL, max_tokens=LESSON_MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )
    return (await _ant_text(resp)).strip()


async def _extract_claims(body_md: str, hits: list[dict]) -> list[dict]:
    prompt = (
        "Extract the atomic, cite-able claims from the lesson below. A claim is a single\n"
        "factual assertion the learner should be able to verify against a cited excerpt.\n\n"
        "Lesson body:\n---\n"
        f"{body_md}\n---\n\n"
        "Available excerpts (same numbering as the lesson's inline [n] citations):\n---\n"
        f"{_corpus_block(hits, per_snippet=260)}\n---\n\n"
        "Respond ONLY as JSON array:\n"
        '[{"claim":"...","citation_ns":[1,3]}, ...]\n'
        "Use at most 8 claims. Skip anything not grounded in an excerpt."
    )
    resp = await _client().messages.create(
        model=CLAIMS_MODEL, max_tokens=CLAIMS_MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )
    try:
        parsed = _parse_json(await _ant_text(resp))
    except Exception as e:
        log.warning("claims parse failed: %s", e)
        return []
    if not isinstance(parsed, list):
        return []
    # resolve [n] references to the actual hit metadata
    out = []
    for c in parsed[:8]:
        claim = (c.get("claim") or "").strip()
        ns = [int(n) for n in (c.get("citation_ns") or []) if isinstance(n, (int, float))]
        if not claim:
            continue
        cites = []
        for n in ns:
            if 1 <= n <= len(hits):
                h = hits[n - 1]
                cites.append({
                    "n": n,
                    "report_id": h.get("report_id", ""),
                    "thread_id": h.get("thread_id", ""),
                    "query": h.get("query", ""),
                    "snippet": (h.get("text") or "")[:320],
                    "score": h.get("score", 0.0),
                })
        out.append({"claim": claim, "citations": cites})
    return out


def _hit_to_citation(h: dict, n: int) -> dict:
    return {
        "n": n,
        "report_id": h.get("report_id", ""),
        "thread_id": h.get("thread_id", ""),
        "query": h.get("query", ""),
        "snippet": (h.get("text") or "")[:320],
        "score": h.get("score", 0.0),
    }


async def generate_course(course_id: str, query_seed: str, scope: dict) -> None:
    """Background task: run the 4-stage pipeline, persist, mark draft or failed."""
    try:
        courses_db.update_course(course_id, status="generating")

        corpus_hits = await li.retrieve(
            query_seed, k=CORPUS_TOP_K, hybrid=True, rerank=True,
        )
        if not corpus_hits:
            raise RuntimeError("corpus retrieval returned 0 hits")

        plan = await _plan_course(query_seed, corpus_hits)
        courses_db.update_course(
            course_id,
            title=plan.get("title") or "(untitled)",
            objective=plan.get("objective") or query_seed,
            model=PLAN_MODEL,
        )

        for idx, lesson in enumerate(plan.get("lessons") or []):
            rq = lesson.get("retrieval_query") or lesson.get("title") or query_seed
            lesson_hits = await li.retrieve(rq, k=LESSON_TOP_K, hybrid=True, rerank=True)
            if not lesson_hits:
                lesson_hits = corpus_hits[:LESSON_TOP_K]

            body_md = await _write_lesson(
                course_title=plan.get("title") or "",
                course_objective=plan.get("objective") or "",
                lesson=lesson, hits=lesson_hits,
            )
            key_claims = await _extract_claims(body_md, lesson_hits)
            citations = [_hit_to_citation(h, i + 1) for i, h in enumerate(lesson_hits)]

            courses_db.insert_lesson(
                course_id=course_id,
                idx=idx,
                title=lesson.get("title") or f"Lesson {idx+1}",
                objective=lesson.get("objective") or "",
                bloom_level=lesson.get("bloom_level"),
                body_md=body_md,
                key_claims=key_claims,
                citations=citations,
                retrieval={
                    "k": LESSON_TOP_K, "rerank": True,
                    "query": rq,
                    "lesson_model": LESSON_MODEL,
                    "claims_model": CLAIMS_MODEL,
                },
            )

        courses_db.update_course(course_id, status="draft")
        log.info("course %s draft ready", course_id)
    except Exception as e:
        log.exception("course %s failed", course_id)
        courses_db.update_course(course_id, status="failed", error=str(e)[:500])


async def regenerate_lesson(lesson_id: str, feedback: str) -> dict:
    """Rewrite one lesson body using user feedback, keep siblings untouched."""
    lesson = courses_db.get_lesson(lesson_id)
    if not lesson:
        raise ValueError("lesson not found")
    course = courses_db.get_course(lesson["course_id"], include_lessons=False)
    if not course:
        raise ValueError("course not found")

    rq = (lesson.get("retrieval") or {}).get("query") or lesson.get("title") or ""
    hits = await li.retrieve(rq, k=LESSON_TOP_K, hybrid=True, rerank=True)
    if not hits:
        raise RuntimeError("no retrieval hits for lesson")

    prompt = (
        "Rewrite ONE lesson body based on reviewer feedback. Keep the lesson objective unless\n"
        "the feedback requires changing it; if it must change, state the new objective in the\n"
        "first line as 'Objective: ...' and then the Markdown body.\n\n"
        f"Course title: {course.get('title','')}\n"
        f"Course objective: {course.get('objective','')}\n\n"
        f"Lesson title: {lesson.get('title','')}\n"
        f"Lesson objective: {lesson.get('objective','')}\n"
        f"Bloom level: {lesson.get('bloom_level') or 'unspecified'}\n\n"
        "Previous body:\n---\n"
        f"{lesson.get('body_md','')}\n---\n\n"
        f"Reviewer feedback:\n{feedback}\n\n"
        "Ground new content ONLY in the excerpts below, cite inline as [n].\n\n"
        "Excerpts:\n---\n"
        f"{_corpus_block(hits)}\n---\n\n"
        "Use the Gagne 1-5 structure (hook, objective, bridge, content, guidance). Return Markdown only."
    )
    resp = await _client().messages.create(
        model=LESSON_MODEL, max_tokens=LESSON_MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )
    body_md = (await _ant_text(resp)).strip()
    key_claims = await _extract_claims(body_md, hits)
    citations = [_hit_to_citation(h, i + 1) for i, h in enumerate(hits)]

    courses_db.update_lesson(
        lesson_id,
        mark_edited=True,  # stamp source='edited' + edited_at
        body_md=body_md,
        key_claims_json=json.dumps(key_claims),
        citations_json=json.dumps(citations),
        retrieval_json=json.dumps({
            "k": LESSON_TOP_K, "rerank": True, "query": rq,
            "lesson_model": LESSON_MODEL, "claims_model": CLAIMS_MODEL,
            "regenerated_with_feedback": True,
        }),
    )
    return {"lesson_id": lesson_id, "model": LESSON_MODEL, "nodes": len(hits)}


ASK_TOP_K = int(os.environ.get("COURSE_ASK_K", "8"))
ASK_MODEL = os.environ.get("COURSE_ASK_MODEL", "claude-sonnet-4-6")
ASK_MAX_TOKENS = int(os.environ.get("COURSE_ASK_MAX_TOKENS", "1536"))


async def ask_lesson(lesson_id: str, question: str) -> dict:
    """Follow-up Q&A anchored on one lesson, citing the broader corpus."""
    lesson = courses_db.get_lesson(lesson_id)
    if not lesson:
        raise ValueError("lesson not found")
    course = courses_db.get_course(lesson["course_id"], include_lessons=False)
    if not course:
        raise ValueError("course not found")

    # blend question with the lesson retrieval query so the rerank stays on-topic
    rq = (lesson.get("retrieval") or {}).get("query") or lesson.get("title") or ""
    search = f"{question}\n{rq}" if rq else question
    hits = await li.retrieve(search, k=ASK_TOP_K, hybrid=True, rerank=True)

    prompt = (
        "Answer the learner's follow-up question about one lesson in a short self-study course.\n\n"
        f"Course: {course.get('title','')}\n"
        f"Course objective: {course.get('objective','')}\n\n"
        f"Lesson: {lesson.get('title','')}\n"
        f"Lesson objective: {lesson.get('objective','')}\n\n"
        "Lesson body (reference only — do not repeat verbatim):\n---\n"
        f"{lesson.get('body_md','')}\n---\n\n"
        f"Learner question: {question}\n\n"
        "Supporting excerpts from the corpus (cite inline as [n]):\n---\n"
        f"{_corpus_block(hits)}\n---\n\n"
        "Guidelines:\n"
        "- Answer directly first, then expand with cited evidence.\n"
        "- Prefer claims the excerpts support. If the corpus doesn't cover the question,\n"
        "  say so plainly and suggest what to research next.\n"
        "- Keep it tight: 3-6 short paragraphs max. Return Markdown."
    )
    resp = await _client().messages.create(
        model=ASK_MODEL, max_tokens=ASK_MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )
    answer_md = (await _ant_text(resp)).strip()
    citations = [_hit_to_citation(h, i + 1) for i, h in enumerate(hits)]
    fid = courses_db.add_follow_up(
        lesson_id=lesson_id, question=question, answer_md=answer_md,
        citations=citations, model=ASK_MODEL,
    )
    return {
        "follow_up_id": fid, "lesson_id": lesson_id,
        "question": question, "answer_md": answer_md,
        "citations": citations, "model": ASK_MODEL,
    }
