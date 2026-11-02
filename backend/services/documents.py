import csv
import html
import io
import re
import zipfile
from html.parser import HTMLParser

from backend.p0.moodle import MAX_BYTES, inspect_document, probe


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


def parse(data, suffix, mime, file_id):
    flags = []
    if suffix in {".pdf", ".docx"}:
        chunks = inspect_document(data, suffix, mime)
        if suffix == ".docx":
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                content = archive.read("word/document.xml")
                if any(key in content for key in (b"<w:drawing", b"<m:oMath", b"<w:object")):
                    flags.append("NON_TEXT_CONTENT_REQUIRES_REVIEW")
        else:
            flags.append("PDF_TABLES_IMAGES_EQUATIONS_REQUIRE_REVIEW")
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
