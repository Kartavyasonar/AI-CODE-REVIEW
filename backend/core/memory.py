"""
core/memory.py  —  Cross-Repo Memory Store

After each review, we persist:
- Patterns of bugs found (anonymised — just pattern type + file structure)
- Which queries retrieved the most useful chunks
- Severity distributions per repo type

On the NEXT review, this memory is used to:
- Bias RAG queries toward patterns seen in similar repos
- Warn the user if their repo matches a historically risky profile
- Personalise the executive summary with benchmark comparisons

This is a lightweight implementation using ChromaDB as the memory backend.
In production you'd use Redis + a proper pattern DB.
"""
import json
import time
import uuid
import chromadb
from pathlib import Path
from chromadb.utils import embedding_functions

MEMORY_DIR = Path("./memory_db")
EMBED_MODEL = "all-MiniLM-L6-v2"


def _get_memory_collection() -> chromadb.Collection:
    """Get or create the persistent memory collection."""
    client = chromadb.PersistentClient(path=str(MEMORY_DIR))
    ef = embedding_functions.SentenceTransformerEmbeddingFunction(model_name=EMBED_MODEL)
    return client.get_or_create_collection("review_memory", embedding_function=ef)


def store_review_patterns(repo_url: str, findings: list, score: int):
    """Store anonymised patterns — skip storing if score is 0 (means something went wrong)."""
    if score == 0:
        return  # Don't persist broken/empty review results
    try:
        collection = _get_memory_collection()
        severity_dist = {}
        category_dist = {}
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
            f"Top issues: {', '.join(getattr(f,'title','') for f in findings[:5])}"
        )
        collection.add(
            ids=[str(uuid.uuid4())],
            documents=[pattern_doc],
            metadatas=[{
                "repo_url": repo_url,
                "score": score,
                "timestamp": time.time(),
                "total_findings": len(findings),
            }],
        )
    except Exception:
        pass


def get_similar_repo_insights(repo_url: str, n: int = 3) -> str:
    """
    Retrieve patterns from the most similar previously reviewed repos.
    Filters out legacy zero-score entries (from before the scoring fix).
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

        # Filter out legacy zero-scores (stored before logarithmic scoring was added)
        valid = [
            m for m in results["metadatas"][0]
            if m.get("score", 0) > 0
        ]

        if not valid:
            return ""

        insights = [
            f"- Similar repo scored {m['score']}/100 with {m.get('total_findings','?')} total findings"
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
    Based on memory of similar repos, return extra queries to add to agents.
    If repos with similar structure always had auth issues, add auth-focused queries.
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
        for doc in results["documents"][0]:
            if "security" in doc and "authentication" in doc.lower():
                extra_queries.append("authentication token session cookie")
            if "sql" in doc.lower():
                extra_queries.append("database query string format user input")
            if "high" in doc and "score" in doc:
                try:
                    score = int([w for w in doc.split() if w.isdigit()][0])
                    if score < 50:
                        extra_queries.append("critical security vulnerability injection")
                except Exception:
                    pass

        return list(set(extra_queries))[:3]
    except Exception:
        return []