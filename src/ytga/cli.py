"""CLI entry point: extract frames + (optionally) run claude analysis."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .analyze import ClaudeNotFoundError, ClaudeRunError, analyze
from .extract import _video_id_from_url, extract


DEFAULT_OUT = Path("output")


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
        help="Output root directory (a per-video subdirectory is created)",
    )
    p.add_argument(
        "--max-duration",
        type=int,
        default=600,
        help="Cap downloaded clip length in seconds (default: 600). 0 = full video.",
    )
    p.add_argument("--max-height", type=int, default=1080)
    p.add_argument("--min-frames", type=int, default=10)
    p.add_argument("--max-frames", type=int, default=30)
    p.add_argument("--scene-threshold", type=float, default=0.3)
    p.add_argument("--format", choices=("jpg", "png"), default="jpg", dest="image_format")
    p.add_argument("--keep-video", action="store_true", help="Keep the source video file")
    p.add_argument("--no-analyze", action="store_true", help="Only extract frames, skip Claude")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    out_root = Path(args.out)
    video_id = _video_id_from_url(args.url)
    out_dir = out_root / video_id

    print(f"Output: {out_dir.resolve()}")
    print()
    print("=== Frame extraction ===")
    result = extract(
        args.url,
        out_dir=out_dir,
        max_duration=args.max_duration,
        max_height=args.max_height,
        min_frames=args.min_frames,
        max_frames=args.max_frames,
        scene_threshold=args.scene_threshold,
        image_format=args.image_format,
        keep_video=args.keep_video,
        on_progress=_print_progress,
    )
    print(f"\n{len(result.frames)} frames → {result.out_dir}")
    print(f"Title: {result.title}")
    print(f"Manifest: {Path(result.out_dir) / 'manifest.json'}")

    if args.no_analyze:
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
        return 2
    except ClaudeRunError as e:
        print(f"\nClaude failed: {e}", file=sys.stderr)
        return 3

    report_path = Path(result.out_dir) / "report.md"
    report_path.write_text(analysis.text, encoding="utf-8")
    print(f"\nReport saved: {report_path}\n")
    print("=" * 60)
    print(analysis.text)
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
