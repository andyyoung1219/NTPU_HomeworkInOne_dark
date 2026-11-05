import csv
import html
import io
import re
import zipfile
from html.parser import HTMLParser
from pathlib import Path

from backend.p0.moodle import MAX_BYTES, inspect_document, probe
from backend.services.ocr import IMAGE_SUFFIXES, document_budget, image_bytes, recognize


class PlainHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.skip += 1
        if tag in {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "tr"}:
            self.parts.append("\n")
        if tag in {"td", "th"} and self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append(" | ")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.skip = max(0, self.skip - 1)
        if tag in {"p", "div", "li", "h1", "h2", "h3", "h4", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def plain_html(value):
    parser = PlainHTML()
    parser.feed(value or "")
    return html.unescape("".join(parser.parts)).strip()


def assignment_text(raw, module=None, submission=None):
    """Normalize only instructor-facing fields, never student submission/feedback text."""
    parts = []
    seen = set()
    candidates = [
        ("intro", "Moodle 作業敘述", "mod_assign_get_assignments.intro", raw.get("intro")),
        ("activity", "Moodle 活動指示", "mod_assign_get_assignments.activity", raw.get("activity")),
        ("activity", "Moodle 活動指示", "mod_assign_get_submission_status.assignmentdata.activity",
         ((submission or {}).get("assignmentdata") or {}).get("activity")),
    ]
    if not plain_html(raw.get("intro", "")):
        candidates.append(("description", "Moodle 課程頁作業敘述", "core_course_get_contents.modules.description",
                           (module or {}).get("description")))
    for kind, label, source, value in candidates:
        if not isinstance(value, str):
            continue
        text = plain_html(value)
        normalized = " ".join(text.split())
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        parts.append({"kind": kind, "label": label, "source": source, "text": text})
    return {"description": "\n\n".join(f"【{part['label']}】\n{part['text']}" for part in parts),
            "description_parts": parts, "description_status": "available" if parts else "not_returned"}


@document_budget
def parse(data, suffix, mime, file_id):
    flags = []
    suffix = suffix.lower()
    if suffix in {".pdf", ".docx"}:
        try:
            chunks = inspect_document(data, suffix, mime)
        except probe.ProbeError as exc:
            if suffix != ".pdf" or exc.code != "pdf_requires_ocr":
                raise
            chunks = []
        if suffix == ".pdf":
            import pymupdf
            flags.append("PDF_TABLES_IMAGES_EQUATIONS_REQUIRE_REVIEW")
            with pymupdf.open(stream=data, filetype="pdf") as document:
                if not chunks:
                    chunks = [{"page": i + 1, "text": page.get_text()} for i, page in enumerate(document)]
                ocr_count = 0
                for page, chunk in zip(document, chunks):
                    visual = bool(page.get_images() or page.get_drawings())
                    if visual:
                        chunk["has_visual"] = True
                        if "VISUAL_CONTENT_AVAILABLE" not in flags:
                            flags.append("VISUAL_CONTENT_AVAILABLE")
                    if len(chunk["text"].strip()) < 30:
                        if ocr_count >= 12:
                            flags.append("OCR_PAGE_LIMIT_REQUIRES_REVIEW")
                            continue
                        ocr_count += 1
                        scale = min(2.0, 1800 / max(page.rect.width, page.rect.height))
                        png = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).tobytes("png")
                        result = recognize(png)
                        ocr_text = "\n".join(line["text"] for line in result["lines"])
                        original_text = chunk["text"].strip()
                        combined = original_text or ocr_text
                        if original_text and ocr_text and ocr_text.strip() != original_text:
                            combined = original_text + "\n[OCR 輔助文字]\n" + ocr_text
                        chunk.update(text=combined, ocr=result, has_visual=True)
                        if not chunk["text"].strip() and visual:
                            chunk["text"] = "[此頁未辨識到可靠文字，須於核准後以頁面影像判讀圖表與公式。]"
                        if "OCR_TEXT_REQUIRES_REVIEW" not in flags:
                            flags.append("OCR_TEXT_REQUIRES_REVIEW")
            if not any(c["text"].strip() for c in chunks):
                raise probe.ProbeError("pdf_requires_ocr")
        else:
            from lxml import etree
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                content = archive.read("word/document.xml")
                xml = etree.fromstring(content, parser=etree.XMLParser(resolve_entities=False, no_network=True))
                ns = {"m": "http://schemas.openxmlformats.org/officeDocument/2006/math"}
                for index, equation in enumerate(xml.xpath("//m:oMath", namespaces=ns), 1):
                    chunks.append({"section": f"equation-{index}", "text": "[Word 公式] " + omml_text(equation),
                                   "has_visual": True})
                    if "FORMULA_TEXT_REQUIRES_REVIEW" not in flags:
                        flags.append("FORMULA_TEXT_REQUIRES_REVIEW")
                images = docx_image_entries(archive)
                for index, (name, crop) in enumerate(images[:8], 1):
                    result = recognize(docx_image_bytes(archive, name, crop))
                    chunks.append({"section": f"image-{index}",
                        "text": "[Word 圖片 OCR]\n" + ("\n".join(line["text"] for line in result["lines"])
                              or "[圖片須於核准後進行視覺判讀。]"), "ocr": result, "has_visual": True})
                if images:
                    flags.extend(["OCR_TEXT_REQUIRES_REVIEW", "VISUAL_CONTENT_AVAILABLE"])
                if len(images) > 8:
                    flags.append("OCR_IMAGE_LIMIT_REQUIRES_REVIEW")
                if any(key in content for key in (b"<w:drawing", b"<m:oMath", b"<w:object")):
                    flags.append("NON_TEXT_CONTENT_REQUIRES_REVIEW")
    elif suffix in IMAGE_SUFFIXES:
        if mime not in {"image/png", "image/jpeg", "image/webp", "application/octet-stream"}:
            raise probe.ProbeError("attachment_type_mismatch")
        result = recognize(image_bytes(data))
        chunks = [{"section": 1, "text": "\n".join(line["text"] for line in result["lines"])
                   or "[圖片未辨識到可靠文字，須於核准後判讀圖表與公式。]", "ocr": result, "has_visual": True}]
        flags.extend(["OCR_TEXT_REQUIRES_REVIEW", "VISUAL_CONTENT_AVAILABLE"])
    elif suffix in {".txt", ".csv"}:
        if len(data) > MAX_BYTES or b"\x00" in data:
            raise probe.ProbeError("text_invalid_or_too_large")
        if mime not in {"text/plain", "text/csv", "application/csv", "application/octet-stream",
                        "application/vnd.ms-excel"}:
            raise probe.ProbeError("attachment_type_mismatch")
        text = data.decode("utf-8-sig")
        if suffix == ".csv":
            reader = csv.reader(io.StringIO(text))
            rows = []
            for _, row in zip(range(6), reader):
                if len(row) > 100:
                    raise probe.ProbeError("csv_too_many_columns")
                rows.append([cell[:200] for cell in row])
            text = "CSV 欄位與前五筆預覽（未執行統計分析）：\n" + "\n".join(" | ".join(r) for r in rows)
            flags.append("CSV_PREVIEW_ONLY")
        chunks = [{"section": 1, "text": text[:50000]}]
        if len(text) > 50000:
            flags.append("TEXT_TRUNCATED")
    else:
        raise probe.ProbeError("UNSUPPORTED_FORMAT")
    flags = list(dict.fromkeys(flags))
    for chunk in chunks:
        chunk.update(file_id=file_id, extraction_flags=flags)
    return chunks, flags


def requirements(chunks):
    result = []
    for chunk in chunks:
        for line in chunk["text"].splitlines():
            line = line.strip()
            if len(line) >= 8 and re.search(r"請|須|需|繳|提交|報告|比較|實作|分析|must|submit|require", line, re.I):
                result.append({"id": f"REQ-{len(result) + 1:02}", "text": line[:500],
                               "source": {key: chunk[key] for key in ("file_id", "page", "section") if key in chunk},
                               "status": "needs_review"})
            if len(result) >= 30:
                return result
    return result


def omml_text(node):
    """Preserve common native Word equation structure as reviewable LaTeX."""
    from lxml.etree import QName
    kind = QName(node).localname
    children = list(node)
    def child(name):
        return next((omml_text(c) for c in children if QName(c).localname == name), "")
    if kind == "t":
        return node.text or ""
    if kind == "f":
        return r"\frac{" + child("num") + "}{" + child("den") + "}"
    if kind in {"sSup", "sSub", "sSubSup"}:
        text = child("e")
        if kind in {"sSub", "sSubSup"}:
            text += "_{" + child("sub") + "}"
        if kind in {"sSup", "sSubSup"}:
            text += "^{" + child("sup") + "}"
        return text
    if kind == "rad":
        degree = child("deg")
        return r"\sqrt" + ("[" + degree + "]" if degree else "") + "{" + child("e") + "}"
    if kind == "nary":
        props = next((c for c in children if QName(c).localname == "naryPr"), None)
        chars = props.findall("{*}chr") if props is not None else []
        symbol = chars[0].get("{http://schemas.openxmlformats.org/officeDocument/2006/math}val", "∫") if chars else "∫"
        operator = {"∑": r"\sum", "∫": r"\int", "∏": r"\prod", "⋃": r"\bigcup"}.get(symbol, symbol)
        return operator + ("_{" + child("sub") + "}" if child("sub") else "") + ("^{" + child("sup") + "}" if child("sup") else "") + " " + child("e")
    if kind == "m":
        rows = [c for c in children if QName(c).localname == "mr"]
        return r"\begin{matrix}" + r" \\ ".join(" & ".join(omml_text(c) for c in row if QName(c).localname == "e") for row in rows) + r"\end{matrix}"
    if kind == "d":
        props = next((c for c in children if QName(c).localname == "dPr"), None)
        def delimiter(name, default):
            value = props.find("{*}" + name) if props is not None else None
            return value.get("{http://schemas.openxmlformats.org/officeDocument/2006/math}val", default) if value is not None else default
        return delimiter("begChr", "(") + child("e") + delimiter("endChr", ")")
    if kind in {"limLow", "limUpp"}:
        return child("e") + ("_{" if kind == "limLow" else "^{") + child("lim") + "}"
    if kind == "sPre":
        return "{}_{" + child("sub") + "}^{" + child("sup") + "}" + child("e")
    if kind == "acc":
        props = next((c for c in children if QName(c).localname == "accPr"), None)
        symbol = props.find("{*}chr") if props is not None else None
        value = symbol.get("{http://schemas.openxmlformats.org/officeDocument/2006/math}val", "̂") if symbol is not None else "̂"
        command = {"̂": r"\hat", "^": r"\hat", "¯": r"\bar", "̃": r"\tilde", "→": r"\vec"}.get(value)
        return command + "{" + child("e") + "}" if command else "[公式重音需核對:" + value + "]" + child("e")
    if kind == "bar":
        position = node.find("{*}barPr/{*}pos")
        lower = position is not None and position.get("{http://schemas.openxmlformats.org/officeDocument/2006/math}val") == "bot"
        return (r"\underline{" if lower else r"\overline{") + child("e") + "}"
    if kind == "eqArr":
        return r"\begin{aligned}" + r" \\ ".join(omml_text(c) for c in children if QName(c).localname == "e") + r"\end{aligned}"
    if kind.endswith("Pr"):
        return ""
    text = "".join(omml_text(c) for c in children)
    if kind not in {"oMath", "oMathPara", "r", "e", "num", "den", "sub", "sup", "deg", "lim", "fName", "func", "box", "borderBox", "phant"}:
        return "[公式結構需核對:" + kind + "]" + text
    return text


def docx_image_entries(archive):
    """Only displayed body images, in document order; never orphan ZIP media."""
    import posixpath

    from lxml import etree
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    if "word/_rels/document.xml.rels" not in archive.namelist():
        return []
    relationships = etree.fromstring(archive.read("word/_rels/document.xml.rels"), parser)
    targets = {rel.get("Id"): rel.get("Target", "") for rel in relationships
               if rel.get("TargetMode") != "External" and rel.get("Type", "").endswith("/image")}
    document = etree.fromstring(archive.read("word/document.xml"), parser)
    ns = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main",
          "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
    result = []
    for blip in document.xpath("//a:blip[@r:embed]", namespaces=ns):
        target = targets.get(blip.get("{" + ns["r"] + "}embed"), "")
        name = posixpath.normpath(posixpath.join("word", target))
        if not name.startswith("word/media/") or Path(name).suffix.lower() not in IMAGE_SUFFIXES:
            continue
        if name not in archive.namelist():
            raise probe.ProbeError("DOCX_IMAGE_MISSING")
        rectangle = blip.getparent().find("a:srcRect", ns)
        crop = {edge: int(rectangle.get(edge, "0")) for edge in ("l", "t", "r", "b")} if rectangle is not None else {}
        if any(value < 0 or value >= 100000 for value in crop.values()):
            raise probe.ProbeError("DOCX_IMAGE_CROP_UNSUPPORTED")
        result.append((name, crop))
    return result


def docx_image_bytes(archive, name, crop):
    from PIL import Image
    normalized = image_bytes(archive.read(name))
    if not crop:
        return normalized
    with Image.open(io.BytesIO(normalized)) as image:
        width, height = image.size
        box = (int(width * crop['l'] / 100000), int(height * crop['t'] / 100000),
               int(width * (1 - crop['r'] / 100000)), int(height * (1 - crop['b'] / 100000)))
        if box[0] >= box[2] or box[1] >= box[3]:
            raise probe.ProbeError("DOCX_IMAGE_CROP_UNSUPPORTED")
        stream = io.BytesIO()
        image.crop(box).save(stream, format="PNG")
        return stream.getvalue()
