"""Shared ChromaDB PersistentClient.

Single source for the on-disk Chroma client so multiple modules
(llamaindex_store, future course layer) can share one instance —
chromadb refuses two PersistentClients on the same path with
different Settings.
"""
import os
from pathlib import Path

import chromadb
from chromadb.config import Settings

DATA_DIR = Path(os.path.expanduser("~/research-data/chromadb"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

_client = chromadb.PersistentClient(
    path=str(DATA_DIR),
    settings=Settings(anonymized_telemetry=False),
)
