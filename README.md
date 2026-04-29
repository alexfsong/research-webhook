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

`run_research.sh` is a manual helper to invoke the LangGraph deep-research
agent from the shell — not used by the webhook directly.

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

## Architecture (one-screen)

```
              ┌───────────────────────────────┐
              │     PWA (static/, browser)    │
              └─────────────┬─────────────────┘
                            │ Bearer
        https://lisearch.duckdns.org    (Caddy + Let's Encrypt)
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
curl -sS https://lisearch.duckdns.org/health
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
