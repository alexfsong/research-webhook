"""LlamaIndex-backed retrieval and synthesis over research reports.

Parallel to persist.py: ingests the same reports into a sibling ChromaDB
collection (default 'research_li') via a LlamaIndex IngestionPipeline, then
exposes hybrid (vector + BM25) retrieval with cross-encoder rerank, a
CitationQueryEngine for answers, and a SubQuestionQueryEngine for
decomposed questions.

Graph-RAG path (PropertyGraphIndex) is gated by LI_GRAPH_ENABLED and is
off by default — flipping the flag turns on LLM-based triplet extraction
at ingest and enables graph_query()."""
import asyncio
import json
import logging
import os
import threading
from pathlib import Path

log = logging.getLogger("llamaindex_store")

DATA_DIR = Path(os.path.expanduser("~/research-data"))
CHROMA_DIR = DATA_DIR / "chromadb"
DOCSTORE_DIR = DATA_DIR / "llamaindex-docstore"
GRAPH_DIR = DATA_DIR / "llamaindex-graph"

COLLECTION = os.environ.get("LI_COLLECTION", "research_li")
EMBED_MODEL = os.environ.get("LI_EMBED_MODEL", "BAAI/bge-small-en-v1.5")
RERANK_MODEL = os.environ.get("LI_RERANK_MODEL", "BAAI/bge-reranker-base")
RERANK_TOP_N = int(os.environ.get("LI_RERANK_TOP_N", "5"))
RETRIEVE_TOP_K = int(os.environ.get("LI_RETRIEVE_TOP_K", "20"))
CHUNK_SIZE = int(os.environ.get("LI_CHUNK_SIZE", "512"))
CHUNK_OVERLAP = int(os.environ.get("LI_CHUNK_OVERLAP", "64"))
LLM_MODEL = os.environ.get("SYNTHESIS_MODEL", "claude-sonnet-4-6")
GRAPH_ENABLED = os.environ.get("LI_GRAPH_ENABLED", "false").lower() == "true"

_lock = threading.Lock()
_state: dict = {}


def _init() -> dict:
    if _state.get("ready"):
        return _state
    with _lock:
        if _state.get("ready"):
            return _state

        from llama_index.core import Settings, StorageContext, VectorStoreIndex
        from llama_index.core.node_parser import SentenceSplitter
        from llama_index.core.storage.docstore import SimpleDocumentStore
        from llama_index.embeddings.huggingface import HuggingFaceEmbedding
        from llama_index.llms.anthropic import Anthropic
        from llama_index.vector_stores.chroma import ChromaVectorStore

        DOCSTORE_DIR.mkdir(parents=True, exist_ok=True)

        import persist  # reuse the existing PersistentClient to avoid settings-conflict
        client = persist._client
        col = client.get_or_create_collection(
            name=COLLECTION,
            metadata={"hnsw:space": "cosine"},
        )
        vstore = ChromaVectorStore(chroma_collection=col)

        docstore_file = DOCSTORE_DIR / "docstore.json"
        if docstore_file.exists():
            docstore = SimpleDocumentStore.from_persist_path(str(docstore_file))
        else:
            docstore = SimpleDocumentStore()

        log.info("loading embed model %s", EMBED_MODEL)
        embed = HuggingFaceEmbedding(model_name=EMBED_MODEL)
        llm = Anthropic(model=LLM_MODEL)
        splitter = SentenceSplitter(chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP)

        Settings.llm = llm
        Settings.embed_model = embed
        Settings.chunk_size = CHUNK_SIZE

        storage = StorageContext.from_defaults(vector_store=vstore, docstore=docstore)
        index = VectorStoreIndex.from_vector_store(
            vstore, storage_context=storage, embed_model=embed
        )

        _state.update(
            client=client,
            collection=col,
            vstore=vstore,
            docstore=docstore,
            docstore_file=docstore_file,
            embed=embed,
            llm=llm,
            splitter=splitter,
            storage=storage,
            index=index,
            ready=True,
        )
        log.info(
            "llamaindex_store ready: collection=%s chroma_count=%d docstore_count=%d graph=%s",
            COLLECTION, col.count(), len(docstore.docs), GRAPH_ENABLED,
        )
    return _state


def _persist_docstore() -> None:
    s = _state
    s["docstore"].persist(str(s["docstore_file"]))


def _invalidate_retrievers() -> None:
    for key in ("bm25", "fusion_vector_only", "fusion_hybrid", "reranker_pipeline"):
        _state.pop(key, None)


def _safe_meta(md: dict) -> dict:
    """Coerce metadata values to Chroma-compatible primitives (str/int/float/bool).

    Chroma rejects lists and dicts; join/serialize those. Drop None."""
    out = {}
    for k, v in md.items():
        if v is None:
            continue
        if isinstance(v, (list, tuple)):
            out[k] = ",".join(str(x) for x in v)
        elif isinstance(v, dict):
            out[k] = json.dumps(v, sort_keys=True)
        else:
            out[k] = v
    return out


def has_doc(doc_id: str) -> bool:
    """True if any node with this report_id is already in the Chroma collection
    (pre-ingest dedupe check). Queries Chroma metadata directly — simpler and
    more reliable than docstore.ref_doc_info across LI versions."""
    s = _init()
    try:
        r = s["collection"].get(where={"report_id": doc_id})
        return len(r.get("ids", [])) > 0
    except Exception:
        log.exception("has_doc check failed for %s", doc_id)
        return False


def ingest_document(
    text: str,
    doc_id: str,
    metadata: dict,
    chunk_size: int | None = None,
) -> dict:
    """Sync ingest one document with arbitrary metadata. Generic path used by
    both /research reports (ingest_report wrapper) and /ingest routine uploads.

    If chunk_size is provided, uses a per-call SentenceSplitter instead of the
    shared 512/64 default. Overlap scales to max(32, chunk_size // 8).
    Upserts via DocstoreStrategy.UPSERTS — same doc_id replaces prior nodes."""
    from llama_index.core import Document
    from llama_index.core.ingestion import DocstoreStrategy, IngestionPipeline
    from llama_index.core.node_parser import SentenceSplitter

    s = _init()
    splitter = s["splitter"]
    if chunk_size and chunk_size > 0:
        splitter = SentenceSplitter(
            chunk_size=chunk_size,
            chunk_overlap=max(32, chunk_size // 8),
        )
    doc = Document(text=text, doc_id=doc_id, metadata=_safe_meta(metadata))
    pipeline = IngestionPipeline(
        transformations=[splitter, s["embed"]],
        vector_store=s["vstore"],
        docstore=s["docstore"],
        docstore_strategy=DocstoreStrategy.UPSERTS,
    )
    nodes = pipeline.run(documents=[doc], show_progress=False)
    _persist_docstore()
    _invalidate_retrievers()

    if GRAPH_ENABLED:
        try:
            _ingest_graph(doc)
        except Exception:
            log.exception("graph ingest failed for %s", doc_id)

    log.info("ingested doc=%s nodes=%d type=%s", doc_id, len(nodes), metadata.get("type", "?"))
    return {"doc_id": doc_id, "nodes": len(nodes)}


async def aingest_document(
    text: str,
    doc_id: str,
    metadata: dict,
    chunk_size: int | None = None,
) -> dict:
    return await asyncio.to_thread(ingest_document, text, doc_id, metadata, chunk_size)


def ingest_report(report_text: str, query: str, thread_id: str, report_id: str) -> dict:
    """ODR-report ingest path. Back-compat wrapper over ingest_document."""
    result = ingest_document(
        text=report_text,
        doc_id=report_id,
        metadata={
            "type": "report",
            "report_id": report_id,
            "thread_id": thread_id or "",
            "query": query,
        },
    )
    return {"report_id": report_id, "nodes": result["nodes"]}


async def aingest_report(report_text: str, query: str, thread_id: str, report_id: str) -> dict:
    return await asyncio.to_thread(
        ingest_report, report_text, query, thread_id, report_id
    )


def _get_retriever(hybrid: bool):
    from llama_index.core.retrievers import QueryFusionRetriever
    from llama_index.retrievers.bm25 import BM25Retriever

    s = _init()
    key = "fusion_hybrid" if hybrid else "fusion_vector_only"
    if key in s:
        return s[key]

    vector_ret = s["index"].as_retriever(similarity_top_k=RETRIEVE_TOP_K)
    if not hybrid:
        s[key] = vector_ret
        return vector_ret

    all_nodes = list(s["docstore"].docs.values())
    if not all_nodes:
        s[key] = vector_ret
        return vector_ret

    bm25 = BM25Retriever.from_defaults(nodes=all_nodes, similarity_top_k=RETRIEVE_TOP_K)
    fusion = QueryFusionRetriever(
        [vector_ret, bm25],
        similarity_top_k=RETRIEVE_TOP_K,
        mode="reciprocal_rerank",
        num_queries=1,
    )
    s[key] = fusion
    return fusion


def _get_reranker():
    s = _init()
    if "reranker" in s:
        return s["reranker"]
    from llama_index.core.postprocessor import SentenceTransformerRerank

    log.info("loading reranker %s", RERANK_MODEL)
    rr = SentenceTransformerRerank(model=RERANK_MODEL, top_n=RERANK_TOP_N)
    s["reranker"] = rr
    return rr


def _node_dict(n) -> dict:
    md = n.node.metadata or {}
    return {
        "text": n.node.get_content() or "",
        "score": float(n.score) if n.score is not None else 0.0,
        "report_id": md.get("report_id", ""),
        "thread_id": md.get("thread_id", ""),
        "query": md.get("query", ""),
        "node_id": n.node.node_id,
    }


def _retrieve_sync(query: str, k: int, hybrid: bool, rerank: bool) -> list[dict]:
    from llama_index.core.schema import QueryBundle

    retriever = _get_retriever(hybrid)
    nodes = retriever.retrieve(query)
    if rerank and nodes:
        rr = _get_reranker()
        rr.top_n = max(k, rr.top_n)
        nodes = rr.postprocess_nodes(nodes, query_bundle=QueryBundle(query))
    return [_node_dict(n) for n in nodes[:k]]


async def retrieve(query: str, k: int = 10, hybrid: bool = True, rerank: bool = True) -> list[dict]:
    return await asyncio.to_thread(_retrieve_sync, query, k, hybrid, rerank)


def _build_citation_engine(k: int, rerank: bool):
    from llama_index.core.query_engine import CitationQueryEngine

    s = _init()
    retriever = _get_retriever(hybrid=True)
    postprocessors = []
    if rerank:
        rr = _get_reranker()
        rr.top_n = max(k, RERANK_TOP_N)
        postprocessors = [rr]
    return CitationQueryEngine.from_args(
        s["index"],
        retriever=retriever,
        similarity_top_k=k,
        node_postprocessors=postprocessors,
        llm=s["llm"],
        citation_chunk_size=512,
    )


def _synthesize_sync(question: str, k: int, rerank: bool, subq: bool) -> dict:
    from llama_index.core.query_engine import SubQuestionQueryEngine
    from llama_index.core.tools import QueryEngineTool, ToolMetadata

    s = _init()
    cqe = _build_citation_engine(k=k, rerank=rerank)
    if subq:
        tool = QueryEngineTool(
            query_engine=cqe,
            metadata=ToolMetadata(
                name="research_reports",
                description=(
                    "Deep-research reports the user has previously produced. "
                    "Ask narrow sub-questions that can be answered from specific excerpts."
                ),
            ),
        )
        engine = SubQuestionQueryEngine.from_defaults(
            query_engine_tools=[tool], llm=s["llm"], use_async=False, verbose=False,
        )
        resp = engine.query(question)
        engine_name = "subquestion"
    else:
        resp = cqe.query(question)
        engine_name = "citation"

    answer = str(resp) if resp is not None else ""
    citations = []
    for i, src in enumerate(getattr(resp, "source_nodes", []) or [], start=1):
        md = src.node.metadata or {}
        citations.append({
            "n": i,
            "report_id": md.get("report_id", ""),
            "thread_id": md.get("thread_id", ""),
            "query": md.get("query", ""),
            "date": md.get("timestamp", "") or md.get("date", ""),
            "snippet": (src.node.get_content() or "")[:320],
            "score": float(src.score) if src.score is not None else 0.0,
        })
    return {
        "answer": answer,
        "citations": citations,
        "engine": engine_name,
        "model": LLM_MODEL,
    }


async def synthesize(question: str, k: int = 8, rerank: bool = True, subq: bool = False) -> dict:
    return await asyncio.to_thread(_synthesize_sync, question, k, rerank, subq)


def delete_report(report_id: str) -> dict:
    """Drop all nodes for a ref_doc_id from vector store + docstore."""
    s = _init()
    before = s["collection"].count()
    vstore_err = None
    try:
        s["vstore"].delete(ref_doc_id=report_id)
    except Exception as e:
        vstore_err = str(e)
        log.exception("vstore delete failed for %s", report_id)

    # docstore: remove via ref_doc mapping, then sweep any stray nodes by metadata
    try:
        s["docstore"].delete_ref_doc(report_id, raise_error=False)
    except Exception:
        log.exception("docstore.delete_ref_doc failed for %s", report_id)
    try:
        s["docstore"].delete_document(report_id, raise_error=False)
    except Exception:
        pass
    # metadata sweep: older LI versions may not maintain ref_doc_info for all nodes
    for node_id in list(s["docstore"].docs.keys()):
        node = s["docstore"].docs.get(node_id)
        md = getattr(node, "metadata", None) or {}
        if md.get("report_id") == report_id:
            try:
                s["docstore"].delete_document(node_id, raise_error=False)
            except Exception:
                pass

    _persist_docstore()
    _invalidate_retrievers()
    after = s["collection"].count()
    return {
        "before": before,
        "after": after,
        "removed_chroma": before - after,
        "docstore_remaining": len(s["docstore"].docs),
        "vstore_error": vstore_err,
    }


def stats() -> dict:
    s = _init()
    return {
        "collection": COLLECTION,
        "chroma_count": s["collection"].count(),
        "docstore_count": len(s["docstore"].docs),
        "embed_model": EMBED_MODEL,
        "rerank_model": RERANK_MODEL,
        "graph_enabled": GRAPH_ENABLED,
        "llm": LLM_MODEL,
    }


# --- PropertyGraphIndex scaffold (LI_GRAPH_ENABLED=true to activate) -------

def _init_graph():
    if not GRAPH_ENABLED:
        return None
    if _state.get("graph_index") is not None:
        return _state["graph_index"]

    from llama_index.core import PropertyGraphIndex
    from llama_index.core.graph_stores import SimplePropertyGraphStore
    from llama_index.core.indices.property_graph import SimpleLLMPathExtractor

    s = _init()
    GRAPH_DIR.mkdir(parents=True, exist_ok=True)
    persist = GRAPH_DIR / "graph_store.json"
    store = (
        SimplePropertyGraphStore.from_persist_path(str(persist))
        if persist.exists()
        else SimplePropertyGraphStore()
    )
    extractor = SimpleLLMPathExtractor(llm=s["llm"])
    graph_index = PropertyGraphIndex.from_documents(
        [],
        property_graph_store=store,
        kg_extractors=[extractor],
        embed_model=s["embed"],
        llm=s["llm"],
        show_progress=False,
    )
    _state["graph_index"] = graph_index
    _state["graph_store"] = store
    _state["graph_persist_path"] = persist
    return graph_index


def _ingest_graph(doc) -> None:
    idx = _init_graph()
    if idx is None:
        return
    idx.insert(doc)
    _state["graph_store"].persist(str(_state["graph_persist_path"]))


def _graph_query_sync(question: str) -> dict:
    idx = _init_graph()
    if idx is None:
        return {"answer": "graph disabled", "nodes": []}
    engine = idx.as_query_engine(include_text=True, llm=_state["llm"])
    resp = engine.query(question)
    return {
        "answer": str(resp) if resp is not None else "",
        "nodes": [
            {"text": (n.node.get_content() or "")[:320], "score": float(n.score) if n.score is not None else 0.0}
            for n in getattr(resp, "source_nodes", []) or []
        ],
    }


async def graph_query(question: str) -> dict:
    return await asyncio.to_thread(_graph_query_sync, question)
