"""ytga project file format.

A "project" is just a folder. After analysis it contains:

    <project_dir>/
      project.json     ← metadata (this module)
      manifest.json    ← frame list (written by extract.py)
      report.md        ← human-readable analysis + Q&A
      chat.json        ← structured Q&A history (this module)
      frame_001.jpg
      frame_002.jpg
      ...

The folder is portable — you can zip it, share it, or open it on another
machine and re-load the report. Follow-up Q&A is still available as long
as the underlying Claude Code session id is still resumable on that
machine.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional


PROJECT_SCHEMA = "ytga.project.v1"
PROJECT_FILENAME = "project.json"
CHAT_FILENAME = "chat.json"
REPORT_FILENAME = "report.md"
MANIFEST_FILENAME = "manifest.json"


@dataclass
class ChatMessage:
    role: str  # "user" or "assistant"
    text: str


@dataclass
class Project:
    name: str = ""
    url: str = ""
    video_id: str = ""
    video_title: str = ""
    session_id: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""
    frame_count: int = 0
    schema: str = PROJECT_SCHEMA
    project_dir: str = ""               # absolute path to project folder
    report_text: str = ""               # not stored in project.json itself
    chat_history: list[ChatMessage] = field(default_factory=list)


# Characters illegal in Windows filenames + control chars
_INVALID_FS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_WINDOWS = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize_name(s: str, max_length: int = 80) -> str:
    """Make a string safe to use as a folder name on Windows + macOS + Linux."""
    s = (s or "").strip()
    s = _INVALID_FS_RE.sub("_", s)
    s = re.sub(r"\s+", " ", s).strip(" ._-")
    if len(s) > max_length:
        s = s[:max_length].rstrip()
    if s.upper() in _RESERVED_WINDOWS:
        s = f"_{s}"
    return s or "project"


def resolve_unique_dir(root: Path, name: str) -> Path:
    """Return root/name, with a numeric suffix if it already exists."""
    base = root / name
    if not base.exists():
        return base
    i = 2
    while True:
        candidate = root / f"{name}_{i}"
        if not candidate.exists():
            return candidate
        i += 1


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _build_combined_markdown(report_text: str, chat: list[ChatMessage]) -> str:
    out = report_text or ""
    if chat:
        out += "\n\n---\n\n## 후속 질문 (Q&A)\n"
        for m in chat:
            if m.role == "user":
                out += f"\n**Q.** {m.text}\n"
            else:
                out += f"\n{m.text}\n"
    return out


_CHAT_SEPARATOR = "\n\n---\n\n## 후속 질문 (Q&A)"


def _strip_chat_section(report_md: str) -> str:
    """Remove the Q&A section from the combined report.md."""
    if _CHAT_SEPARATOR in report_md:
        return report_md.split(_CHAT_SEPARATOR)[0]
    return report_md


def save_project(project: Project) -> Path:
    """Write project.json, chat.json, and the combined report.md.

    `project.project_dir` must be set. The directory is created if missing.
    Returns the project directory path.
    """
    if not project.project_dir:
        raise ValueError("project.project_dir is required")

    p = Path(project.project_dir)
    p.mkdir(parents=True, exist_ok=True)

    if not project.created_at:
        project.created_at = _now_iso()
    project.updated_at = _now_iso()

    # project.json — metadata only (report and chat live in their own files)
    meta = {
        "schema": project.schema,
        "name": project.name,
        "url": project.url,
        "video_id": project.video_id,
        "video_title": project.video_title,
        "session_id": project.session_id,
        "created_at": project.created_at,
        "updated_at": project.updated_at,
        "frame_count": project.frame_count,
    }
    (p / PROJECT_FILENAME).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # chat.json — structured Q&A so we can round-trip back into the GUI
    chat_data = {"history": [asdict(m) for m in project.chat_history]}
    (p / CHAT_FILENAME).write_text(
        json.dumps(chat_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # report.md — combined human-readable form
    if project.report_text or project.chat_history:
        (p / REPORT_FILENAME).write_text(
            _build_combined_markdown(project.report_text, project.chat_history),
            encoding="utf-8",
        )

    return p


def load_project(project_dir: str | Path) -> Project:
    """Load a project from its folder. Raises FileNotFoundError if not a project."""
    p = Path(project_dir)
    meta_path = p / PROJECT_FILENAME
    if not meta_path.exists():
        raise FileNotFoundError(
            f"Not a ytga project (missing {PROJECT_FILENAME}): {p}"
        )

    meta = json.loads(meta_path.read_text(encoding="utf-8"))

    chat_history: list[ChatMessage] = []
    chat_path = p / CHAT_FILENAME
    if chat_path.exists():
        chat_data = json.loads(chat_path.read_text(encoding="utf-8"))
        for m in chat_data.get("history", []):
            chat_history.append(ChatMessage(role=m["role"], text=m["text"]))

    report_text = ""
    report_path = p / REPORT_FILENAME
    if report_path.exists():
        report_text = _strip_chat_section(report_path.read_text(encoding="utf-8"))

    return Project(
        name=meta.get("name", ""),
        url=meta.get("url", ""),
        video_id=meta.get("video_id", ""),
        video_title=meta.get("video_title", ""),
        session_id=meta.get("session_id"),
        created_at=meta.get("created_at", ""),
        updated_at=meta.get("updated_at", ""),
        frame_count=meta.get("frame_count", 0),
        schema=meta.get("schema", PROJECT_SCHEMA),
        project_dir=str(p.resolve()),
        report_text=report_text,
        chat_history=chat_history,
    )


def is_project_dir(path: str | Path) -> bool:
    return (Path(path) / PROJECT_FILENAME).exists()
