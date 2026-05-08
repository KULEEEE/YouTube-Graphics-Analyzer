"""Run headless `claude -p` against the extracted frames and capture the report.

Exposes two entry points:

- `analyze(frame_paths, ...)` — first turn: builds the analysis prompt, runs
  `claude -p`, captures the report and the session id.
- `follow_up(session_id, question, ...)` — subsequent turns: resumes the same
  Claude Code session via `--resume`, so the model still has the frames and
  the previous report in context.
"""

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
    session_id: str | None = None
    raw_events: list[dict] = field(default_factory=list)


@dataclass
class FollowUpResult:
    text: str
    session_id: str | None = None
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


@dataclass
class _SessionRun:
    text: str
    session_id: str | None
    frames_read: int
    raw_events: list[dict]


def _run_claude_session(
    prompt: str,
    *,
    resume_session_id: str | None,
    on_progress: ProgressCallback | None,
    on_text_chunk: TextCallback | None,
    progress_total_frames: int | None,
    timeout: float | None,
    extra_args: list[str] | None,
) -> _SessionRun:
    """Execute one `claude -p` invocation, optionally resuming a session."""
    claude_exe = _find_claude()

    cmd: list[str] = [
        claude_exe,
        "-p",
        "--output-format", "stream-json",
        "--input-format", "text",
        "--verbose",
        "--allowed-tools", "Read",
        "--permission-mode", "bypassPermissions",
    ]
    if resume_session_id:
        cmd.extend(["--resume", resume_session_id])
    if extra_args:
        cmd.extend(extra_args)

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

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
        pass

    final_text = ""
    frames_read = 0
    session_id: str | None = None
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

        if etype == "system":
            sid = event.get("session_id")
            if sid:
                session_id = sid

        elif etype == "assistant":
            msg = event.get("message", {})
            for block in msg.get("content", []) or []:
                btype = block.get("type")
                if btype == "tool_use" and block.get("name") == "Read":
                    frames_read += 1
                    if progress_total_frames:
                        pct = frames_read / progress_total_frames
                        _emit(
                            on_progress,
                            f"프레임 분석 중 {frames_read}/{progress_total_frames}",
                            pct,
                        )
                    else:
                        _emit(on_progress, f"프레임 재참조 {frames_read}", None)
                elif btype == "text":
                    text = block.get("text", "")
                    if text:
                        if on_text_chunk:
                            on_text_chunk(text)
                        final_text += text

        elif etype == "result":
            if event.get("subtype") == "success":
                final_text = event.get("result", final_text) or final_text
                # session_id often appears here too
                sid = event.get("session_id")
                if sid:
                    session_id = sid
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

    return _SessionRun(
        text=final_text,
        session_id=session_id,
        frames_read=frames_read,
        raw_events=raw_events,
    )


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
    """First-turn analysis: read all frames and emit the structured report."""
    total = len(frame_paths)
    prompt = build_analysis_prompt(frame_paths, video_url, video_title)

    _emit(on_progress, "Claude 호출 시작...", None)
    run = _run_claude_session(
        prompt,
        resume_session_id=None,
        on_progress=on_progress,
        on_text_chunk=on_text_chunk,
        progress_total_frames=total,
        timeout=timeout,
        extra_args=extra_args,
    )
    _emit(on_progress, "분석 완료", 1.0)

    return AnalysisResult(
        text=run.text,
        frames_read=run.frames_read,
        session_id=run.session_id,
        raw_events=run.raw_events,
    )


_FOLLOWUP_PREAMBLE = (
    "다음은 위 분석 리포트에 대한 사용자의 후속 질문이다. 같은 영상/프레임을 "
    "이미 보았으므로 필요하면 Read 툴로 특정 프레임을 다시 들여다봐도 좋다. "
    "근거는 여전히 프레임 번호를 인용하고, 답은 한국어로, 군더더기 없이 "
    "짧고 정확하게.\n\n"
    "질문:\n"
)


def follow_up(
    session_id: str,
    question: str,
    *,
    on_progress: ProgressCallback | None = None,
    on_text_chunk: TextCallback | None = None,
    timeout: float | None = None,
    extra_args: list[str] | None = None,
) -> FollowUpResult:
    """Ask a follow-up question against the same Claude Code session.

    Args:
        session_id: The session id captured from the initial `analyze()` run.
        question: The user's follow-up question (Korean or English).
    """
    if not session_id:
        raise ValueError("session_id is required for follow_up()")

    prompt = _FOLLOWUP_PREAMBLE + question.strip() + "\n"

    _emit(on_progress, "후속 질문 전송 중...", None)
    run = _run_claude_session(
        prompt,
        resume_session_id=session_id,
        on_progress=on_progress,
        on_text_chunk=on_text_chunk,
        progress_total_frames=None,
        timeout=timeout,
        extra_args=extra_args,
    )
    _emit(on_progress, "응답 완료", 1.0)

    return FollowUpResult(
        text=run.text,
        # session_id may rotate (Claude Code sometimes returns a new id per turn)
        session_id=run.session_id or session_id,
        raw_events=run.raw_events,
    )
