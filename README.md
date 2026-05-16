# research-webhook

FastAPI service for an agentic research + course-generation system. Hosts a
ChromaDB-backed LlamaIndex corpus, a SQLite course store, a PWA Learn tab,
and a small set of HTTP endpoints for ingestion, retrieval, and gap-fill.

Pairs with the **agentic-research-play** repo, which holds the Claude Code
Routines that feed `/ingest` and trigger `/research_fire`.

## What's here

| Path | Role |
|------|------|
| `webhook.py` | FastAPI app — routes for ingest, search, courses, gap-fill, follow-up Q&A |
| `courses.py` | Course generation pipeline (plan → lessons → claims) using Anthropic SDK |
| `courses_db.py` | SQLite store for courses, lessons, follow-ups, progress |
| `llamaindex_store.py` | LlamaIndex ingestion + retrieval (BM25 + vector + bge-reranker) |
| `persist.py` | Singleton ChromaDB PersistentClient |
| `static/` | PWA shell (`index.html` + `app.js` + `style.css`) |
| `deploy/research-webhook.service` | systemd unit (template) |
| `deploy/Caddyfile.snippet` | Caddy reverse-proxy block |
| `.env.example` | Env var reference |
| `tests/` | Unit + opt-in eval suite — see [TESTING.md](TESTING.md) |
| `requirements-dev.txt` | Test-only deps (`pytest`, plugins) |

`run_research.sh` is a manual helper to invoke the LangGraph deep-research
agent from the shell — not used by the webhook directly.

**Tests required for prompt changes.** Any change to the course-generation
prompts in `courses.py` (or to the models it points at via `COURSE_*_MODEL`
env vars) should be validated with the eval suite before deploy. The unit
suite covers parser/DAO/contract regressions and runs on every push; the
eval suite is opt-in (`pytest -m eval` with `ANTHROPIC_API_KEY` set). See
[TESTING.md](TESTING.md) for cost expectations and tuning knobs.

## Endpoint surface

```
POST   /ingest                                          insert/upsert a doc
GET    /search2?q=&k=                                   hybrid retrieval
POST   /synthesize2                                     LLM answer over corpus

POST   /research                                        kick a deep-research run
GET    /threads, /reports, /reports/{id}, /corpus/{id}  research artifact CRUD

POST   /courses                                         create draft course
GET    /courses, /courses/{id}, /courses/{id}/status
PATCH  /courses/{id}                                    edit course header
DELETE /courses/{id}
POST   /courses/{id}/lessons                            add hand-written lesson
PATCH  /courses/{id}/lessons/reorder                    reorder
PATCH  /courses/{id}/lessons/{lesson_id}                edit lesson
DELETE /courses/{id}/lessons/{lesson_id}
POST   /courses/{id}/lessons/{lesson_id}/regenerate     rewrite with feedback
POST   /courses/{id}/lessons/{lesson_id}/ask            follow-up Q&A
GET    /courses/{id}/lessons/{lesson_id}/follow_ups

POST   /courses/{id}/lessons/{lid}/research_fire        trigger gap-fill routine
POST   /research_callback                               (routine -> server) result push
GET    /research/{run_id}                               poll run status
```

All routes (except `/health`) require `Authorization: Bearer <WEBHOOK_API_KEY>`.

## Ask depth tiers

`POST /ask` accepts `depth` ∈ `{"standard","deep"}` (default `standard`).

- **standard** — today's single-pass behavior: one web search → ~5 fetches → cited
  Markdown answer. Default. Cost ≈ one Sonnet call + small fetch budget per turn.
- **deep** — multi-round iterative loop. Each round: corpus retrieve → one
  Anthropic tool-use call that emits section drafts + next-round gap queries →
  dedupe → repeat until gaps empty / iteration cap / token cap. Output is a
  sectioned long-form report with TOC + per-section citations, not a single
  answer block. **Cost: materially higher** — typically minutes per turn and
  several × the Sonnet + web token spend of a standard run. Gate behind an
  explicit user choice in the UI.

**Queue + subscription auth.** Submitting `depth=deep` enqueues the run on a
persisted SQLite FIFO (`ask_deep_queue`) per bearer; a serial drainer pops the
oldest queued row and runs it. The PWA shows `queued · position N of M` until
the slot opens. The synthesizer for each round runs under `claude -p
"/deep-synth …"` by default, so deep-research cost charges against the
operator's Claude Pro/Max plan — set `ASK_DEPTH_DEEP_BACKEND=api` to force the
Anthropic SDK path with `ANTHROPIC_API_KEY` instead.

Caps (set in `.env`, see `ASK_DEPTH_*` block):

- Per-tier `MAX_{SEARCHES,FETCHES,TOKENS,ITERATIONS}`.
- `ASK_DEPTH_DEEP_MAX_PER_DAY` per bearer, enforced at enqueue time
  (over → HTTP 429 `deep_daily_cap`).
- One in-flight deep per bearer; additional submissions enqueue rather than 429.
- `ASK_DEPTH_DEEP_MAX_DRAINERS` caps concurrent deep subprocesses across all
  bearers.

Rollback: set `ASK_DEPTH_ENABLED=false` to force every `/ask` back to standard
regardless of the requested tier.

## Architecture (one-screen)

```
              ┌───────────────────────────────┐
              │     PWA (static/, browser)    │
              └─────────────┬─────────────────┘
                            │ Bearer
  https://lisearch.195-201-99-206.sslip.io  (Caddy + Let's Encrypt)
                            │
                            ▼
       ┌────────────────────────────────────────────┐
       │      research-webhook.service (FastAPI)    │
       │  webhook.py  →  courses.py · llamaindex_   │
       │                  store.py · persist.py     │
       └────┬──────────────────┬──────────────┬─────┘
            │ ingest/retrieve  │ Anthropic    │ Anthropic /fire
            ▼                  ▼ SDK          ▼ (gap-fill only)
     ┌───────────────┐  ┌────────────────┐  ┌────────────────────┐
     │ ChromaDB +    │  │ Sonnet 4.6 /   │  │ ingest-research    │
     │ SQLite courses│  │ Haiku 4.5      │  │ routine (cloud)    │
     │ (research-data)│ │                │  └────────┬───────────┘
     └───────────────┘  └────────────────┘           │ POST /research_callback
                                                     ▼ (back to webhook)
```

## Box ops (multi-app)

This service shares a Hetzner box with other apps. **Read [INFRA.md](INFRA.md)
before deploying anything new** — it documents the box conventions (port
registry, hostname pattern, systemd templates, Caddy reload flow) other agents
must follow to avoid clashes.

## Deploy on Hetzner (or any Ubuntu 24.04 box)

Assumes a `researcher` user with sudo, an existing Python venv at
`/home/researcher/open_deep_research/.venv` (LangGraph agent's venv;
shared by the webhook), Caddy installed, ufw open on 22/80/443.

```bash
# 1. Clone
sudo -u researcher git clone https://github.com/alexfsong/research-webhook.git \
    /home/researcher/research-webhook
cd /home/researcher/research-webhook

# 2. Configure
sudo -u researcher cp .env.example .env
sudo -u researcher chmod 600 .env
sudo -u researcher $EDITOR .env   # set WEBHOOK_API_KEY, ANTHROPIC_API_KEY,
                                  # ANTHROPIC_OAT, ROUTINE_RESEARCH_FIRE_URL

# 3. (If venv missing) install deps
/home/researcher/open_deep_research/.venv/bin/python -m pip install -r requirements.txt

# 4. systemd unit
sudo cp deploy/research-webhook.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now research-webhook.service
sudo systemctl status research-webhook.service

# 5. Caddy reverse proxy + Let's Encrypt
sudo tee -a /etc/caddy/Caddyfile < deploy/Caddyfile.snippet
sudo systemctl reload caddy

# 6. Smoke test
curl -sS https://lisearch.195-201-99-206.sslip.io/health
```

The service binds to `127.0.0.1:8000`; only Caddy reaches it. Don't open
8000 in ufw.

## Update flow

```bash
# locally
git push

# on the box
sudo -u researcher bash -lc 'cd /home/researcher/research-webhook && git pull'
sudo systemctl restart research-webhook.service
```

PWA changes (`static/`) take effect without restart — Caddy serves them
straight from disk after `git pull`.

## Persistent state

Outside this repo, NOT version-controlled:

| Path | Purpose |
|------|---------|
| `~/research-data/courses.db` | SQLite courses + lessons + follow-ups |
| `~/research-data/chroma/` | ChromaDB vectors |
| `~/research-data/reports/` | Deep-research markdown reports |
| `~/research-data/hf-cache/` | HuggingFace model cache (bge embeddings + reranker) |

Back these up if the corpus matters.

## Companion repo

[`agentic-research-play`](https://github.com/alexfsong/agentic-research-play)
holds the Claude Code Routines (`ingest-arxiv`, `ingest-news`, `ingest-url`,
`ingest-research`) that POST to `/ingest` and the gap-fill flow that calls
`/research_fire` + `/research_callback`.
