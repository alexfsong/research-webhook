"""Shared pytest fixtures for the research-webhook test suite.

Unit-layer fixtures are hermetic: no network, no real Anthropic client, no
production data dir. Eval-layer fixtures build a temporary LlamaIndex over
the checked-in fixture corpus and may consume API tokens — only used by
tests marked `eval`.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# courses_db: in-memory* SQLite (sqlite3 connections to a fresh tmp file).
# `:memory:` would not survive the multiple connect() calls courses_db makes,
# so we use a per-test tmp file and re-run init_db().
# ---------------------------------------------------------------------------

@pytest.fixture
def mem_courses_db(tmp_path, monkeypatch):
    """Yield a freshly migrated courses_db module pointed at a tmp SQLite file."""
    db_file = tmp_path / "courses.db"
    import courses_db  # noqa: WPS433

    monkeypatch.setattr(courses_db, "DB_PATH", db_file)
    courses_db.init_db()
    yield courses_db


# ---------------------------------------------------------------------------
# Anthropic stub: canned responses, no network. Patch courses._client.
# Tests queue responses FIFO; an unmatched call raises with the full kw dump
# so failures point at the offending prompt rather than "AttributeError".
# ---------------------------------------------------------------------------

class _StubBlock:
    __slots__ = ("type", "text")

    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _StubResp:
    def __init__(self, text: str) -> None:
        self.content = [_StubBlock(text)]


class StubAnthropic:
    """Minimal AsyncAnthropic stub. Exposes `.messages.create(**kw)` like the real client."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._queued: list[dict] = []

    def queue(
        self,
        *,
        text: str | None = None,
        json_obj: Any = None,
        raises: Exception | None = None,
    ) -> None:
        """Queue one response. Provide exactly one of {text, json_obj, raises}."""
        provided = sum(x is not None for x in (text, json_obj, raises))
        if provided != 1:
            raise ValueError("queue() needs exactly one of text/json_obj/raises")
        self._queued.append({"text": text, "json_obj": json_obj, "raises": raises})

    @property
    def messages(self) -> "StubAnthropic":  # noqa: D401
        return self

    async def create(self, **kw):  # noqa: ANN003
        self.calls.append(kw)
        if not self._queued:
            raise RuntimeError(
                f"StubAnthropic: no canned response for call #{len(self.calls)}; "
                f"kw keys={sorted(kw)}"
            )
        r = self._queued.pop(0)
        if r["raises"] is not None:
            raise r["raises"]
        text = r["text"] if r["text"] is not None else json.dumps(r["json_obj"])
        return _StubResp(text)


@pytest.fixture
def mock_anthropic(monkeypatch):
    """Patch `courses._client` to return a StubAnthropic. Returns the stub."""
    import courses  # noqa: WPS433

    stub = StubAnthropic()
    monkeypatch.setattr(courses, "_client", lambda: stub)
    return stub


# ---------------------------------------------------------------------------
# LlamaIndex tmp dir — only meaningful for eval tests. Ensures we never touch
# ~/research-data/ from inside a test.
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_llamaindex_dir(tmp_path, monkeypatch):
    """Redirect llamaindex_store paths to a tmp dir. Eval suite imports it lazily."""
    root = tmp_path / "li-data"
    root.mkdir()
    chroma = root / "chromadb"
    docstore = root / "llamaindex-docstore"
    graph = root / "llamaindex-graph"
    for d in (chroma, docstore, graph):
        d.mkdir()

    # Patch *before* llamaindex_store is imported by any test path.
    monkeypatch.setenv("LI_COLLECTION", f"test_li_{os.getpid()}")
    # Reload if already imported so module-level path constants pick up.
    for mod in ("llamaindex_store", "persist"):
        if mod in sys.modules:
            del sys.modules[mod]

    import llamaindex_store  # noqa: WPS433
    import persist  # noqa: WPS433

    monkeypatch.setattr(llamaindex_store, "DATA_DIR", root)
    monkeypatch.setattr(llamaindex_store, "CHROMA_DIR", chroma)
    monkeypatch.setattr(llamaindex_store, "DOCSTORE_DIR", docstore)
    monkeypatch.setattr(llamaindex_store, "GRAPH_DIR", graph)
    monkeypatch.setattr(persist, "DATA_DIR", root)
    monkeypatch.setattr(persist, "CHROMA_DIR", chroma)
    return {"root": root, "chroma": chroma, "docstore": docstore, "graph": graph}


# ---------------------------------------------------------------------------
# Deterministic retrieval results — match the shape returned by
# llamaindex_store._node_dict so courses.py can consume them unchanged.
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_retrieval_results() -> list[dict]:
    return [
        {
            "text": (
                "FSRS uses three latent variables — difficulty, stability, and "
                "retrievability — updated by a 17-weight neural model trained on "
                "review histories. Replaces SM-2's fixed-arithmetic ease factor."
            ),
            "score": 0.92,
            "report_id": "rep_aaa1",
            "thread_id": "thr_xxx1",
            "query": "FSRS algorithm overview",
            "node_id": "n1",
        },
        {
            "text": (
                "SM-2 is a rule-based algorithm with O(1) state updates per review. "
                "Card state is independent across reviews, making concurrency trivial."
            ),
            "score": 0.88,
            "report_id": "rep_aaa2",
            "thread_id": "thr_xxx1",
            "query": "SM-2 concurrency",
            "node_id": "n2",
        },
        {
            "text": (
                "Anki-rs ships a Rust core with WASM and Python bindings; review "
                "scheduling stays stateless per card while the optimizer runs offline."
            ),
            "score": 0.81,
            "report_id": "rep_aaa3",
            "thread_id": "thr_xxx2",
            "query": "anki-rs implementation",
            "node_id": "n3",
        },
    ]


# ---------------------------------------------------------------------------
# Path sanity — make the project root importable regardless of CWD pytest ran from.
# ---------------------------------------------------------------------------

def pytest_configure(config):
    root = Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
