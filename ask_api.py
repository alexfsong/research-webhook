"""Ask pipeline: POST /ask, /ask_callback, /ask_runs/{id}, /threads/* + deep-queue drainer.

Extracted from webhook.py to keep that file under the 1000-line split trigger.
Owns its own router + module-level state (in-memory ask runs, deep drainers,
pool-full cooldown). Imports tiny utilities (`check_auth`, `_bearer`, audit log
path) from webhook.py — no circular import because webhook.py imports this
module after defining those utilities.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import time
from datetime import datetime, timezone
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

import courses_db


log = logging.getLogger("webhook.ask")


# ---- Config (env vars used only by the Ask pipeline) ----------------------
ANTHROPIC_OAT = os.environ.get("ANTHROPIC_OAT", "").strip()
ROUTINE_INGEST_ASK_FIRE_URL = os.environ.get("ROUTINE_INGEST_ASK_FIRE_URL", "").strip()
ROUTINE_ASK_DEEP_FIRE_URL = os.environ.get("ROUTINE_ASK_DEEP_FIRE_URL", "").strip()
ROUTINE_FIRE_BETA = os.environ.get("ROUTINE_FIRE_BETA", "experimental-cc-routine-2026-04-01").strip()
ASK_RUN_TTL = int(os.environ.get("ASK_RUN_TTL", "3600"))
ASK_DEFAULT_MAX_FETCHES = int(os.environ.get("ASK_DEFAULT_MAX_FETCHES", "10"))
ASK_HISTORY_MAX_TURNS = int(os.environ.get("ASK_HISTORY_MAX_TURNS", "4"))
ASK_HISTORY_ANSWER_TRUNC = int(os.environ.get("ASK_HISTORY_ANSWER_TRUNC", "800"))
ASK_DEPTH_ENABLED = os.environ.get("ASK_DEPTH_ENABLED", "true").lower() == "true"
THREAD_BRANCHING_ENABLED = os.environ.get("THREAD_BRANCHING_ENABLED", "true").lower() == "true"

CLAUDE_FALLBACK_USER = os.environ.get("CLAUDE_FALLBACK_USER", "claude-runner").strip()
CLAUDE_FALLBACK_TIMEOUT = int(os.environ.get("CLAUDE_FALLBACK_TIMEOUT", "240"))
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "/home/claude-runner/.npm-global/bin/claude").strip()
POOL_FULL_COOLDOWN_S = int(os.environ.get("POOL_FULL_COOLDOWN_S", "60"))


def _depth_budget(tier: str) -> dict:
    """Read per-tier ASK_DEPTH_<TIER>_MAX_* env vars into a budget dict."""
    T = tier.upper()
    return {
        "max_searches": int(os.environ.get(f"ASK_DEPTH_{T}_MAX_SEARCHES", "1" if tier == "standard" else "8")),
        "max_fetches": int(os.environ.get(f"ASK_DEPTH_{T}_MAX_FETCHES", "5" if tier == "standard" else "25")),
        "max_tokens": int(os.environ.get(f"ASK_DEPTH_{T}_MAX_TOKENS", "8000" if tier == "standard" else "80000")),
        "max_iterations": int(os.environ.get(f"ASK_DEPTH_{T}_MAX_ITERATIONS", "1" if tier == "standard" else "8")),
    }


ASK_DEPTH_BUDGETS: dict[str, dict] = {
    "standard": _depth_budget("standard"),
    "deep": _depth_budget("deep"),
}
ASK_DEPTH_DEEP_MAX_PER_DAY = int(os.environ.get("ASK_DEPTH_DEEP_MAX_PER_DAY", "20"))
ASK_DEPTH_DEEP_MAX_DRAINERS = int(os.environ.get("ASK_DEPTH_DEEP_MAX_DRAINERS", "3"))
ASK_DEPTH_DEEP_BACKEND = os.environ.get("ASK_DEPTH_DEEP_BACKEND", "subscription").strip().lower()
ASK_DEPTH_DEEP_SUBPROCESS_TIMEOUT = int(os.environ.get("ASK_DEPTH_DEEP_SUBPROCESS_TIMEOUT", "600"))


# ---- Request / response models --------------------------------------------

class AskRequest(BaseModel):
    question: str
    mode: Literal["auto", "cloud", "local"] = "auto"
    depth: Literal["standard", "deep"] = "standard"
    thread_id: str | None = None
    max_fetches: int | None = None
    urls: list[str] | None = None
    topic: str | None = None
    # Branching: single-parent in v1, schema is DAG-ready.
    parent_thread_id: str | None = None
    parent_turn_id: str | None = None
    parent_quote: str | None = None


class ReportSection(BaseModel):
    heading: str
    body: str
    citations: list[dict] = []


class ReportPayload(BaseModel):
    toc: list[str]
    sections: list[ReportSection]
    termination: Literal["empty_gaps", "iteration_cap", "token_cap"] | None = None


class AskCallback(BaseModel):
    run_id: str
    route: Literal["cloud", "local"] = "cloud"
    status: Literal["complete", "failed"] = "complete"
    ingested: list[dict] = []
    skipped: list[dict] = []
    errors: list[dict] = []
    answer_md: str = ""
    citations: list[dict] = []
    report: ReportPayload | None = None


# ---- Module state ---------------------------------------------------------

_ask_runs: dict[str, dict] = {}
_ask_runs_lock = asyncio.Lock()
_pool_full_at: float = 0.0  # epoch seconds; cooldown window for cloud route

# Deep-tier queue accounting. Queue lives in SQLite (ask_deep_queue);
# this layer tracks which bearers have an active drainer task in-process.
_deep_drainers: dict[str, asyncio.Task] = {}
_deep_drainer_lock = asyncio.Lock()
_deep_global_sem: asyncio.Semaphore | None = None


# Filled in by webhook.py at import time so we can call back into shared utils
# without creating an import cycle.
_check_auth = None  # type: ignore[assignment]
_bearer_fn = None  # type: ignore[assignment]
_audit_log = None  # type: ignore[assignment]
_llamaindex_enabled = True
_api_key = ""


def configure(*, check_auth, bearer_fn, audit_log, llamaindex_enabled: bool, api_key: str) -> None:
    """Wire shared helpers from webhook.py. Call once at import-time wiring."""
    global _check_auth, _bearer_fn, _audit_log, _llamaindex_enabled, _api_key
    _check_auth = check_auth
    _bearer_fn = bearer_fn
    _audit_log = audit_log
    _llamaindex_enabled = llamaindex_enabled
    _api_key = api_key


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _summarize_title(question: str) -> str:
    """First sentence (or first 80 chars) of the question, sanitized for a title row."""
    q = (question or "").strip().replace("\n", " ")
    head = re.split(r"(?<=[.?!])\s+", q, maxsplit=1)[0] if q else "Untitled"
    return (head[:80] or "Untitled").rstrip()


def _deep_sem() -> asyncio.Semaphore:
    global _deep_global_sem
    if _deep_global_sem is None:
        _deep_global_sem = asyncio.Semaphore(max(1, ASK_DEPTH_DEEP_MAX_DRAINERS))
    return _deep_global_sem


async def _ensure_drainer(bearer: str) -> None:
    """Spawn the drainer for this bearer if one isn't already running."""
    async with _deep_drainer_lock:
        existing = _deep_drainers.get(bearer)
        if existing and not existing.done():
            return
        _deep_drainers[bearer] = asyncio.create_task(_drain_deep(bearer))


async def _drain_deep(bearer: str) -> None:
    """Serially pop + run deep rows for one bearer. Exits when queue is empty."""
    import deep_research  # lazy
    while True:
        row = await asyncio.to_thread(courses_db.pop_next_for_bearer, bearer)
        if not row:
            async with _deep_drainer_lock:
                _deep_drainers.pop(bearer, None)
            return
        run_id = row["run_id"]
        payload = row["payload"]
        async with _ask_runs_lock:
            run = _ask_runs.get(run_id)
            if run is not None:
                run["status"] = "running"
                run["started_at"] = time.time()
        async with _deep_sem():
            try:
                await deep_research.run_deep(run_id=run_id, payload=payload, bearer=bearer)
            except Exception as e:
                log.exception("deep run crashed run=%s", run_id)
                await _ask_fail(run_id, f"deep crash: {e}")
                await asyncio.to_thread(courses_db.mark_deep_done, run_id, error=str(e)[:300])
            else:
                await asyncio.to_thread(courses_db.mark_deep_done, run_id)


async def resume_deep_queue() -> None:
    """Revive any rows left in 'running' from a previous process, then spawn drainers.

    Called from webhook.py's startup event so the drainer lifecycle stays in one
    place per repo audit.
    """
    try:
        revived = await asyncio.to_thread(courses_db.revive_stale_running)
        if revived:
            log.info("deep queue: revived %d stale running rows", revived)
        bearers = await asyncio.to_thread(courses_db.bearers_with_queued)
        for b in bearers:
            await _ensure_drainer(b)
        if bearers:
            log.info("deep queue: spawned drainers for %d bearer(s)", len(bearers))
    except Exception:
        log.exception("deep queue startup failed")


async def _evict_old_ask_runs() -> None:
    cutoff = time.time() - ASK_RUN_TTL
    async with _ask_runs_lock:
        stale = [k for k, v in _ask_runs.items()
                 if v.get("finished_at") and v["finished_at"] < cutoff]
        for k in stale:
            _ask_runs.pop(k, None)


def _pool_full_recently() -> bool:
    return (time.time() - _pool_full_at) < POOL_FULL_COOLDOWN_S


async def _pick_ask_route(mode: str, depth: str = "standard") -> str:
    if mode == "cloud":
        return "cloud"
    if mode == "local":
        return "local"
    fire_url = ROUTINE_ASK_DEEP_FIRE_URL if depth == "deep" else ROUTINE_INGEST_ASK_FIRE_URL
    if not ANTHROPIC_OAT or not fire_url:
        return "local"
    if _pool_full_recently():
        return "local"
    return "cloud"


async def _fire_ask_routine(payload: dict, run_id: str, depth: str = "standard") -> None:
    global _pool_full_at
    fire_url = ROUTINE_ASK_DEEP_FIRE_URL if depth == "deep" else ROUTINE_INGEST_ASK_FIRE_URL
    headers = {
        "Authorization": f"Bearer {ANTHROPIC_OAT}",
        "anthropic-beta": ROUTINE_FIRE_BETA,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    body = {"text": json.dumps(payload)}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(fire_url, headers=headers, json=body)
    except httpx.HTTPError as e:
        await _ask_fail(run_id, f"routine fire transport: {e}")
        return
    pool_full = (
        r.status_code in (429, 503)
        or "pool" in r.text.lower()
        or "capacity" in r.text.lower()
    )
    if pool_full:
        _pool_full_at = time.time()
        log.warning("routine pool full; falling back to local skill (run=%s)", run_id)
        await _fire_ask_local_skill(payload, run_id, fallback_from_cloud=True)
        return
    if r.status_code >= 400:
        await _ask_fail(run_id, f"routine fire {r.status_code}: {r.text[:300]}")
        return
    try:
        body_json = r.json()
        async with _ask_runs_lock:
            run = _ask_runs.get(run_id)
            if run is not None:
                run["claude_session_url"] = body_json.get("claude_code_session_url")
    except Exception:
        log.exception("routine fire response parse failed (continuing)")


async def _fire_ask_local_skill(payload: dict, run_id: str, *, fallback_from_cloud: bool = False) -> None:
    if fallback_from_cloud:
        async with _ask_runs_lock:
            run = _ask_runs.get(run_id)
            if run is not None:
                run["route"] = "local"
                if run.get("turn_id"):
                    courses_db.update_ask_turn_route(run["turn_id"], "local")
    arg = json.dumps(payload)
    cmd = [
        "sudo", "-n", "-u", CLAUDE_FALLBACK_USER, CLAUDE_BIN,
        "-p", f"/ingest-ask {arg}",
        "--allowedTools", "WebSearch,WebFetch,Bash",
        "--permission-mode", "bypassPermissions",
    ]
    env = {
        "WEBHOOK_URL": "http://127.0.0.1:8000",
        "WEBHOOK_API_KEY": _api_key,
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
        await _ask_fail(run_id, f"claude binary not found at {CLAUDE_BIN}")
        return
    except PermissionError as e:
        await _ask_fail(run_id, f"sudo to {CLAUDE_FALLBACK_USER} denied: {e}")
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=CLAUDE_FALLBACK_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await _ask_fail(run_id, f"local skill timed out after {CLAUDE_FALLBACK_TIMEOUT}s")
        return
    if proc.returncode != 0:
        try:
            err = (await proc.stderr.read()).decode(errors="replace")[:300]
        except Exception:
            err = "subprocess returned non-zero"
        await _ask_fail(run_id, f"local skill rc={proc.returncode}: {err}")


async def _ask_fail(run_id: str, message: str) -> None:
    async with _ask_runs_lock:
        run = _ask_runs.get(run_id)
        if not run or run.get("status") in ("complete", "failed"):
            return
        run["status"] = "failed"
        run["finished_at"] = time.time()
        run["errors"].append({"message": message})
    log.warning("ask run %s failed: %s", run_id, message)


_CITE_PATTERN = re.compile(r"\[(\d+)\]")


def _resolve_carried_sources(parent_quote: str, parent_citations: list[dict],
                             *, cap: int = 5) -> list[dict]:
    """Find `[n]` markers inside `parent_quote` and resolve them against
    parent_citations. Returns [] when quote is empty or no markers overlap.

    Caps at the first `cap` distinct sources so prompt context stays bounded.
    """
    if not parent_quote or not parent_citations:
        return []
    indices: list[int] = []
    seen: set[int] = set()
    for m in _CITE_PATTERN.finditer(parent_quote):
        try:
            n = int(m.group(1))
        except ValueError:
            continue
        if n in seen:
            continue
        seen.add(n)
        indices.append(n)
        if len(indices) >= cap:
            break
    by_n = {int(c.get("n", 0)): c for c in parent_citations if c.get("n")}
    out: list[dict] = []
    for n in indices:
        c = by_n.get(n)
        if not c:
            continue
        out.append({"n": n, "title": c.get("title") or "", "url": c.get("url") or ""})
    return out


def _build_parent_context(parent_thread_id: str, parent_turn_id: str,
                          parent_quote: str | None) -> dict | None:
    """Pull parent turn + carried sources for the routine payload.

    Returns None when the parent turn can't be loaded (caller already
    validated existence, this is defensive against race conditions).
    """
    turn = courses_db.get_ask_turn(parent_turn_id)
    if not turn or turn.get("thread_id") != parent_thread_id:
        return None
    parent_question = turn.get("question") or ""
    parent_answer = turn.get("answer_md") or ""
    parent_citations = turn.get("citations") or []

    answer_excerpt = parent_answer
    if len(answer_excerpt) > ASK_HISTORY_ANSWER_TRUNC:
        answer_excerpt = answer_excerpt[:ASK_HISTORY_ANSWER_TRUNC].rstrip() + "…"

    carried = _resolve_carried_sources(parent_quote or "", parent_citations)
    out: dict = {
        "question": parent_question,
        "answer_excerpt": answer_excerpt,
        "carried_sources": carried,
    }
    if parent_quote:
        out["quote"] = parent_quote
    return out


async def _ask_kickoff(req: AskRequest, bearer: str = "anon") -> dict:
    """Shared entry point for POST /ask and POST /threads/{id}/ask."""
    if not _llamaindex_enabled:
        raise HTTPException(503, "llamaindex disabled")
    q = (req.question or "").strip()
    urls = list(req.urls or [])
    if not q and not urls:
        raise HTTPException(400, "question or urls required")

    depth = req.depth if ASK_DEPTH_ENABLED else "standard"
    if depth not in ASK_DEPTH_BUDGETS:
        raise HTTPException(422, f"unknown depth: {depth}")

    # --- Branching: validate parent fields when present ----------------
    has_parent = bool(req.parent_thread_id or req.parent_turn_id or req.parent_quote)
    if has_parent:
        if not THREAD_BRANCHING_ENABLED:
            raise HTTPException(400, "thread branching disabled")
        if not (req.parent_thread_id and req.parent_turn_id):
            raise HTTPException(400, "parent_thread_id and parent_turn_id are both required when branching")
        if not courses_db.get_ask_thread(req.parent_thread_id):
            raise HTTPException(400, "parent_thread_id not found")
        parent_turn = courses_db.get_ask_turn(req.parent_turn_id)
        if not parent_turn or parent_turn.get("thread_id") != req.parent_thread_id:
            raise HTTPException(400, "parent_turn_id does not belong to parent_thread_id")

    if has_parent:
        # Branches always force a brand-new thread (bypass thread_id reuse path).
        try:
            thread_id = courses_db.create_ask_thread(
                title=_summarize_title(q or "URL ingest"),
                parent_thread_id=req.parent_thread_id,
                parent_turn_id=req.parent_turn_id,
                parent_quote=req.parent_quote,
            )
        except courses_db.SingleParentViolation:
            # Physically unreachable for fresh tids today; here so a future
            # change to re-parenting helpers surfaces as HTTP 400, not 500.
            raise HTTPException(400, "thread already has a parent")
        is_continuation = False
    else:
        is_continuation = bool(req.thread_id)
        thread_id = req.thread_id or courses_db.create_ask_thread(
            title=_summarize_title(q or "URL ingest"),
        )
    history: list[dict] = []
    if is_continuation:
        thread = courses_db.get_ask_thread(thread_id)
        if thread:
            for t in (thread.get("turns") or []):
                a = (t.get("answer_md") or "").strip()
                if not a:
                    continue
                if len(a) > ASK_HISTORY_ANSWER_TRUNC:
                    a = a[:ASK_HISTORY_ANSWER_TRUNC].rstrip() + "…"
                history.append({"q": t.get("question") or "", "a": a})
            if len(history) > ASK_HISTORY_MAX_TURNS:
                history = history[-ASK_HISTORY_MAX_TURNS:]

    run_id = "ask_" + secrets.token_hex(8)
    budget = ASK_DEPTH_BUDGETS[depth]
    max_fetches = max(1, min(int(req.max_fetches or budget["max_fetches"]), 25))
    payload = {
        "run_id": run_id,
        "question": q,
        "thread_id": thread_id,
        "depth": depth,
        "budget": budget,
        "max_fetches": max_fetches,
        "topic": (req.topic or "").strip(),
        "urls": urls,
        "history": history,
    }
    if has_parent:
        pc = _build_parent_context(req.parent_thread_id, req.parent_turn_id, req.parent_quote)
        if pc is not None:
            payload["parent_context"] = pc

    if depth == "deep":
        today_n = await asyncio.to_thread(courses_db.count_deep_today, bearer)
        if today_n >= ASK_DEPTH_DEEP_MAX_PER_DAY:
            raise HTTPException(429, "deep_daily_cap")

    route = await _pick_ask_route(req.mode, depth)
    turn_id = courses_db.add_ask_turn(thread_id, q or "(urls)", route, run_id, depth=depth)

    initial_status = "queued" if depth == "deep" else "pending"
    async with _ask_runs_lock:
        _ask_runs[run_id] = {
            "run_id": run_id,
            "status": initial_status,
            "route": route,
            "depth": depth,
            "bearer": bearer,
            "thread_id": thread_id,
            "turn_id": turn_id,
            "question": q,
            "ingested": [],
            "skipped": [],
            "errors": [],
            "synthesis": None,
            "started_at": time.time(),
        }

    if _audit_log is not None:
        _audit_log({
            "ts": _now_iso(), "run_id": run_id, "route": route, "depth": depth,
            "thread_id": thread_id, "turn_id": turn_id,
            "q_prefix": q[:80],
            "parent_thread_id": req.parent_thread_id or "",
            "parent_turn_id": req.parent_turn_id or "",
        })

    if depth == "deep":
        await asyncio.to_thread(
            courses_db.enqueue_deep, run_id, bearer, payload,
            thread_id=thread_id, turn_id=turn_id, depth=depth,
        )
        await _ensure_drainer(bearer)
    elif route == "cloud":
        asyncio.create_task(_fire_ask_routine(payload, run_id, depth))
    else:
        asyncio.create_task(_fire_ask_local_skill(payload, run_id))

    return {"run_id": run_id, "thread_id": thread_id, "turn_id": turn_id, "route": route, "depth": depth, "status": initial_status}


def _validate_report(report: ReportPayload) -> None:
    if len(report.toc) != len(report.sections):
        raise HTTPException(400, "report.toc length must match sections length")
    for i, sec in enumerate(report.sections):
        body = sec.body or ""
        cite_idxs = {int(m.group(1)) for m in re.finditer(r"\[(\d+)\]", body)}
        cite_count = len(sec.citations or [])
        for idx in cite_idxs:
            if idx < 1 or idx > cite_count:
                raise HTTPException(400, f"section {i} cites [{idx}] but only {cite_count} citations supplied")


def _report_to_answer_md(report: ReportPayload) -> str:
    """Flatten report for legacy renderers + thread history. PWA reads payload_shape='report' from answer_md JSON."""
    return json.dumps({
        "shape": "report",
        "toc": report.toc,
        "sections": [s.dict() for s in report.sections],
        "termination": report.termination,
    })


# ---- Router ---------------------------------------------------------------

def _require_auth(authorization: str | None = Header(default=None)):
    """Delegate to webhook.check_auth wired in via configure()."""
    return _check_auth(authorization)  # type: ignore[misc]


router = APIRouter()


@router.get("/threads", dependencies=[Depends(_require_auth)])
async def threads_list(limit: int = 20):
    return courses_db.list_ask_threads(limit=max(1, min(limit, 200)))


@router.get("/threads/{thread_id}", dependencies=[Depends(_require_auth)])
async def thread_detail(thread_id: str):
    t = courses_db.get_ask_thread(thread_id)
    if not t:
        raise HTTPException(404, "thread not found")
    # Always include `parents` (length ≤ 1 in v1) so the PWA breadcrumb code path
    # doesn't have to branch on whether the field is present.
    t["parents"] = courses_db.get_ask_thread_parents(thread_id)
    return t


@router.get("/threads/{thread_id}/children", dependencies=[Depends(_require_auth)])
async def thread_children(thread_id: str):
    """Immediate child threads (one level). Sorted by created_at DESC."""
    if not courses_db.get_ask_thread(thread_id):
        raise HTTPException(404, "thread not found")
    return courses_db.get_ask_thread_children(thread_id)


@router.get("/threads/{thread_id}/descendants", dependencies=[Depends(_require_auth)])
async def thread_descendants(thread_id: str):
    """Transitive descendants for the tree-collapse Threads index renderer.

    Each entry includes a `depth` field (1 = immediate child) so the PWA can
    indent by nesting level.
    """
    if not courses_db.get_ask_thread(thread_id):
        raise HTTPException(404, "thread not found")
    return courses_db.get_ask_thread_descendants(thread_id)


@router.delete("/threads/{thread_id}", dependencies=[Depends(_require_auth)])
async def delete_thread(thread_id: str):
    if not courses_db.delete_ask_thread(thread_id):
        raise HTTPException(404, "thread not found")
    return {"deleted": thread_id}


@router.post("/threads/{thread_id}/ask", dependencies=[Depends(_require_auth)])
async def thread_ask(thread_id: str, req: AskRequest, authorization: str | None = Header(default=None)):
    if not courses_db.get_ask_thread(thread_id):
        raise HTTPException(404, "thread not found")
    req.thread_id = thread_id
    return await _ask_kickoff(req, bearer=_bearer_fn(authorization))  # type: ignore[misc]


@router.post("/ask", dependencies=[Depends(_require_auth)])
async def ask(req: AskRequest, authorization: str | None = Header(default=None)):
    return await _ask_kickoff(req, bearer=_bearer_fn(authorization))  # type: ignore[misc]


@router.post("/ask_callback", dependencies=[Depends(_require_auth)])
async def ask_callback(cb: AskCallback):
    if cb.report is not None:
        _validate_report(cb.report)
        answer_md = _report_to_answer_md(cb.report)
        flat_citations: list[dict] = []
        for sec in cb.report.sections:
            flat_citations.extend(sec.citations or [])
        citations = flat_citations
        payload_shape = "report"
        syn = {"answer": answer_md, "citations": citations, "report": cb.report.dict()}
    else:
        answer_md = cb.answer_md or ""
        citations = cb.citations or []
        payload_shape = "flat"
        syn = {"answer": answer_md, "citations": citations} if answer_md else None

    async with _ask_runs_lock:
        run = _ask_runs.get(cb.run_id)
        if run is None:
            return {"ok": False, "reason": "unknown run_id"}
        if run.get("status") in ("complete", "failed"):
            return {"ok": True, "noop": True}
        run["ingested"] = cb.ingested or []
        run["skipped"] = cb.skipped or []
        run["errors"] = list(run.get("errors", [])) + (cb.errors or [])
        run["synthesis"] = syn
        run["status"] = "complete" if cb.status == "complete" else "failed"
        run["finished_at"] = time.time()
        run["payload_shape"] = payload_shape
        turn_id = run.get("turn_id")

    if turn_id:
        try:
            courses_db.update_ask_turn(
                turn_id,
                ingested_doc_ids=[i.get("doc_id") for i in (cb.ingested or []) if i.get("doc_id")],
                answer_md=answer_md,
                citations=citations,
                payload_shape=payload_shape,
            )
        except Exception:
            log.exception("ask_turn persist failed run=%s turn=%s", cb.run_id, turn_id)

    asyncio.create_task(_evict_old_ask_runs())
    return {"ok": True}


@router.get("/ask_runs/{run_id}", dependencies=[Depends(_require_auth)])
async def ask_run_status(run_id: str):
    async with _ask_runs_lock:
        run = _ask_runs.get(run_id)
    if not run:
        # Fall back to the persisted turn if the in-memory run was evicted.
        with courses_db.get_conn() as conn:
            row = conn.execute(
                "SELECT id, thread_id, route, answer_md, citations_json, "
                "ingested_doc_ids_json, created_at FROM ask_turns WHERE run_id=?",
                (run_id,),
            ).fetchone()
        if not row:
            raise HTTPException(404, "run not found (expired or never existed)")
        return {
            "run_id": run_id,
            "status": "complete" if row["answer_md"] else "expired",
            "route": row["route"],
            "thread_id": row["thread_id"],
            "turn_id": row["id"],
            "ingested": [{"doc_id": d} for d in json.loads(row["ingested_doc_ids_json"] or "[]")],
            "skipped": [],
            "errors": [],
            "synthesis": ({
                "answer": row["answer_md"],
                "citations": json.loads(row["citations_json"] or "[]"),
            } if row["answer_md"] else None),
        }
    resp = {
        "run_id": run_id,
        "status": run["status"],
        "route": run["route"],
        "depth": run.get("depth", "standard"),
        "thread_id": run["thread_id"],
        "turn_id": run["turn_id"],
        "ingested": run.get("ingested", []),
        "skipped": run.get("skipped", []),
        "errors": run.get("errors", []),
        "synthesis": run.get("synthesis"),
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
        "claude_code_session_url": run.get("claude_session_url"),
    }
    if run.get("depth") == "deep" and run["status"] in ("queued", "running", "pending"):
        try:
            qp = await asyncio.to_thread(courses_db.queue_position, run_id)
            if qp:
                resp["queue_position"] = qp.get("queue_position", 0)
                resp["queue_total"] = qp.get("queue_total", 0)
                if qp.get("status") == "running" and resp["status"] == "queued":
                    resp["status"] = "running"
        except Exception:
            log.exception("queue_position lookup failed run=%s", run_id)
    return resp
