"""
core/hyde.py  —  Hypothetical Document Embedding (HyDE)

Instead of embedding a raw query like "sql injection vulnerability",
we ask the LLM to generate what REAL vulnerable code looks like,
then embed THAT. This lands much closer to actual code vectors in ChromaDB
than a plain keyword query ever could.

Paper: "Precise Zero-Shot Dense Retrieval without Relevance Labels" (Gao et al. 2022)

FIXES applied vs original:
  - hyde_queries: was fully sequential — N queries = N sequential LLM round-trips.
    With 4 agents × ~8 queries each = 32 sequential Groq calls before analysis
    even begins. This burned rate-limit tokens slowly and added ~30-60s of latency.
    Fixed: parallel execution via ThreadPoolExecutor(max_workers=4).
    Results are returned in the original order (futures dict preserves index).
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

from langchain_core.messages import HumanMessage, SystemMessage

from backend.core.llm import get_llm

HYDE_SYSTEM = """You are a senior software engineer.
When given a description of a code issue, write a SHORT realistic code snippet
(8-15 lines) that DEMONSTRATES that exact issue — as if it were real vulnerable/buggy
code found in a production codebase.
Return ONLY the code snippet. No explanation. No markdown fences."""


def expand_query_with_hyde(query: str, language: str = "python") -> str:
    """
    Given a plain-text query describing a code issue,
    generate a hypothetical code snippet that embodies the issue.
    Returns the combined original query + hypothetical snippet for richer embedding.
    Falls back to the original query on any error.
    """
    llm = get_llm(temperature=0.3)
    messages = [
        SystemMessage(content=HYDE_SYSTEM),
        HumanMessage(content=f"Write a {language} code example demonstrating: {query}"),
    ]
    try:
        response    = llm.invoke(messages)
        hypothetical = response.content.strip()
        return f"{query}\n\n{hypothetical}"
    except Exception:
        return query  # Graceful fallback — original query still works, just less precise


def hyde_queries(queries: list[str], language: str = "python") -> list[str]:
    """
    Expand a list of retrieval queries using HyDE.

    FIXED: parallelised with ThreadPoolExecutor so all N queries run concurrently
    instead of sequentially.  Results are reassembled in original order.
    max_workers=4 keeps Groq API pressure reasonable while still being fast.
    """
    if not queries:
        return []

    results: list[str | None] = [None] * len(queries)

    with ThreadPoolExecutor(max_workers=4) as executor:
        future_to_index = {
            executor.submit(expand_query_with_hyde, q, language): i
            for i, q in enumerate(queries)
        }
        for future in as_completed(future_to_index):
            idx         = future_to_index[future]
            try:
                results[idx] = future.result()
            except Exception:
                results[idx] = queries[idx]  # fallback to original on per-query error

    # Safety: fill any None slots (shouldn't happen, but be defensive)
    return [r if r is not None else queries[i] for i, r in enumerate(results)]
