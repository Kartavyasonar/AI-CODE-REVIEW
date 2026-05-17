"""
core/memory.py  —  Cross-Repo Memory Store

After each review we persist anonymised patterns (bug types, severity distributions)
so that the NEXT review of a similar repo can:
  - Bias RAG queries toward historically risky patterns
  - Benchmark the new repo's score against past similar repos

FIXES applied vs original:
  - MEMORY_DIR resolves from $MEMORY_DB_DIR env var (absolute path).
    Original used a hardcoded relative "./memory_db" which breaks when the process
    starts from a different working directory (common on Render/Railway).
  - get_high_risk_query_hints: removed the fragile int-parsing hack
    (`int([w for w in doc.split() if w.isdigit()][0])`).
    Score is now read directly from the metadata dict where it is already stored
    as an integer — no string parsing needed.
  - _get_memory_collection: now creates the parent directory if it doesn't exist,
    preventing PersistentClient from throwing on first run.
"""
import json
import os
import time
import uuid
from pathlib import Path

import chromadb
from chromadb.utils import embedding_functions

# FIXED: resolve absolute path from env var; fall back to ./memory_db
MEMORY_DIR  = Path(os.getenv("MEMORY_DB_DIR", "./memory_db")).resolve()
EMBED_MODEL = "all-MiniLM-L6-v2"


def _get_memory_collection() -> chromadb.Collection:
    """Get or create the persistent memory collection."""
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)   # ensure dir exists before PersistentClient
    client = chromadb.PersistentClient(path=str(MEMORY_DIR))
    ef     = embedding_functions.SentenceTransformerEmbeddingFunction(model_name=EMBED_MODEL)
    return client.get_or_create_collection("review_memory", embedding_function=ef)


def store_review_patterns(repo_url: str, findings: list, score: int):
    """
    Persist anonymised review patterns.
    Skips storage if score is 0 (means review failed or produced no output).
    """
    if score == 0:
        return
    try:
        collection      = _get_memory_collection()
        severity_dist   = {}
        category_dist   = {}

        for f in findings:
            sev = getattr(f, "severity", "unknown")
            cat = getattr(f, "category", "unknown")
            severity_dist[sev] = severity_dist.get(sev, 0) + 1
            category_dist[cat] = category_dist.get(cat, 0) + 1

        pattern_doc = (
            f"Repository: {repo_url}\n"
            f"Score: {score}/100\n"
            f"Severity distribution: {json.dumps(severity_dist)}\n"
            f"Category distribution: {json.dumps(category_dist)}\n"
            f"Top issues: {', '.join(getattr(f, 'title', '') for f in findings[:5])}"
        )
        collection.add(
            ids=[str(uuid.uuid4())],
            documents=[pattern_doc],
            metadatas=[{
                "repo_url":       repo_url,
                "score":          score,
                "timestamp":      time.time(),
                "total_findings": len(findings),
            }],
        )
    except Exception:
        pass  # Memory store is best-effort; never crash a review because of it


def get_similar_repo_insights(repo_url: str, n: int = 3) -> str:
    """
    Retrieve patterns from the most semantically similar previously reviewed repos.
    Returns a formatted string for inclusion in the executive summary.
    """
    try:
        collection = _get_memory_collection()
        if collection.count() == 0:
            return ""

        results = collection.query(
            query_texts=[f"Repository review patterns for {repo_url}"],
            n_results=min(n, collection.count()),
        )

        if not results["documents"][0]:
            return ""

        # Filter out legacy zero-score entries (stored before scoring was fixed)
        valid = [m for m in results["metadatas"][0] if m.get("score", 0) > 0]
        if not valid:
            return ""

        insights = [
            f"- Similar repo scored {m['score']}/100 "
            f"with {m.get('total_findings', '?')} total findings"
            for m in valid
        ]
        avg = sum(m["score"] for m in valid) / len(valid)

        return (
            f"\n\n**Benchmark ({len(valid)} previously reviewed repo(s)):**\n"
            + "\n".join(insights)
            + f"\nAverage score of similar repos: {avg:.0f}/100"
        )
    except Exception:
        return ""


def get_high_risk_query_hints(repo_url: str) -> list[str]:
    """
    Based on memory of similar repos, return extra RAG queries for agents.
    E.g. if similar repos always had auth issues, add auth-focused queries.

    FIXED: original tried to parse `score` out of free-text document strings
    using `int([w for w in doc.split() if w.isdigit()][0])` — very fragile.
    Score is already stored as an integer in metadata; read it from there.
    """
    try:
        collection = _get_memory_collection()
        if collection.count() == 0:
            return []

        results = collection.query(
            query_texts=[repo_url],
            n_results=min(3, collection.count()),
        )

        extra_queries = []

        for doc, meta in zip(
            results["documents"][0],
            results["metadatas"][0],
        ):
            doc_lower = doc.lower()

            if "security" in doc_lower and "authentication" in doc_lower:
                extra_queries.append("authentication token session cookie")

            if "sql" in doc_lower:
                extra_queries.append("database query string format user input")

            # FIXED: read score directly from metadata integer, no string parsing
            score = meta.get("score", 100)
            if isinstance(score, (int, float)) and score < 50:
                extra_queries.append("critical security vulnerability injection")

        return list(set(extra_queries))[:3]

    except Exception:
        return []
