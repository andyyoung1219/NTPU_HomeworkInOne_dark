"""Local deterministic Markdown -> embedded CJK PDF -> independent validation."""

import hashlib
import html
import io
import re
import threading
from pathlib import Path

import pymupdf
from fontTools.ttLib import TTFont as FontToolsFont
from fontTools.varLib.instancer import instantiateVariableFont
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    Image,
    LongTable,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    TableStyle,
)
from reportlab.platypus.doctemplate import LayoutError

from backend.p0.evidence import ROOT
from backend.p0.moodle import probe

_font_lock = threading.Lock()
_render_lock = threading.Lock()
FONT_DIR = ROOT / ".local/fonts"
PAGE_SIZES = {"portrait": A4, "landscape": landscape(A4)}
SIDE_MARGIN, TOP_MARGIN, BOTTOM_MARGIN, FRAME_PADDING = 50, 46, 48, 6
LANDSCAPE_COLUMNS = 12


def prepare_font():
    with _font_lock:
        target = FONT_DIR / "NotoSansTC-Regular.ttf"
        if not target.exists():
            source = FONT_DIR / "NotoSansTC-Variable.ttf"
            if not source.exists():
                raise probe.ProbeError("PDF_FONT_NOT_INSTALLED")
            font = FontToolsFont(source)
            instantiateVariableFont(font, {"wght": 400}, inplace=True).save(target)
            font.close()
        if "NotoTC" not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont("NotoTC", str(target)))
        return target


def clean_inline(text):
    text = re.sub(r"^>\s?", "", text)
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"[圖片待補：\1]", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", text)
    text = text.replace("**", "").replace("`", "")
    return html.escape(text)


def render_pdf(markdown, output, images=None):
    with _render_lock:
        return _render_pdf(markdown, output, images or {})


def report_table(rows, columns, orientation, style):
    width = PAGE_SIZES[orientation][0] - 2 * (SIDE_MARGIN + FRAME_PADDING)
    content = [[Paragraph(clean_inline(row[c] if c < len(row) else ""), style)
                for c in columns] for row in rows]
    return LongTable(content, colWidths=[width / len(columns)] * len(columns), repeatRows=1,
                     splitByRow=1, splitInRow=1, hAlign="LEFT", spaceAfter=10, style=TableStyle([
                         ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8f2ef")),
                         ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#c5d5d0")),
                         ("VALIGN", (0, 0), (-1, -1), "TOP"),
                         ("LEFTPADDING", (0, 0), (-1, -1), 7),
                         ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                         ("TOPPADDING", (0, 0), (-1, -1), 6),
                         ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                     ]))


def add_section(sections, orientation, flowables):
    while flowables and isinstance(flowables[-1], Spacer):
        flowables.pop()
    if flowables:
        if sections and sections[-1][0] == orientation:
            sections[-1][1].extend(flowables)
        else:
            sections.append((orientation, flowables))


def _render_pdf(markdown, output, images):
    font_path = prepare_font()
    with FontToolsFont(font_path) as font:
        supported = font.getBestCmap()
    missing = {ord(char) for char in markdown if not char.isspace() and ord(char) not in supported}
    if missing:
        raise probe.ProbeError("PDF_FONT_MISSING_GLYPHS")
    style = ParagraphStyle("Body", fontName="NotoTC", fontSize=10, leading=17,
                           spaceAfter=8, wordWrap="CJK", alignment=TA_LEFT)
    headings = {i: ParagraphStyle(f"H{i}", parent=style, fontSize={1: 21, 2: 14, 3: 11.5}[i],
                                  leading={1: 29, 2: 22, 3: 19}[i], spaceBefore=12,
                                  # Reserve one glyph for CJK hanging punctuation
                                  # so large headings remain inside page bounds.
                                  rightIndent={1: 21, 2: 14, 3: 11.5}[i],
                                  spaceAfter=9, keepWithNext=True) for i in (1, 2, 3)}
    cell_style = ParagraphStyle("Cell", parent=style, fontSize=9, leading=14, spaceAfter=0)
    caption_style = ParagraphStyle("TableCaption", parent=cell_style, spaceAfter=5, keepWithNext=True)
    code_style = ParagraphStyle("Code", parent=style, fontSize=9, leading=13,
                                spaceAfter=0, wordWrap=None, leftIndent=8)
    sections, story, lines = [], [], markdown.splitlines()
    index, in_code = 0, False
    while index < len(lines):
        line = lines[index].rstrip()
        picture = re.fullmatch(r"!\[([^\]]*)\]\(([^)]+)\)", line)
        if not in_code and picture and picture[2] in images:
            # Only caller-verified artifacts; Markdown cannot request local/remote reads.
            asset = images[picture[2]]
            chart = Image(io.BytesIO(asset) if isinstance(asset, bytes) else str(asset))
            chart.drawHeight *= min(1, (A4[0] - 100) / chart.drawWidth, 300 / chart.drawHeight)
            chart.drawWidth = chart.drawHeight * chart.imageWidth / chart.imageHeight
            story.extend([chart, Paragraph(clean_inline(picture[1]), cell_style), Spacer(1, 12)])
            index += 1
            continue
        if line.startswith("```"):
            in_code = not in_code
            index += 1
            continue
        if in_code:
            # Preserve indentation and literal operators/markup in program text.
            # Normal prose cleanup would remove ** and backticks from source code.
            if line:
                literal = html.escape(line.expandtabs(4)).replace(" ", "&#160;")
                story.append(Paragraph(literal, code_style))
            else:
                story.append(Spacer(1, code_style.leading))
            index += 1
            continue
        if not in_code and line.startswith("|") and index + 1 < len(lines) and re.match(r"^\|?[\s:|-]+$", lines[index + 1]):
            table_lines = [line]
            index += 2
            while index < len(lines) and lines[index].startswith("|"):
                table_lines.append(lines[index])
                index += 1
            cells = [[cell.strip() for cell in row.strip("|").split("|")] for row in table_lines]
            columns = max(len(row) for row in cells)
            if columns <= 6:
                story.append(report_table(cells, range(columns), "portrait", cell_style))
                continue

            # Keep the preceding heading with the landscape table, including
            # when the report starts directly with that heading and table.
            while story and isinstance(story[-1], Spacer):
                story.pop()
            wide_story = []
            if story and story[-1].getKeepWithNext():
                wide_story.append(story.pop())
            add_section(sections, "portrait", story)
            story = []
            if columns <= LANDSCAPE_COLUMNS:
                wide_story.append(report_table(cells, range(columns), "landscape", cell_style))
            else:
                for start in range(1, columns, LANDSCAPE_COLUMNS - 1):
                    end = min(start + LANDSCAPE_COLUMNS - 1, columns)
                    caption = f"欄位 {start + 1} 至 {end}（各組均保留首欄）"
                    wide_story.append(Paragraph(caption, caption_style))
                    wide_story.append(report_table(cells, [0, *range(start, end)], "landscape", cell_style))
            add_section(sections, "landscape", wide_story)
            continue
        match = re.match(r"^(#{1,3})\s+(.+)$", line)
        if line:
            if match and not in_code:
                story.append(Paragraph(clean_inline(match[2]), headings[len(match[1])]))
            else:
                line = re.sub(r"^[-*]\s+", "• ", line)
                story.append(Paragraph(clean_inline(line), style))
        else:
            story.append(Spacer(1, 5))
        index += 1
    add_section(sections, "portrait", story)
    if not sections:
        raise probe.ProbeError("EMPTY_REPORT")
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    pending = output.with_suffix(".pending.pdf")

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("NotoTC", 8)
        canvas.setFillColor(colors.HexColor("#60736c"))
        canvas.drawRightString(doc.pageTemplate.pagesize[0] - SIDE_MARGIN, 28, f"第 {doc.page} 頁")
        canvas.restoreState()

    doc = BaseDocTemplate(str(pending), pagesize=PAGE_SIZES[sections[0][0]], title="課程作業報告", author="")
    for orientation in dict.fromkeys(orientation for orientation, _ in sections):
        width, height = PAGE_SIZES[orientation]
        frame = Frame(SIDE_MARGIN, BOTTOM_MARGIN, width - 2 * SIDE_MARGIN,
                      height - TOP_MARGIN - BOTTOM_MARGIN,
                      leftPadding=FRAME_PADDING, rightPadding=FRAME_PADDING)
        doc.addPageTemplates(PageTemplate(id=orientation, frames=frame,
                                          pagesize=(width, height), onPage=footer))
    story = []
    for orientation, flowables in sections:
        if story:
            story.append(PageBreak(nextTemplate=orientation))
        story.extend(flowables)
    try:
        doc.build(story)
        quality = validate_pdf(pending)
        if not quality["passed"]:
            raise probe.ProbeError("PDF_QA_FAILED")
        pending.replace(output)
    except LayoutError:
        raise probe.ProbeError("PDF_QA_FAILED") from None
    finally:
        pending.unlink(missing_ok=True)
    return {**quality, "sha256": hashlib.sha256(output.read_bytes()).hexdigest()}


def validate_pdf(path):
    problems = []
    with pymupdf.open(path) as document:
        if not 0 < len(document) <= 150:
            problems.append("PAGE_COUNT")
        for page in document:
            body = page.get_text(clip=pymupdf.Rect(0, 0, page.rect.width, page.rect.height - 45))
            if not body.strip():
                problems.append("BLANK_BODY_PAGE")
            if "\ufffd" in body:
                problems.append("REPLACEMENT_GLYPH")
            for word in page.get_text("words"):
                x0, y0, x1, y1 = word[:4]
                if x0 < 40 or x1 > page.rect.width - 40 or y0 < 20 or y1 > page.rect.height - 15:
                    problems.append("TEXT_OVERFLOW")
            for font in page.get_fonts(full=True):
                if "NotoSansTC" in font[3] and not document.extract_font(font[0])[3]:
                    problems.append("FONT_NOT_EMBEDDED")
        fonts = [f[3] for page in document for f in page.get_fonts()]
        if not any("NotoSansTC" in f for f in fonts):
            problems.append("CJK_FONT_MISSING")
        pages = len(document)
    return {"passed": not problems, "pages": pages, "checks": ["open", "text", "blank_pages", "bounds", "font_embedding"],
            "problems": sorted(set(problems)), "visual_review_required": True}
