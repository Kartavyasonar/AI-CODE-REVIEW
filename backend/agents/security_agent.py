"""agents/security_agent.py  v2"""
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

Respond ONLY with valid JSON array:
{
  "severity": "critical|high|medium|low",
  "category": "security",
  "file": "<filepath>",
  "line": <line or null>,
  "title": "<vulnerability name>",
  "description": "<attack scenario>",
  "suggestion": "<concrete fix>",
  "code_snippet": "<vulnerable code, max 3 lines>"
}
Only JSON. No markdown. Return [].
"""

SECRET_PATTERNS = [
    (
        r'(?i)(api_key|apikey|secret|password|token|passwd)\s*=\s*["\'][^"\']{8,}["\']',
        "Hardcoded secret",
        "Move this value to an environment variable and load with os.getenv() or python-dotenv.",
    ),
    (
        r'verify\s*=\s*False',
        "SSL verification disabled",
        "Set verify=True (default). If testing locally, use a proper CA bundle instead of disabling verification.",
    ),
    (
        r'pickle\.loads\s*\(',
        "Unsafe pickle deserialization",
        "Never unpickle data from untrusted sources. Use json.loads() or a safe serialization format instead.",
    ),
    (
        r'\beval\s*\(',
        "Dangerous eval() usage",
        "Avoid eval() entirely. Use ast.literal_eval() for safe expression parsing, or refactor the logic.",
    ),
    (
        r'(?i)(md5|sha1)\s*\(',
        "Weak cryptographic hash",
        "MD5/SHA1 are broken for security use. Use hashlib.sha256() or bcrypt/argon2 for passwords.",
    ),
    (
        r'DEBUG\s*=\s*True',
        "Debug mode enabled",
        "Never deploy with DEBUG=True. Set via environment variable: DEBUG = os.getenv('DEBUG', 'false') == 'true'.",
    ),
    (
        r'os\.system\s*\(',
        "Shell injection via os.system",
        "Replace os.system() with subprocess.run([...], shell=False) and pass arguments as a list.",
    ),
    (
        r'subprocess.*shell\s*=\s*True',
        "Shell injection via subprocess",
        "Use shell=False and pass arguments as a list: subprocess.run(['cmd', 'arg1'], shell=False).",
    ),
]

BASE_QUERIES = [
    "sql query user input string concatenation format",
    "authentication password token session cookie",
    "subprocess exec shell command user input",
    "cors headers access control origin",
    "requests http url user input fetch external",
]


def _static_scan(all_chunks):
    findings = []
    for chunk in all_chunks:
        content = chunk.get("content", "")
        filepath = chunk.get("filepath", "unknown")
        # Skip test files for verify=False and pickle (intentional in tests)
        is_test = "test" in filepath.lower()
        for pattern, title, suggestion in SECRET_PATTERNS:
            if is_test and any(k in pattern for k in ["verify", "pickle"]):
                continue
            for match in re.finditer(pattern, content):
                line_num = content[:match.start()].count("\n") + 1
                findings.append(Finding(
                    agent="security_agent", severity="high", category="security",
                    file=filepath, line=line_num, title=title,
                    description=f"Detected: `{match.group()[:80]}`",
                    suggestion=suggestion,
                    code_snippet=match.group()[:120],
                ))
    return findings


def run_security_agent(state, collection, extra_queries=None):
    llm = get_llm()
    all_chunks = getattr(state, "all_chunks", [])
    static_findings = _static_scan(all_chunks)

    if collection.count() == 0:
        state.security_findings = static_findings
        return state

    all_queries = BASE_QUERIES + (extra_queries or [])
    hyde_expanded = hyde_queries(all_queries)
    llm_findings = []
    seen = set()
    all_code_context = []

    for original_q, hyde_q in zip(all_queries, hyde_expanded):
        chunks = retrieve_and_rerank(collection, original_q, hyde_q,
                                     n_retrieve=min(30, collection.count()), top_k=10)
        if not chunks:
            continue
        code_context = "\n\n---\n\n".join(
            f"File: {c.get('filepath','?')}\n{c['content']}" for c in chunks)
        all_code_context.append(code_context)
        try:
            response = llm.invoke([SystemMessage(content=SYSTEM_PROMPT),
                                    HumanMessage(content=f"Security audit:\n\n{code_context}")])
            raw = response.content.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            for fd in json.loads(raw):
                if not isinstance(fd, dict):
                    continue
                key = (fd.get("file",""), fd.get("title",""))
                if key in seen:
                    continue
                seen.add(key)
                llm_findings.append(Finding(
                    agent="security_agent",
                    severity=fd.get("severity","low"), category=fd.get("category","security"),
                    file=fd.get("file","unknown"), line=fd.get("line"),
                    title=fd.get("title","Untitled"), description=fd.get("description",""),
                    suggestion=fd.get("suggestion",""), code_snippet=fd.get("code_snippet"),
                ))
        except Exception:
            pass

    combined_context = "\n\n".join(all_code_context)[:4000]
    llm_findings = score_and_reflect(llm_findings, combined_context)
    state.security_findings = static_findings + llm_findings
    return state