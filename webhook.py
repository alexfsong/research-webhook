"""FastAPI webhook: PWA backend + research/course/gap-fill endpoints.

Ask pipeline (POST /ask, /ask_callback, /ask_runs/{id}, /threads/*, deep queue
drainer) lives in `ask_api.py` and is mounted here via APIRouter. Course
endpoints, ingest, search, reports, and research_fire stay in this module.
"""
import asyncio
import hashlib
import json
import logging
import os
import re
import sys
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import persist  # noqa: E402 — shared ChromaDB client (imported for side-effect of initializing _client)
import courses_db  # noqa: E402 — sqlite course store
import courses  # noqa: E402 — course generation pipeline
import ask_api  # noqa: E402 — Ask pipeline router + deep-queue drainer
courses_db.init_db()  # idempotent schema bootstrap

BASE_DIR = Path(__file__).parent.resolve()
STATIC_DIR = BASE_DIR / "static"
SYNTHESIS_RATE_PER_MIN = int(os.environ.get("SYNTHESIS_RATE_PER_MIN", "10"))
LLAMAINDEX_ENABLED = os.environ.get("LLAMAINDEX_ENABLED", "true").lower() == "true"
REPORTS_DIR = Path(os.path.expanduser("~/research-data/reports")).resolve()
REPORTS_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR = REPORTS_DIR.parent
AUDIT_LOG_PATH = DATA_DIR / "ask-audit.log"
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


def check_auth(authorization: str | None = Header(default=None)):
    if not API_KEY:
        return
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")
    parts = authorization.split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        raise HTTPException(401, "missing bearer token")
    if parts[1].strip() != API_KEY:
        raise HTTPException(403, "invalid token")


def _bearer(authorization: str | None) -> str:
    """Extract the bearer token (or 'anon' if no API_KEY set). Used as the deep-rate-limit key."""
    if not authorization:
        return "anon"
    parts = authorization.split(None, 1)
    if len(parts) < 2:
        return "anon"
    return parts[1].strip() or "anon"


def _audit_log(record: dict) -> None:
    try:
        with AUDIT_LOG_PATH.open("a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        log.exception("audit log write failed")


# Wire shared helpers into ask_api + mount its router.
ask_api.configure(
    check_auth=check_auth,
    bearer_fn=_bearer,
    audit_log=_audit_log,
    llamaindex_enabled=LLAMAINDEX_ENABLED,
    api_key=API_KEY,
)
app.include_router(ask_api.router)


@app.on_event("startup")
async def _resume_deep_queue() -> None:
    await ask_api.resume_deep_queue()


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


class SynthesizeLIRequest(BaseModel):
    question: str
    k: int = 8
    rerank: bool = True
    subq: bool = False


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
