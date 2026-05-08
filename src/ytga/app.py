"""Flet desktop GUI for ytga."""

from __future__ import annotations

import json
import os
import sys
import threading
import traceback
from dataclasses import asdict
from pathlib import Path

import flet as ft

from .analyze import (
    ClaudeNotFoundError,
    ClaudeRunError,
    analyze,
    follow_up,
)
from .extract import ExtractResult, FrameInfo, _video_id_from_url, extract, peek_metadata
from .pdf_report import generate_pdf
from .project import (
    ChatMessage,
    Project,
    is_project_dir,
    load_project,
    resolve_unique_dir,
    sanitize_name,
    save_project,
)


DEFAULT_OUT = Path.home() / "ytga-output"
PLACEHOLDER_REPORT = (
    "분석 시작 후 이 영역에 결과가 표시됩니다.\n\n"
    "- **두드러지는 기법** — 확신도와 근거 프레임 인용\n"
    "- **파이프라인 총평** — 엔진/렌더러 추정\n\n"
    "또는 우측 상단 **프로젝트 열기** 로 저장된 프로젝트 폴더를 다시 불러올 수 있습니다."
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


def _frames_from_manifest(project_dir: Path) -> ExtractResult | None:
    manifest_path = project_dir / "manifest.json"
    if not manifest_path.exists():
        return None
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    frames = [
        FrameInfo(
            index=f.get("index", i + 1),
            path=f.get("path", ""),
            timestamp=f.get("timestamp"),
            is_supplemented=f.get("is_supplemented", False),
        )
        for i, f in enumerate(data.get("frames", []))
    ]
    return ExtractResult(
        url=data.get("url", ""),
        video_id=data.get("video_id", ""),
        title=data.get("title", ""),
        duration=data.get("duration", 0.0),
        out_dir=data.get("out_dir", str(project_dir.resolve())),
        frames=frames,
    )


def _build_ui(page: ft.Page) -> None:
    page.title = "ytga – Graphics Analyzer"
    page.theme_mode = ft.ThemeMode.DARK
    page.padding = 24
    page.window.width = 1200
    page.window.height = 980
    page.window.min_width = 860
    page.window.min_height = 660

    state: dict = {
        "current_result": None,
        "running": False,
        "report_text": "",
        "session_id": None,
        "chat_history": [],            # list[ChatMessage]
        "chatting": False,
        "project_dir": None,           # Path of current project
        "video_id": "",
        "video_title": "",
    }

    # ============================================================
    # File pickers (open dir + save PDF)
    # ============================================================
    file_picker = ft.FilePicker()
    pdf_picker = ft.FilePicker()
    page.overlay.append(file_picker)
    page.overlay.append(pdf_picker)

    # ============================================================
    # Header
    # ============================================================
    header_path_label = ft.Text(
        "",
        size=12,
        color=ft.Colors.ON_SURFACE_VARIANT,
        selectable=True,
    )
    open_btn = ft.OutlinedButton(
        text="프로젝트 열기",
        icon=ft.Icons.FOLDER_OPEN_ROUNDED,
    )
    new_btn = ft.OutlinedButton(
        text="새 프로젝트",
        icon=ft.Icons.RESTART_ALT_ROUNDED,
    )

    # ============================================================
    # Inputs
    # ============================================================
    url_field = ft.TextField(
        label="YouTube URL",
        hint_text="https://www.youtube.com/watch?v=...",
        expand=True,
        autofocus=True,
        border_radius=8,
    )
    project_name_field = ft.TextField(
        label="프로젝트 이름 (선택)",
        hint_text="비우면 영상 제목 사용",
        width=280,
        border_radius=8,
    )
    start_seconds_field = ft.TextField(
        label="시작 시각 (초)",
        value="0",
        width=120,
        border_radius=8,
        tooltip="앞쪽 인트로/타이틀을 건너뛰려면 60, 90 등으로 설정",
    )
    max_duration_field = ft.TextField(
        label="최대 길이 (초)",
        value="600",
        width=130,
        border_radius=8,
        tooltip="시작 시각 이후 분석할 최대 길이. 0이면 끝까지",
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
    sample_interval_field = ft.TextField(
        label="샘플 간격(초)",
        value="1.0",
        width=130,
        border_radius=8,
        tooltip="N초마다 1프레임 추출. 0.5=조밀, 1.0=기본, 2.0=듬성. 작을수록 후보 多 → pHash dedup이 거름.",
    )
    phash_threshold_field = ft.TextField(
        label="pHash 임계값",
        value="12",
        width=130,
        border_radius=8,
        tooltip="0=비활성. 6(약한 dedup) / 12(기본) / 20(매우 적극적). 시각적 유사 묶기 강도.",
    )
    quality_filter_switch = ft.Switch(
        label="품질 필터",
        value=True,
        tooltip="플래시/단색/저에지 프레임 자동 제거",
    )
    run_btn = ft.FilledButton(
        text="프레임 추출",
        icon=ft.Icons.PHOTO_CAMERA_ROUNDED,
        height=52,
        style=ft.ButtonStyle(
            shape=ft.RoundedRectangleBorder(radius=10),
            padding=ft.padding.symmetric(horizontal=22),
        ),
        tooltip="다운로드 + scene/quality/pHash 처리. AI는 호출하지 않음.",
    )
    analyze_btn = ft.FilledButton(
        text="AI 분석 실행",
        icon=ft.Icons.AUTO_AWESOME_ROUNDED,
        visible=False,
        tooltip="현재 프레임을 Claude에게 보내 그래픽스 기법 분석 리포트 생성",
    )
    progress_bar = ft.ProgressBar(width=None, value=None, visible=False, bar_height=6)
    progress_text = ft.Text("대기 중", size=13, color=ft.Colors.ON_SURFACE_VARIANT)

    # ============================================================
    # Frames grid
    # ============================================================
    frames_count_label = ft.Text("", size=12, color=ft.Colors.ON_SURFACE_VARIANT)
    frames_header = ft.Row(
        [
            ft.Text("추출된 프레임", size=15, weight=ft.FontWeight.BOLD),
            ft.Container(expand=True),
            frames_count_label,
            analyze_btn,
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

    # ============================================================
    # Report card
    # ============================================================
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
    export_pdf_btn = ft.FilledTonalButton(
        "PDF 저장", icon=ft.Icons.PICTURE_AS_PDF_ROUNDED, visible=False
    )
    rename_btn = ft.OutlinedButton(
        "프로젝트 이름 변경", icon=ft.Icons.DRIVE_FILE_RENAME_OUTLINE_ROUNDED, visible=False
    )
    report_body = ft.Container(
        content=ft.Column([report_md], scroll=ft.ScrollMode.AUTO),
        padding=14,
        border=ft.border.all(1, ft.Colors.OUTLINE_VARIANT),
        border_radius=8,
        height=400,
    )
    report_section = ft.Container(
        content=ft.Column(
            [
                ft.Row(
                    [
                        ft.Text("분석 결과", size=15, weight=ft.FontWeight.BOLD),
                        ft.Container(expand=True),
                        rename_btn,
                        copy_btn,
                        save_btn,
                        export_pdf_btn,
                        open_folder_btn,
                    ]
                ),
                report_body,
            ],
            spacing=10,
        ),
        padding=ft.padding.only(top=8),
    )

    # ============================================================
    # Follow-up chat
    # ============================================================
    chat_history_column = ft.Column(spacing=8, scroll=ft.ScrollMode.AUTO, auto_scroll=True)
    chat_history_container = ft.Container(
        content=chat_history_column,
        padding=14,
        border=ft.border.all(1, ft.Colors.OUTLINE_VARIANT),
        border_radius=8,
        height=300,
    )
    question_field = ft.TextField(
        label="후속 질문",
        hint_text="예: 프레임 5에서 SSAO 흔적이 정확히 어디에 보여?",
        expand=True,
        multiline=True,
        min_lines=1,
        max_lines=3,
        border_radius=8,
        shift_enter=True,
    )
    send_btn = ft.FilledButton(
        text="질문 전송",
        icon=ft.Icons.SEND_ROUNDED,
        height=52,
        style=ft.ButtonStyle(
            shape=ft.RoundedRectangleBorder(radius=10),
            padding=ft.padding.symmetric(horizontal=20),
        ),
    )
    clear_chat_btn = ft.TextButton("대화 초기화", icon=ft.Icons.RESTART_ALT_ROUNDED)
    chat_status = ft.Text("", size=12, color=ft.Colors.ON_SURFACE_VARIANT)

    chat_section = ft.Container(
        content=ft.Column(
            [
                ft.Row(
                    [
                        ft.Text("후속 질문", size=15, weight=ft.FontWeight.BOLD),
                        ft.Container(expand=True),
                        chat_status,
                        clear_chat_btn,
                    ]
                ),
                chat_history_container,
                ft.Row(
                    [question_field, send_btn],
                    vertical_alignment=ft.CrossAxisAlignment.END,
                ),
            ],
            spacing=10,
        ),
        padding=ft.padding.only(top=8),
        visible=False,
    )

    # ============================================================
    # UI helpers
    # ============================================================
    def _ui(fn):
        try:
            fn()
            page.update()
        except Exception:  # noqa: BLE001
            traceback.print_exc()

    def _set_running(running: bool, analyzing: bool = False) -> None:
        """`analyzing=True` while Claude is the active step (so we update the
        analyze button label rather than the extract button label)."""
        def _do():
            state["running"] = running
            progress_bar.visible = running
            url_field.disabled = running
            project_name_field.disabled = running
            start_seconds_field.disabled = running
            max_duration_field.disabled = running
            max_frames_field.disabled = running
            fmt_dropdown.disabled = running
            sample_interval_field.disabled = running
            phash_threshold_field.disabled = running
            quality_filter_switch.disabled = running
            open_btn.disabled = running
            new_btn.disabled = running
            run_btn.disabled = running
            analyze_btn.disabled = running
            if analyzing:
                analyze_btn.text = "분석 중..." if running else "AI 분석 실행"
                run_btn.text = "프레임 추출"
            else:
                run_btn.text = "추출 중..." if running else (
                    "다시 추출" if state["current_result"] else "프레임 추출"
                )
                analyze_btn.text = "AI 분석 실행"
        _ui(_do)

    def _set_chatting(chatting: bool) -> None:
        def _do():
            state["chatting"] = chatting
            send_btn.disabled = chatting
            send_btn.text = "전송 중..." if chatting else "질문 전송"
            question_field.disabled = chatting
            chat_status.value = "응답 받는 중..." if chatting else ""
        _ui(_do)

    def _set_progress(status: str, pct: float | None) -> None:
        def _do():
            progress_text.value = status
            progress_bar.value = pct
        _ui(_do)

    def _toast(msg: str) -> None:
        def _do():
            page.open(ft.SnackBar(ft.Text(msg), open=True))
        _ui(_do)

    def _create_frame_tile(f: FrameInfo) -> ft.Control:
        ts = f.timestamp
        label = f"#{f.index}" + (f"  ·  {ts:.1f}s" if ts is not None else "")
        if f.is_supplemented:
            label += "  (보충)"

        delete_btn = ft.IconButton(
            icon=ft.Icons.CLOSE_ROUNDED,
            icon_size=14,
            icon_color=ft.Colors.ON_ERROR,
            bgcolor=ft.Colors.ERROR,
            style=ft.ButtonStyle(
                shape=ft.CircleBorder(),
                padding=ft.padding.all(0),
            ),
            width=24,
            height=24,
            tooltip="이 프레임 삭제",
            on_click=lambda e, idx=f.index: _delete_frame(idx),
        )

        image_stack = ft.Stack(
            controls=[
                ft.Container(
                    content=ft.Image(
                        src=f.path,
                        fit=ft.ImageFit.COVER,
                        width=200,
                        height=112,
                        border_radius=6,
                    ),
                    border_radius=6,
                    ink=True,
                    on_click=lambda e, p=f.path: _open_in_explorer(p),
                ),
                ft.Container(
                    content=delete_btn,
                    alignment=ft.alignment.top_right,
                    padding=ft.padding.all(2),
                ),
            ],
            width=200,
            height=112,
        )

        return ft.Container(
            content=ft.Column(
                [
                    image_stack,
                    ft.Text(
                        label,
                        size=11,
                        color=ft.Colors.ON_SURFACE_VARIANT,
                    ),
                ],
                spacing=4,
            ),
            padding=4,
            border_radius=8,
        )

    def _delete_frame(index: int) -> None:
        result = state["current_result"]
        if not result:
            return
        target = next((f for f in result.frames if f.index == index), None)
        if not target:
            return

        try:
            Path(target.path).unlink()
        except OSError:
            pass

        result.frames = [f for f in result.frames if f.index != index]

        # Rewrite manifest.json so a re-load reflects the deletion
        try:
            manifest_path = Path(result.out_dir) / "manifest.json"
            manifest_path.write_text(
                json.dumps(asdict(result), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001
            traceback.print_exc()

        _populate_frames(result)
        _persist()

        if state.get("report_text"):
            _toast(
                f"프레임 #{index} 삭제됨 — 분석 결과는 옛 프레임 기준입니다. "
                f"다시 분석하려면 'AI 분석 실행'을 클릭하세요."
            )
        else:
            _toast(f"프레임 #{index} 삭제됨")

    def _populate_frames(result: ExtractResult) -> None:
        def _do():
            frames_grid.controls.clear()
            for f in result.frames:
                frames_grid.controls.append(_create_frame_tile(f))
            frames_count_label.value = f"{len(result.frames)} 장"
            frames_section.visible = bool(result.frames)
        _ui(_do)

    def _make_message_bubble(role: str, text: str) -> ft.Control:
        if role == "user":
            return ft.Row(
                [
                    ft.Container(expand=True),
                    ft.Container(
                        content=ft.Text(text, selectable=True, color=ft.Colors.ON_PRIMARY),
                        bgcolor=ft.Colors.PRIMARY,
                        padding=ft.padding.symmetric(horizontal=14, vertical=10),
                        border_radius=12,
                        width=600,
                    ),
                ]
            )
        return ft.Row(
            [
                ft.Container(
                    content=ft.Markdown(
                        value=text,
                        selectable=True,
                        extension_set=ft.MarkdownExtensionSet.GITHUB_WEB,
                        on_tap_link=lambda e: page.launch_url(e.data),
                    ),
                    bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
                    padding=ft.padding.symmetric(horizontal=14, vertical=10),
                    border_radius=12,
                    expand=True,
                ),
            ]
        )

    def _append_chat_local(role: str, text: str) -> None:
        """Append to chat (UI + state) without persistence."""
        def _do():
            state["chat_history"].append(ChatMessage(role=role, text=text))
            chat_history_column.controls.append(_make_message_bubble(role, text))
        _ui(_do)

    def _refresh_header_path() -> None:
        def _do():
            pd = state["project_dir"]
            header_path_label.value = f"{pd}" if pd else ""
        _ui(_do)

    def _show_report_buttons(visible: bool) -> None:
        def _do():
            save_btn.visible = visible
            open_folder_btn.visible = visible
            copy_btn.visible = visible
            rename_btn.visible = visible
            export_pdf_btn.visible = visible
        _ui(_do)

    # ============================================================
    # Project save / load
    # ============================================================
    def _build_project() -> Project:
        return Project(
            name=Path(state["project_dir"]).name if state["project_dir"] else "",
            url=(url_field.value or "").strip(),
            video_id=state["video_id"],
            video_title=state["video_title"],
            session_id=state["session_id"],
            frame_count=len(state["current_result"].frames) if state["current_result"] else 0,
            project_dir=str(state["project_dir"]) if state["project_dir"] else "",
            report_text=state["report_text"],
            chat_history=list(state["chat_history"]),
        )

    def _persist() -> None:
        if not state["project_dir"]:
            return
        try:
            save_project(_build_project())
        except Exception:  # noqa: BLE001
            traceback.print_exc()

    def _clear_for_new_project() -> None:
        def _do():
            url_field.value = ""
            project_name_field.value = ""
            state["current_result"] = None
            state["report_text"] = ""
            state["session_id"] = None
            state["chat_history"] = []
            state["project_dir"] = None
            state["video_id"] = ""
            state["video_title"] = ""
            frames_grid.controls.clear()
            frames_section.visible = False
            chat_history_column.controls.clear()
            chat_section.visible = False
            report_md.value = PLACEHOLDER_REPORT
            save_btn.visible = False
            open_folder_btn.visible = False
            copy_btn.visible = False
            rename_btn.visible = False
            export_pdf_btn.visible = False
            analyze_btn.visible = False
            analyze_btn.text = "AI 분석 실행"
            header_path_label.value = ""
            progress_text.value = "대기 중"
            progress_bar.value = None
            run_btn.text = "프레임 추출"
        _ui(_do)

    def _load_project_dir(project_dir: str) -> None:
        path = Path(project_dir)
        try:
            proj = load_project(path)
        except FileNotFoundError as e:
            _toast(str(e))
            return
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            _toast(f"프로젝트 로드 실패: {e}")
            return

        result = _frames_from_manifest(path)

        def _apply():
            state["project_dir"] = path.resolve()
            state["video_id"] = proj.video_id
            state["video_title"] = proj.video_title
            state["report_text"] = proj.report_text
            state["session_id"] = proj.session_id
            state["chat_history"] = list(proj.chat_history)
            state["current_result"] = result

            url_field.value = proj.url
            project_name_field.value = proj.name

            # frames
            frames_grid.controls.clear()
            if result and result.frames:
                for f in result.frames:
                    frames_grid.controls.append(_create_frame_tile(f))
                frames_count_label.value = f"{len(result.frames)} 장"
                frames_section.visible = True
            else:
                frames_section.visible = False

            # report
            report_md.value = proj.report_text or PLACEHOLDER_REPORT
            save_btn.visible = bool(proj.report_text)
            open_folder_btn.visible = True
            copy_btn.visible = bool(proj.report_text)
            rename_btn.visible = True
            export_pdf_btn.visible = bool(proj.report_text)

            # analyze button — visible whenever we have frames; allow re-running
            analyze_btn.visible = bool(result and result.frames)
            analyze_btn.text = "다시 분석" if proj.report_text else "AI 분석 실행"
            run_btn.text = "다시 추출" if (result and result.frames) else "프레임 추출"

            # chat
            chat_history_column.controls.clear()
            for m in proj.chat_history:
                chat_history_column.controls.append(_make_message_bubble(m.role, m.text))
            chat_section.visible = bool(proj.session_id) or bool(proj.chat_history)
            if proj.session_id:
                chat_status.value = ""
            else:
                chat_status.value = "세션 정보 없음 — 후속 질문 불가"

            header_path_label.value = str(path)
            progress_text.value = f"프로젝트 로드 완료: {proj.name}"
            progress_bar.value = 1.0

        _ui(_apply)

    # ============================================================
    # Button handlers
    # ============================================================
    def _on_save(_e: ft.ControlEvent) -> None:
        if not state["project_dir"]:
            _toast("아직 저장된 프로젝트가 없습니다")
            return
        _persist()
        _toast(f"저장됨: {state['project_dir']}")

    def _on_open_folder(_e: ft.ControlEvent) -> None:
        if state["project_dir"]:
            _open_in_explorer(state["project_dir"])

    def _on_copy(_e: ft.ControlEvent) -> None:
        if not state["report_text"]:
            return
        chat = state["chat_history"]
        text = state["report_text"]
        if chat:
            text += "\n\n---\n\n## 후속 질문 (Q&A)\n"
            for m in chat:
                if m.role == "user":
                    text += f"\n**Q.** {m.text}\n"
                else:
                    text += f"\n{m.text}\n"
        page.set_clipboard(text)
        _toast("리포트가 클립보드에 복사되었습니다")

    def _on_clear_chat(_e: ft.ControlEvent) -> None:
        def _do():
            state["chat_history"] = []
            chat_history_column.controls.clear()
            chat_status.value = "대화 초기화됨"
        _ui(_do)
        _persist()

    def _on_rename(_e: ft.ControlEvent) -> None:
        if not state["project_dir"]:
            return
        old = Path(state["project_dir"])
        requested = (project_name_field.value or "").strip()
        if not requested:
            _toast("새 이름을 '프로젝트 이름' 칸에 입력해주세요")
            return
        sanitized = sanitize_name(requested)
        if sanitized == old.name:
            return
        new_path = resolve_unique_dir(old.parent, sanitized)
        try:
            old.rename(new_path)
        except OSError as e:
            _toast(f"이름 변경 실패: {e}")
            return
        state["project_dir"] = new_path.resolve()
        # frame paths in manifest still point to old path; rewrite frames + manifest
        result = _frames_from_manifest(new_path)
        if result:
            state["current_result"] = result
            for fi in result.frames:
                # paths in old manifest reference the renamed dir's old absolute path
                old_str = str(old.resolve())
                new_str = str(new_path.resolve())
                fi.path = fi.path.replace(old_str, new_str)
            (new_path / "manifest.json").write_text(
                json.dumps(
                    {
                        "url": result.url,
                        "video_id": result.video_id,
                        "title": result.title,
                        "duration": result.duration,
                        "out_dir": str(new_path.resolve()),
                        "frames": [
                            {
                                "index": f.index,
                                "path": f.path,
                                "timestamp": f.timestamp,
                                "is_supplemented": f.is_supplemented,
                            }
                            for f in result.frames
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        _persist()
        header_path_label.value = str(new_path)
        page.update()
        _toast(f"이름 변경됨: {new_path.name}")

    def _on_pick_directory(e: ft.FilePickerResultEvent) -> None:
        if e.path:
            if not is_project_dir(e.path):
                _toast("project.json이 없는 폴더입니다")
                return
            _load_project_dir(e.path)

    def _generate_pdf_worker(out_path: str) -> None:
        try:
            _set_progress("PDF 생성 중...", None)
            proj = _build_project()
            result_path = generate_pdf(proj, out_path)
            _toast(f"PDF 저장됨: {result_path}")
            _set_progress(f"PDF 저장 완료: {result_path.name}", 1.0)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            _toast(f"PDF 생성 실패: {e}")
            _set_progress(f"PDF 실패: {e}", None)

    def _on_pdf_picked(e: ft.FilePickerResultEvent) -> None:
        if not e.path:
            return
        path = e.path
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        threading.Thread(target=_generate_pdf_worker, args=(path,), daemon=True).start()

    def _on_export_pdf(_e: ft.ControlEvent) -> None:
        if state["running"]:
            return
        if not state["report_text"]:
            _toast("아직 분석 결과가 없습니다")
            return
        if not state["project_dir"]:
            _toast("프로젝트가 없습니다")
            return
        default_name = f"{Path(state['project_dir']).name}.pdf"
        try:
            pdf_picker.save_file(
                dialog_title="PDF로 저장",
                file_name=default_name,
                allowed_extensions=["pdf"],
                initial_directory=str(Path(state["project_dir"]).parent),
            )
        except Exception as e:  # noqa: BLE001
            _toast(f"PDF 저장 다이얼로그 열기 실패: {e}")

    def _on_open_btn(_e: ft.ControlEvent) -> None:
        DEFAULT_OUT.mkdir(parents=True, exist_ok=True)
        file_picker.get_directory_path(
            dialog_title="ytga 프로젝트 폴더 선택",
            initial_directory=str(DEFAULT_OUT),
        )

    def _on_new_btn(_e: ft.ControlEvent) -> None:
        if state["running"] or state["chatting"]:
            return
        _clear_for_new_project()

    file_picker.on_result = _on_pick_directory
    pdf_picker.on_result = _on_pdf_picked
    save_btn.on_click = _on_save
    open_folder_btn.on_click = _on_open_folder
    copy_btn.on_click = _on_copy
    rename_btn.on_click = _on_rename
    export_pdf_btn.on_click = _on_export_pdf
    clear_chat_btn.on_click = _on_clear_chat
    open_btn.on_click = _on_open_btn
    new_btn.on_click = _on_new_btn

    # ============================================================
    # Worker threads
    # ============================================================
    def _decide_project_dir(requested_name: str, video_title: str, video_id: str) -> Path:
        """Reuse the current project folder if the user kept the same name;
        otherwise create a fresh `name_2` etc. so re-extracts can either
        overwrite (default) or branch (by typing a new name)."""
        DEFAULT_OUT.mkdir(parents=True, exist_ok=True)
        if requested_name.strip():
            base = sanitize_name(requested_name)
        elif video_title:
            base = sanitize_name(video_title)
        else:
            base = sanitize_name(video_id)
        current = state["project_dir"]
        if current and Path(current).name == base:
            return Path(current)
        return resolve_unique_dir(DEFAULT_OUT, base)

    def _extract_worker(
        url: str,
        project_name: str,
        start_sec: float,
        max_dur: int,
        max_fr: int,
        image_format: str,
        sample_int: float,
        phash_thresh: int,
        quality_on: bool,
    ) -> None:
        try:
            _set_running(True, analyzing=False)

            # 1. Peek metadata (cheap, ~1-3s)
            _set_progress("메타데이터 조회 중...", None)
            try:
                meta = peek_metadata(url)
            except Exception as e:  # noqa: BLE001
                meta = {"id": _video_id_from_url(url), "title": "", "duration": 0}
                _toast(f"메타데이터 조회 실패: {e}")

            video_id = meta.get("id") or _video_id_from_url(url)
            video_title = meta.get("title") or video_id

            # 2. Decide project folder (reuse if same name)
            project_dir = _decide_project_dir(project_name, video_title, video_id)
            is_new = project_dir.name != Path(state["project_dir"]).name if state["project_dir"] else True

            state["video_id"] = video_id
            state["video_title"] = video_title
            state["project_dir"] = project_dir.resolve()
            _refresh_header_path()
            _ui(lambda: setattr(project_name_field, "value", project_dir.name))

            # If we're branching to a new folder, reset the analysis state
            if is_new:
                state["report_text"] = ""
                state["session_id"] = None
                state["chat_history"] = []
                def _reset_report():
                    report_md.value = PLACEHOLDER_REPORT
                    chat_history_column.controls.clear()
                    chat_section.visible = False
                    _show_report_buttons(False)
                _ui(_reset_report)

            # 3. Extract
            result = extract(
                url,
                out_dir=project_dir,
                start_seconds=start_sec,
                max_duration=max_dur,
                max_frames=max_fr,
                sample_interval=sample_int,
                phash_threshold=phash_thresh,
                quality_filter=quality_on,
                image_format=image_format,
                on_progress=_set_progress,
            )
            state["current_result"] = result
            _populate_frames(result)

            # Show analyze button now that frames are ready (and report is empty)
            def _ready_to_analyze():
                analyze_btn.visible = True
                if state["report_text"]:
                    chat_status.value = (
                        "프레임이 새로 추출됨 — '다시 분석'을 누르면 새 프레임으로 재분석됩니다."
                    )
            _ui(_ready_to_analyze)

            _persist()  # save project.json so the folder is identifiable even before analysis

            _set_progress(
                f"추출 완료. 프레임 {len(result.frames)}장. 마음에 들면 'AI 분석 실행'을 누르세요.",
                1.0,
            )
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            _toast(f"추출 오류: {e}")
            _set_progress(f"추출 실패: {e}", None)
        finally:
            _set_running(False, analyzing=False)

    def _analyze_only_worker() -> None:
        result = state["current_result"]
        if not result:
            _toast("먼저 프레임을 추출해주세요")
            return
        try:
            _set_running(True, analyzing=True)
            _set_progress("Claude 분석 시작...", None)

            analysis = analyze(
                [f.path for f in result.frames],
                video_url=result.url,
                video_title=result.title,
                on_progress=_set_progress,
            )

            state["report_text"] = analysis.text
            state["session_id"] = analysis.session_id
            state["chat_history"] = []

            def _show_report():
                report_md.value = analysis.text
                _show_report_buttons(True)
                chat_history_column.controls.clear()
                chat_section.visible = bool(analysis.session_id)
                if not analysis.session_id:
                    chat_status.value = "session_id를 받지 못해 후속 질문을 사용할 수 없습니다."
                else:
                    chat_status.value = ""
                # After successful analysis, allow re-running with new label
                analyze_btn.text = "다시 분석"
            _ui(_show_report)

            _persist()
            _set_progress(
                f"분석 완료. 프레임 {len(result.frames)}장 중 {analysis.frames_read}장 읽음.",
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
            _set_running(False, analyzing=True)

    def _follow_up_worker(question: str) -> None:
        try:
            _set_chatting(True)
            sid = state["session_id"]
            if not sid:
                _toast("세션이 없습니다. 분석을 먼저 실행해주세요.")
                return

            response = follow_up(
                sid,
                question,
                on_progress=lambda s, p: None,
            )
            if response.session_id:
                state["session_id"] = response.session_id

            _append_chat_local("assistant", response.text)
            _persist()

        except ClaudeRunError as e:
            _toast(f"질문 실패 (code {e.returncode})")
            _append_chat_local("assistant", f"_(질문 실패: {e})_")
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            _toast(f"오류: {e}")
            _append_chat_local("assistant", f"_(오류: {e})_")
        finally:
            _set_chatting(False)

    # ============================================================
    # Top-level handlers
    # ============================================================
    def _on_run(_e: ft.ControlEvent) -> None:
        if state["running"]:
            return
        url = (url_field.value or "").strip()
        if not url:
            _toast("URL을 입력해주세요")
            return
        try:
            sstart = float(start_seconds_field.value or "0")
            mdur = int(max_duration_field.value or "600")
            mfr = int(max_frames_field.value or "30")
            sthr = float(sample_interval_field.value or "1.0")
            pthr = int(phash_threshold_field.value or "12")
        except ValueError:
            _toast("숫자 칸에는 숫자만 입력해주세요")
            return
        threading.Thread(
            target=_extract_worker,
            args=(
                url,
                project_name_field.value or "",
                sstart,
                mdur,
                mfr,
                fmt_dropdown.value or "jpg",
                sthr,
                pthr,
                bool(quality_filter_switch.value),
            ),
            daemon=True,
        ).start()

    def _on_analyze(_e: ft.ControlEvent) -> None:
        if state["running"]:
            return
        if not state["current_result"]:
            _toast("먼저 프레임을 추출해주세요")
            return
        threading.Thread(target=_analyze_only_worker, daemon=True).start()

    def _on_send_question(_e: ft.ControlEvent | None = None) -> None:
        if state["chatting"]:
            return
        if not state["session_id"]:
            _toast("분석을 먼저 실행해주세요 (또는 세션이 만료된 프로젝트입니다)")
            return
        question = (question_field.value or "").strip()
        if not question:
            return
        _append_chat_local("user", question)
        _persist()
        question_field.value = ""
        page.update()
        threading.Thread(
            target=_follow_up_worker, args=(question,), daemon=True
        ).start()

    run_btn.on_click = _on_run
    url_field.on_submit = _on_run
    analyze_btn.on_click = _on_analyze
    send_btn.on_click = _on_send_question
    question_field.on_submit = _on_send_question

    # ============================================================
    # Layout
    # ============================================================
    page.add(
        ft.Column(
            [
                ft.Row(
                    [
                        ft.Text("ytga", size=30, weight=ft.FontWeight.BOLD),
                        ft.Container(width=12),
                        ft.Column(
                            [
                                ft.Text(
                                    "YouTube 게임플레이 영상 → 그래픽스 기법 분석",
                                    size=13,
                                    color=ft.Colors.ON_SURFACE_VARIANT,
                                ),
                                header_path_label,
                            ],
                            spacing=2,
                            tight=True,
                        ),
                        ft.Container(expand=True),
                        open_btn,
                        new_btn,
                    ],
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                ft.Divider(height=1),
                ft.Container(
                    content=ft.Column(
                        [
                            ft.Row(
                                [url_field, project_name_field, run_btn],
                                vertical_alignment=ft.CrossAxisAlignment.END,
                            ),
                            ft.Row(
                                [
                                    start_seconds_field,
                                    max_duration_field,
                                    max_frames_field,
                                    sample_interval_field,
                                    phash_threshold_field,
                                    fmt_dropdown,
                                    quality_filter_switch,
                                ],
                                vertical_alignment=ft.CrossAxisAlignment.END,
                                spacing=10,
                            ),
                            ft.Row([progress_bar]),
                            ft.Row([progress_text, ft.Container(expand=True)]),
                        ],
                        spacing=10,
                    ),
                    padding=ft.padding.symmetric(vertical=8),
                ),
                frames_section,
                report_section,
                chat_section,
            ],
            spacing=12,
            scroll=ft.ScrollMode.AUTO,
            expand=True,
        )
    )


def main() -> None:
    ft.app(target=_build_ui)


if __name__ == "__main__":
    main()
