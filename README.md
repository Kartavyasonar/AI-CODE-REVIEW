# AI Code Review Agent

Multi-agent AI system that reviews GitHub repositories for bugs, security vulnerabilities, code quality issues, and performance bottlenecks — powered by LangGraph, ChromaDB, and Groq LLaMA 3.3 70B.

![Python](https://img.shields.io/badge/Python-3.11-blue) ![LangGraph](https://img.shields.io/badge/LangGraph-0.5.4-purple) ![ChromaDB](https://img.shields.io/badge/ChromaDB-latest-green) ![FastAPI](https://img.shields.io/badge/FastAPI-latest-teal) ![Groq](https://img.shields.io/badge/Groq-LLaMA3.3_70B-orange)

## Live Demo

🌐 **Frontend:** [ai-code-review-xi-one.vercel.app](https://ai-code-review-xi-one.vercel.app)  
⚙️ **Backend:** Self-hosted on Oracle Cloud (Always Free) via Caddy HTTPS reverse proxy  
📡 **API:** `https://ai-code-review.duckdns.org`

---

## Demo Output

```
$ python cli.py https://github.com/username/my-project

🔍 Cloning repo...       ████████████ done  (127 files found)
🧩 Chunking + embedding  ████████████ done  (843 code chunks, ChromaDB)
🤖 Bug agent             ████████████ done  (12 findings)
🔒 Security agent        ████████████ done  (7 findings — 2 CRITICAL)
🧹 Quality agent         ████████████ done  (19 findings)
⚡ Performance agent     ████████████ done  (8 findings)
📝 Synthesizing report   ████████████ done

Overall Score: 61/100  ████████░░

## Executive Summary
The codebase has solid architecture but critical SQL injection vulnerabilities
in api/routes.py that must be fixed immediately. Authentication is well-structured
but two hardcoded API keys were found. Start by addressing the injection flaws
and rotating the exposed credentials.
```

---

## Architecture

```
GitHub URL
    │
    ▼
┌─────────────────────────────────────┐
│         Ingestion Layer             │
│  GitPython clone · File walker      │
│  AST chunker · ChromaDB embeddings  │
└───────────────┬─────────────────────┘
                │
    ┌───────────▼───────────┐
    │  LangGraph Orchestrator│
    │  (fan-out / fan-in)    │
    └──┬────┬────┬────┬──────┘
       │    │    │    │
  ┌────▼─┐┌─▼──┐┌▼───┐┌▼────┐
  │ Bug  ││Sec ││Qual││Perf │
  │Agent ││Agt ││Agt ││Agt  │
  └──────┘└────┘└────┘└─────┘
       │    │    │    │
    ┌──▼────▼────▼────▼──┐
    │    Synthesizer      │
    │  Dedup · Score ·    │
    │  Markdown Report    │
    └────────────────────┘
```

---

## Tech Stack

| Layer | Technology | Purpose |
|-------|-----------|---------|
| LLM | Groq LLaMA 3.3 70B | All agent reasoning |
| Orchestration | LangGraph 0.5.4 | Multi-agent state machine |
| RAG | ChromaDB + sentence-transformers | Code semantic search |
| Code parsing | Python AST + radon | Chunk at function boundaries, complexity |
| Static analysis | Custom regex + bandit patterns | Secret detection |
| Backend | FastAPI + Uvicorn | REST API + SSE streaming |
| Frontend | React + Vite | Dashboard UI |
| Frontend deploy | Vercel | Auto-deploy from GitHub main |
| Backend deploy | Oracle Cloud VM (Always Free) | Self-hosted, always-on |
| HTTPS | Caddy + DuckDNS | Automatic TLS certificates |

---

## Quick Start

### 1. Clone and setup

```bash
git clone https://github.com/Kartavyasonar/AI-CODE-REVIEW
cd AI-CODE-REVIEW
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

### 2. Add your Groq API key (free at [console.groq.com](https://console.groq.com))

```bash
# Edit .env
GROQ_API_KEY=your_key_here
```

### 3a. CLI — review any repo in your terminal

```bash
python cli.py https://github.com/username/repo
```

### 3b. API + React dashboard

```bash
# Terminal 1 — backend
uvicorn backend.api.main:app --reload

# Terminal 2 — frontend
cd frontend && npm install && npm run dev
```

### 3c. Docker (one command)

```bash
docker-compose up --build
# Frontend: http://localhost:3000
# API docs: http://localhost:8000/docs
```

---

## Self-Hosted Deployment (Oracle Cloud)

The production backend runs on an **Oracle Cloud Always Free** VM with the following setup:

- **VM:** VM.Standard.E2.1.Micro (1 OCPU, 1GB RAM + 2GB swap)
- **OS:** Ubuntu 22.04
- **Process manager:** systemd (`codereview.service`)
- **HTTPS:** Caddy reverse proxy with automatic Let's Encrypt certs via DuckDNS
- **Domain:** `ai-code-review.duckdns.org`

```bash
# Service management
sudo systemctl status codereview   # check status
sudo systemctl restart codereview  # restart backend
sudo systemctl status caddy        # check HTTPS proxy
```

### Environment variables (Vercel)

| Variable | Value |
|----------|-------|
| `VITE_API_URL` | `https://ai-code-review.duckdns.org` |

---

## Agent Breakdown

### Bug Agent
- Semantic search for null reference, exception handling, logic error patterns
- RAG retrieves the 12 most relevant chunks per query (4 targeted queries)
- Deduplicates by code snippet to avoid repeat findings

### Security Agent
- Static scan first: Regex patterns for hardcoded secrets, SSL disable, eval, pickle, MD5
- LLM analysis: OWASP Top 10 patterns via 5 semantic queries
- SQL injection, SSRF, broken auth, insecure deserialization

### Quality Agent
- Radon integration: Cyclomatic complexity (CC ≥ 10 flagged), Maintainability Index (MI < 20 flagged)
- LLM analysis: Docstring gaps, magic numbers, deep nesting, naming issues
- All Python files scanned statically regardless of LLM budget

### Performance Agent
- N+1 database query detection
- Blocking I/O inside async functions
- Algorithmic complexity issues (O(n²) loops, sort-in-loop)
- Memory inefficiencies (unbounded lists, unnecessary list() conversion)

### Synthesizer
- Deduplicates across all agents (same file + title = single finding)
- Scores codebase: `100 - Σ(severity_weight × count)` where critical=20, high=10, medium=4, low=1
- Generates executive summary via LLM
- Outputs structured Markdown report

---

## API Reference

```
POST /review
  Body: { "repo_url": "https://github.com/..." }
  Returns: { "job_id": "uuid", "status": "queued" }

GET /review/{job_id}
  Returns: full job result including findings and report_markdown

GET /review/{job_id}/stream
  Returns: SSE stream of status updates → final result

GET /health
  Returns: { "status": "ok" }
```

---

## Project Structure

```
AI-CODE-REVIEW/
├── backend/
│   ├── agents/
│   │   ├── bug_agent.py       # Logic, null refs, exceptions
│   │   ├── security_agent.py  # OWASP, secrets, injection
│   │   ├── quality_agent.py   # Complexity, style, docs
│   │   ├── perf_agent.py      # N+1, async, Big-O
│   │   └── synthesizer.py     # Merge, score, report
│   ├── core/
│   │   ├── ingestion.py       # Clone, walk, chunk, embed
│   │   ├── pipeline.py        # LangGraph graph
│   │   ├── state.py           # Shared state schema
│   │   └── llm.py             # Groq client
│   └── api/
│       └── main.py            # FastAPI endpoints
├── frontend/
│   └── src/App.jsx            # React dashboard
├── cli.py                     # Terminal runner
├── docker-compose.yml
├── Dockerfile.backend
└── requirements.txt
```

---

## Roadmap

- [ ] GitHub PR integration (post review as PR comments)
- [ ] Support for private repos (GitHub token auth)
- [ ] Persistent job history with SQLite
- [ ] Custom rule configuration per project
- [ ] CI/CD integration (GitHub Actions step)
- [ ] Diff-only review mode (only changed files)

---

## Built With

- [LangGraph](https://github.com/langchain-ai/langgraph) — agent orchestration
- [Groq](https://console.groq.com) — free LLM inference
- [ChromaDB](https://www.trychroma.com) — vector database
- [sentence-transformers](https://www.sbert.net) — code embeddings
- [radon](https://radon.readthedocs.io) — Python complexity metrics
- [FastAPI](https://fastapi.tiangolo.com) — backend
- [React](https://react.dev) — frontend
- [Caddy](https://caddyserver.com) — HTTPS reverse proxy
- [Oracle Cloud](https://www.oracle.com/cloud/free/) — always-free VM hosting

---

<<<<<<< HEAD
Built by **Kartavya Sonar** — MSc Computer Science, University of Leeds
=======
Built by **Kartavya Sonar** — MSc Computer Science, University of Leeds
>>>>>>> 0e69695 (fix: chromadb EphemeralClient permanent fix)
