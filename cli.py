#!/usr/bin/env python3
"""
cli.py — Run a code review directly from the terminal.
Usage: python cli.py https://github.com/username/repo

Uses synchronous pipeline to avoid Windows asyncio/ProactorEventLoop conflicts.
"""
import sys
import os
from pathlib import Path

# Must be set BEFORE any chromadb import to suppress telemetry
os.environ["ANONYMIZED_TELEMETRY"] = "false"
os.environ["CHROMA_TELEMETRY"] = "false"

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
import threading

console = Console()


def main():
    if len(sys.argv) < 2:
        console.print("[bold red]Usage:[/bold red] python cli.py https://github.com/username/repo")
        console.print("\n[dim]Example repos to try:[/dim]")
        console.print("  python cli.py https://github.com/pallets/flask")
        console.print("  python cli.py https://github.com/psf/requests")
        console.print("  python cli.py https://github.com/encode/httpx")
        sys.exit(1)

    repo_url = sys.argv[1].strip()

    console.print(Panel.fit(
        f"[bold cyan]AI Code Review Agent v2[/bold cyan]\n"
        f"[dim]LangGraph · HyDE · Cross-Encoder Reranker · Self-Reflection · Groq LLaMA 3.3 70B[/dim]\n\n"
        f"Reviewing: [yellow]{repo_url}[/yellow]",
        border_style="cyan",
    ))

    # Import here so env vars are set before chromadb loads
    from backend.core.pipeline import run_review_sync

    result = None
    error_holder = []

    def run_in_thread():
        try:
            nonlocal result
            result = run_review_sync(repo_url)
        except Exception as e:
            error_holder.append(e)

    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), console=console, transient=True) as p:
        task = p.add_task("Ingesting repo + embedding chunks...", total=None)
        t = threading.Thread(target=run_in_thread, daemon=True)
        t.start()
        t.join()  # Wait for completion
        p.remove_task(task)

    if error_holder:
        console.print(f"[bold red]Error:[/bold red] {error_holder[0]}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    if result is None:
        console.print("[red]Review returned no result.[/red]")
        sys.exit(1)

    # Handle both dict and object results
    if isinstance(result, dict):
        error  = result.get("error")
        report = result.get("report_markdown", "")
        score  = result.get("score", 0)
        bugs   = len(result.get("bug_findings") or [])
        secs   = len(result.get("security_findings") or [])
        quals  = len(result.get("quality_findings") or [])
        perfs  = len(result.get("perf_findings") or [])
    else:
        error  = getattr(result, "error", None)
        report = getattr(result, "report_markdown", "")
        score  = getattr(result, "score", 0)
        bugs   = len(getattr(result, "bug_findings", []))
        secs   = len(getattr(result, "security_findings", []))
        quals  = len(getattr(result, "quality_findings", []))
        perfs  = len(getattr(result, "perf_findings", []))

    if error:
        console.print(f"[bold red]Review failed:[/bold red] {error}")
        sys.exit(1)

    if not report:
        console.print("[yellow]No report generated.[/yellow] The repo may have no supported source files.")
        console.print("Supported extensions: .py .js .ts .jsx .tsx .java .go .rs .cpp .c .cs .rb")
        sys.exit(0)

    # Print the report
    console.print(Markdown(report))

    # Summary box
    console.print()
    console.print(Panel(
        f"[bold green]Score: {score}/100[/bold green]\n"
        f"🐛 Bugs: {bugs}  |  🔒 Security: {secs}  |  🧹 Quality: {quals}  |  ⚡ Perf: {perfs}",
        title="Review Complete",
        border_style="green",
    ))

    # Save report
    out = Path("code_review_report.md")
    out.write_text(report, encoding="utf-8")
    console.print(f"[dim]Report saved → {out.absolute()}[/dim]")


if __name__ == "__main__":
    main()
