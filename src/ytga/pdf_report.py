"""Generate a PDF report (cover + analysis + frame gallery + Q&A) from a Project."""

from __future__ import annotations

import html
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    HRFlowable,
    Image,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from .project import Project


# ---------------------------------------------------------------
# Korean font discovery
# ---------------------------------------------------------------
def _register_korean_font() -> tuple[str, str]:
    """Locate a TTF that can render Korean text. Returns (regular, bold) names.

    Falls back to Helvetica (which can't render Korean) if nothing is found —
    the PDF will still build but Korean glyphs will appear as boxes.
    """
    candidates = []
    if os.name == "nt":
        candidates += [
            (r"C:\Windows\Fonts\malgun.ttf", r"C:\Windows\Fonts\malgunbd.ttf"),
            (r"C:\Windows\Fonts\NanumGothic.ttf", r"C:\Windows\Fonts\NanumGothicBold.ttf"),
        ]
    elif sys.platform == "darwin":
        candidates += [
            ("/System/Library/Fonts/AppleSDGothicNeo.ttc", "/System/Library/Fonts/AppleSDGothicNeo.ttc"),
            ("/Library/Fonts/AppleGothic.ttf", "/Library/Fonts/AppleGothic.ttf"),
        ]
    else:
        candidates += [
            ("/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
             "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf"),
            ("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
             "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc"),
        ]

    for reg_path, bold_path in candidates:
        if os.path.exists(reg_path):
            try:
                pdfmetrics.registerFont(TTFont("YtgaKorean", reg_path))
                if os.path.exists(bold_path) and bold_path != reg_path:
                    pdfmetrics.registerFont(TTFont("YtgaKoreanBold", bold_path))
                    return ("YtgaKorean", "YtgaKoreanBold")
                return ("YtgaKorean", "YtgaKorean")
            except Exception:
                continue

    return ("Helvetica", "Helvetica-Bold")


# ---------------------------------------------------------------
# Markdown → reportlab paragraphs
# ---------------------------------------------------------------
def _inline_md(text: str) -> str:
    """Inline markdown → reportlab mini-HTML.

    Order matters: escape first so user text never breaks the parser, then
    swap the markdown markers for <b>/<i> tags.
    """
    text = html.escape(text, quote=False)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"`([^`]+)`", r'<font face="Courier">\1</font>', text)
    text = re.sub(r"(?<![*\w])\*(?!\s)([^*]+?)(?<!\s)\*(?![*\w])", r"<i>\1</i>", text)
    return text


def _markdown_to_flowables(md: str, styles: dict) -> list:
    flowables: list = []
    for raw in (md or "").splitlines():
        line = raw.rstrip()
        if not line:
            flowables.append(Spacer(1, 6))
            continue
        if line.startswith("### "):
            flowables.append(Paragraph(_inline_md(line[4:]), styles["h3"]))
        elif line.startswith("## "):
            flowables.append(Paragraph(_inline_md(line[3:]), styles["h2"]))
        elif line.startswith("# "):
            flowables.append(Paragraph(_inline_md(line[2:]), styles["h1"]))
        elif line.startswith("- ") or line.startswith("* "):
            flowables.append(Paragraph("• " + _inline_md(line[2:]), styles["bullet"]))
        elif re.match(r"^\d+\.\s", line):
            flowables.append(Paragraph(_inline_md(line), styles["bullet"]))
        elif line.strip() == "---":
            flowables.append(Spacer(1, 6))
            flowables.append(HRFlowable(width="100%", thickness=0.5, color=HexColor("#888888")))
            flowables.append(Spacer(1, 6))
        else:
            flowables.append(Paragraph(_inline_md(line), styles["body"]))
    return flowables


# ---------------------------------------------------------------
# Frame gallery
# ---------------------------------------------------------------
def _build_frame_gallery(project: Project, styles: dict) -> list:
    """2-column gallery: thumbnail on left/right, caption underneath."""
    if not project.chat_history and not project.report_text and not project.frame_count:
        return []

    rows: list = []
    pair: list = []

    img_w = 8.5 * cm
    img_h = img_w * 9 / 16  # 16:9

    # Use Project's own frames — but we need them. They're in manifest.json,
    # which the GUI loaded into Project before calling us.
    # Fall through to manifest reload if not present.
    frames = _load_frames_for_project(project)
    if not frames:
        return []

    for f in frames:
        img_path = Path(f["path"])
        if not img_path.exists():
            continue
        ts = f.get("timestamp")
        idx = f.get("index", "?")
        ts_text = f"  ·  {ts:.1f}s" if isinstance(ts, (int, float)) else ""
        suppl = "  (보충)" if f.get("is_supplemented") else ""
        caption_text = f"<b>#{idx}</b>{ts_text}{suppl}"

        cell = [
            Image(str(img_path), width=img_w, height=img_h),
            Spacer(1, 2),
            Paragraph(caption_text, styles["caption"]),
        ]
        pair.append(cell)
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        pair.append("")  # pad to 2 columns
        rows.append(pair)

    if not rows:
        return []

    table = Table(rows, colWidths=[img_w + 0.3 * cm, img_w + 0.3 * cm])
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    return [table]


def _load_frames_for_project(project: Project) -> list[dict]:
    """Read manifest.json from the project folder."""
    if not project.project_dir:
        return []
    manifest = Path(project.project_dir) / "manifest.json"
    if not manifest.exists():
        return []
    import json

    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        return data.get("frames", [])
    except Exception:  # noqa: BLE001
        return []


# ---------------------------------------------------------------
# Q&A
# ---------------------------------------------------------------
def _build_chat_section(project: Project, styles: dict) -> list:
    if not project.chat_history:
        return []
    flowables: list = [
        Paragraph("후속 질문", styles["h2"]),
        Spacer(1, 6),
    ]
    for msg in project.chat_history:
        if msg.role == "user":
            flowables.append(Paragraph(f"<b>Q.</b> {_inline_md(msg.text)}", styles["question"]))
            flowables.append(Spacer(1, 4))
        else:
            flowables += _markdown_to_flowables(msg.text, styles)
            flowables.append(Spacer(1, 10))
    return flowables


# ---------------------------------------------------------------
# Public API
# ---------------------------------------------------------------
def generate_pdf(project: Project, output_path: str | Path) -> Path:
    """Render the project to a PDF. Returns the path written."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    reg, bold = _register_korean_font()

    base = ParagraphStyle(
        name="base",
        fontName=reg,
        fontSize=10.5,
        leading=15,
        textColor=HexColor("#222222"),
    )
    styles = {
        "title": ParagraphStyle(
            "title", parent=base, fontName=bold, fontSize=24, leading=30,
            textColor=HexColor("#111111"),
        ),
        "subtitle": ParagraphStyle(
            "subtitle", parent=base, fontSize=12, leading=18,
            textColor=HexColor("#555555"),
        ),
        "meta": ParagraphStyle(
            "meta", parent=base, fontSize=10, leading=14,
            textColor=HexColor("#666666"),
        ),
        "h1": ParagraphStyle(
            "h1", parent=base, fontName=bold, fontSize=18, leading=24,
            spaceBefore=12, spaceAfter=8, textColor=HexColor("#111111"),
        ),
        "h2": ParagraphStyle(
            "h2", parent=base, fontName=bold, fontSize=15, leading=20,
            spaceBefore=10, spaceAfter=6, textColor=HexColor("#111111"),
        ),
        "h3": ParagraphStyle(
            "h3", parent=base, fontName=bold, fontSize=12.5, leading=18,
            spaceBefore=8, spaceAfter=4, textColor=HexColor("#222222"),
        ),
        "body": ParagraphStyle(
            "body", parent=base, fontSize=10.5, leading=16,
        ),
        "bullet": ParagraphStyle(
            "bullet", parent=base, fontSize=10.5, leading=15, leftIndent=14,
        ),
        "caption": ParagraphStyle(
            "caption", parent=base, fontSize=9.5, leading=12, alignment=1,
            textColor=HexColor("#444444"),
        ),
        "question": ParagraphStyle(
            "question", parent=base, fontName=bold, fontSize=11, leading=16,
            textColor=HexColor("#0a4a90"), spaceBefore=8,
        ),
    }

    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=A4,
        topMargin=2 * cm,
        bottomMargin=2 * cm,
        leftMargin=2 * cm,
        rightMargin=2 * cm,
        title=f"ytga – {project.video_title or project.name}",
        author="ytga",
    )

    story: list = []

    # ---- Cover ----
    story.append(Spacer(1, 4 * cm))
    story.append(Paragraph(html.escape(project.video_title or project.name or "(제목 없음)"), styles["title"]))
    story.append(Spacer(1, 0.6 * cm))
    story.append(Paragraph("YouTube 게임플레이 그래픽스 분석", styles["subtitle"]))
    story.append(Spacer(1, 1.5 * cm))

    meta_rows = [
        ("프로젝트", project.name or "-"),
        ("URL", project.url or "-"),
        ("생성일", datetime.now().strftime("%Y-%m-%d %H:%M")),
        ("프레임 수", str(project.frame_count)),
    ]
    if project.chat_history:
        qcount = sum(1 for m in project.chat_history if m.role == "user")
        meta_rows.append(("후속 질문", f"{qcount}개"))

    meta_table = Table(
        [[Paragraph(f"<b>{k}</b>", styles["meta"]),
          Paragraph(html.escape(str(v)), styles["meta"])] for k, v in meta_rows],
        colWidths=[3 * cm, 12 * cm],
    )
    meta_table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    story.append(meta_table)

    story.append(PageBreak())

    # ---- Analysis ----
    if project.report_text:
        story += _markdown_to_flowables(project.report_text, styles)
    else:
        story.append(Paragraph("(분석 리포트 없음)", styles["body"]))

    # ---- Frames Gallery ----
    gallery = _build_frame_gallery(project, styles)
    if gallery:
        story.append(PageBreak())
        story.append(Paragraph("프레임 갤러리", styles["h1"]))
        story.append(Spacer(1, 6))
        story += gallery

    # ---- Q&A ----
    chat = _build_chat_section(project, styles)
    if chat:
        story.append(PageBreak())
        story += chat

    doc.build(story)
    return output_path
