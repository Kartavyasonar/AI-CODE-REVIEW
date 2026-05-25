"""
agents/quality_agent.py  v3

Improvements vs v2:
  1. Complexity scan skips test files — test functions are intentionally complex
  2. File type context in LLM prompt
  3. LLM prompt explicitly ignores test file quality issues
  4. Higher CC threshold for test files (15 instead of 10)
  5. Maintainability index skipped for test files entirely
"""
import json
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage

from backend.core.llm import get_llm
from backend.core.hyde import hyde_queries
from backend.core.reranker import retrieve_and_rerank
from backend.core.reflection import score_and_reflect
from backend.core.state import Finding

try:
    from radon.complexity import cc_visit
    from radon.metrics import mi_visit
    RADON_AVAILABLE = True
except ImportError:
    RADON_AVAILABLE = False

SYSTEM_PROMPT = """You are a senior engineer focused on code quality in PRODUCTION code.

Analyze for: missing docstrings on public functions, functions >50 lines,
deep nesting (>3 levels), magic numbers, poor naming, duplicate code,
unused imports, missing type annotations on public APIs.

IMPORTANT RULES:
- IGNORE files marked as [TEST FILE] — tests intentionally have different quality standards
- IGNORE files marked as [EXAMPLE CODE] — tutorials prioritize readability over production quality
- Only flag quality issues in [PRODUCTION] files
- Missing docstrings in test functions is acceptable
- Complex test setup functions are acceptable

Respond ONLY with valid JSON array:
{
  "severity": "high|medium|low|info",
  "category": "quality",
  "file": "<filepath>",
  "line": <line or null>,
  "title": "<issue>",
  "description": "<why it matters in production>",
  "suggestion": "<fix>",
  "code_snippet": "<code, max 3 lines>"
}
Only JSON. Return [].
"""

BASE_QUERIES = [
    "function missing docstring no documentation",
    "magic number hardcoded string constant",
    "long function deep nesting if else",
    "unused variable import dead code",
    "duplicate code repeated logic",
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


def _complexity_scan(repo_path: str) -> list[Finding]:
    if not RADON_AVAILABLE or not repo_path:
        return []
    findings = []
    try:
        for py_file in Path(repo_path).rglob("*.py"):
            if any(p in py_file.parts for p in [".git", "venv", "__pycache__", "node_modules"]):
                continue

            rel = str(py_file.relative_to(repo_path))
            is_test = _is_test_or_example(rel)

            # Use higher threshold for test files
            cc_threshold = 20 if is_test else 10

            try:
                content = py_file.read_text(encoding="utf-8", errors="ignore")

                for block in cc_visit(content):
                    if block.complexity >= cc_threshold:
                        # Skip test files for complexity unless extremely complex
                        if is_test and block.complexity < 25:
                            continue
                        sev = "high" if block.complexity >= 15 else "medium"
                        findings.append(Finding(
                            agent       ="quality_agent",
                            severity    =sev,
                            category    ="quality",
                            file        =rel,
                            line        =block.lineno,
                            title       =f"High complexity: {block.name} (CC={block.complexity})",
                            description =f"Cyclomatic complexity {block.complexity}. Above 10 is hard to test and maintain.",
                            suggestion  ="Break into smaller functions with single responsibilities. Target CC < 5.",
                            code_snippet=f"def {block.name}(...):  # CC={block.complexity}",
                        ))

                # Skip MI for test files entirely
                if not is_test:
                    mi = mi_visit(content, multi=True)
                    if isinstance(mi, (int, float)) and mi < 20:
                        findings.append(Finding(
                            agent       ="quality_agent",
                            severity    ="medium",
                            category    ="quality",
                            file        =rel,
                            line        =None,
                            title       =f"Low maintainability index: {mi:.1f}/100",
                            description =f"MI={mi:.1f}. Below 20 indicates unmaintainable code.",
                            suggestion  ="Add docstrings, reduce function size, simplify logic.",
                            code_snippet=None,
                        ))
            except Exception:
                pass
    except Exception:
        pass
    return findings


def _should_skip_finding(fd: dict) -> bool:
    return _is_test_or_example(fd.get("file", ""))


def run_quality_agent(state, collection, extra_queries=None):
    llm       = get_llm()
    repo_path = getattr(state, "repo_path", "")
    static_findings = _complexity_scan(repo_path)

    if collection.count() == 0:
        state.quality_findings = static_findings
        return state

    all_queries      = BASE_QUERIES + (extra_queries or [])
    hyde_expanded    = hyde_queries(all_queries)
    llm_findings     = []
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
                HumanMessage(content=f"Review quality:\n\n{code_context}"),
            ])
            raw = response.content.strip()
            raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            for fd in json.loads(raw):
                if not isinstance(fd, dict):
                    continue
                if _should_skip_finding(fd):
                    continue
                key = (fd.get("file", ""), fd.get("title", ""))
                if key in seen:
                    continue
                seen.add(key)
                llm_findings.append(Finding(
                    agent       ="quality_agent",
                    severity    =fd.get("severity",    "low"),
                    category    =fd.get("category",    "quality"),
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
    llm_findings     = score_and_reflect(llm_findings, combined_context)
    state.quality_findings = static_findings + llm_findings
    return state