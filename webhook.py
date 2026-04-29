"""FastAPI webhook for triggering deep research runs + PWA backend."""
import asyncio
import hashlib
import json
import logging
import os
import re
import sys
import time
import traceback
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from langgraph_sdk import get_client
from pydantic import BaseModel

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import persist  # noqa: E402 — shared ChromaDB client (imported for side-effect of initializing _client)
import courses_db  # noqa: E402 — sqlite course store
import courses  # noqa: E402 — course generation pipeline
courses_db.init_db()  # idempotent schema bootstrap


def _load_env_file(path: str) -> None:
    """Lightweight .env loader so the webhook shares keys with the agent service."""
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except FileNotFoundError:
        pass


_load_env_file(os.path.expanduser("~/open_deep_research/.env"))
_load_env_file(os.path.expanduser("~/research-agent-tools/.env"))

BASE_DIR = Path(__file__).parent.resolve()
STATIC_DIR = BASE_DIR / "static"
LANGGRAPH_URL = os.environ.get("LANGGRAPH_URL", "http://127.0.0.1:2024")
ASSISTANT_ID = os.environ.get("LANGGRAPH_ASSISTANT", "Deep Researcher")
RUN_TIMEOUT_SECONDS = int(os.environ.get("RUN_TIMEOUT_SECONDS", "1800"))
SYNTHESIS_RATE_PER_MIN = int(os.environ.get("SYNTHESIS_RATE_PER_MIN", "10"))
LLAMAINDEX_ENABLED = os.environ.get("LLAMAINDEX_ENABLED", "true").lower() == "true"
REPORTS_DIR = Path(os.path.expanduser("~/research-data/reports")).resolve()
REPORTS_DIR.mkdir(parents=True, exist_ok=True)
ERRORS_DIR = (REPORTS_DIR.parent / "errors").resolve()
ERRORS_DIR.mkdir(parents=True, exist_ok=True)
API_KEY = os.environ.get("WEBHOOK_API_KEY", "").strip()
ANTHROPIC_OAT = os.environ.get("ANTHROPIC_OAT", "").strip()
ROUTINE_RESEARCH_FIRE_URL = os.environ.get("ROUTINE_RESEARCH_FIRE_URL", "").strip()
ROUTINE_FIRE_BETA = os.environ.get("ROUTINE_FIRE_BETA", "experimental-cc-routine-2026-04-01").strip()
RESEARCH_RUN_TTL = int(os.environ.get("RESEARCH_RUN_TTL", "3600"))

log = logging.getLogger("webhook")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = FastAPI(title="Research Webhook")

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


_li_store = None
def _li():
    """Return the llamaindex_store module if enabled, else raise 503."""
    global _li_store
    if not LLAMAINDEX_ENABLED:
        raise HTTPException(503, "llamaindex disabled (set LLAMAINDEX_ENABLED=true)")
    if _li_store is None:
        import llamaindex_store as m  # lazy: first call pays HF model load
        _li_store = m
    return _li_store


def check_auth(authorization: str | None = Header(default=None)):
    if not API_KEY:
        return
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")
    if authorization.split(None, 1)[1].strip() != API_KEY:
        raise HTTPException(403, "invalid token")


class ResearchRequest(BaseModel):
    query: str


class SynthesizeLIRequest(BaseModel):
    question: str
    k: int = 8
    rerank: bool = True
    subq: bool = False


class ContinueRequest(BaseModel):
    instruction: str


class IngestRequest(BaseModel):
    source: str
    title: str
    content: str
    metadata: dict = {}
    dedupe_key: str | None = None
    chunk_size: int | None = None


class CourseRequest(BaseModel):
    query_seed: str
    scope: dict = {}


class CoursePatch(BaseModel):
    title: str | None = None
    objective: str | None = None


class LessonCreate(BaseModel):
    title: str
    objective: str
    body_md: str = ""
    bloom_level: str | None = None


class LessonPatch(BaseModel):
    title: str | None = None
    objective: str | None = None
    body_md: str | None = None
    bloom_level: str | None = None


class LessonReorder(BaseModel):
    order: list[str]


class LessonRegenerate(BaseModel):
    feedback: str


class LessonAsk(BaseModel):
    question: str


def slug(s: str, max_len: int = 60) -> str:
    s = re.sub(r"[^a-zA-Z0-9\s-]", "", s).strip().lower()
    s = re.sub(r"\s+", "-", s)
    return s[:max_len] or "query"


_JUNK_PATTERNS = (
    "error generating final report",
    "error code: 429",
    "rate_limit_error",
    "anthropic.ratelimiterror",
)


def _looks_like_junk(report: str) -> bool:
    """True if report body is a rate-limit/error stub worth excluding from the corpus."""
    body = report.strip()
    if not body:
        return True
    head = body[:600].lower()
    if any(p in head for p in _JUNK_PATTERNS):
        return True
    return False


async def _save_report(*, query: str, report: str, thread_id: str, ts: str, base: str) -> Path:
    header = f"# Research: {query}\n\n_Generated {ts} · thread={thread_id}_\n\n---\n\n"
    if _looks_like_junk(report):
        err_path = ERRORS_DIR / f"{base}.err.md"
        err_path.write_text(header + report)
        log.warning("report matched junk pattern; wrote %s and skipped corpus ingest", err_path)
        return err_path
    report_path = REPORTS_DIR / f"{base}.md"
    report_path.write_text(header + report)
    report_id = f"report_{uuid.uuid4().hex[:12]}"
    if LLAMAINDEX_ENABLED:
        try:
            r = await _li().aingest_report(
                report_text=report,
                query=query,
                thread_id=thread_id,
                report_id=report_id,
            )
            log.info("llamaindex ingested %s: %d nodes", r["report_id"], r["nodes"])
        except Exception:
            log.exception("llamaindex ingest failed (continuing)")
    log.info("saved %s (report_id=%s)", report_path, report_id)
    return report_path


async def _await_run(client, thread_id: str, run_id: str) -> str:
    deadline = asyncio.get_event_loop().time() + RUN_TIMEOUT_SECONDS
    while True:
        if asyncio.get_event_loop().time() > deadline:
            raise TimeoutError(f"run exceeded {RUN_TIMEOUT_SECONDS}s")
        status = (await client.runs.get(thread_id, run_id)).get("status")
        if status in ("success", "error", "interrupted", "timeout"):
            return status
        await asyncio.sleep(5)


async def _extract_report(client, thread_id: str) -> str:
    state = await client.threads.get_state(thread_id)
    values = state.get("values", {}) if isinstance(state, dict) else {}
    report = values.get("final_report") or ""
    if not report:
        msgs = values.get("messages", [])
        if msgs:
            last = msgs[-1]
            if isinstance(last, dict):
                c = last.get("content")
                if isinstance(c, list):
                    c = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in c)
                report = c or ""
            else:
                report = str(last)
    if not report:
        raise RuntimeError("no report in final state")
    return report


async def _run_research(query: str, thread_id: str | None = None, label_query: str | None = None):
    """Background: run deep research on a thread (new or existing), save, persist."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    label = label_query or query
    base = f"{ts}_{slug(label)}"
    try:
        client = get_client(url=LANGGRAPH_URL)
        if thread_id is None:
            thread = await client.threads.create()
            thread_id = thread["thread_id"]
        log.info("thread=%s query=%r", thread_id, query)

        run = await client.runs.create(
            thread_id=thread_id,
            assistant_id=ASSISTANT_ID,
            input={"messages": [{"role": "user", "content": query}]},
        )
        run_id = run["run_id"]

        status = await _await_run(client, thread_id, run_id)
        if status != "success":
            raise RuntimeError(f"run status={status}")
        report = await _extract_report(client, thread_id)
        await _save_report(query=label, report=report, thread_id=thread_id, ts=ts, base=base)
    except Exception as e:
        err_path = ERRORS_DIR / f"{base}.err"
        err_path.write_text(f"query: {query}\n\n{traceback.format_exc()}")
        log.error("research failed: %s (see %s)", e, err_path)


_rate_buckets: dict[str, tuple[int, int]] = defaultdict(lambda: (0, 0))
def _rate_gate(ip: str) -> None:
    now_min = int(time.time() // 60)
    bucket, count = _rate_buckets[ip]
    if bucket != now_min:
        count = 0
    if count >= SYNTHESIS_RATE_PER_MIN:
        raise HTTPException(429, f"rate limit {SYNTHESIS_RATE_PER_MIN}/min exceeded")
    _rate_buckets[ip] = (now_min, count + 1)


def _safe_report_path(name: str) -> Path:
    p = (REPORTS_DIR / name).resolve()
    try:
        p.relative_to(REPORTS_DIR)
    except ValueError:
        raise HTTPException(400, "invalid path")
    if not p.is_file() or p.suffix != ".md":
        raise HTTPException(404, "not found")
    return p


CONTINUE_WRAPPER = (
    "Revise and extend your prior research. Follow-up from the user: {instr}. "
    "Re-plan sub-questions if needed and produce an updated final_report."
)


@app.get("/")
def root():
    idx = STATIC_DIR / "index.html"
    if not idx.is_file():
        return {"status": "ok", "hint": "PWA assets missing"}
    return FileResponse(str(idx))


@app.get("/manifest.webmanifest")
def manifest():
    p = STATIC_DIR / "manifest.webmanifest"
    if not p.is_file():
        raise HTTPException(404)
    return FileResponse(str(p), media_type="application/manifest+json")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/stats", dependencies=[Depends(check_auth)])
def stats():
    s = {"reports_on_disk": len(list(REPORTS_DIR.glob("*.md")))}
    if LLAMAINDEX_ENABLED and _li_store is not None:
        try:
            s["llamaindex"] = _li_store.stats()
        except Exception as e:
            s["llamaindex"] = {"error": str(e)}
    else:
        s["llamaindex"] = {"enabled": LLAMAINDEX_ENABLED, "initialized": False}
    return s


@app.get("/reports", dependencies=[Depends(check_auth)])
def reports():
    files = sorted(REPORTS_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)[:50]
    return [{
        "file": f.name,
        "size": f.stat().st_size,
        "mtime": datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc).isoformat(),
    } for f in files]


@app.get("/reports/{file}", dependencies=[Depends(check_auth)])
def report_file(file: str):
    p = _safe_report_path(file)
    return PlainTextResponse(p.read_text(), media_type="text/markdown")


@app.delete("/reports/{file}", dependencies=[Depends(check_auth)])
def delete_report_file(file: str):
    p = _safe_report_path(file)
    p.unlink()
    return {"file": file, "removed": True}


@app.delete("/corpus/{report_id}", dependencies=[Depends(check_auth)])
async def delete_corpus(report_id: str):
    if not re.match(r"^(report|ingest)_[a-zA-Z0-9_]+$", report_id):
        raise HTTPException(400, "invalid report_id")
    if LLAMAINDEX_ENABLED:
        try:
            li_result = await asyncio.to_thread(_li().delete_report, report_id)
        except Exception as e:
            li_result = {"error": str(e)}
    else:
        li_result = {"enabled": False}
    return {"report_id": report_id, "llamaindex": li_result}


@app.get("/threads", dependencies=[Depends(check_auth)])
async def threads_list(limit: int = 20):
    client = get_client(url=LANGGRAPH_URL)
    tlist = await client.threads.search(limit=max(1, min(limit, 100)))
    out = []
    for t in tlist:
        tid = t.get("thread_id")
        created = t.get("created_at", "")
        query = ""
        has_report = False
        try:
            state = await client.threads.get_state(tid)
            values = state.get("values", {}) if isinstance(state, dict) else {}
            for m in values.get("messages", []) or []:
                if not isinstance(m, dict):
                    continue
                if m.get("type") == "human" or m.get("role") == "user":
                    c = m.get("content")
                    if isinstance(c, list):
                        c = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in c)
                    query = c if isinstance(c, str) else str(c)
                    break
            has_report = bool(values.get("final_report"))
        except Exception as e:
            log.warning("thread %s state fetch failed: %s", tid, e)
        out.append({
            "thread_id": tid,
            "created_at": created,
            "query": (query or "")[:240],
            "has_report": has_report,
        })
    return out


@app.get("/threads/{thread_id}", dependencies=[Depends(check_auth)])
async def thread_detail(thread_id: str):
    client = get_client(url=LANGGRAPH_URL)
    state = await client.threads.get_state(thread_id)
    values = state.get("values", {}) if isinstance(state, dict) else {}
    msgs_raw = values.get("messages", []) or []
    msgs = []
    for m in msgs_raw:
        if not isinstance(m, dict):
            continue
        role = m.get("role") or m.get("type") or ""
        content = m.get("content")
        if isinstance(content, list):
            content = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
        msgs.append({"role": role, "content": content or ""})
    return {
        "thread_id": thread_id,
        "messages": msgs,
        "final_report": values.get("final_report", "") or "",
    }


@app.post("/threads/{thread_id}/continue", dependencies=[Depends(check_auth)])
async def thread_continue(thread_id: str, req: ContinueRequest, bg: BackgroundTasks):
    wrapped = CONTINUE_WRAPPER.format(instr=req.instruction)
    bg.add_task(_run_research, wrapped, thread_id, req.instruction)
    return {"status": "accepted", "thread_id": thread_id, "instruction": req.instruction}


@app.get("/search2", dependencies=[Depends(check_auth)])
async def search2(q: str, k: int = 10, rerank: bool = True, hybrid: bool = True):
    k = max(1, min(k, 50))
    hits = await _li().retrieve(q, k=k, hybrid=hybrid, rerank=rerank)
    return [{
        "text": h["text"],
        "query": h["query"],
        "score": h["score"],
        "report_id": h["report_id"],
        "thread_id": h["thread_id"],
        "node_id": h["node_id"],
        "score_source": "hybrid+rerank" if (hybrid and rerank) else ("hybrid" if hybrid else "vector"),
    } for h in hits]


@app.post("/synthesize2", dependencies=[Depends(check_auth)])
async def synthesize2(req: SynthesizeLIRequest, request: Request):
    _rate_gate(request.client.host if request.client else "unknown")
    k = max(1, min(req.k, 20))
    out = await _li().synthesize(req.question, k=k, rerank=req.rerank, subq=req.subq)
    return out


@app.post("/ingest", dependencies=[Depends(check_auth)])
async def ingest(req: IngestRequest):
    """Ingest a document from an external source (routines, manual dumps, etc.).

    Body: {source, title, content, metadata?}. Metadata is a free-form dict;
    values get coerced to Chroma-compatible primitives on the LlamaIndex side.
    Common keys: type (news|paper|scrape|manual), topic, url, published_at, tags.
    """
    if not LLAMAINDEX_ENABLED:
        raise HTTPException(503, "llamaindex disabled")
    if not req.content.strip():
        raise HTTPException(400, "empty content")
    if req.dedupe_key:
        h = hashlib.sha1(req.dedupe_key.strip().encode("utf-8")).hexdigest()[:12]
        doc_id = f"ingest_{h}"
    else:
        doc_id = f"ingest_{uuid.uuid4().hex[:12]}"
    existed = await asyncio.to_thread(_li().has_doc, doc_id)
    extras = {k: v for k, v in (req.metadata or {}).items() if k not in {"type"}}
    meta = {
        "type": (req.metadata or {}).get("type", "ingest"),
        "source": req.source,
        "title": req.title,
        "query": req.title,
        "report_id": doc_id,
        "thread_id": "",
        "ingested_at": datetime.now(timezone.utc).isoformat(),
        **({"dedupe_key": req.dedupe_key} if req.dedupe_key else {}),
        **extras,
    }
    result = await _li().aingest_document(
        text=req.content,
        doc_id=doc_id,
        metadata=meta,
        chunk_size=req.chunk_size,
    )
    log.info(
        "/ingest source=%s doc_id=%s nodes=%d existed=%s chunk_size=%s",
        req.source, doc_id, result["nodes"], existed, req.chunk_size,
    )
    return {
        "doc_id": doc_id,
        "source": req.source,
        "title": req.title,
        "nodes": result["nodes"],
        "existed": existed,
    }


@app.post("/research", dependencies=[Depends(check_auth)])
async def research(req: ResearchRequest, bg: BackgroundTasks):
    existing = []
    if LLAMAINDEX_ENABLED:
        try:
            hits = await _li().retrieve(req.query, k=3, hybrid=True, rerank=False)
            existing = [{
                "query": h.get("query", ""),
                "preview": (h.get("text", "") or "")[:240],
                "report_id": h.get("report_id", ""),
                "score": h.get("score", 0.0),
            } for h in hits]
        except Exception:
            log.exception("existing-match retrieve failed")
    bg.add_task(_run_research, req.query)
    return {
        "status": "accepted",
        "query": req.query,
        "existing_matches": existing,
    }


@app.post("/courses", dependencies=[Depends(check_auth)])
async def create_course(req: CourseRequest, bg: BackgroundTasks):
    if not LLAMAINDEX_ENABLED:
        raise HTTPException(503, "llamaindex disabled")
    q = (req.query_seed or "").strip()
    if not q:
        raise HTTPException(400, "empty query_seed")
    # seed a pending row so the client can poll status immediately
    course_id = courses_db.insert_course(
        title=q[:80], objective=q, scope=req.scope or {}, status="pending",
    )
    bg.add_task(courses.generate_course, course_id, q, req.scope or {})
    return {"course_id": course_id, "status": "pending"}


@app.get("/courses/{course_id}/status", dependencies=[Depends(check_auth)])
async def course_status(course_id: str):
    row = courses_db.get_status(course_id)
    if not row:
        raise HTTPException(404, "course not found")
    return row


@app.get("/courses", dependencies=[Depends(check_auth)])
async def list_courses(limit: int = 50):
    return courses_db.list_courses(limit=max(1, min(limit, 200)))


@app.get("/courses/{course_id}", dependencies=[Depends(check_auth)])
async def get_course(course_id: str):
    c = courses_db.get_course(course_id, include_lessons=True)
    if not c:
        raise HTTPException(404, "course not found")
    return c


@app.delete("/courses/{course_id}", dependencies=[Depends(check_auth)])
async def delete_course(course_id: str):
    if not courses_db.delete_course(course_id):
        raise HTTPException(404, "course not found")
    return {"deleted": course_id}


@app.patch("/courses/{course_id}", dependencies=[Depends(check_auth)])
async def patch_course(course_id: str, req: CoursePatch):
    fields = {k: v for k, v in req.dict().items() if v is not None}
    if not fields:
        raise HTTPException(400, "no fields to update")
    if not courses_db.get_status(course_id):
        raise HTTPException(404, "course not found")
    courses_db.update_course(course_id, **fields)
    return courses_db.get_course(course_id, include_lessons=False)


@app.patch("/courses/{course_id}/lessons/reorder", dependencies=[Depends(check_auth)])
async def reorder_course_lessons(course_id: str, req: LessonReorder):
    if not courses_db.get_status(course_id):
        raise HTTPException(404, "course not found")
    ok = courses_db.reorder_lessons(course_id, req.order)
    if not ok:
        raise HTTPException(400, "order list must contain every lesson id exactly once")
    return courses_db.get_course(course_id)


@app.post("/courses/{course_id}/lessons", dependencies=[Depends(check_auth)])
async def add_course_lesson(course_id: str, req: LessonCreate):
    lid = courses_db.append_lesson(
        course_id=course_id,
        title=req.title, objective=req.objective,
        body_md=req.body_md or "", bloom_level=req.bloom_level,
        source="hand_written",
    )
    if not lid:
        raise HTTPException(404, "course not found")
    return courses_db.get_lesson(lid)


@app.patch("/courses/{course_id}/lessons/{lesson_id}", dependencies=[Depends(check_auth)])
async def patch_lesson(course_id: str, lesson_id: str, req: LessonPatch):
    lesson = courses_db.get_lesson(lesson_id)
    if not lesson or lesson["course_id"] != course_id:
        raise HTTPException(404, "lesson not found in course")
    fields = {k: v for k, v in req.dict().items() if v is not None}
    if not fields:
        raise HTTPException(400, "no fields to update")
    courses_db.update_lesson(lesson_id, mark_edited=True, **fields)
    return courses_db.get_lesson(lesson_id)


@app.delete("/courses/{course_id}/lessons/{lesson_id}", dependencies=[Depends(check_auth)])
async def delete_course_lesson(course_id: str, lesson_id: str):
    lesson = courses_db.get_lesson(lesson_id)
    if not lesson or lesson["course_id"] != course_id:
        raise HTTPException(404, "lesson not found in course")
    courses_db.delete_lesson(lesson_id)
    return {"deleted": lesson_id, "course_id": course_id}


@app.post("/courses/{course_id}/lessons/{lesson_id}/regenerate", dependencies=[Depends(check_auth)])
async def regenerate_course_lesson(course_id: str, lesson_id: str, req: LessonRegenerate):
    if not LLAMAINDEX_ENABLED:
        raise HTTPException(503, "llamaindex disabled")
    lesson = courses_db.get_lesson(lesson_id)
    if not lesson or lesson["course_id"] != course_id:
        raise HTTPException(404, "lesson not found in course")
    fb = (req.feedback or "").strip()
    if not fb:
        raise HTTPException(400, "empty feedback")
    try:
        await courses.regenerate_lesson(lesson_id, fb)
    except Exception as e:
        log.exception("regenerate_lesson failed")
        raise HTTPException(500, f"regenerate failed: {e}")
    return courses_db.get_lesson(lesson_id)


@app.post("/courses/{course_id}/lessons/{lesson_id}/ask", dependencies=[Depends(check_auth)])
async def ask_course_lesson(course_id: str, lesson_id: str, req: LessonAsk, request: Request):
    if not LLAMAINDEX_ENABLED:
        raise HTTPException(503, "llamaindex disabled")
    lesson = courses_db.get_lesson(lesson_id)
    if not lesson or lesson["course_id"] != course_id:
        raise HTTPException(404, "lesson not found in course")
    q = (req.question or "").strip()
    if not q:
        raise HTTPException(400, "empty question")
    _rate_gate(request.client.host if request.client else "unknown")
    try:
        return await courses.ask_lesson(lesson_id, q)
    except Exception as e:
        log.exception("ask_lesson failed")
        raise HTTPException(500, f"ask failed: {e}")


@app.get("/courses/{course_id}/lessons/{lesson_id}/follow_ups", dependencies=[Depends(check_auth)])
async def list_course_follow_ups(course_id: str, lesson_id: str):
    lesson = courses_db.get_lesson(lesson_id)
    if not lesson or lesson["course_id"] != course_id:
        raise HTTPException(404, "lesson not found in course")
    return courses_db.list_follow_ups(lesson_id)


# ---- gap-fill: research_fire / research_callback / research_status ----

class ResearchFireReq(BaseModel):
    question: str
    urls: list[str] | None = None
    max_fetches: int = 5
    topic: str | None = None


class ResearchCallback(BaseModel):
    run_id: str
    status: str = "complete"
    ingested: list[dict] = []
    skipped: list[dict] = []
    errors: list[dict] = []


_research_runs: dict[str, dict] = {}
_research_runs_lock = asyncio.Lock()


async def _evict_old_research_runs() -> None:
    cutoff = time.time() - RESEARCH_RUN_TTL
    async with _research_runs_lock:
        stale = [k for k, v in _research_runs.items()
                 if v.get("finished_at") and v["finished_at"] < cutoff]
        for k in stale:
            _research_runs.pop(k, None)


@app.post("/courses/{course_id}/lessons/{lesson_id}/research_fire", dependencies=[Depends(check_auth)])
async def research_fire(course_id: str, lesson_id: str, req: ResearchFireReq):
    if not ANTHROPIC_OAT or not ROUTINE_RESEARCH_FIRE_URL:
        raise HTTPException(503, "research routine not configured (set ANTHROPIC_OAT + ROUTINE_RESEARCH_FIRE_URL)")
    lesson = courses_db.get_lesson(lesson_id)
    if not lesson or lesson["course_id"] != course_id:
        raise HTTPException(404, "lesson not found in course")
    q = (req.question or "").strip()
    urls = list(req.urls or [])
    if not q and not urls:
        raise HTTPException(400, "question or urls required")
    run_id = "rr_" + uuid.uuid4().hex[:16]
    text_payload = json.dumps({
        "run_id": run_id,
        "question": q,
        "course_id": course_id,
        "lesson_id": lesson_id,
        "urls": urls,
        "max_fetches": max(1, min(int(req.max_fetches or 5), 20)),
        "topic": (req.topic or "").strip(),
    })
    started = time.time()
    async with _research_runs_lock:
        _research_runs[run_id] = {
            "status": "pending",
            "course_id": course_id,
            "lesson_id": lesson_id,
            "question": q,
            "started_at": started,
        }
    headers = {
        "Authorization": f"Bearer {ANTHROPIC_OAT}",
        "anthropic-beta": ROUTINE_FIRE_BETA,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(ROUTINE_RESEARCH_FIRE_URL, headers=headers, json={"text": text_payload})
        if r.status_code >= 400:
            async with _research_runs_lock:
                _research_runs[run_id].update({
                    "status": "failed",
                    "finished_at": time.time(),
                    "errors": [{"message": f"fire {r.status_code}: {r.text[:300]}"}],
                })
            raise HTTPException(502, f"fire failed {r.status_code}")
        body = r.json()
        async with _research_runs_lock:
            _research_runs[run_id]["claude_session_id"] = body.get("claude_code_session_id")
            _research_runs[run_id]["claude_session_url"] = body.get("claude_code_session_url")
        return {"run_id": run_id, "claude_code_session_url": body.get("claude_code_session_url")}
    except httpx.HTTPError as e:
        async with _research_runs_lock:
            _research_runs[run_id].update({
                "status": "failed",
                "finished_at": time.time(),
                "errors": [{"message": f"fire transport: {e}"}],
            })
        raise HTTPException(502, f"fire transport error: {e}")


@app.post("/research_callback", dependencies=[Depends(check_auth)])
async def research_callback(cb: ResearchCallback):
    async with _research_runs_lock:
        run = _research_runs.get(cb.run_id)
        if run is None:
            return {"ok": False, "reason": "unknown run_id"}
        run.update({
            "status": cb.status if cb.status in {"complete", "failed"} else "complete",
            "ingested": cb.ingested,
            "skipped": cb.skipped,
            "errors": cb.errors,
            "finished_at": time.time(),
        })
    asyncio.create_task(_evict_old_research_runs())
    return {"ok": True}


@app.get("/research/{run_id}", dependencies=[Depends(check_auth)])
async def research_status(run_id: str):
    async with _research_runs_lock:
        run = _research_runs.get(run_id)
    if not run:
        raise HTTPException(404, "run not found (expired or never existed)")
    return {
        "run_id": run_id,
        "status": run["status"],
        "course_id": run.get("course_id"),
        "lesson_id": run.get("lesson_id"),
        "question": run.get("question"),
        "ingested": run.get("ingested", []),
        "skipped": run.get("skipped", []),
        "errors": run.get("errors", []),
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
        "claude_code_session_url": run.get("claude_session_url"),
    }
