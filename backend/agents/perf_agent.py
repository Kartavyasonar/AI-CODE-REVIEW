"""
agents/perf_agent.py  v3

Improvements vs v2:
  1. Test file filtering — performance patterns in tests are acceptable
  2. File type context in LLM prompt
  3. Tighter prompt — ignores test setup patterns
"""
import json
import re

from langchain_core.messages import HumanMessage, SystemMessage

from backend.core.llm import get_llm
from backend.core.hyde import hyde_queries
from backend.core.reranker import retrieve_and_rerank
from backend.core.reflection import score_and_reflect
from backend.core.state import Finding

SYSTEM_PROMPT = """You are a performance engineering expert analyzing PRODUCTION code.

Analyze for: O(n²) algorithms, N+1 database queries, blocking I/O in async,
string concat in loops, missing caching, unbounded memory growth, SELECT *.

IMPORTANT RULES:
- IGNORE files marked as [TEST FILE] — performance is not critical in tests
- IGNORE files marked as [EXAMPLE CODE] — tutorial code prioritizes clarity over performance
- Only flag issues in [PRODUCTION] files where real users will be affected
- Nested loops in test setup are acceptable
- Only flag issues with clear performance impact evidence in the code

Respond ONLY with valid JSON array:
{
  "severity": "high|medium|low|info",
  "category": "performance",
  "file": "<filepath>",
  "line": <line or null>,
  "title": "<issue>",
  "description": "<why slow and what the real-world impact is>",
  "suggestion": "<concrete fix with example>",
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

_TEST_MARKERS    = {"test_", "_test", "tests/", "/tests/", "conftest", "fixtures"}
_EXAMPLE_MARKERS = {"examples/", "/examples/", "tutorial/", "demo/", "sample/"}


def _classify_file(filepath: str) -> str:
    fp = filepath.lower().replace("\\", "/")
    if any(m in fp for m in _TEST_MARKERS):
        return "TEST FILE"
    if any(m in fp for m in _EXAMPLE_MARKERS):
        return "EXAMPLE CODE"
    return "PRODUCTION"


def _is_test_or_example(filepath: str) -> bool:
    fp = filepath.lower().replace("\\", "/")
    return any(m in fp for m in _TEST_MARKERS) or any(m in fp for m in _EXAMPLE_MARKERS)


def _normalize_title(title: str) -> str:
    return re.sub(r'\s+', ' ', title.lower().strip())


def run_perf_agent(state, collection, extra_queries=None):
    llm = get_llm()

    if collection.count() == 0:
        state.perf_findings = []
        return state

    all_queries      = BASE_QUERIES + (extra_queries or [])
    hyde_expanded    = hyde_queries(all_queries)
    all_findings     = []
    seen             = set()
    all_code_context = []

    for original_q, hyde_q in zip(all_queries, hyde_expanded):
        chunks = retrieve_and_rerank(
            collection,
            original_q,
            hyde_q,
            n_retrieve=min(25, collection.count()),
            top_k=10,
        )
        if not chunks:
            continue

        code_context = "\n\n---\n\n".join(
            f"File: {c.get('filepath','?')} [{_classify_file(c.get('filepath',''))}]\n{c['content']}"
            for c in chunks
        )
        all_code_context.append(code_context)

        try:
            response = llm.invoke([
                SystemMessage(content=SYSTEM_PROMPT),
                HumanMessage(content=f"Performance analysis:\n\n{code_context}"),
            ])
            raw = response.content.strip()
            raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            for fd in json.loads(raw):
                if not isinstance(fd, dict):
                    continue
                # Skip test and example findings
                if _is_test_or_example(fd.get("file", "")):
                    continue
                key = (fd.get("file", ""), _normalize_title(fd.get("title", "")))
                if key in seen:
                    continue
                seen.add(key)
                all_findings.append(Finding(
                    agent       ="perf_agent",
                    severity    =fd.get("severity",    "low"),
                    category    =fd.get("category",    "performance"),
                    file        =fd.get("file",        "unknown"),
                    line        =fd.get("line"),
                    title       =fd.get("title",       "Untitled"),
                    description =fd.get("description", ""),
                    suggestion  =fd.get("suggestion",  ""),
                    code_snippet=fd.get("code_snippet"),
                ))
        except Exception:
            pass

    combined_context = "\n\n".join(all_code_context)[:4000]
    all_findings     = score_and_reflect(all_findings, combined_context)
    state.perf_findings = all_findings
    return state