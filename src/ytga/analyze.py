"""Run headless `claude -p` against the extracted frames and capture the report."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .prompts import build_analysis_prompt


ProgressCallback = Callable[[str, Optional[float]], None]
TextCallback = Callable[[str], None]


class ClaudeNotFoundError(RuntimeError):
    """Claude Code CLI is not installed or not on PATH."""


class ClaudeRunError(RuntimeError):
    """`claude -p` exited non-zero."""

    def __init__(self, returncode: int, stderr: str):
        super().__init__(f"claude -p exited with code {returncode}: {stderr[-2000:]}")
        self.returncode = returncode
        self.stderr = stderr


@dataclass
class AnalysisResult:
    text: str
    frames_read: int
    raw_events: list[dict] = field(default_factory=list)


def _find_claude() -> str:
    """Locate the Claude Code CLI executable.

    On Windows, the `where` lookup may turn up `claude.cmd`. shutil.which handles
    PATHEXT correctly. Fall back to common npm-global install locations.
    """
    found = shutil.which("claude")
    if found:
        return found

    candidates: list[Path] = []
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.append(Path(appdata) / "npm" / "claude.cmd")
            candidates.append(Path(appdata) / "npm" / "claude.exe")
        local = os.environ.get("LOCALAPPDATA")
        if local:
            candidates.append(Path(local) / "Programs" / "claude" / "claude.exe")
    else:
        for d in ("/usr/local/bin", "/opt/homebrew/bin", str(Path.home() / ".local" / "bin")):
            candidates.append(Path(d) / "claude")

    for c in candidates:
        if c.exists():
            return str(c)

    raise ClaudeNotFoundError(
        "Claude Code CLI not found on PATH. Install from "
        "https://docs.claude.com/en/docs/claude-code and ensure `claude --version` works."
    )


def _emit(cb: ProgressCallback | None, status: str, progress: float | None) -> None:
    if cb is not None:
        try:
            cb(status, progress)
        except Exception:
            pass


def analyze(
    frame_paths: list[str | Path],
    *,
    video_url: str,
    video_title: str | None = None,
    on_progress: ProgressCallback | None = None,
    on_text_chunk: TextCallback | None = None,
    timeout: float | None = None,
    extra_args: list[str] | None = None,
) -> AnalysisResult:
    """Run `claude -p` headlessly, stream progress, and return the final report."""
    claude_exe = _find_claude()
    total = len(frame_paths)
    prompt = build_analysis_prompt(frame_paths, video_url, video_title)

    cmd: list[str] = [
        claude_exe,
        "-p",
        "--output-format",
        "stream-json",
        "--input-format",
        "text",
        "--verbose",  # required to use stream-json output
        "--allowed-tools",
        "Read",
        "--permission-mode",
        "bypassPermissions",
    ]
    if extra_args:
        cmd.extend(extra_args)

    _emit(on_progress, "Claude 호출 시작...", None)

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,  # line-buffered
    )

    # Drain stderr in a background thread to avoid deadlocks
    stderr_lines: list[str] = []

    def _drain_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_lines.append(line)

    stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
    stderr_thread.start()

    assert proc.stdin is not None
    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
    except (BrokenPipeError, OSError):
        pass  # process likely failed early, returncode below will surface it

    final_text = ""
    frames_read = 0
    raw_events: list[dict] = []

    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        raw_events.append(event)
        etype = event.get("type")

        if etype == "assistant":
            msg = event.get("message", {})
            for block in msg.get("content", []) or []:
                btype = block.get("type")
                if btype == "tool_use" and block.get("name") == "Read":
                    frames_read += 1
                    pct = frames_read / total if total else None
                    _emit(
                        on_progress,
                        f"프레임 분석 중 {frames_read}/{total}",
                        pct,
                    )
                elif btype == "text":
                    text = block.get("text", "")
                    if text:
                        if on_text_chunk:
                            on_text_chunk(text)
                        final_text += text

        elif etype == "result":
            if event.get("subtype") == "success":
                # `result` carries the canonical final text
                final_text = event.get("result", final_text) or final_text
            elif event.get("is_error"):
                stderr_lines.append(json.dumps(event))

    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise

    stderr_thread.join(timeout=2.0)

    if proc.returncode != 0:
        raise ClaudeRunError(proc.returncode, "".join(stderr_lines))

    if not final_text.strip():
        raise ClaudeRunError(
            proc.returncode,
            "Claude returned no text. stderr tail:\n" + "".join(stderr_lines)[-2000:],
        )

    _emit(on_progress, "분석 완료", 1.0)
    return AnalysisResult(text=final_text, frames_read=frames_read, raw_events=raw_events)
