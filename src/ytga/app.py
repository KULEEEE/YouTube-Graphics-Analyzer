"""Flet desktop GUI for ytga."""

from __future__ import annotations

import os
import sys
import threading
import traceback
from pathlib import Path

import flet as ft

from .analyze import ClaudeNotFoundError, ClaudeRunError, analyze
from .extract import ExtractResult, _video_id_from_url, extract


DEFAULT_OUT = Path.home() / "ytga-output"
PLACEHOLDER_REPORT = (
    "분석 시작 후 이 영역에 결과가 표시됩니다.\n\n"
    "- **두드러지는 기법** — 확신도와 근거 프레임 인용\n"
    "- **파이프라인 총평** — 엔진/렌더러 추정\n"
)


def _open_in_explorer(path: str | Path) -> None:
    p = str(path)
    if os.name == "nt":
        try:
            os.startfile(p)  # type: ignore[attr-defined]
        except OSError:
            pass
    elif sys.platform == "darwin":
        os.system(f'open "{p}"')
    else:
        os.system(f'xdg-open "{p}"')


def _build_ui(page: ft.Page) -> None:
    page.title = "ytga – Graphics Analyzer"
    page.theme_mode = ft.ThemeMode.DARK
    page.padding = 24
    page.window.width = 1180
    page.window.height = 880
    page.window.min_width = 820
    page.window.min_height = 620

    state: dict = {"current_result": None, "running": False, "report_text": ""}

    # ---- Widgets ----
    url_field = ft.TextField(
        label="YouTube URL",
        hint_text="https://www.youtube.com/watch?v=...",
        expand=True,
        autofocus=True,
        border_radius=8,
    )
    max_duration_field = ft.TextField(
        label="최대 길이 (초)",
        value="600",
        width=130,
        border_radius=8,
        tooltip="0이면 전체 영상 다운로드",
    )
    max_frames_field = ft.TextField(
        label="최대 프레임",
        value="30",
        width=110,
        border_radius=8,
    )
    fmt_dropdown = ft.Dropdown(
        label="이미지",
        value="jpg",
        width=100,
        options=[ft.dropdown.Option("jpg"), ft.dropdown.Option("png")],
        border_radius=8,
    )

    run_btn = ft.FilledButton(
        text="분석 시작",
        icon=ft.Icons.PLAY_ARROW_ROUNDED,
        height=52,
        style=ft.ButtonStyle(
            shape=ft.RoundedRectangleBorder(radius=10),
            padding=ft.padding.symmetric(horizontal=22),
        ),
    )

    progress_bar = ft.ProgressBar(width=None, value=None, visible=False, bar_height=6)
    progress_text = ft.Text("대기 중", size=13, color=ft.Colors.ON_SURFACE_VARIANT)
    elapsed_text = ft.Text("", size=12, color=ft.Colors.ON_SURFACE_VARIANT)

    frames_header = ft.Row(
        [
            ft.Text("추출된 프레임", size=15, weight=ft.FontWeight.BOLD),
            ft.Container(expand=True),
            ft.Text("", size=12, color=ft.Colors.ON_SURFACE_VARIANT),
        ]
    )
    frames_grid = ft.GridView(
        runs_count=6,
        max_extent=210,
        child_aspect_ratio=16 / 11,
        spacing=10,
        run_spacing=10,
        height=290,
    )
    frames_section = ft.Container(
        content=ft.Column([frames_header, frames_grid], spacing=10),
        padding=ft.padding.only(top=8),
        visible=False,
    )

    report_md = ft.Markdown(
        value=PLACEHOLDER_REPORT,
        selectable=True,
        extension_set=ft.MarkdownExtensionSet.GITHUB_WEB,
        on_tap_link=lambda e: page.launch_url(e.data),
    )
    save_btn = ft.OutlinedButton("리포트 저장", icon=ft.Icons.SAVE_ROUNDED, visible=False)
    open_folder_btn = ft.OutlinedButton(
        "폴더 열기", icon=ft.Icons.FOLDER_OPEN_ROUNDED, visible=False
    )
    copy_btn = ft.OutlinedButton(
        "리포트 복사", icon=ft.Icons.CONTENT_COPY_ROUNDED, visible=False
    )

    report_section = ft.Container(
        content=ft.Column(
            [
                ft.Row(
                    [
                        ft.Text("분석 결과", size=15, weight=ft.FontWeight.BOLD),
                        ft.Container(expand=True),
                        copy_btn,
                        save_btn,
                        open_folder_btn,
                    ]
                ),
                ft.Container(
                    content=ft.Column(
                        [report_md], scroll=ft.ScrollMode.AUTO, expand=True
                    ),
                    padding=14,
                    border=ft.border.all(1, ft.Colors.OUTLINE_VARIANT),
                    border_radius=8,
                    expand=True,
                ),
            ],
            spacing=10,
            expand=True,
        ),
        padding=ft.padding.only(top=8),
        expand=True,
    )

    # ---- Helpers (must call page.update from main thread context) ----
    def _ui(fn):
        """Schedule a UI mutation on the Flet main thread."""
        try:
            fn()
            page.update()
        except Exception:  # noqa: BLE001
            traceback.print_exc()

    def _set_running(running: bool) -> None:
        def _do():
            state["running"] = running
            run_btn.disabled = running
            run_btn.text = "분석 중..." if running else "분석 시작"
            progress_bar.visible = running
            url_field.disabled = running
            max_duration_field.disabled = running
            max_frames_field.disabled = running
            fmt_dropdown.disabled = running
        _ui(_do)

    def _set_progress(status: str, pct: float | None) -> None:
        def _do():
            progress_text.value = status
            progress_bar.value = pct  # None = indeterminate
        _ui(_do)

    def _toast(msg: str) -> None:
        def _do():
            page.open(ft.SnackBar(ft.Text(msg), open=True))
        _ui(_do)

    def _populate_frames(result: ExtractResult) -> None:
        def _do():
            frames_grid.controls.clear()
            for f in result.frames:
                ts = f.timestamp
                label = f"#{f.index}" + (
                    f"  ·  {ts:.1f}s" if ts is not None else ""
                )
                if f.is_supplemented:
                    label += "  (보충)"
                tile = ft.Container(
                    content=ft.Column(
                        [
                            ft.Container(
                                content=ft.Image(
                                    src=f.path,
                                    fit=ft.ImageFit.COVER,
                                    width=200,
                                    height=112,
                                    border_radius=6,
                                ),
                                border_radius=6,
                            ),
                            ft.Text(
                                label,
                                size=11,
                                color=ft.Colors.ON_SURFACE_VARIANT,
                            ),
                        ],
                        spacing=4,
                    ),
                    ink=True,
                    on_click=lambda e, p=f.path: _open_in_explorer(p),
                    padding=4,
                    border_radius=8,
                )
                frames_grid.controls.append(tile)
            frames_header.controls[2].value = f"{len(result.frames)} 장"
            frames_section.visible = True
        _ui(_do)

    # ---- Button handlers ----
    def _on_save(_e: ft.ControlEvent) -> None:
        result = state["current_result"]
        if not result or not state["report_text"]:
            return
        report_path = Path(result.out_dir) / "report.md"
        report_path.write_text(state["report_text"], encoding="utf-8")
        _toast(f"저장됨: {report_path}")

    def _on_open_folder(_e: ft.ControlEvent) -> None:
        result = state["current_result"]
        if result:
            _open_in_explorer(result.out_dir)

    def _on_copy(_e: ft.ControlEvent) -> None:
        if state["report_text"]:
            page.set_clipboard(state["report_text"])
            _toast("리포트가 클립보드에 복사되었습니다")

    save_btn.on_click = _on_save
    open_folder_btn.on_click = _on_open_folder
    copy_btn.on_click = _on_copy

    # ---- Worker thread ----
    def _worker(url: str, max_dur: int, max_fr: int, image_format: str) -> None:
        try:
            _set_running(True)
            video_id = _video_id_from_url(url)
            out_dir = DEFAULT_OUT / video_id

            _set_progress("준비 중...", None)
            result = extract(
                url,
                out_dir=out_dir,
                max_duration=max_dur,
                max_frames=max_fr,
                image_format=image_format,
                on_progress=_set_progress,
            )
            state["current_result"] = result
            _populate_frames(result)

            _set_progress("Claude 분석 시작...", None)
            analysis = analyze(
                [f.path for f in result.frames],
                video_url=url,
                video_title=result.title,
                on_progress=_set_progress,
            )

            state["report_text"] = analysis.text

            def _show_report():
                report_md.value = analysis.text
                save_btn.visible = True
                open_folder_btn.visible = True
                copy_btn.visible = True
            _ui(_show_report)

            (Path(result.out_dir) / "report.md").write_text(
                analysis.text, encoding="utf-8"
            )

            _set_progress(
                f"완료. 프레임 {len(result.frames)}장, 분석 {analysis.frames_read}장 읽음.",
                1.0,
            )
        except ClaudeNotFoundError as e:
            _toast(f"Claude Code CLI를 찾지 못했습니다: {e}")
            _set_progress("Claude CLI 누락", None)
        except ClaudeRunError as e:
            _toast(f"Claude 분석 실패 (code {e.returncode})")
            _set_progress(f"분석 실패: {e}", None)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            _toast(f"오류: {e}")
            _set_progress(f"오류: {e}", None)
        finally:
            _set_running(False)

    def _on_run(_e: ft.ControlEvent) -> None:
        if state["running"]:
            return
        url = (url_field.value or "").strip()
        if not url:
            _toast("URL을 입력해주세요")
            return
        try:
            mdur = int(max_duration_field.value or "600")
            mfr = int(max_frames_field.value or "30")
        except ValueError:
            _toast("최대 길이/프레임은 숫자로 입력해주세요")
            return
        threading.Thread(
            target=_worker,
            args=(url, mdur, mfr, fmt_dropdown.value or "jpg"),
            daemon=True,
        ).start()

    run_btn.on_click = _on_run
    url_field.on_submit = _on_run

    # ---- Layout ----
    page.add(
        ft.Column(
            [
                ft.Row(
                    [
                        ft.Text("ytga", size=30, weight=ft.FontWeight.BOLD),
                        ft.Container(width=12),
                        ft.Text(
                            "YouTube 게임플레이 영상 → 그래픽스 기법 분석",
                            size=13,
                            color=ft.Colors.ON_SURFACE_VARIANT,
                        ),
                    ],
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                ft.Divider(height=1),
                ft.Container(
                    content=ft.Column(
                        [
                            ft.Row(
                                [url_field, max_duration_field, max_frames_field, fmt_dropdown, run_btn],
                                vertical_alignment=ft.CrossAxisAlignment.END,
                            ),
                            ft.Row(
                                [
                                    progress_bar,
                                ],
                            ),
                            ft.Row([progress_text, ft.Container(expand=True), elapsed_text]),
                        ],
                        spacing=10,
                    ),
                    padding=ft.padding.symmetric(vertical=8),
                ),
                frames_section,
                report_section,
            ],
            spacing=12,
            expand=True,
        )
    )


def main() -> None:
    ft.app(target=_build_ui)


if __name__ == "__main__":
    main()
