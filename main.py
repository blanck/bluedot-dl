#!/usr/bin/env python3
"""BlueDot HQ collection video downloader."""

import json
import platform
import re
import subprocess
import sys
import webbrowser
from pathlib import Path

import httpx
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, BarColumn, DownloadColumn, TransferSpeedColumn
from rich.prompt import Prompt
from rich.table import Table
from rich.panel import Panel

console = Console()

BASE_URL = "https://app.bluedothq.com"
CONFIG_DIR = Path.home() / ".config" / "bluedot-dl"
SESSION_FILE = CONFIG_DIR / "session"
PAGE_SIZE = 16


# ── Auth ──────────────────────────────────────────────────────────────────────

def load_session() -> str | None:
    if SESSION_FILE.exists():
        cookie = SESSION_FILE.read_text().strip()
        if cookie:
            return cookie
    return None


def save_session(cookie: str) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(cookie)
    SESSION_FILE.chmod(0o600)


def test_session(cookie: str) -> bool:
    try:
        with get_client(cookie) as client:
            resp = client.get("/api/v1/workspaces")
            return resp.status_code == 200
    except Exception:
        return False


def read_clipboard() -> str | None:
    """Read text from the system clipboard."""
    try:
        if platform.system() == "Darwin":
            return subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=5).stdout.strip()
        elif platform.system() == "Linux":
            return subprocess.run(["xclip", "-selection", "clipboard", "-o"], capture_output=True, text=True, timeout=5).stdout.strip()
        elif platform.system() == "Windows":
            return subprocess.run(["powershell", "-command", "Get-Clipboard"], capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        pass
    return None


def login() -> str:
    """Guide user through authentication and return a valid session cookie."""
    existing = load_session()
    if existing:
        with console.status("Checking saved session..."):
            if test_session(existing):
                return existing
        console.print("[yellow]Saved session expired.[/yellow]\n")

    console.print(Panel.fit(
        "[bold]Login to BlueDot HQ[/bold]\n\n"
        "1. Opening BlueDot in your browser...\n"
        "2. Sign in if needed\n"
        "3. Open DevTools: [bold cyan]Cmd+Option+I[/bold cyan] (Mac) or [bold cyan]F12[/bold cyan] (Windows)\n"
        "4. Go to [bold cyan]Application[/bold cyan] tab → [bold cyan]Cookies[/bold cyan] → [bold cyan]https://app.bluedothq.com[/bold cyan]\n"
        "5. Find [bold green]bluedotSession[/bold green], double-click the value, [bold]copy it[/bold]\n"
        "6. Come back here and press [bold cyan]Enter[/bold cyan]",
        title="🔐 Authentication",
        border_style="blue",
    ))

    webbrowser.open(BASE_URL)

    while True:
        try:
            input("\nCopy the cookie, then press Enter here... ")
        except EOFError:
            sys.exit(1)

        cookie = read_clipboard()
        if not cookie:
            console.print("[red]Could not read clipboard. Make sure you copied the cookie value.[/red]")
            continue

        # Basic sanity check - bluedotSession cookies start with "Fe26."
        if not cookie.startswith("Fe26."):
            console.print("[red]That doesn't look like a bluedotSession cookie (should start with Fe26.)[/red]")
            continue

        console.print("Verifying...", style="dim")
        if test_session(cookie):
            save_session(cookie)
            console.print("[green]Logged in![/green]\n")
            return cookie
        console.print("[red]Cookie is invalid or expired. Try copying it again.[/red]")


# ── API ───────────────────────────────────────────────────────────────────────

def get_client(cookie: str) -> httpx.Client:
    return httpx.Client(
        base_url=BASE_URL,
        cookies={"bluedotSession": cookie},
        headers={"Accept": "application/json"},
        timeout=30,
        follow_redirects=True,
    )


def fetch_workspaces(client: httpx.Client) -> list[dict]:
    resp = client.get("/api/v1/workspaces")
    resp.raise_for_status()
    return resp.json().get("participates", [])


def fetch_collections(client: httpx.Client, workspace_id: str) -> list[dict]:
    resp = client.get(f"/api/v1/workspaces/{workspace_id}/collections")
    resp.raise_for_status()
    return resp.json().get("collections", [])


def fetch_videos(
    client: httpx.Client,
    workspace_id: str,
    collection_id: str | None = None,
    tenancy: str = "workspace",
) -> list[dict]:
    all_videos = []
    page = 1
    while True:
        params: dict = {
            "pageNumber": page,
            "pageSize": PAGE_SIZE,
            "tenancy": tenancy,
            "sortBy": "uploadedAt",
            "order": "desc",
        }
        if collection_id:
            params["collectionId"] = collection_id
        resp = client.get(
            f"/api/v1/workspaces/{workspace_id}/videos",
            params=params,
        )
        resp.raise_for_status()
        data = resp.json()
        all_videos.extend(data.get("items", []))
        if page >= data.get("pagination", {}).get("total", 1):
            break
        page += 1
    return all_videos


def fetch_video_detail(client: httpx.Client, video_id: str) -> dict:
    resp = client.get(f"/api/v1/videos/{video_id}")
    resp.raise_for_status()
    return resp.json()


# ── UI ────────────────────────────────────────────────────────────────────────

def pick_workspace(workspaces: list[dict]) -> dict:
    if len(workspaces) == 1:
        return workspaces[0]
    table = Table(title="Workspaces")
    table.add_column("#", style="dim")
    table.add_column("Name")
    for i, ws in enumerate(workspaces, 1):
        table.add_row(str(i), ws["name"])
    console.print(table)
    choice = Prompt.ask("Select workspace", choices=[str(i) for i in range(1, len(workspaces) + 1)])
    return workspaces[int(choice) - 1]


def format_duration(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m"
    return f"{m}m {s}s"


def pick_videos(videos: list[dict]) -> list[dict]:
    table = Table(title=f"Videos ({len(videos)} total)")
    table.add_column("#", style="dim")
    table.add_column("Title")
    table.add_column("Duration", justify="right")
    table.add_column("Date", justify="right")
    for i, v in enumerate(videos, 1):
        date = v.get("createdAt", "")[:10]
        table.add_row(str(i), v["title"], format_duration(v.get("duration", 0)), date)
    console.print(table)

    console.print("\nEnter video numbers to download (e.g. [bold]1,3,5[/bold] or [bold]1-5[/bold])")
    choice = Prompt.ask("Selection", default="all")

    if choice.strip().lower() == "all":
        return videos

    selected = []
    for part in choice.split(","):
        part = part.strip()
        if "-" in part:
            start, end = part.split("-", 1)
            for n in range(int(start), int(end) + 1):
                if 1 <= n <= len(videos):
                    selected.append(videos[n - 1])
        elif part.isdigit():
            n = int(part)
            if 1 <= n <= len(videos):
                selected.append(videos[n - 1])
    return selected


# ── Export helpers ────────────────────────────────────────────────────────────

def sanitize_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', "", name)
    return name.strip(". ") or "untitled"


def format_ts_short(seconds: float) -> str:
    """Format seconds as M:SS or H:MM:SS."""
    total = int(seconds)
    m, s = divmod(total, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def format_length(seconds: float) -> str:
    total = int(seconds)
    m, s = divmod(total, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    return f"{m}m{s:02d}s"


def build_transcript_text(detail: dict) -> str | None:
    """Convert word-level transcription into BlueDot's text export format."""
    transcription = detail.get("videoTranscription", {})
    entries = transcription.get("transcription", [])
    if not entries:
        return None

    # Header
    title = detail.get("title", "")
    duration = detail.get("duration", 0)
    created = detail.get("createdAt", "")

    header_lines = [title]

    # Date formatting: "11:00 AM on Feb 20, 2026"
    if created:
        from datetime import datetime, timezone
        try:
            dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            header_lines.append(f"Date: {dt.strftime('%-I:%M %p on %b %-d, %Y')}")
        except Exception:
            pass

    header_lines.append(f"Length: {format_length(duration)}")
    header = "\n".join(header_lines)

    # Group by speaker+paragraph: new block when either changes
    blocks = []
    current_speaker = None
    current_paragraph = None
    current_words = []
    current_start = 0.0

    for entry in entries:
        text = entry.get("text", "")
        if not text:
            continue
        speaker = entry.get("speakerTag", "Unknown")
        paragraph = entry.get("paragraph", 0)

        if speaker != current_speaker or paragraph != current_paragraph:
            if current_words and current_speaker is not None:
                ts = format_ts_short(current_start)
                blocks.append(f"{current_speaker}  {ts}\n{' '.join(current_words)}")
            current_speaker = speaker
            current_paragraph = paragraph
            current_words = [text]
            current_start = entry["start"]
        else:
            current_words.append(text)

    if current_words and current_speaker is not None:
        ts = format_ts_short(current_start)
        blocks.append(f"{current_speaker}  {ts}\n{' '.join(current_words)}")

    return header + "\n\n" + "\n\n".join(blocks) + " "


def build_summary_text(summary: dict) -> str | None:
    """Convert summary JSON into readable markdown."""
    summary_data = summary.get("summary", {})
    entries = summary_data.get("entries", [])
    if not entries:
        return None

    sections = []
    for entry in entries:
        name = entry.get("name", "")
        sections.append(f"# {name}\n")
        for block in entry.get("blocks", []):
            sections.append(_render_block(block, depth=0))
    return "\n".join(sections)


def _render_block(block: dict, depth: int) -> str:
    """Recursively render a summary block."""
    lines = []
    block_type = block.get("type", "simple")
    indent = "  " * depth

    if block_type == "with-header":
        header = block.get("header", "")
        start_time = block.get("startTime")
        ts = f" [{format_ts_short(start_time)}]" if start_time is not None else ""
        lines.append(f"{indent}## {header}{ts}\n")
        for sub in block.get("blocks", []):
            lines.append(_render_block(sub, depth + 1))
    else:
        value = block.get("value", "")
        if value:
            lines.append(f"{indent}{value}\n")

    return "\n".join(lines)


def download_file(url: str, dest: Path) -> None:
    """Download a file with a progress bar."""
    with httpx.stream("GET", url, timeout=300, follow_redirects=True) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("content-length", 0))
        with Progress(
            SpinnerColumn(),
            "[progress.description]{task.description}",
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
        ) as progress:
            task = progress.add_task(dest.name, total=total or None)
            with open(dest, "wb") as f:
                for chunk in resp.iter_bytes(chunk_size=64 * 1024):
                    f.write(chunk)
                    progress.advance(task, len(chunk))


def save_video_data(client: httpx.Client, video_id: str, title: str, video_dir: Path) -> None:
    """Fetch full video detail and save video, transcript, summary, and metadata."""
    video_dir.mkdir(parents=True, exist_ok=True)
    safe_title = sanitize_filename(title)

    detail = fetch_video_detail(client, video_id)

    # Video file
    video_url = detail.get("originalVideoUrl")
    if video_url:
        ext = "." + video_url.split("?")[0].rsplit(".", 1)[-1] if "." in video_url.split("?")[0].split("/")[-1] else ".webm"
        video_path = video_dir / (safe_title + ext)
        if video_path.exists():
            console.print("  [yellow]Video: skipped (exists)[/yellow]")
        else:
            download_file(video_url, video_path)

    # Transcript
    transcription = detail.get("videoTranscription", {})
    if transcription.get("status") == "ready":
        transcript_text = build_transcript_text(detail)
        if transcript_text:
            transcript_path = video_dir / (safe_title + ".txt")
            transcript_path.write_text(transcript_text, encoding="utf-8")
            console.print(f"  [green]Transcript saved[/green]")

    # Summary
    summary = detail.get("summary", {})
    if summary.get("status") == "ready":
        summary_text = build_summary_text(summary)
        if summary_text:
            summary_path = video_dir / (safe_title + " - Summary.md")
            summary_path.write_text(summary_text, encoding="utf-8")
            console.print(f"  [green]Summary saved[/green]")

    # Full metadata JSON
    metadata_path = video_dir / (safe_title + ".json")
    metadata_path.write_text(json.dumps(detail, indent=2, ensure_ascii=False), encoding="utf-8")
    console.print(f"  [green]Metadata saved[/green]")


def download_all(client: httpx.Client, videos: list[dict], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    console.print(f"\nDownloading [bold]{len(videos)}[/bold] videos to [cyan]{output_dir}[/cyan]\n")

    for i, video in enumerate(videos, 1):
        title = video.get("title", video["id"])
        duration_min = video.get("duration", 0) / 60
        console.print(f"[dim][{i}/{len(videos)}][/dim] [bold]{title}[/bold] ({duration_min:.0f} min)")
        video_dir = output_dir / sanitize_filename(title)
        save_video_data(client, video["id"], title, video_dir)
        console.print()

    console.print(f"[bold green]Done![/bold green] Saved to [cyan]{output_dir}[/cyan]")


# ── Main ──────────────────────────────────────────────────────────────────────

def pick_source(collections: list[dict]) -> dict | None:
    """Let user pick a collection or the meetings library. Returns None for library."""
    table = Table(title="Download from")
    table.add_column("#", style="dim")
    table.add_column("Name")
    table.add_row("1", "My Meetings (private library)")
    for i, col in enumerate(collections, 2):
        table.add_row(str(i), col["name"])
    console.print(table)
    choices = [str(i) for i in range(1, len(collections) + 2)]
    choice = Prompt.ask("Selection", choices=choices)
    idx = int(choice)
    if idx == 1:
        return None
    return collections[idx - 2]


def main():
    console.print(Panel.fit("[bold]BlueDot Video Downloader[/bold]", border_style="blue"))

    cookie = login()

    with get_client(cookie) as client:
        with console.status("Loading workspaces..."):
            workspaces = fetch_workspaces(client)
        if not workspaces:
            console.print("[red]No workspaces found.[/red]")
            return
        workspace = pick_workspace(workspaces)

        with console.status("Loading collections..."):
            collections = fetch_collections(client, workspace["id"])

        source = pick_source(collections)

        if source is None:
            # Private meetings library
            with console.status("Loading meetings..."):
                videos = fetch_videos(client, workspace["id"], tenancy="user")
            output_name = "My Meetings"
        else:
            with console.status("Loading videos..."):
                videos = fetch_videos(client, workspace["id"], collection_id=source["id"])
            output_name = source["name"]

        if not videos:
            console.print("[red]No videos found.[/red]")
            return

        selected = pick_videos(videos)
        if not selected:
            console.print("[yellow]No videos selected.[/yellow]")
            return

        output_dir = Path("downloads") / sanitize_filename(output_name)
        download_all(client, selected, output_dir)


if __name__ == "__main__":
    main()
