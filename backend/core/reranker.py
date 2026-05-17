"""
core/reranker.py  —  Cross-Encoder Reranking

Standard RAG retrieves top-k chunks by cosine similarity (fast but imprecise).
A cross-encoder reads (query, document) together — much more accurate but slower.
We use it as a second-pass filter: retrieve 30 candidates, rerank, keep top 10.

Model: cross-encoder/ms-marco-MiniLM-L-6-v2 (free, local, ~80MB)
This is the industry-standard reranker for code/text retrieval.

No fixes needed — this file was correct in the original.
lru_cache(maxsize=1) on _load_reranker is correct here because the reranker
takes no arguments (unlike get_llm which took temperature — that was the bug).
"""
from functools import lru_cache

try:
    from sentence_transformers import CrossEncoder
    RERANKER_AVAILABLE = True
except ImportError:
    RERANKER_AVAILABLE = False


@lru_cache(maxsize=1)
def _load_reranker():
    """Load and cache the cross-encoder model (runs once per process)."""
    return CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2", max_length=512)


def rerank(query: str, chunks: list[dict], top_k: int = 10) -> list[dict]:
    """
    Rerank retrieved chunks using a cross-encoder.

    Args:
        query:  The ORIGINAL retrieval query (not the HyDE expansion —
                cross-encoder needs the real query for relevance scoring).
        chunks: List of chunk dicts with a 'content' key.
        top_k:  How many to keep after reranking.

    Returns:
        Top-k chunks sorted by cross-encoder relevance score (highest first).
    """
    if not RERANKER_AVAILABLE or not chunks:
        return chunks[:top_k]

    try:
        reranker = _load_reranker()
        pairs    = [(query, c["content"][:512]) for c in chunks]
        scores   = reranker.predict(pairs)
        scored   = sorted(zip(scores, chunks), key=lambda x: x[0], reverse=True)
        return [chunk for _, chunk in scored[:top_k]]
    except Exception:
        return chunks[:top_k]   # fallback: return original order


def retrieve_and_rerank(
    collection,
    original_query: str,
    hyde_query:     str,
    n_retrieve:     int = 30,
    top_k:          int = 10,
) -> list[dict]:
    """
    Two-stage retrieval:
      1. Dense retrieval with HyDE-expanded query (high recall)
      2. Cross-encoder rerank with original query   (high precision)
    """
    n = min(n_retrieve, collection.count())
    if n == 0:
        return []

    results    = collection.query(query_texts=[hyde_query], n_results=n)
    candidates = [
        {"content": doc, **results["metadatas"][0][i]}
        for i, doc in enumerate(results["documents"][0])
    ]
    return rerank(original_query, candidates, top_k=top_k)
