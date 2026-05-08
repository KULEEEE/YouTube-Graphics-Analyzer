"""Download a YouTube video and extract scene-change frames for analysis."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

from imageio_ffmpeg import get_ffmpeg_exe
from yt_dlp import YoutubeDL


ProgressCallback = Callable[[str, Optional[float]], None]


@dataclass
class FrameInfo:
    index: int
    path: str
    timestamp: Optional[float] = None  # seconds into the video
    is_supplemented: bool = False  # True if added by even-spacing fallback


@dataclass
class ExtractResult:
    url: str
    video_id: str
    title: str
    duration: float
    out_dir: str
    frames: list[FrameInfo] = field(default_factory=list)


_SHOWINFO_RE = re.compile(r"pts_time:([\d.]+)")
_VIDEO_ID_RE = re.compile(r"(?:v=|/shorts/|youtu\.be/|/embed/)([A-Za-z0-9_-]{11})")


def _video_id_from_url(url: str) -> str:
    m = _VIDEO_ID_RE.search(url)
    return m.group(1) if m else "video"


def _parse_showinfo_timestamps(stderr: str) -> list[float]:
    out = []
    for line in stderr.splitlines():
        if "Parsed_showinfo" not in line:
            continue
        m = _SHOWINFO_RE.search(line)
        if m:
            out.append(float(m.group(1)))
    return out


def _emit(cb: ProgressCallback | None, status: str, progress: float | None = None) -> None:
    if cb is not None:
        try:
            cb(status, progress)
        except Exception:
            # progress callbacks must never break the pipeline
            pass


def _download_video(
    url: str,
    target_path: Path,
    *,
    max_height: int,
    max_duration: int | None,
    on_progress: ProgressCallback | None,
) -> dict:
    def _hook(d: dict) -> None:
        if d.get("status") == "downloading":
            downloaded = d.get("downloaded_bytes") or 0
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                pct = downloaded / total
                _emit(on_progress, f"다운로드 중 {pct * 100:.0f}%", pct)
            else:
                _emit(on_progress, "다운로드 중...", None)
        elif d.get("status") == "finished":
            _emit(on_progress, "다운로드 완료", 1.0)

    fmt = (
        f"bestvideo[height<={max_height}][ext=mp4]+bestaudio[ext=m4a]/"
        f"best[height<={max_height}][ext=mp4]/best[height<={max_height}]/best"
    )

    ydl_opts: dict = {
        "format": fmt,
        "outtmpl": str(target_path.with_suffix("")) + ".%(ext)s",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "progress_hooks": [_hook],
        "ffmpeg_location": str(Path(get_ffmpeg_exe()).parent),
    }

    if max_duration:
        def _ranges(info, ydl):  # noqa: ARG001
            return [{"start_time": 0, "end_time": max_duration}]

        ydl_opts["download_ranges"] = _ranges
        ydl_opts["force_keyframes_at_cuts"] = True

    _emit(on_progress, "메타데이터 조회 중...", None)
    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)

    return info


def _resolve_downloaded_file(stem_path: Path) -> Path:
    parent = stem_path.parent
    stem = stem_path.name
    candidates = sorted(parent.glob(f"{stem}.*"))
    # Prefer mp4 if multiple show up (yt-dlp partials, etc.)
    for c in candidates:
        if c.suffix.lower() == ".mp4":
            return c
    if candidates:
        return candidates[0]
    raise FileNotFoundError(f"yt-dlp produced no file at {stem_path}.*")


def _extract_scene_frames(
    ffmpeg: str,
    video_path: Path,
    out_dir: Path,
    *,
    threshold: float,
    image_format: str,
) -> list[tuple[Path, float]]:
    pattern = str(out_dir / f"scene_%05d.{image_format}")
    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-i",
        str(video_path),
        "-vf",
        f"select='gt(scene,{threshold})',showinfo",
        "-fps_mode",
        "vfr",
        "-q:v",
        "3",
        pattern,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg scene-detect failed:\n{proc.stderr[-2000:]}")

    timestamps = _parse_showinfo_timestamps(proc.stderr)
    paths = sorted(out_dir.glob(f"scene_*.{image_format}"))

    pairs: list[tuple[Path, float]] = []
    for i, p in enumerate(paths):
        ts = timestamps[i] if i < len(timestamps) else float("nan")
        pairs.append((p, ts))
    return pairs


def _extract_at(
    ffmpeg: str,
    video_path: Path,
    timestamp: float,
    out_path: Path,
) -> None:
    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-ss",
        f"{timestamp:.3f}",
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        "-q:v",
        "3",
        str(out_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg seek failed at t={timestamp}:\n{proc.stderr[-1500:]}")


def _evenly_spaced_targets(duration: float, n: int) -> list[float]:
    if n <= 0:
        return []
    return [duration * (i + 1) / (n + 1) for i in range(n)]


def _decimate_evenly(items: list, keep: int) -> list:
    if keep >= len(items):
        return items
    indices = [round(i * (len(items) - 1) / (keep - 1)) for i in range(keep)] if keep > 1 else [0]
    seen = set()
    out = []
    for idx in indices:
        if idx not in seen:
            seen.add(idx)
            out.append(items[idx])
    return out


def extract(
    url: str,
    out_dir: str | Path,
    *,
    max_duration: int = 600,
    max_height: int = 1080,
    min_frames: int = 10,
    max_frames: int = 30,
    scene_threshold: float = 0.3,
    image_format: str = "jpg",
    on_progress: ProgressCallback | None = None,
    keep_video: bool = False,
) -> ExtractResult:
    """Run the full pipeline: download → scene detect → balance frame count.

    Args:
        url: YouTube URL.
        out_dir: Where the final frames + manifest land. Created if missing.
        max_duration: Cap source download length (seconds). 0/None = full video.
        max_height: Max video height to request (1080 is plenty for analysis).
        min_frames / max_frames: Final frame count is clamped into this range.
        scene_threshold: ffmpeg scene-change threshold (0.3 ~= medium-strict).
        image_format: 'jpg' or 'png'.
        on_progress: callback(status_text, progress_0_to_1_or_None).
        keep_video: if True, save the source video next to the frames.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if image_format not in ("jpg", "png"):
        raise ValueError("image_format must be 'jpg' or 'png'")

    ffmpeg = get_ffmpeg_exe()
    video_id = _video_id_from_url(url)

    with tempfile.TemporaryDirectory(prefix="ytga_") as td:
        td_path = Path(td)
        stem = td_path / "source"

        info = _download_video(
            url,
            stem,
            max_height=max_height,
            max_duration=max_duration if max_duration and max_duration > 0 else None,
            on_progress=on_progress,
        )

        video_path = _resolve_downloaded_file(stem)
        title = info.get("title") or video_id
        full_duration = float(info.get("duration") or 0.0)

        # Effective duration of the *downloaded* clip
        clip_duration = (
            float(min(full_duration, max_duration))
            if max_duration and max_duration > 0 and full_duration
            else full_duration
        )

        _emit(on_progress, "프레임 추출 중 (scene detection)...", None)
        scene_pairs = _extract_scene_frames(
            ffmpeg,
            video_path,
            out_dir,
            threshold=scene_threshold,
            image_format=image_format,
        )

        # If we have too many scene frames, decimate evenly through the timeline
        if len(scene_pairs) > max_frames:
            keep = _decimate_evenly(scene_pairs, max_frames)
            keep_set = {p for p, _ in keep}
            for p, _ in scene_pairs:
                if p not in keep_set:
                    p.unlink(missing_ok=True)
            scene_pairs = keep

        # If we have too few, supplement with evenly spaced samples
        supplemented_pairs: list[tuple[Path, float, bool]] = [
            (p, ts, False) for p, ts in scene_pairs
        ]

        if len(scene_pairs) < min_frames and clip_duration > 0:
            need = min_frames - len(scene_pairs)
            existing_ts = sorted(ts for _, ts in scene_pairs if ts == ts)  # filter NaN
            target_ts = _evenly_spaced_targets(clip_duration, need * 3)
            # Pick those farthest from any existing timestamp
            def _min_dist(t: float) -> float:
                if not existing_ts:
                    return float("inf")
                return min(abs(t - e) for e in existing_ts)

            target_ts.sort(key=_min_dist, reverse=True)
            target_ts = target_ts[:need]
            target_ts.sort()

            for i, ts in enumerate(target_ts):
                _emit(on_progress, f"보충 프레임 추출 중 ({i + 1}/{need})", (i + 1) / need)
                supp_path = out_dir / f"extra_{i:03d}.{image_format}"
                try:
                    _extract_at(ffmpeg, video_path, ts, supp_path)
                    supplemented_pairs.append((supp_path, ts, True))
                except RuntimeError:
                    continue

        # Sort all frames by timestamp (NaN/missing → end)
        def _sort_key(item):
            _, ts, _ = item
            return (ts != ts, ts if ts == ts else 0.0)  # ts != ts is True for NaN

        supplemented_pairs.sort(key=_sort_key)

        # Renumber so frame_001.jpg is the chronologically first
        frames: list[FrameInfo] = []
        for i, (p, ts, suppl) in enumerate(supplemented_pairs, start=1):
            new_path = out_dir / f"frame_{i:03d}.{image_format}"
            if p != new_path:
                if new_path.exists():
                    new_path.unlink()
                p.rename(new_path)
            frames.append(
                FrameInfo(
                    index=i,
                    path=str(new_path.resolve()),
                    timestamp=None if ts != ts else round(ts, 3),
                    is_supplemented=suppl,
                )
            )

        # Optionally keep the source video
        if keep_video:
            kept_video = out_dir / f"source{video_path.suffix}"
            shutil.copy2(video_path, kept_video)

    result = ExtractResult(
        url=url,
        video_id=video_id,
        title=title,
        duration=full_duration,
        out_dir=str(out_dir.resolve()),
        frames=frames,
    )

    manifest = out_dir / "manifest.json"
    manifest.write_text(
        json.dumps(asdict(result), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    _emit(on_progress, f"추출 완료: {len(frames)}장", 1.0)
    return result
