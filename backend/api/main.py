"""
api/main.py — FastAPI backend with granular SSE progress events + Nuclear CORS
"""
import asyncio
import json
import os
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
    """Injects CORS headers on EVERY response — OPTIONS, POST, GET, SSE."""
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


@asynccontextmanager
async def lifespan(app: FastAPI):
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


def emit(event: str, data: dict) -> str:
    return f"data: {json.dumps({'event': event, **data})}\n\n"


def run_review_sync(job_id: str, repo_url: str):
    """Run the full pipeline synchronously in a background thread."""
    try:
        jobs[job_id]["events"].append({"event": "status", "message": "Cloning repository...", "progress": 5})

        # Import here to avoid startup delays
        from backend.core.ingestion import ingest_repo
        from backend.core.pipeline import run_pipeline

        jobs[job_id]["events"].append({"event": "status", "message": "Ingesting & embedding code...", "progress": 15})

        collection = ingest_repo(repo_url, job_id=job_id, events=jobs[job_id]["events"])

        if collection is None:
            jobs[job_id]["events"].append({"event": "error", "message": "No Python/JS files found in repo."})
            jobs[job_id]["status"] = "failed"
            return

        jobs[job_id]["events"].append({"event": "status", "message": "Running AI agents in parallel...", "progress": 40})

        result = run_pipeline(collection, repo_url, job_id=job_id, events=jobs[job_id]["events"])

        jobs[job_id]["result"] = result
        jobs[job_id]["status"] = "complete"
        jobs[job_id]["events"].append({
            "event": "complete",
            "score": result.get("score", 0),
            "total_findings": result.get("total_findings", 0),
            "progress": 100
        })

    except Exception as e:
        jobs[job_id]["status"] = "failed"
        jobs[job_id]["events"].append({"event": "error", "message": str(e)})


@app.get("/health")
async def health():
    return {"status": "ok", "active_jobs": len([j for j in jobs.values() if j["status"] == "running"])}


@app.post("/review")
async def start_review(request: ReviewRequest):
    """Start a code review job. Returns job_id immediately."""
    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {
        "status": "running",
        "repo_url": request.repo_url,
        "created_at": time.time(),
        "events": [],
        "result": None,
    }

    # Run in background thread (not blocking the event loop)
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, run_review_sync, job_id, request.repo_url)

    return {"job_id": job_id, "status": "started"}


@app.get("/review/{job_id}")
async def get_review(job_id: str):
    """Get the final result of a completed review."""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    job = jobs[job_id]
    if job["status"] != "complete":
        return {"status": job["status"]}
    return {"status": "complete", "result": job["result"]}


@app.get("/review/{job_id}/stream")
async def stream_review(job_id: str):
    """SSE stream — sends events as the pipeline progresses."""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    async def event_generator():
        sent = 0
        while True:
            job = jobs.get(job_id, {})
            events = job.get("events", [])

            # Send any new events
            while sent < len(events):
                ev = events[sent]
                yield emit(ev.get("event", "status"), ev)
                sent += 1

            # If job is done, close stream
            if job.get("status") in ("complete", "failed"):
                break

            await asyncio.sleep(0.5)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Access-Control-Allow-Origin": "*",
        },
    )