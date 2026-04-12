"""
core/reranker.py  —  Cross-Encoder Reranking

Standard RAG retrieves top-k chunks by cosine similarity (fast but imprecise).
A cross-encoder reads (query, document) together — much more accurate but slower.
We use it as a second-pass filter: retrieve 30 candidates, rerank, keep top 10.

Model: cross-encoder/ms-marco-MiniLM-L-6-v2 (free, local, ~80MB)
This is the industry-standard reranker for code/text retrieval.
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
        query: The original retrieval query (NOT the HyDE expansion — 
               cross-encoder needs the real query for relevance scoring)
        chunks: List of chunk dicts with 'content' key
        top_k: How many to keep after reranking
    
    Returns:
        Top-k chunks sorted by cross-encoder relevance score (highest first)
    """
    if not RERANKER_AVAILABLE or not chunks:
        return chunks[:top_k]

    try:
        reranker = _load_reranker()
        pairs = [(query, c["content"][:512]) for c in chunks]
        scores = reranker.predict(pairs)

        scored = sorted(
            zip(scores, chunks),
            key=lambda x: x[0],
            reverse=True
        )
        return [chunk for _, chunk in scored[:top_k]]
    except Exception:
        return chunks[:top_k]  # Fallback: return original order


def retrieve_and_rerank(
    collection,
    original_query: str,
    hyde_query: str,
    n_retrieve: int = 30,
    top_k: int = 10,
) -> list[dict]:
    """
    Full two-stage retrieval:
      1. Retrieve n_retrieve candidates using HyDE-expanded query (dense vector search)
      2. Rerank with cross-encoder using the original query (precision filter)
    
    This combination gives you the recall of dense retrieval + precision of cross-encoding.
    """
    # Stage 1: Dense retrieval with HyDE query
    n = min(n_retrieve, collection.count())
    if n == 0:
        return []

    results = collection.query(query_texts=[hyde_query], n_results=n)
    candidates = []
    for i, doc in enumerate(results["documents"][0]):
        meta = results["metadatas"][0][i]
        candidates.append({"content": doc, **meta})

    # Stage 2: Cross-encoder reranking with original query
    return rerank(original_query, candidates, top_k=top_k)
