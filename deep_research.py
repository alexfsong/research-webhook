"""Deep-research orchestrator: multi-round gap-driven loop over the LI corpus.

Each round emits one structured `{sections[], gap_queries[]}` payload. The
default backend is the `/deep-synth` local skill invoked via `claude -p` so
per-round LLM cost charges against the operator's Claude Pro/Max subscription
rather than `ANTHROPIC_API_KEY`. Set `ASK_DEPTH_DEEP_BACKEND=api` to use the
Anthropic SDK with the API key instead (for testing / when the subscription
path is unavailable).

The orchestrator dedups queries, enforces budgets, snapshots per-section
citation maps, then POSTs the assembled report back to /ask_callback.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any

import httpx

import llamaindex_store as li

log = logging.getLogger("deep_research")

DEEP_BACKEND = os.environ.get("ASK_DEPTH_DEEP_BACKEND", "subscription").strip().lower()
DEEP_MODEL = os.environ.get("ASK_DEPTH_DEEP_MODEL", "claude-sonnet-4-6")
DEEP_PER_QUERY_K = int(os.environ.get("ASK_DEPTH_DEEP_PER_QUERY_K", "6"))
DEEP_ROUND_MAX_TOKENS = int(os.environ.get("ASK_DEPTH_DEEP_ROUND_MAX_TOKENS", "4096"))
DEEP_SUBPROCESS_TIMEOUT = int(os.environ.get("ASK_DEPTH_DEEP_SUBPROCESS_TIMEOUT", "600"))
DEEP_PER_SNIPPET = int(os.environ.get("ASK_DEPTH_DEEP_PER_SNIPPET", "500"))
LOCAL_WEBHOOK_URL = os.environ.get("WEBHOOK_LOCAL_URL", "http://127.0.0.1:8000").rstrip("/")
WEBHOOK_API_KEY = os.environ.get("WEBHOOK_API_KEY", "").strip()
CLAUDE_FALLBACK_USER = os.environ.get("CLAUDE_FALLBACK_USER", "claude-runner").strip()
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "/home/claude-runner/.npm-global/bin/claude").strip()

ROUND_TOOL = {
    "name": "record_round",
    "description": (
        "Emit this round's section drafts plus next-round gap queries. "
        "Sections must cite only the corpus excerpts provided. Use [n] markers that map "
        "to the excerpt numbers shown. Return an empty gap_queries list when the answer is complete."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "sections": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "heading": {"type": "string"},
                        "body": {"type": "string"},
                        "citation_ns": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "description": "Excerpt numbers used by this section's [n] markers.",
                        },
                    },
                    "required": ["heading", "body", "citation_ns"],
                },
            },
            "gap_queries": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Concrete search queries to fill remaining knowledge gaps. Empty if complete.",
            },
        },
        "required": ["sections", "gap_queries"],
    },
}


def _corpus_block(hits: list[dict], per_snippet: int = 500) -> str:
    return "\n\n".join(
        f"[{i+1}] query={h.get('query','')!r} score={h.get('score',0):.3f}\n"
        f"{(h.get('text') or '')[:per_snippet]}"
        for i, h in enumerate(hits)
    )


def _hit_to_citation(h: dict, n: int) -> dict:
    return {
        "n": n,
        "report_id": h.get("report_id", ""),
        "thread_id": h.get("thread_id", ""),
        "query": h.get("query", ""),
        "snippet": (h.get("text") or "")[:320],
        "score": h.get("score", 0.0),
    }


async def _retrieve_for_queries(queries: list[str], per_q_k: int) -> list[dict]:
    """Fan-out retrieve, dedup by node_id."""
    seen: set[str] = set()
    out: list[dict] = []
    for q in queries:
        hits = await li.retrieve(q, k=per_q_k, hybrid=True, rerank=True)
        for h in hits:
            nid = h.get("node_id") or (h.get("report_id", "") + ":" + (h.get("text", "")[:32]))
            if nid in seen:
                continue
            seen.add(nid)
            out.append(h)
    return out


def _hits_to_excerpts(hits: list[dict]) -> list[dict]:
    """Compact excerpt payload for the /deep-synth skill input."""
    return [
        {
            "n": i + 1,
            "query": h.get("query", ""),
            "score": float(h.get("score", 0.0)),
            "text": (h.get("text") or "")[:DEEP_PER_SNIPPET],
        }
        for i, h in enumerate(hits)
    ]


_JSON_BLOCK_RE = re.compile(r"\{(?:[^{}]|\{[^{}]*\})*\}", re.DOTALL)


def _extract_last_json(stdout: str) -> dict[str, Any] | None:
    """Find the last balanced top-level JSON object printed by the skill."""
    s = (stdout or "").strip()
    if not s:
        return None
    # Strip Markdown code fences if the model wrapped output.
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.MULTILINE)
    matches = list(_JSON_BLOCK_RE.finditer(s))
    for m in reversed(matches):
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    return None


async def _synth_round_subprocess(
    run_id: str,
    question: str,
    hits: list[dict],
    prior_headings: list[str],
    iteration: int,
    executed_queries: set[str],
) -> dict[str, Any]:
    """Invoke `claude -p /deep-synth <json>` and parse the printed JSON."""
    payload = {
        "run_id": run_id,
        "iteration": iteration,
        "question": question,
        "prior_headings": prior_headings,
        "executed_queries": sorted(executed_queries),
        "excerpts": _hits_to_excerpts(hits),
        "round_max_tokens": DEEP_ROUND_MAX_TOKENS,
    }
    arg = json.dumps(payload)
    cmd = [
        "sudo", "-n", "-u", CLAUDE_FALLBACK_USER, CLAUDE_BIN,
        "-p", f"/deep-synth {arg}",
        "--allowedTools", "",
        "--permission-mode", "bypassPermissions",
    ]
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": f"/home/{CLAUDE_FALLBACK_USER}",
    }
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, env=env, cwd="/tmp",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        raise RuntimeError(f"claude binary not found at {CLAUDE_BIN}")
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=DEEP_SUBPROCESS_TIMEOUT,
        )
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"/deep-synth subprocess timed out after {DEEP_SUBPROCESS_TIMEOUT}s")
    if proc.returncode != 0:
        raise RuntimeError(
            f"/deep-synth exited rc={proc.returncode}: {stderr_b.decode(errors='replace')[:300]}"
        )
    out_text = stdout_b.decode(errors="replace")
    parsed = _extract_last_json(out_text)
    if not parsed:
        raise RuntimeError(f"/deep-synth produced no parseable JSON. stdout head: {out_text[:300]!r}")
    return {
        "sections": parsed.get("sections") or [],
        "gap_queries": parsed.get("gap_queries") or [],
        # Subscription path: no token usage reported by the subprocess. Approximate
        # against the round cap so the token budget still drains across rounds.
        "tokens_used": DEEP_ROUND_MAX_TOKENS,
    }


async def _synth_round_sdk(
    question: str,
    hits: list[dict],
    prior_headings: list[str],
    iteration: int,
    executed_queries: set[str],
) -> dict[str, Any]:
    """API-key Anthropic SDK fallback (only when ASK_DEPTH_DEEP_BACKEND=api)."""
    import anthropic  # lazy; subscription path doesn't need the SDK
    prior_str = "\n".join(f"- {h}" for h in prior_headings) or "(none — this is the first round)"
    executed_str = "\n".join(f"- {q}" for q in sorted(executed_queries)) or "(none)"
    prompt = (
        "You are drafting one round of a sectioned long-form research report.\n\n"
        f"Original question: {question}\n\n"
        f"Round: {iteration + 1}\n\n"
        f"Sections already drafted in prior rounds (do not duplicate):\n{prior_str}\n\n"
        f"Queries already executed (do not repeat):\n{executed_str}\n\n"
        "Available corpus excerpts:\n---\n"
        f"{_corpus_block(hits, per_snippet=DEEP_PER_SNIPPET)}\n---\n\n"
        "Tasks:\n"
        "1. Draft 1-3 NEW sections that advance the report. Each section MUST cite the corpus inline as [n].\n"
        "2. Identify remaining knowledge gaps and emit concrete search queries for the next round.\n"
        "3. Return an empty gap_queries list when the report is complete.\n\n"
        "Call the record_round tool with the result. Do not output prose outside the tool call."
    )
    resp = await anthropic.AsyncAnthropic().messages.create(
        model=DEEP_MODEL,
        max_tokens=DEEP_ROUND_MAX_TOKENS,
        tools=[ROUND_TOOL],
        tool_choice={"type": "tool", "name": "record_round"},
        messages=[{"role": "user", "content": prompt}],
    )
    tool_input: dict[str, Any] = {}
    for block in resp.content or []:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == "record_round":
            tool_input = block.input or {}
            break
    usage = getattr(resp, "usage", None)
    in_tok = getattr(usage, "input_tokens", 0) or 0
    out_tok = getattr(usage, "output_tokens", 0) or 0
    return {
        "sections": tool_input.get("sections") or [],
        "gap_queries": tool_input.get("gap_queries") or [],
        "tokens_used": in_tok + out_tok,
    }


async def _synth_round(
    run_id: str,
    question: str,
    hits: list[dict],
    prior_headings: list[str],
    iteration: int,
    executed_queries: set[str],
) -> dict[str, Any]:
    """Dispatch one synthesizer round to the configured backend."""
    if DEEP_BACKEND == "api":
        return await _synth_round_sdk(question, hits, prior_headings, iteration, executed_queries)
    return await _synth_round_subprocess(
        run_id, question, hits, prior_headings, iteration, executed_queries,
    )


def _snapshot_citations(section_in: dict, hits: list[dict]) -> tuple[dict, list[dict]]:
    """Resolve a round's section against the round-local hits list.

    Returns (section_payload, citations[]) where the section body is renumbered
    so [1..len(citations)] indices are stable for the lifetime of this section.
    """
    ns = [int(n) for n in (section_in.get("citation_ns") or []) if isinstance(n, (int, float))]
    seen: dict[int, int] = {}  # original n -> new n
    citations: list[dict] = []
    for orig in ns:
        if not (1 <= orig <= len(hits)):
            continue
        if orig in seen:
            continue
        seen[orig] = len(citations) + 1
        citations.append(_hit_to_citation(hits[orig - 1], seen[orig]))

    body = section_in.get("body") or ""
    # Renumber [n] markers in body. If a marker refers to an excerpt that didn't
    # make the citation cut, drop the marker.
    import re

    def repl(m: re.Match) -> str:
        try:
            orig = int(m.group(1))
        except ValueError:
            return m.group(0)
        new = seen.get(orig)
        return f"[{new}]" if new else ""

    body = re.sub(r"\[(\d+)\]", repl, body)

    return (
        {"heading": section_in.get("heading") or "", "body": body, "citations": citations},
        citations,
    )


async def _post_callback(run_id: str, *, status: str, report: dict | None, error: str | None = None) -> None:
    headers = {"Content-Type": "application/json"}
    if WEBHOOK_API_KEY:
        headers["Authorization"] = f"Bearer {WEBHOOK_API_KEY}"
    body: dict[str, Any] = {"run_id": run_id, "route": "cloud", "status": status}
    if report is not None:
        body["report"] = report
    if error:
        body["errors"] = [{"message": error}]
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            await c.post(f"{LOCAL_WEBHOOK_URL}/ask_callback", headers=headers, json=body)
    except httpx.HTTPError:
        log.exception("deep callback POST failed run=%s", run_id)


async def run_deep(*, run_id: str, payload: dict, bearer: str) -> None:
    """Background entry point. Always POSTs to /ask_callback on exit (success or failure)."""
    question = (payload.get("question") or "").strip()
    budget = payload.get("budget") or {}
    max_iter = int(budget.get("max_iterations", 8))
    max_tokens = int(budget.get("max_tokens", 80000))

    queries: list[str] = [question] if question else []
    executed: set[str] = set()
    all_sections: list[dict] = []
    tokens_used = 0
    termination = "iteration_cap"

    try:
        for it in range(max_iter):
            fresh = [q for q in queries if q and q not in executed]
            if not fresh:
                termination = "empty_gaps"
                break
            executed.update(fresh)

            hits = await _retrieve_for_queries(fresh, per_q_k=DEEP_PER_QUERY_K)
            if not hits:
                # No corpus context — emit a single termination section and stop.
                all_sections.append({
                    "heading": "No corpus evidence",
                    "body": "The corpus contains no documents relevant to this question. Ingest sources first, then re-ask.",
                    "citations": [],
                })
                termination = "empty_gaps"
                break

            try:
                round_out = await _synth_round(
                    run_id, question, hits, [s["heading"] for s in all_sections], it, executed,
                )
            except Exception as e:
                log.exception("deep round %d synthesizer call failed run=%s", it, run_id)
                await _post_callback(run_id, status="failed", report=None, error=f"deep round {it}: {e}")
                return

            tokens_used += round_out.get("tokens_used", 0)

            for sec in round_out.get("sections", []):
                section_payload, _ = _snapshot_citations(sec, hits)
                if section_payload["body"].strip():
                    all_sections.append(section_payload)

            gaps = [g.strip() for g in round_out.get("gap_queries", []) if g and g.strip()]
            gaps = [g for g in gaps if g not in executed]
            if not gaps:
                termination = "empty_gaps"
                break
            if tokens_used >= max_tokens:
                termination = "token_cap"
                break

            queries = gaps

        report = {
            "toc": [s["heading"] for s in all_sections],
            "sections": all_sections,
            "termination": termination,
        }
        await _post_callback(run_id, status="complete", report=report)
    except Exception as e:
        log.exception("deep run failed run=%s", run_id)
        await _post_callback(run_id, status="failed", report=None, error=str(e)[:300])
