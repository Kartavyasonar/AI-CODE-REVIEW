"""
core/ingestion.py
Clones a GitHub repo, walks files, chunks at AST boundaries, embeds into ChromaDB.

FIXES applied vs original:
  - chunk_python_file: only chunks TOP-LEVEL nodes (col_offset == 0).
    Original used ast.walk() which visits ALL nested nodes, so methods inside
    classes were triple-embedded (class body + each method separately).
  - clone_repo: no change needed here — cleanup is now done by pipeline._synthesize_node
    via shutil.rmtree(repo_path) after synthesis completes.
  - No other logic changes — ingestion was otherwise correct.
"""
import ast
import logging
import os
import tempfile
import uuid
from pathlib import Path

# Must be set BEFORE chromadb import to suppress telemetry noise
os.environ["ANONYMIZED_TELEMETRY"] = "false"
os.environ["CHROMA_TELEMETRY"]     = "false"
logging.getLogger("chromadb").setLevel(logging.CRITICAL)
logging.getLogger("chromadb.telemetry").setLevel(logging.CRITICAL)

import chromadb
import git
from chromadb.utils import embedding_functions
from rich.console import Console

console = Console()

SUPPORTED_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx",
    ".java", ".go", ".rs", ".cpp", ".c", ".cs", ".rb",
}
MAX_CHUNK_CHARS = 2000
EMBED_MODEL     = "all-MiniLM-L6-v2"

# Skip directories that are never user source code
_SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", ".next", ".cache", "coverage", ".tox",
    "htmlcov", "site-packages", "eggs",
}


# ── Clone ──────────────────────────────────────────────────────────────────────

def clone_repo(repo_url: str) -> str:
    """
    Shallow-clone repo_url into a temp directory and return the path.
    Caller (pipeline._synthesize_node) is responsible for cleanup via shutil.rmtree.
    """
    tmp = tempfile.mkdtemp(prefix="codereview_")
    console.log(f"[cyan]Cloning[/cyan] {repo_url} → {tmp}")
    try:
        git.Repo.clone_from(repo_url, tmp, depth=1)
    except Exception as e:
        raise RuntimeError(
            f"Failed to clone repo: {e}\n"
            "Check the URL is public and you have internet access."
        )
    return tmp


# ── File walking ───────────────────────────────────────────────────────────────

def walk_files(repo_path: str) -> list:
    files = []
    for root, dirs, filenames in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for fname in filenames:
            ext = Path(fname).suffix
            if ext not in SUPPORTED_EXTENSIONS:
                continue
            full = os.path.join(root, fname)
            rel  = os.path.relpath(full, repo_path)
            try:
                content = Path(full).read_text(encoding="utf-8", errors="ignore")
                if len(content.strip()) > 20:
                    files.append({
                        "path":     rel,
                        "content":  content,
                        "language": ext.lstrip("."),
                    })
            except Exception:
                pass
    console.log(f"[green]Found[/green] {len(files)} source files")
    return files


# ── Chunking ───────────────────────────────────────────────────────────────────

def chunk_python_file(content: str, filepath: str) -> list:
    """
    AST-aware chunking for Python.

    FIXED: original used ast.walk() which visits ALL nodes recursively.
    A class Foo with methods bar() and baz() produced chunks for:
      - Foo (full class including all method bodies)
      - bar (already inside Foo's chunk)
      - baz (already inside Foo's chunk)
    This triple-embedded the same code, polluting retrieval with duplicates.

    Fix: only emit nodes whose col_offset == 0 (top-level in the module).
    Methods and nested classes are covered by their parent's chunk.
    """
    chunks = []
    lines  = content.splitlines()
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return chunk_naive(content, filepath, "py")

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        # FIXED: only top-level definitions (col_offset == 0 means not inside a class/function)
        if node.col_offset != 0:
            continue
        start      = node.lineno - 1
        end        = getattr(node, "end_lineno", start + 10)
        chunk_text = "\n".join(lines[start:end])
        if len(chunk_text.strip()) < 30:
            continue
        chunks.append({
            "id":         str(uuid.uuid4()),
            "content":    chunk_text[:MAX_CHUNK_CHARS],
            "filepath":   filepath,
            "type":       type(node).__name__,
            "name":       node.name,
            "start_line": node.lineno,
            "end_line":   end,
            "language":   "py",
        })

    return chunks if chunks else chunk_naive(content, filepath, "py")


def chunk_naive(content: str, filepath: str, language: str) -> list:
    """Sliding-window chunker for non-Python files."""
    chunks = []
    step   = MAX_CHUNK_CHARS - 200  # 200-char overlap between chunks
    for i in range(0, max(1, len(content)), step):
        chunk_text = content[i: i + MAX_CHUNK_CHARS]
        if len(chunk_text.strip()) < 30:
            continue
        chunks.append({
            "id":         str(uuid.uuid4()),
            "content":    chunk_text,
            "filepath":   filepath,
            "type":       "chunk",
            "name":       f"chunk_{i}",
            "start_line": None,
            "end_line":   None,
            "language":   language,
        })
    return chunks


def chunk_file(file_info: dict) -> list:
    if file_info["language"] == "py":
        return chunk_python_file(file_info["content"], file_info["path"])
    return chunk_naive(file_info["content"], file_info["path"], file_info["language"])


# ── Vector store ───────────────────────────────────────────────────────────────

def build_vector_store(chunks: list, collection_name: str):
    ef     = embedding_functions.SentenceTransformerEmbeddingFunction(model_name=EMBED_MODEL)
    client = chromadb.Client()  # in-memory: no disk writes, no telemetry

    try:
        client.delete_collection(collection_name)
    except Exception:
        pass

    collection = client.create_collection(name=collection_name, embedding_function=ef)

    if not chunks:
        console.log("[yellow]Warning:[/yellow] No chunks to embed.")
        return collection

    batch_size   = 50
    total_batches = (len(chunks) - 1) // batch_size + 1

    for i in range(0, len(chunks), batch_size):
        batch      = chunks[i: i + batch_size]
        clean_meta = []
        for c in batch:
            m = {k: v for k, v in c.items() if k not in ("id", "content") and v is not None}
            clean_meta.append({
                k: str(v) if not isinstance(v, (str, int, float, bool)) else v
                for k, v in m.items()
            })
        collection.add(
            ids=[c["id"] for c in batch],
            documents=[c["content"] for c in batch],
            metadatas=clean_meta,
        )
        batch_num = i // batch_size + 1
        if batch_num % 10 == 0 or batch_num == total_batches:
            console.log(f"[blue]Embedded[/blue] batch {batch_num}/{total_batches}")

    return collection


# ── Public entry point ─────────────────────────────────────────────────────────

def ingest_repo(repo_url: str):
    """
    Clone, walk, chunk, and embed a repository.
    Returns (collection, all_chunks, repo_path).
    repo_path must be deleted by the caller after use.
    """
    repo_path = clone_repo(repo_url)
    files     = walk_files(repo_path)

    all_chunks = []
    for f in files:
        all_chunks.extend(chunk_file(f))
    console.log(f"[green]Total chunks:[/green] {len(all_chunks)}")

    collection_name = "cr_" + str(uuid.uuid4()).replace("-", "")[:12]
    collection      = build_vector_store(all_chunks, collection_name)
    return collection, all_chunks, repo_path
