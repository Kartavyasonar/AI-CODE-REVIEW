"""agents/perf_agent.py  v2"""
import json
from langchain_core.messages import HumanMessage, SystemMessage

from backend.core.llm import get_llm
from backend.core.hyde import hyde_queries
from backend.core.reranker import retrieve_and_rerank
from backend.core.reflection import score_and_reflect
from backend.core.state import Finding

SYSTEM_PROMPT = """You are a performance engineering expert.
Analyze for: O(n²) algorithms, N+1 database queries, blocking I/O in async,
string concat in loops, missing caching, unbounded memory growth, SELECT *.

Respond ONLY with valid JSON array:
{
  "severity": "high|medium|low|info",
  "category": "performance",
  "file": "<filepath>",
  "line": <line or null>,
  "title": "<issue>",
  "description": "<why slow>",
  "suggestion": "<concrete fix>",
  "code_snippet": "<slow code, max 3 lines>"
}
Only JSON. Return [].
"""

BASE_QUERIES = [
    "for loop database query orm N+1 nested loop",
    "string concatenation append list loop",
    "async await blocking sleep requests io",
    "sort nested loop O n squared algorithm",
    "select all columns memory load entire file",
]


def run_perf_agent(state, collection, extra_queries=None):
    llm = get_llm()

    if collection.count() == 0:
        state.perf_findings = []
        return state

    all_queries = BASE_QUERIES + (extra_queries or [])
    hyde_expanded = hyde_queries(all_queries)
    all_findings = []
    seen = set()
    all_code_context = []

    for original_q, hyde_q in zip(all_queries, hyde_expanded):
        chunks = retrieve_and_rerank(collection, original_q, hyde_q,
                                     n_retrieve=min(25, collection.count()), top_k=10)
        if not chunks:
            continue
        code_context = "\n\n---\n\n".join(f"File: {c.get('filepath','?')}\n{c['content']}" for c in chunks)
        all_code_context.append(code_context)
        try:
            response = llm.invoke([SystemMessage(content=SYSTEM_PROMPT),
                                    HumanMessage(content=f"Performance analysis:\n\n{code_context}")])
            raw = response.content.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            for fd in json.loads(raw):
                if not isinstance(fd, dict):
                    continue
                key = (fd.get("file",""), fd.get("title",""))
                if key in seen:
                    continue
                seen.add(key)
                all_findings.append(Finding(
                    agent="perf_agent",
                    severity=fd.get("severity","low"), category=fd.get("category","performance"),
                    file=fd.get("file","unknown"), line=fd.get("line"),
                    title=fd.get("title","Untitled"), description=fd.get("description",""),
                    suggestion=fd.get("suggestion",""), code_snippet=fd.get("code_snippet"),
                ))
        except Exception:
            pass

    combined_context = "\n\n".join(all_code_context)[:4000]
    all_findings = score_and_reflect(all_findings, combined_context)
    state.perf_findings = all_findings
    return state
