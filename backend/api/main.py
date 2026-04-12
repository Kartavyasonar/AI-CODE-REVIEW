"""
api/main.py — FastAPI backend with granular SSE progress events
"""
import asyncio
import json
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

# Job store: { job_id: { status, steps, findings, ... } }
jobs: dict = {}

@asynccontextmanager
async def lifespan(app: FastAPI):
    yield

app = FastAPI(title="AI Code Review Agent v2", version="2.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class ReviewRequest(BaseModel):
    repo_url: str


def _push_step(job_id: str, step: str, detail: str = "", progress: int = 0):
    """Update job with a new step event — SSE picks this up."""
    if job_id not in jobs:
        return
    jobs[job_id]["current_step"] = step
    jobs[job_id]["current_detail"] = detail
    jobs[job_id]["progress"] = progress
    jobs[job_id]["steps"].append({
        "step": step, "detail": detail, "progress": progress, "ts": time.time()
    })


async def _run_job(job_id: str, repo_url: str):
    jobs[job_id]["status"] = "running"
    _push_step(job_id, "cloning", f"Cloning {repo_url}...", 5)

    try:
        import os, logging
        os.environ["ANONYMIZED_TELEMETRY"] = "false"
        os.environ["CHROMA_TELEMETRY"] = "false"
        logging.getLogger("chromadb").setLevel(logging.CRITICAL)

        from backend.core.ingestion import clone_repo, walk_files, chunk_file, build_vector_store
        import uuid as _uuid

        # Step 1: Clone
        repo_path = clone_repo(repo_url)
        _push_step(job_id, "walking", "Walking file tree...", 10)

        # Step 2: Walk
        files = walk_files(repo_path)
        jobs[job_id]["file_count"] = len(files)
        _push_step(job_id, "chunking", f"Found {len(files)} source files. Chunking code...", 18)

        # Step 3: Chunk
        all_chunks = []
        for f in files:
            all_chunks.extend(chunk_file(f))
        jobs[job_id]["chunk_count"] = len(all_chunks)
        _push_step(job_id, "embedding", f"Embedding {len(all_chunks)} code chunks into ChromaDB...", 25)

        # Step 4: Embed
        collection_name = "cr_" + str(_uuid.uuid4()).replace("-", "")[:12]
        collection = build_vector_store(all_chunks, collection_name)
        _push_step(job_id, "hyde", "Running HyDE query expansion across 4 agents...", 38)

        # Store collection in module-level store
        from backend.core import pipeline as _pl
        _pl._active_collections[collection_name] = collection

        # Step 5: Agents
        _push_step(job_id, "agents", "4 agents running in parallel: Bug · Security · Quality · Performance", 45)

        # Build a minimal state namespace
        class NS: pass
        ns = NS()
        ns.repo_url = repo_url
        ns.repo_path = repo_path
        ns.collection_name = collection_name
        ns.all_chunks = all_chunks
        ns.bug_findings = []
        ns.security_findings = []
        ns.quality_findings = []
        ns.perf_findings = []

        from backend.core.memory import get_high_risk_query_hints
        extra = get_high_risk_query_hints(repo_url)

        # Run agents with individual status updates
        from backend.agents.bug_agent import run_bug_agent
        from backend.agents.security_agent import run_security_agent
        from backend.agents.quality_agent import run_quality_agent
        from backend.agents.perf_agent import run_perf_agent
        from backend.core.reflection import detect_vulnerability_chains
        from backend.agents.synthesizer import run_synthesizer
        from backend.core.memory import get_similar_repo_insights, store_review_patterns
        import threading

        results = {}
        errors = {}

        def run_agent(name, fn, attr):
            try:
                _push_step(job_id, f"agent_{name}", f"{name.title()} agent: HyDE → rerank → LLM → reflect", 50)
                r = fn(ns, collection, extra_queries=extra)
                results[attr] = getattr(r, attr, [])
            except Exception as e:
                errors[name] = str(e)
                results[attr] = []

        threads = [
            threading.Thread(target=run_agent, args=("bug",      run_bug_agent,      "bug_findings")),
            threading.Thread(target=run_agent, args=("security", run_security_agent, "security_findings")),
            threading.Thread(target=run_agent, args=("quality",  run_quality_agent,  "quality_findings")),
            threading.Thread(target=run_agent, args=("perf",     run_perf_agent,     "perf_findings")),
        ]
        for t in threads: t.start()

        # Emit progress ticks while agents run
        start = time.time()
        progress_msgs = [
            "Bug agent: retrieving code with HyDE embeddings...",
            "Security agent: scanning for OWASP Top 10...",
            "Quality agent: measuring cyclomatic complexity...",
            "Performance agent: detecting N+1 and async issues...",
            "Self-reflection: auditing findings for confidence...",
            "Cross-encoder reranker rescoring top candidates...",
            "Agents still running — large repos take 3-5 min...",
            "Almost there — synthesizing findings...",
        ]
        msg_idx = 0
        while any(t.is_alive() for t in threads):
            await asyncio.sleep(4)
            elapsed = int(time.time() - start)
            prog = min(85, 50 + elapsed // 5)
            msg = progress_msgs[min(msg_idx, len(progress_msgs)-1)]
            _push_step(job_id, "agents_running", f"[{elapsed}s] {msg}", prog)
            msg_idx += 1

        for t in threads: t.join()

        ns.bug_findings      = results.get("bug_findings", [])
        ns.security_findings = results.get("security_findings", [])
        ns.quality_findings  = results.get("quality_findings", [])
        ns.perf_findings     = results.get("perf_findings", [])

        # Step 6: Chain detection
        _push_step(job_id, "chains", "Multi-hop chain detection across all agents...", 88)
        all_f = ns.bug_findings + ns.security_findings + ns.quality_findings + ns.perf_findings
        chains = detect_vulnerability_chains(all_f)
        if chains:
            ns.security_findings = ns.security_findings + chains

        # Step 7: Synthesize
        _push_step(job_id, "synthesizing", "Generating executive summary and final report...", 93)
        memory_insights = get_similar_repo_insights(repo_url)
        result = run_synthesizer(ns, memory_insights=memory_insights)
        all_final = result.bug_findings + result.security_findings + result.quality_findings + result.perf_findings
        store_review_patterns(repo_url, all_final, result.score)

        def safe_dump(findings):
            out = []
            for f in (findings or []):
                out.append(f.model_dump() if hasattr(f, "model_dump") else (f if isinstance(f, dict) else {}))
            return out

        jobs[job_id].update({
            "status": "done",
            "score": result.score,
            "summary": result.summary,
            "report_markdown": result.report_markdown,
            "findings": {
                "bugs":        len(result.bug_findings),
                "security":    len(result.security_findings),
                "quality":     len(result.quality_findings),
                "performance": len(result.perf_findings),
            },
            "bug_findings":      safe_dump(result.bug_findings),
            "security_findings": safe_dump(result.security_findings),
            "quality_findings":  safe_dump(result.quality_findings),
            "perf_findings":     safe_dump(result.perf_findings),
            "file_count":        len(files),
            "chunk_count":       len(all_chunks),
            "elapsed":           int(time.time() - jobs[job_id]["started_at"]),
        })
        _push_step(job_id, "done", "Review complete!", 100)

    except Exception as e:
        import traceback
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)
        jobs[job_id]["traceback"] = traceback.format_exc()
        _push_step(job_id, "error", str(e), 0)


@app.post("/review")
async def start_review(request: ReviewRequest, background_tasks: BackgroundTasks):
    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "job_id": job_id, "status": "queued",
        "repo_url": request.repo_url,
        "started_at": time.time(),
        "steps": [], "current_step": "queued",
        "current_detail": "Starting...", "progress": 0,
        "file_count": 0, "chunk_count": 0,
    }
    background_tasks.add_task(_run_job, job_id, request.repo_url)
    return {"job_id": job_id, "status": "queued"}


@app.get("/review/{job_id}")
async def get_review(job_id: str):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    return jobs[job_id]


@app.get("/review/{job_id}/stream")
async def stream_review(job_id: str):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    async def gen():
        last_step_count = 0
        while True:
            job = jobs.get(job_id, {})
            status = job.get("status", "unknown")
            steps = job.get("steps", [])

            # Send any new steps
            for step in steps[last_step_count:]:
                yield f"data: {json.dumps({'type': 'step', **step, 'status': status})}\n\n"
            last_step_count = len(steps)

            if status == "done":
                yield f"data: {json.dumps({'type': 'done', 'status': 'done', 'score': job.get('score'), 'summary': job.get('summary'), 'findings': job.get('findings'), 'elapsed': job.get('elapsed'), 'file_count': job.get('file_count'), 'chunk_count': job.get('chunk_count')})}\n\n"
                break
            elif status == "error":
                yield f"data: {json.dumps({'type': 'error', 'status': 'error', 'error': job.get('error')})}\n\n"
                break
            await asyncio.sleep(1)

    return StreamingResponse(gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/health")
async def health():
    return {"status": "ok", "active_jobs": len(jobs)}