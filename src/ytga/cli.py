"""CLI entry point: extract frames + (optionally) run claude analysis."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .analyze import ClaudeNotFoundError, ClaudeRunError, analyze
from .extract import _video_id_from_url, extract, peek_metadata
from .pdf_report import generate_pdf
from .project import (
    ChatMessage,
    Project,
    resolve_unique_dir,
    sanitize_name,
    save_project,
)


DEFAULT_OUT = Path.home() / "ytga-output"


def _print_progress(status: str, pct: float | None) -> None:
    if pct is None:
        print(f"  {status}", flush=True)
    else:
        print(f"  {status} ({pct * 100:.0f}%)", flush=True)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ytga",
        description="YouTube gameplay → frame extraction → Claude graphics analysis.",
    )
    p.add_argument("url", help="YouTube URL")
    p.add_argument(
        "--out",
        default=str(DEFAULT_OUT),
        help=f"Project root directory (default: {DEFAULT_OUT})",
    )
    p.add_argument(
        "--project-name",
        default=None,
        help="Folder name under --out. If omitted, uses the video title (sanitized) "
             "with a numeric suffix on collision.",
    )
    p.add_argument(
        "--start-seconds",
        type=float,
        default=0.0,
        help="Skip the first N seconds of the source video (e.g., 60 to skip an intro).",
    )
    p.add_argument(
        "--max-duration",
        type=int,
        default=600,
        help="Cap analyzed clip length after the start offset, in seconds. "
             "Default: 600. 0 = no cap.",
    )
    p.add_argument("--max-height", type=int, default=1080)
    p.add_argument("--min-frames", type=int, default=10)
    p.add_argument("--max-frames", type=int, default=30)
    p.add_argument(
        "--sample-interval",
        type=float,
        default=1.0,
        help="Seconds between sampled frames (default: 1.0 = 1 frame per second). "
             "Lower = more granular dedup; higher = faster.",
    )
    p.add_argument(
        "--phash-threshold",
        type=int,
        default=12,
        help="Perceptual-hash Hamming distance for dedup. 0 disables. "
             "Higher = more aggressive merging. 6/12/20 are reasonable points.",
    )
    p.add_argument("--format", choices=("jpg", "png"), default="jpg", dest="image_format")
    p.add_argument(
        "--no-quality-filter",
        action="store_true",
        help="Disable the brightness/edge filter that drops flash/logo/uniform frames.",
    )
    p.add_argument("--keep-video", action="store_true", help="Keep the source video file")
    p.add_argument("--no-analyze", action="store_true", help="Only extract frames, skip Claude")
    p.add_argument(
        "--export-pdf",
        action="store_true",
        help="Also write report.pdf (cover + analysis + frame gallery + Q&A) "
             "into the project folder.",
    )
    return p


def _derive_project_dir(
    out_root: Path,
    requested_name: str | None,
    video_title: str,
    video_id: str,
) -> Path:
    if requested_name:
        name = sanitize_name(requested_name)
    elif video_title:
        name = sanitize_name(video_title)
    else:
        name = sanitize_name(video_id)
    return resolve_unique_dir(out_root, name)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    out_root = Path(args.out)

    print("=== Metadata ===")
    try:
        meta = peek_metadata(args.url)
    except Exception as e:  # noqa: BLE001
        print(f"WARN: metadata fetch failed ({e}); falling back to video id", file=sys.stderr)
        meta = {"id": _video_id_from_url(args.url), "title": "", "duration": 0}

    print(f"  title:    {meta.get('title') or '(unknown)'}")
    print(f"  duration: {meta.get('duration') or 0}s")

    out_dir = _derive_project_dir(
        out_root, args.project_name, meta.get("title", ""), meta.get("id", "")
    )
    print(f"Project dir: {out_dir.resolve()}")
    print()

    print("=== Frame extraction ===")
    result = extract(
        args.url,
        out_dir=out_dir,
        start_seconds=args.start_seconds,
        max_duration=args.max_duration,
        max_height=args.max_height,
        min_frames=args.min_frames,
        max_frames=args.max_frames,
        sample_interval=args.sample_interval,
        phash_threshold=args.phash_threshold,
        quality_filter=not args.no_quality_filter,
        image_format=args.image_format,
        keep_video=args.keep_video,
        on_progress=_print_progress,
    )
    print(f"\n{len(result.frames)} frames → {result.out_dir}")
    print(f"Title: {result.title}")
    print(f"Manifest: {Path(result.out_dir) / 'manifest.json'}")

    project = Project(
        name=out_dir.name,
        url=args.url,
        video_id=result.video_id,
        video_title=result.title,
        frame_count=len(result.frames),
        project_dir=str(out_dir.resolve()),
    )

    if args.no_analyze:
        save_project(project)
        print(f"\nProject saved (no analysis): {out_dir / 'project.json'}")
        return 0

    print()
    print("=== Claude analysis ===")
    try:
        analysis = analyze(
            [f.path for f in result.frames],
            video_url=args.url,
            video_title=result.title,
            on_progress=_print_progress,
        )
    except ClaudeNotFoundError as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        save_project(project)
        return 2
    except ClaudeRunError as e:
        print(f"\nClaude failed: {e}", file=sys.stderr)
        save_project(project)
        return 3

    project.report_text = analysis.text
    project.session_id = analysis.session_id
    save_project(project)

    print(f"\nProject saved: {out_dir / 'project.json'}")
    print(f"Report:        {out_dir / 'report.md'}")

    if args.export_pdf:
        try:
            pdf_path = generate_pdf(project, out_dir / "report.pdf")
            print(f"PDF:           {pdf_path}")
        except Exception as e:  # noqa: BLE001
            print(f"PDF export failed: {e}", file=sys.stderr)

    print()
    print("=" * 60)
    print(analysis.text)
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
