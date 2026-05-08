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

try:
    import imagehash
    from PIL import Image, ImageStat
    _HAS_IMAGEHASH = True
except ImportError:  # pragma: no cover
    _HAS_IMAGEHASH = False


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


def peek_metadata(url: str) -> dict:
    """Cheap metadata-only fetch (no download). Returns a dict with at least
    `id`, `title`, and `duration` when available. Used by the GUI to derive
    a default project folder name from the video title before kicking off
    the full pipeline.
    """
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noprogress": True,
    }
    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False) or {}
    return {
        "id": info.get("id") or _video_id_from_url(url),
        "title": info.get("title") or "",
        "duration": info.get("duration") or 0,
        "uploader": info.get("uploader") or "",
    }


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
    on_progress: ProgressCallback | None,
) -> dict:
    """Download a video-only single-file stream — no audio merge, no partial range.

    We avoid yt-dlp's ffmpeg-dependent paths (download_ranges, audio merging)
    because imageio-ffmpeg ships only ffmpeg (no ffprobe) under a non-standard
    filename. Trimming is done after the download by our own ffmpeg call.
    """
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

    # Prefer a single-file video-only stream (no audio merge needed for analysis).
    fmt = (
        f"bestvideo[height<={max_height}][ext=mp4]/"
        f"bestvideo[height<={max_height}]/"
        f"best[height<={max_height}][ext=mp4]/"
        f"best[height<={max_height}]/best"
    )

    ydl_opts: dict = {
        "format": fmt,
        "outtmpl": str(target_path.with_suffix("")) + ".%(ext)s",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "progress_hooks": [_hook],
        # Pass the explicit file path so yt-dlp's basename-based detection
        # accepts imageio-ffmpeg's "ffmpeg-win-x86_64-vN.N.exe" naming.
        "ffmpeg_location": get_ffmpeg_exe(),
    }

    _emit(on_progress, "메타데이터 조회 중...", None)
    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)

    return info


def _trim_video(
    ffmpeg: str,
    src: Path,
    dst: Path,
    *,
    start_seconds: float = 0.0,
    max_duration: int | None = None,
) -> None:
    """Lossless cut: optional start offset + optional duration cap (keyframe-aligned)."""
    cmd = [ffmpeg, "-y", "-hide_banner"]
    if start_seconds > 0:
        cmd.extend(["-ss", f"{start_seconds:.3f}"])
    cmd.extend(["-i", str(src)])
    if max_duration and max_duration > 0:
        cmd.extend(["-t", str(max_duration)])
    cmd.extend(["-c", "copy", "-avoid_negative_ts", "make_zero", str(dst)])
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg trim failed:\n{proc.stderr[-1500:]}")


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


def _sample_uniform_frames(
    ffmpeg: str,
    video_path: Path,
    out_dir: Path,
    *,
    image_format: str,
    sample_interval: float,
    start_seconds: float = 0.0,
    end_seconds: float | None = None,
) -> list[tuple[Path, float]]:
    """Sample frames at a fixed interval across [start_seconds, end_seconds].

    We use ffmpeg's `select` filter with `prev_selected_t` so the kept frames
    keep their ORIGINAL pts_time (no `fps` filter, which re-times to a
    constant output rate and would ruin the absolute-timestamp mapping).

    Why uniform sampling instead of `gt(scene, T)`:
      ffmpeg's `scene` score measures pixel-difference vs the previous frame
      — i.e. it favors *transitions*. For graphics-technique analysis we want
      *stable, informative* gameplay frames, which by definition have low
      scene scores. So scene-change detection systematically picked up the
      explosion/cut moments and dropped the actual rendered scenes. Sampling
      at fixed intervals (then deduping by pHash) gives representative
      frames.
    """
    pattern = str(out_dir / f"sample_%05d.{image_format}")

    s = max(0.0, start_seconds or 0.0)
    e = end_seconds if (end_seconds is not None and end_seconds > 0) else 1e9
    si = max(0.05, float(sample_interval))

    # We do NOT use ffmpeg's between(t, ...) for the time window — empirically
    # combining it with the prev_selected_t branch produced frames whose
    # `pts_time` reported by showinfo no longer matched the actual decoded
    # content (visuals from t=0 with labels of +start_seconds). Sample across
    # the whole video, then filter by time range in Python, where the
    # (path, pts_time) mapping is unambiguous.
    select_expr = (
        f"if(isnan(prev_selected_t),1,gte(t,prev_selected_t+{si:.3f}))"
    )

    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-i",
        str(video_path),
        "-vf",
        f"select='{select_expr}',showinfo",
        "-fps_mode",
        "vfr",
        "-q:v",
        "3",
        pattern,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg sample failed:\n{proc.stderr[-2000:]}")

    timestamps = _parse_showinfo_timestamps(proc.stderr)
    paths = sorted(out_dir.glob(f"sample_*.{image_format}"))

    debug_path = out_dir / "_sample_debug.txt"
    try:
        with debug_path.open("w", encoding="utf-8") as f:
            f.write(
                f"# uniform sample (interval={si}s, requested range=[{s},{e}])\n"
                f"# files: {len(paths)}, showinfo: {len(timestamps)}\n"
            )
            n = max(len(paths), len(timestamps))
            for i in range(n):
                fp = paths[i].name if i < len(paths) else "(missing)"
                ts = timestamps[i] if i < len(timestamps) else float("nan")
                f.write(f"{i:5d}  {fp:30s}  pts_time={ts}\n")
    except OSError:
        pass

    if len(paths) != len(timestamps):
        raise RuntimeError(
            f"sample/timestamp count mismatch: {len(paths)} files vs "
            f"{len(timestamps)} showinfo entries. See {debug_path}"
        )

    # Python-side time window filter. Out-of-range files are deleted from
    # disk; in-range files are renamed to embed the timestamp.
    pairs: list[tuple[Path, float]] = []
    for p, ts in zip(paths, timestamps):
        if s <= ts <= e:
            new_name = f"sample_{ts:010.3f}{p.suffix}"
            new_path = p.with_name(new_name)
            try:
                if new_path.exists() and new_path != p:
                    new_path.unlink()
                if new_path != p:
                    p.rename(new_path)
                pairs.append((new_path, ts))
            except OSError:
                pairs.append((p, ts))
        else:
            try:
                p.unlink()
            except OSError:
                pass
    return pairs


def _extract_at(
    ffmpeg: str,
    video_path: Path,
    timestamp: float,
    out_path: Path,
) -> None:
    """Frame-accurate seek to `timestamp` and emit one image.

    We use OUTPUT seeking (`-ss` AFTER `-i`) — slower than input seeking but
    exact. With input seeking ffmpeg can land on the prior keyframe and emit
    that frame instead of the requested timestamp, which is a real risk on
    DASH-merged YouTube mp4s.
    """
    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-i",
        str(video_path),
        "-ss",
        f"{timestamp:.3f}",
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


def _quality_filter_frames(
    scene_pairs: list[tuple[Path, float]],
    on_progress: ProgressCallback | None = None,
) -> list[tuple[Path, float]]:
    """Drop near-pure-white / near-pure-black frames (flashes, fades, blackouts).

    Only the brightness check remains. The previous stddev / edge-density
    heuristics were too aggressive on stylized scenes (heavy bloom, single-color
    skies, motion-blurred action) and dropped real gameplay frames.
    """
    if not _HAS_IMAGEHASH or not scene_pairs:
        return scene_pairs

    total = len(scene_pairs)
    kept: list[tuple[Path, float]] = []
    dropped = 0

    for i, (p, ts) in enumerate(scene_pairs, start=1):
        _emit(on_progress, f"품질 필터 검사 {i}/{total}", i / total)
        try:
            with Image.open(p) as img:
                gray = img.convert("L")
                gray.thumbnail((480, 270))  # downscale for speed
                mean_v = ImageStat.Stat(gray).mean[0]
        except Exception:
            kept.append((p, ts))  # don't drop on error
            continue

        if mean_v < 18 or mean_v > 238:
            try:
                p.unlink()
            except OSError:
                pass
            dropped += 1
        else:
            kept.append((p, ts))

    if dropped:
        _emit(on_progress, f"품질 필터로 {dropped}장 제거됨 (플래시/검은화면)", None)

    return kept


def _dedupe_by_phash(
    scene_pairs: list[tuple[Path, float]],
    hash_threshold: int,
    on_progress: ProgressCallback | None = None,
) -> list[tuple[Path, float]]:
    """Greedy perceptual-hash dedup.

    For each frame in chronological order, drop it if its pHash is within
    `hash_threshold` Hamming distance of any already-kept frame. The first
    occurrence of a visually-similar group survives; later ones are deleted
    from disk so the output directory stays clean.

    A higher threshold merges more aggressively. Reasonable range:
      6   → only near-duplicates merged
      12  → "same environment, different action moment" merged   (default)
      20  → very aggressive, can merge distinct shots
    """
    if not _HAS_IMAGEHASH or hash_threshold <= 0 or len(scene_pairs) <= 1:
        return scene_pairs

    total = len(scene_pairs)
    kept: list[tuple[Path, float, "imagehash.ImageHash | None"]] = []
    dropped = 0

    for i, (p, ts) in enumerate(scene_pairs, start=1):
        _emit(on_progress, f"중복 제거 검사 {i}/{total}", i / total)
        try:
            with Image.open(p) as img:
                h = imagehash.phash(img, hash_size=8)
        except Exception:
            kept.append((p, ts, None))  # if hash fails, keep to be safe
            continue

        is_dup = any(
            kh is not None and (h - kh) <= hash_threshold for _, _, kh in kept
        )
        if is_dup:
            try:
                p.unlink()
            except OSError:
                pass
            dropped += 1
        else:
            kept.append((p, ts, h))

    if dropped:
        _emit(on_progress, f"중복 {dropped}장 제거됨", None)

    return [(p, ts) for p, ts, _ in kept]


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
    start_seconds: float = 0.0,
    max_duration: int = 600,
    max_height: int = 1080,
    min_frames: int = 10,
    max_frames: int = 30,
    sample_interval: float = 1.0,
    phash_threshold: int = 12,
    quality_filter: bool = True,
    image_format: str = "jpg",
    on_progress: ProgressCallback | None = None,
    keep_video: bool = False,
) -> ExtractResult:
    """Run the full pipeline: download → uniform sample → quality → pHash → cap.

    Args:
        url: YouTube URL.
        out_dir: Where the final frames + manifest land. Created if missing.
        start_seconds: Skip the first N seconds of the source video before
            extracting frames (useful to skip intros/cinematics). Frame
            timestamps in the manifest are absolute (relative to the original
            video).
        max_duration: Cap analyzed clip length in seconds (after start offset).
            0/None = no cap.
        max_height: Max video height to request (1080 is plenty for analysis).
        min_frames / max_frames: Final frame count is clamped into this range.
        sample_interval: Seconds between sampled frames (default 1.0 = 1fps).
            Lower = more candidates / more granular dedup; higher = faster
            but coarser.
        phash_threshold: perceptual-hash Hamming distance for "same scene"
            grouping (0 = disable). 12 merges "same env, different action moment";
            6 keeps more variants; 20 is very aggressive.
        quality_filter: drop near-pure-black / near-pure-white frames
            (transitions, fade outs). Default True.
        image_format: 'jpg' or 'png'.
        on_progress: callback(status_text, progress_0_to_1_or_None).
        keep_video: if True, save the source video next to the frames.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if image_format not in ("jpg", "png"):
        raise ValueError("image_format must be 'jpg' or 'png'")

    # Clean any frame artifacts from a previous run in this folder so a
    # re-extract always starts from a clean slate (otherwise stale frames
    # could show up in the manifest).
    for prefix in ("frame_", "scene_", "sample_", "extra_"):
        for ext in ("jpg", "png"):
            for old in out_dir.glob(f"{prefix}*.{ext}"):
                try:
                    old.unlink()
                except OSError:
                    pass

    ffmpeg = get_ffmpeg_exe()
    video_id = _video_id_from_url(url)

    with tempfile.TemporaryDirectory(prefix="ytga_") as td:
        td_path = Path(td)
        stem = td_path / "source"

        info = _download_video(
            url,
            stem,
            max_height=max_height,
            on_progress=on_progress,
        )

        video_path = _resolve_downloaded_file(stem)
        title = info.get("title") or video_id
        full_duration = float(info.get("duration") or 0.0)

        # We DO NOT trim the source video before scene detection. Trimming
        # with `-c copy` only aligns to keyframes, which makes pts_time off by
        # up to a GOP (~1-2s). Instead we feed the whole downloaded clip to
        # ffmpeg with a `between(t, start, end)` filter — pts_time is then the
        # absolute original-video timeline, exact to the frame.
        start = max(0.0, start_seconds or 0.0)
        if max_duration and max_duration > 0:
            end_t = start + float(max_duration)
        else:
            end_t = float(full_duration) if full_duration else 999999.0
        if full_duration:
            end_t = min(end_t, float(full_duration))
        clip_duration = max(0.0, end_t - start)

        _emit(on_progress, f"프레임 샘플링 중 ({sample_interval}s 간격)...", None)
        scene_pairs = _sample_uniform_frames(
            ffmpeg,
            video_path,
            out_dir,
            image_format=image_format,
            sample_interval=sample_interval,
            start_seconds=start,
            end_seconds=end_t,
        )

        # All timestamps below are ABSOLUTE (original video timeline).

        # Quality filter: drop pure-flash / monochrome / low-detail frames
        # (game logos, fade transitions, particle-only screens).
        if quality_filter and scene_pairs:
            scene_pairs = _quality_filter_frames(scene_pairs, on_progress=on_progress)

        # Perceptual-hash dedup: merge frames that look the same even though
        # ffmpeg flagged them as scene changes (action moments, particle FX,
        # camera shake within the same shot).
        if phash_threshold > 0 and len(scene_pairs) > 1:
            if not _HAS_IMAGEHASH:
                _emit(on_progress, "imagehash 미설치 — pHash 단계 건너뜀", None)
            else:
                scene_pairs = _dedupe_by_phash(
                    scene_pairs, phash_threshold, on_progress=on_progress
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
            # Targets are absolute timestamps within [start, end_t].
            relative_targets = _evenly_spaced_targets(clip_duration, need * 3)
            target_ts = [start + r for r in relative_targets]

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
            # Timestamps in supplemented_pairs are already absolute (we filter
            # by `between(t,...)` and seek with absolute -ss values), so just
            # round.
            absolute_ts = None if ts != ts else round(ts, 3)
            frames.append(
                FrameInfo(
                    index=i,
                    path=str(new_path.resolve()),
                    timestamp=absolute_ts,
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
