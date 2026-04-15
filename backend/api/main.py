"""
api/main.py — FastAPI backend with SSE + Nuclear CORS + keep-alive
"""
import asyncio
import json
import os
import threading
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel

os.environ["ANONYMIZED_TELEMETRY"] = "false"

jobs: dict = {}


class NuclearCORS(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method == "OPTIONS":
            return JSONResponse(
                status_code=200,
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
                    "Access-Control-Allow-Headers": "Content-Type, Authorization, Accept, Cache-Control",
                    "Access-Control-Max-Age": "86400",
                },
                content={}
            )
        response = await call_next(request)
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, Accept"
        return response


async def self_ping():
    """Ping /health every 10 minutes to prevent Render free tier sleep."""
    import httpx
    await asyncio.sleep(30)  # wait for startup
    while True:
        try:
            async with httpx.AsyncClient() as client:
                await client.get("http://localhost:10000/health", timeout=5)
        except Exception:
            pass
        await asyncio.sleep(540)  # 9 minutes


@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(self_ping())
    yield


app = FastAPI(title="AI Code Review Agent v2", version="2.0.0", lifespan=lifespan)
app.add_middleware(NuclearCORS)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ReviewRequest(BaseModel):
    repo_url: str


def push(job_id: str, event: str, **kwargs):
    jobs[job_id]["events"].append({"event": event, **kwargs})


def run_review_sync(job_id: str, repo_url: str):
    try:
        push(job_id, "status", message="Cloning repository...", progress=5)

        from backend.core.ingestion import ingest_repo
        from backend.core.pipeline import run_pipeline

        push(job_id, "status", message="Ingesting & embedding code into ChromaDB...", progress=10)

        collection = ingest_repo(repo_url, job_id=job_id, events=jobs[job_id]["events"])

        if collection is None:
            push(job_id, "error", message="No Python/JS files found in this repository.")
            jobs[job_id]["status"] = "failed"
            return

        push(job_id, "status", message="Starting LangGraph multi-agent pipeline...", progress=40)
        push(job_id, "agent_start", agent="bug", message="Bug agent starting...")
        push(job_id, "agent_start", agent="security", message="Security agent starting...")
        push(job_id, "agent_start", agent="quality", message="Quality agent starting...")
        push(job_id, "agent_start", agent="perf", message="Performance agent starting...")

        result = run_pipeline(collection, repo_url, job_id=job_id, events=jobs[job_id]["events"])

        jobs[job_id]["result"] = result
        jobs[job_id]["status"] = "complete"
        push(job_id, "complete",
             score=result.get("score", 0),
             total_findings=result.get("total_findings", 0),
             progress=100)

    except Exception as e:
        import traceback
        push(job_id, "error", message=f"Pipeline error: {str(e)}", detail=traceback.format_exc())
        jobs[job_id]["status"] = "failed"


@app.get("/health")
async def health():
    return {"status": "ok", "active_jobs": len([j for j in jobs.values() if j["status"] == "running"])}


@app.get("/ping")
async def ping():
    return "pong"


@app.post("/review")
async def start_review(req: ReviewRequest):
    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {
        "status": "running",
        "repo_url": req.repo_url,
        "created_at": time.time(),
        "events": [],
        "result": None,
    }
    t = threading.Thread(target=run_review_sync, args=(job_id, req.repo_url), daemon=True)
    t.start()
    return {"job_id": job_id, "status": "started"}


@app.get("/review/{job_id}")
async def get_review(job_id: str):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    job = jobs[job_id]
    if job["status"] != "complete":
        return {"status": job["status"]}
    return {"status": "complete", "result": job["result"]}


@app.get("/review/{job_id}/stream")
async def stream_review(job_id: str):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    async def event_generator():
        sent = 0
        started = time.time()
        while time.time() - started < 600:
            events = jobs.get(job_id, {}).get("events", [])
            while sent < len(events):
                yield f"data: {json.dumps(events[sent])}\n\n"
                sent += 1

            if jobs.get(job_id, {}).get("status") in ("complete", "failed"):
                events = jobs.get(job_id, {}).get("events", [])
                while sent < len(events):
                    yield f"data: {json.dumps(events[sent])}\n\n"
                    sent += 1
                break

            # SSE keep-alive comment so Render doesn't close the connection
            yield ": keep-alive\n\n"
            await asyncio.sleep(0.5)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Access-Control-Allow-Origin": "*",
            "Connection": "keep-alive",
        },
    )