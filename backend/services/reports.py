"""Local deterministic Markdown -> embedded CJK PDF -> independent validation."""

import hashlib
import html
import re
import threading
from pathlib import Path

import pymupdf
from fontTools.ttLib import TTFont as FontToolsFont
from fontTools.varLib.instancer import instantiateVariableFont
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Image, LongTable, Paragraph, SimpleDocTemplate, Spacer, TableStyle

from backend.p0.evidence import ROOT
from backend.p0.moodle import probe

_font_lock = threading.Lock()
_render_lock = threading.Lock()
FONT_DIR = ROOT / ".local/fonts"


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
    code_style = ParagraphStyle("Code", parent=style, fontSize=9, leading=13,
                                spaceAfter=0, wordWrap=None, leftIndent=8)
    story, lines = [], markdown.splitlines()
    index, in_code = 0, False
    while index < len(lines):
        line = lines[index].rstrip()
        picture = re.fullmatch(r"!\[([^\]]*)\]\(([^)]+)\)", line)
        if not in_code and picture and picture[2] in images:
            # Only caller-verified artifacts; Markdown cannot request local/remote reads.
            chart = Image(str(images[picture[2]]))
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
            if columns > 6:
                raise probe.ProbeError("PDF_TABLE_TOO_WIDE")
            rows = [[Paragraph(clean_inline(cell), cell_style) for cell in row + [""] * (columns - len(row))] for row in cells]
            table = LongTable(rows, colWidths=[(A4[0] - 100) / columns] * columns, repeatRows=1,
                              splitByRow=1, splitInRow=1, hAlign="LEFT")
            table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8f2ef")),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#c5d5d0")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 7), ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]))
            story += [table, Spacer(1, 10)]
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
    if not any(line.strip() for line in lines):
        raise probe.ProbeError("EMPTY_REPORT")
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    pending = output.with_suffix(".pending.pdf")

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("NotoTC", 8)
        canvas.setFillColor(colors.HexColor("#60736c"))
        canvas.drawString(50, 28, "AI 協作草稿 · 經使用者審閱匯出")
        canvas.drawRightString(A4[0] - 50, 28, f"第 {doc.page} 頁")
        canvas.restoreState()

    doc = SimpleDocTemplate(str(pending), pagesize=A4, rightMargin=50, leftMargin=50,
                            topMargin=46, bottomMargin=48, title="課程作業報告", author="")
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    quality = validate_pdf(pending)
    if not quality["passed"]:
        raise probe.ProbeError("PDF_QA_FAILED")
    pending.replace(output)
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
