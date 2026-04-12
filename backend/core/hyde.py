"""
core/hyde.py  —  Hypothetical Document Embedding (HyDE)

Instead of embedding a raw query like "sql injection vulnerability",
we ask the LLM to generate what a REAL vulnerable code snippet would
look like, then embed THAT. This lands much closer to actual code
vectors in ChromaDB than a plain keyword query ever could.

Paper: "Precise Zero-Shot Dense Retrieval without Relevance Labels" (Gao et al. 2022)
"""
from langchain_core.messages import HumanMessage, SystemMessage
from backend.core.llm import get_llm

HYDE_SYSTEM = """You are a senior software engineer.
When given a description of a code issue, write a SHORT realistic Python code snippet
(8-15 lines) that DEMONSTRATES that exact issue — as if it were real vulnerable/buggy code
found in a production codebase.
Return ONLY the code snippet. No explanation. No markdown fences."""


def expand_query_with_hyde(query: str, language: str = "python") -> str:
    """
    Given a plain-text query describing a code issue,
    generate a hypothetical code snippet that embodies the issue.
    Returns the hypothetical snippet (used as the embedding query).
    Falls back to the original query if generation fails.
    """
    llm = get_llm(temperature=0.3)
    messages = [
        SystemMessage(content=HYDE_SYSTEM),
        HumanMessage(content=f"Write a {language} code example demonstrating: {query}"),
    ]
    try:
        response = llm.invoke(messages)
        hypothetical = response.content.strip()
        # Combine original query + hypothetical for richer embedding
        return f"{query}\n\n{hypothetical}"
    except Exception:
        return query  # Graceful fallback


def hyde_queries(queries: list[str], language: str = "python") -> list[str]:
    """Expand a list of retrieval queries using HyDE."""
    return [expand_query_with_hyde(q, language) for q in queries]
