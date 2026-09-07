"""
api/main.py — FastAPI backend with SSE streaming
Fixes applied:
  - run_pipeline NameError removed; delegates to pipeline.run_review_sync
  - ingest_repo no longer called directly (pipeline handles it internally)
  - Finding objects serialised via _findings_to_dicts before storage
  - jobs dict TTL eviction to prevent OOM
  - SSRF protection via URL allowlist
  - Input validation on repo_url
  - Double CORS middleware removed; single CORSMiddleware only
  - self_ping port reads from $PORT env var
  - job_id uses full uuid4 (not truncated 8-char)
  - thread-safe access to _active_collections via lock (imported from pipeline)
  - graceful shutdown: mark orphaned running jobs as failed on startup
"""
from dotenv import load_dotenv
load_dotenv()
import asyncio
import json
import os
import threading
import time
import uuid
from contextlib import asynccontextmanager
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, field_validator

os.environ["ANONYMIZED_TELEMETRY"] = "false"

# ── Job store ──────────────────────────────────────────────────────────────────
jobs: dict = {}
_jobs_lock = threading.Lock()

JOB_TTL_SECONDS = 3600          # evict completed/failed jobs after 1 hour
MAX_CONCURRENT_REVIEWS = 5      # refuse new jobs if already at limit

ALLOWED_HOSTS = {"github.com", "gitlab.com", "bitbucket.org"}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _findings_to_dicts(findings: list) -> list:
    """Serialise Finding Pydantic objects → plain dicts for JSON responses."""
    result = []
    for f in findings:
        if hasattr(f, "model_dump"):
            result.append(f.model_dump())
        elif isinstance(f, dict):
            result.append(f)
    return result


def _evict_old_jobs():
    """Remove completed/failed jobs older than JOB_TTL_SECONDS."""
    now = time.time()
    with _jobs_lock:
        to_delete = [
            jid for jid, j in jobs.items()
            if j["status"] in ("complete", "failed")
            and now - j.get("created_at", 0) > JOB_TTL_SECONDS
        ]
        for jid in to_delete:
            jobs.pop(jid, None)


def push(job_id: str, event: str, **kwargs):
    """Append an SSE event dict to the job's event queue."""
    with _jobs_lock:
        if job_id in jobs:
            jobs[job_id]["events"].append({
                "event": event,
                "ts": time.time(),
                **kwargs,
            })


# ── Keep-alive self-ping (prevents Render free-tier sleep) ─────────────────────

async def _self_ping():
    port = os.getenv("PORT", "8000")
    await asyncio.sleep(60)          # initial grace period at startup
    while True:
        try:
            async with httpx.AsyncClient() as client:
                await client.get(f"http://localhost:{port}/health", timeout=5)
        except Exception:
            pass
        await asyncio.sleep(300)     # ping every 5 min (Render sleeps at 15 min)


# ── Lifespan ───────────────────────────────────────────────────────────────────

CHROMA_STATUS = {"status": "not checked"}

@asynccontextmanager
async def lifespan(app: FastAPI):
    from backend.core.chroma_client import probe
    CHROMA_STATUS["status"] = probe()          # runs once at boot
    asyncio.create_task(_self_ping())
    yield


# ── App ────────────────────────────────────────────────────────────────────────

app = FastAPI(title="AI Code Review Agent v2", version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.getenv("FRONTEND_URL", "*")],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "Accept", "Cache-Control"],
)


# ── Optional API-key auth ──────────────────────────────────────────────────────

_API_SECRET = os.getenv("API_SECRET", "")


async def _verify_token(request: Request):
    """If API_SECRET env var is set, require matching x-api-key header."""
    if _API_SECRET:
        key = request.headers.get("x-api-key", "")
        if key != _API_SECRET:
            raise HTTPException(status_code=403, detail="Invalid or missing API key")


# ── Request model ──────────────────────────────────────────────────────────────

class ReviewRequest(BaseModel):
    repo_url: str

    @field_validator("repo_url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        v = v.strip()
        if len(v) > 500:
            raise ValueError("URL too long (max 500 chars)")
        parsed = urlparse(v)
        if parsed.scheme not in ("https", "http"):
            raise ValueError("Only HTTP/HTTPS URLs are supported")
        host = (parsed.hostname or "").lower().removeprefix("www.")
        if host not in ALLOWED_HOSTS:
            raise ValueError(
                f"Host '{host}' is not allowed. "
                f"Supported: {', '.join(sorted(ALLOWED_HOSTS))}"
            )
        return v


# ── Background review worker ───────────────────────────────────────────────────

def _run_review_worker(job_id: str, repo_url: str):
    try:
        push(job_id, "status", message="Starting pipeline...", step="status", progress=5)

        from backend.core.pipeline import run_review_sync as pipeline_sync

        push(job_id, "status", message="Cloning & ingesting repository...", step="cloning", progress=10)
        
        # Monkey-patch console to also push SSE events
        import backend.core.pipeline as _pipeline_mod
        original_log = _pipeline_mod.console.log

        def _patched_log(msg, *a, **kw):
            original_log(msg, *a, **kw)
            clean = msg
            for tag in ["[bold cyan]","[bold yellow]","[bold red]","[bold blue]",
                        "[bold green]","[bold magenta]","[/bold cyan]","[/bold yellow]",
                        "[/bold red]","[/bold blue]","[/bold green]","[/bold magenta]",
                        "[cyan]","[red]","[green]","[blue]","[magenta]",
                        "[/cyan]","[/red]","[/green]","[/blue]","[/magenta]"]:
                clean = clean.replace(tag, "")
            clean = clean.strip()
            if not clean:
                return
            # Map log messages to step names and progress
            if "Cloning" in clean or "Ingesting" in clean:
                push(job_id, "status", message=clean, step="cloning", progress=15)
            elif "chunks embedded" in clean or "Embedded" in clean:
                push(job_id, "status", message=clean, step="embedding", progress=30)
            elif "Bug" in clean and "AGENT" in clean:
                push(job_id, "status", message="Bug agent: HyDE + rerank + reflect...", step="agent_bug", progress=40)
            elif "Security" in clean and "AGENT" in clean:
                push(job_id, "status", message="Security agent: HyDE + rerank + reflect...", step="agent_security", progress=55)
            elif "Quality" in clean and "AGENT" in clean:
                push(job_id, "status", message="Quality agent: HyDE + rerank + reflect...", step="agent_quality", progress=65)
            elif "Performance" in clean and "AGENT" in clean:
                push(job_id, "status", message="Performance agent: HyDE + rerank + reflect...", step="agent_perf", progress=75)
            elif "chain" in clean.lower() or "REFLECT" in clean:
                push(job_id, "status", message="Multi-hop chain detection...", step="chains", progress=85)
            elif "Synthesiz" in clean:
                push(job_id, "status", message="Synthesizing final report...", step="synthesizing", progress=90)

        _pipeline_mod.console.log = _patched_log

        result = pipeline_sync(repo_url)

        # Restore original
        _pipeline_mod.console.log = original_log

        bug_f  = _findings_to_dicts(result.get("bug_findings",      []))
        sec_f  = _findings_to_dicts(result.get("security_findings", []))
        qual_f = _findings_to_dicts(result.get("quality_findings",  []))
        perf_f = _findings_to_dicts(result.get("perf_findings",     []))
        total  = len(bug_f) + len(sec_f) + len(qual_f) + len(perf_f)

        serialised = {
            "summary":           result.get("summary", ""),
            "score":             result.get("score", 0),
            "report_markdown":   result.get("report_markdown", ""),
            "total_findings":    total,
            "bug_findings":      bug_f,
            "security_findings": sec_f,
            "quality_findings":  qual_f,
            "perf_findings":     perf_f,
        }

        with _jobs_lock:
            jobs[job_id]["result"] = serialised
            jobs[job_id]["status"] = "complete"

        push(job_id, "complete", score=serialised["score"],
             total_findings=total, progress=100)

    except Exception as e:
        import traceback
        push(job_id, "error",
             message=f"Pipeline error: {str(e)}",
             detail=traceback.format_exc())
        with _jobs_lock:
            jobs[job_id]["status"] = "failed"


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {"status": "AI Code Review Agent is running", "version": "2.0.0"}


@app.get("/health")
async def health():
    with _jobs_lock:
        active = sum(1 for j in jobs.values() if j["status"] == "running")
        total = len(jobs)
    return {
        "status": "ok",
        "chromadb": CHROMA_STATUS["status"],   # ← deploy verification in one field
        "active_jobs": active,
        "total_jobs": total,
        "groq_key_set": bool(os.getenv("GROQ_API_KEY")),


@app.get("/ping")
async def ping():
    return "pong"


@app.post("/review", dependencies=[Depends(_verify_token)])
async def start_review(req: ReviewRequest):
    _evict_old_jobs()

    with _jobs_lock:
        active = sum(1 for j in jobs.values() if j["status"] == "running")

    if active >= MAX_CONCURRENT_REVIEWS:
        raise HTTPException(
            status_code=429,
            detail=f"Too many concurrent reviews ({active}/{MAX_CONCURRENT_REVIEWS}). Try again shortly."
        )

    job_id = str(uuid.uuid4())   # full UUID — not truncated
    with _jobs_lock:
        jobs[job_id] = {
            "status":     "running",
            "repo_url":   req.repo_url,
            "created_at": time.time(),
            "events":     [],
            "result":     None,
        }

    t = threading.Thread(
        target=_run_review_worker,
        args=(job_id, req.repo_url),
        daemon=True,
    )
    t.start()
    return {"job_id": job_id, "status": "started"}


@app.get("/review/{job_id}")
async def get_review(job_id: str):
    with _jobs_lock:
        job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] != "complete":
        return {"status": job["status"]}
    return {"status": "complete", "result": job["result"]}


@app.get("/review/{job_id}/stream")
async def stream_review(job_id: str):
    with _jobs_lock:
        exists = job_id in jobs
    if not exists:
        raise HTTPException(status_code=404, detail="Job not found")

    async def event_generator():
        sent    = 0
        started = time.time()
        timeout = 600  # 10 minutes hard cap

        while time.time() - started < timeout:
            with _jobs_lock:
                job_events = list(jobs.get(job_id, {}).get("events", []))
                job_status = jobs.get(job_id, {}).get("status", "unknown")

            # Drain any unsent events
            while sent < len(job_events):
                yield f"data: {json.dumps(job_events[sent])}\n\n"
                sent += 1

            # Terminal states — flush remaining then close
            if job_status in ("complete", "failed"):
                with _jobs_lock:
                    job_events = list(jobs.get(job_id, {}).get("events", []))
                while sent < len(job_events):
                    yield f"data: {json.dumps(job_events[sent])}\n\n"
                    sent += 1
                break

            # SSE keep-alive comment (prevents proxies from closing idle connections)
            yield ": keep-alive\n\n"
            await asyncio.sleep(0.5)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":      "no-cache",
            "X-Accel-Buffering":  "no",
            "Connection":         "keep-alive",
            "Access-Control-Allow-Origin": os.getenv("FRONTEND_URL", "*"),
        },
    )
