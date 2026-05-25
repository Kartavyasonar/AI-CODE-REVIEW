"""
agents/security_agent.py  v3

Improvements vs v2:
  1. Test file filtering on static scan — expanded to cover more test patterns
  2. Example code filtering — tutorial code like flask/examples not flagged as prod issues
  3. File type context in LLM prompt
  4. Smarter static scan — severity is contextual not always "high"
  5. Better dedup — normalized title matching
  6. Static scan results also filtered through test/example classifier
"""
import json
import re

from langchain_core.messages import HumanMessage, SystemMessage

from backend.core.llm import get_llm
from backend.core.hyde import hyde_queries
from backend.core.reranker import retrieve_and_rerank
from backend.core.reflection import score_and_reflect
from backend.core.state import Finding

SYSTEM_PROMPT = """You are a senior application security engineer.

Analyze for OWASP Top 10: SQL Injection, Command Injection, XSS, Broken Auth,
Sensitive Data Exposure, Insecure Deserialization (pickle/eval), SSRF, weak CORS.
Also: hardcoded secrets, MD5/SHA1 passwords, SSL verify=False.

IMPORTANT RULES:
- IGNORE files marked as [TEST FILE] — security patterns in tests are intentional mocks
- IGNORE files marked as [EXAMPLE CODE] — tutorial code intentionally shows vulnerabilities
- For PRODUCTION files only, flag real vulnerabilities with evidence from the code
- eval() in a CLI tool's REPL feature is NOT a vulnerability — it is intentional
- DEBUG=True in a tutorial config is NOT a production issue
- Only flag issues where real user data could be compromised in production

Respond ONLY with valid JSON array:
{
  "severity": "critical|high|medium|low",
  "category": "security",
  "file": "<filepath>",
  "line": <line or null>,
  "title": "<vulnerability name>",
  "description": "<attack scenario in production>",
  "suggestion": "<concrete fix>",
  "code_snippet": "<vulnerable code, max 3 lines>"
}
Only JSON. No markdown. Return [].
"""

# (pattern, title, suggestion, severity, skip_in_examples)
SECRET_PATTERNS = [
    (
        r'(?i)(api_key|apikey|secret_key|password|passwd|token)\s*=\s*["\'][^"\']{8,}["\']',
        "Hardcoded secret or credential",
        "Move to environment variable: os.getenv('KEY_NAME'). Never commit secrets to git.",
        "critical",
        False,  # flag even in examples
    ),
    (
        r'verify\s*=\s*False',
        "SSL verification disabled",
        "Remove verify=False. Use a proper CA bundle or fix certificate issues instead.",
        "high",
        True,   # skip in test/example files
    ),
    (
        r'pickle\.loads\s*\(',
        "Unsafe pickle deserialization",
        "Never unpickle untrusted data. Use json.loads() or a safe format instead.",
        "high",
        True,
    ),
    (
        r'\beval\s*\(',
        "Dangerous eval() usage",
        "Avoid eval(). Use ast.literal_eval() for safe parsing, or refactor the logic.",
        "high",
        True,
    ),
    (
        r'(?i)(md5|sha1)\s*\(',
        "Weak cryptographic hash",
        "Use hashlib.sha256() for general hashing, bcrypt/argon2 for passwords.",
        "high",
        True,
    ),
    (
        r'DEBUG\s*=\s*True',
        "Debug mode hardcoded to True",
        "Set via env var: DEBUG = os.getenv('DEBUG', 'false') == 'true'",
        "medium",
        True,   # skip in example/tutorial configs
    ),
    (
        r'os\.system\s*\(',
        "Shell injection via os.system",
        "Use subprocess.run([...], shell=False) with args as a list.",
        "high",
        True,
    ),
    (
        r'subprocess.*shell\s*=\s*True',
        "Shell injection via subprocess shell=True",
        "Use shell=False and pass arguments as a list.",
        "high",
        True,
    ),
    (
        r'(?:requests|httpx)\.(get|post|put|delete|request)\s*\(\s*(?:url|repo_url|user_url|\w+_url)',
        "Potential SSRF — user-controlled URL in HTTP request",
        "Validate URL against allowlist. Block file://, internal IPs, localhost.",
        "medium",
        True,
    ),
]

BASE_QUERIES = [
    "sql query user input string concatenation format",
    "authentication password token session cookie",
    "subprocess exec shell command user input",
    "cors headers access control origin",
    "requests http url user input fetch external",
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


def _static_scan(all_chunks: list) -> list[Finding]:
    findings = []
    for chunk in all_chunks:
        content  = chunk.get("content",  "")
        filepath = chunk.get("filepath", "unknown")
        file_class = _classify_file(filepath)
        is_test_or_example = file_class in ("TEST FILE", "EXAMPLE CODE")

        for pattern, title, suggestion, severity, skip_in_examples in SECRET_PATTERNS:
            if skip_in_examples and is_test_or_example:
                continue
            for match in re.finditer(pattern, content):
                line_num = content[: match.start()].count("\n") + 1
                findings.append(Finding(
                    agent       ="security_agent",
                    severity    =severity,
                    category    ="security",
                    file        =filepath,
                    line        =line_num,
                    title       =title,
                    description =f"Detected in {file_class}: `{match.group()[:80]}`",
                    suggestion  =suggestion,
                    code_snippet=match.group()[:120],
                ))
    return findings


def _should_skip_finding(fd: dict) -> bool:
    filepath = fd.get("file", "").lower().replace("\\", "/")
    return _is_test_or_example(filepath)


def _normalize_title(title: str) -> str:
    return re.sub(r'\s+', ' ', title.lower().strip())


def run_security_agent(state, collection, extra_queries=None):
    llm          = get_llm()
    all_chunks   = getattr(state, "all_chunks", [])
    static_findings = _static_scan(all_chunks)

    if collection.count() == 0:
        state.security_findings = static_findings
        return state

    all_queries   = BASE_QUERIES + (extra_queries or [])
    hyde_expanded = hyde_queries(all_queries)
    llm_findings  = []
    seen          = set()
    all_code_context = []

    for original_q, hyde_q in zip(all_queries, hyde_expanded):
        chunks = retrieve_and_rerank(
            collection,
            original_q,
            hyde_q,
            n_retrieve=min(30, collection.count()),
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
                HumanMessage(content=f"Security audit:\n\n{code_context}"),
            ])
            raw = response.content.strip()
            raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            for fd in json.loads(raw):
                if not isinstance(fd, dict):
                    continue
                if _should_skip_finding(fd):
                    continue
                key = (fd.get("file", ""), _normalize_title(fd.get("title", "")))
                if key in seen:
                    continue
                seen.add(key)
                llm_findings.append(Finding(
                    agent       ="security_agent",
                    severity    =fd.get("severity",    "low"),
                    category    =fd.get("category",    "security"),
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
    state.security_findings = static_findings + llm_findings
    return state