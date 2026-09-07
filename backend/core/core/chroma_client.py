"""
core/chroma_client.py — deployment-proof ChromaDB client factory.

Neutralises all three production causes of
"Could not connect to tenant default_tenant":
  1. Stale/corrupt on-disk DB   -> always a brand-new temp dir, never a shared path
  2. Env-var leakage            -> every Settings field passed explicitly,
                                   forcing the in-process SegmentAPI
  3. Broken install             -> heartbeat/tenant self-test + 1 retry +
                                   actionable error including version info
"""
import os
import shutil
import tempfile

os.environ.setdefault("ANONYMIZED_TELEMETRY", "false")

import chromadb
from chromadb.config import Settings

CHROMA_VERSION = getattr(chromadb, "__version__", "unknown")


def _local_settings(persist_dir: str) -> Settings:
    """Explicit local-only settings. Explicit fields override env vars."""
    return Settings(
        anonymized_telemetry=False,
        allow_reset=True,
        is_persistent=True,
        persist_directory=persist_dir,
        chroma_api_impl="chromadb.api.segment.SegmentAPI",  # FORCE in-process
    )


def new_client(prefix: str = "chroma_"):
    """
    Create an isolated, self-tested ChromaDB client.
    Returns (client, temp_dir_path). Caller must rmtree(temp_dir_path) when done.
    """
    last_err = None
    for _ in range(2):
        d = tempfile.mkdtemp(prefix=prefix)
        try:
            client = chromadb.PersistentClient(path=d, settings=_local_settings(d))
            client.heartbeat()          # proves the local API is alive
            client.list_collections()   # proves default_tenant resolves
            return client, d
        except Exception as e:
            last_err = e
            shutil.rmtree(d, ignore_errors=True)
    raise RuntimeError(
        f"ChromaDB {CHROMA_VERSION} could not initialise a LOCAL client: {last_err!r} | "
        "Fix: (a) remove stray CHROMA_* env vars from the deploy environment, "
        "(b) pip install --force-reinstall chromadb, "
        "(c) delete old chroma.sqlite3 / chroma_db / memory_db in the service cwd."
    )


def probe() -> str:
    """One-shot health probe used by /health. Returns 'ok' or an error string."""
    try:
        client, d = new_client(prefix="chroma_health_")
        client.list_collections()
        shutil.rmtree(d, ignore_errors=True)
        return f"ok (chromadb {CHROMA_VERSION})"
    except Exception as e:
        return f"BROKEN: {e}"
