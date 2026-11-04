"""Reviewed Word and ZIP exports using explicit artifact lists, never directory walks."""

import hashlib
import html
import json
import re
import zipfile
from pathlib import Path

from docx import Document
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

from backend.models.store import now
from backend.services.learning import fail
from backend.services.reports import clean_inline


def readable(text):
    return html.unescape(clean_inline(text))


def render_docx(markdown, output):
    document = Document()
    section = document.sections[0]
    section.page_width, section.page_height = Inches(8.5), Inches(11)
    section.top_margin = section.bottom_margin = Inches(0.8)
    section.left_margin = section.right_margin = Inches(0.85)
    for name in ["Normal", "Title", "Heading 1", "Heading 2", "Heading 3", "List Bullet", "List Number"]:
        style = document.styles[name]
        style.font.name = "Microsoft JhengHei"
        style.font.color.rgb = RGBColor(0, 0, 0)
        style.element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), "Microsoft JhengHei")
    normal = document.styles["Normal"]
    normal.font.size = Pt(11)
    normal.paragraph_format.line_spacing = 1.25
    normal.paragraph_format.space_after = Pt(7)
    document.core_properties.author = ""
    document.core_properties.last_modified_by = ""
    document.core_properties.title = next((readable(line.lstrip("# ")) for line in markdown.splitlines()
                                           if line.strip()), "報告")[:200]
    lines, index, in_code = markdown.splitlines(), 0, False
    while index < len(lines):
        line = lines[index]
        index += 1
        if line.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            paragraph = document.add_paragraph()
            paragraph.paragraph_format.space_after = Pt(0)
            run = paragraph.add_run(line)
            run.font.name, run.font.size = "Consolas", Pt(10)
            continue
        if not line.strip():
            continue
        if line.startswith("|") and index < len(lines) and re.fullmatch(r"[\s|:\-]+", lines[index]):
            rows = [[readable(c.strip()) for c in line.strip().strip("|").split("|")]]
            index += 1
            while index < len(lines) and lines[index].startswith("|"):
                rows.append([readable(c.strip()) for c in lines[index].strip().strip("|").split("|")])
                index += 1
            count = max(map(len, rows))
            if count > 6:
                fail("DOCX_TABLE_TOO_WIDE")
            table = document.add_table(rows=0, cols=count)
            table.autofit = False
            # Weighted widths keep short status/number columns compact.
            weights = [max(5, min(36, max(len(r[c]) if c < len(r) else 0 for r in rows))) for c in range(count)]
            for column, weight in zip(table.columns, weights):
                column.width = Inches(6.8 * weight / sum(weights))
            borders = OxmlElement("w:tblBorders")
            for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
                border = OxmlElement("w:" + edge)
                for key, value in (("val", "single"), ("sz", "4"), ("color", "D9D9D9")):
                    border.set(qn("w:" + key), value)
                borders.append(border)
            table._tbl.tblPr.append(borders)
            for row_index, row in enumerate(rows):
                cells = table.add_row().cells
                for c, cell in enumerate(cells):
                    cell.width = table.columns[c].width
                    cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
                    cell.text = row[c] if c < len(row) else ""
                    props = cell._tc.get_or_add_tcPr()
                    margins = OxmlElement("w:tcMar")
                    for edge in ("top", "left", "bottom", "right"):
                        margin = OxmlElement("w:" + edge)
                        margin.set(qn("w:w"), "100")
                        margin.set(qn("w:type"), "dxa")
                        margins.append(margin)
                    props.append(margins)
                    if row_index == 0:
                        shade = OxmlElement("w:shd")
                        shade.set(qn("w:fill"), "DFEAF2")
                        props.append(shade)
                        for run in cell.paragraphs[0].runs:
                            run.bold = True
                if row_index == 0:
                    table.rows[0]._tr.get_or_add_trPr().append(OxmlElement("w:tblHeader"))
            document.add_paragraph().paragraph_format.space_after = Pt(3)
            continue
        heading = re.match(r"^(#{1,6})\s+(.+)$", line)
        if heading:
            level = len(heading[1])
            document.add_paragraph(readable(heading[2]),
                                   "Title" if level == 1 else f"Heading {min(level - 1, 3)}")
        elif re.match(r"^[-*]\s+", line):
            document.add_paragraph(readable(line[2:]), "List Bullet")
        elif re.match(r"^\d+[.)]\s+", line):
            document.add_paragraph(readable(re.sub(r"^\d+[.)]\s+", "", line)), "List Number")
        else:
            document.add_paragraph(readable(line))
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    pending = output.with_suffix(".pending.docx")
    try:
        document.save(pending)
        check = Document(pending)
        if not any(p.text.strip() for p in check.paragraphs) and not check.tables:
            fail("EMPTY_REPORT")
        with zipfile.ZipFile(pending) as archive:
            if archive.testzip():
                fail("DOCX_INVALID")
        pending.replace(output)
    finally:
        pending.unlink(missing_ok=True)
    return {"sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
            "validation": "OOXML_STRUCTURE_VERIFIED", "visual_review_required": True}


def export_extra(study, task_id, kind, include_sources=False):
    if kind not in {"docx", "zip"}:
        fail("EXPORT_FORMAT_INVALID")
    with study.lock:
        from backend.services.study import error_code

        user = study.require_user()
        task = study.get("task", task_id)
        if not task.get("reviewed") or task["status"] not in {"waiting_review", "completed"}:
            fail("REVIEW_REQUIRED")
        if task.get("plan") and not task.get("ready_for_export"):
            fail("INCOMPLETE_ACCEPTANCE_REQUIRED")
        omitted = {}
        if kind == "zip" and not task.get("pdf"):
            try:
                task = study.export(task_id)
            except Exception as exc:
                code = error_code(exc)
                if not code.startswith("PDF_"):
                    raise
                task = study.get("task", task_id)
                omitted["pdf"] = code
        workspace = study.workspace(user, task_id).resolve()
        output = workspace / "output"
        output.mkdir(parents=True, exist_ok=True)
        docx_path = output / "report.docx"
        artifacts = dict(task.get("exports", {}))
        # Reuse a registered Word export when packing ZIP. Rewriting it before
        # all inputs pass validation could invalidate an earlier download if
        # ZIP creation subsequently fails.
        if kind == "docx" or "docx" not in artifacts:
            try:
                qa = render_docx(task["draft"], docx_path)
            except Exception as exc:
                code = error_code(exc)
                if kind != "zip" or not code.startswith("DOCX_"):
                    raise
                omitted["docx"] = code
                artifacts.pop("docx", None)
            else:
                artifacts["docx"] = {**qa, "path": str(docx_path.relative_to(study.root.resolve()))}
        if kind == "zip":
            entries = {}

            def add(name, path, digest=None, legacy_text=False):
                path = path.resolve()
                if not path.is_relative_to(workspace):
                    fail("PATH_OUTSIDE_WORKSPACE")
                if path.stat().st_size > 30_000_000:
                    fail("EXPORT_SIZE_LIMIT")
                data = path.read_bytes()
                actual = hashlib.sha256(data).hexdigest()
                if digest and digest != actual and legacy_text:
                    # Older Windows text snapshots translated LF into CRLF.
                    # Accept only bytes that restore the original approved hash;
                    # never weaken validation for attachments or other content.
                    normalized = data.replace(b"\r\n", b"\n")
                    if hashlib.sha256(normalized).hexdigest() == digest:
                        data, actual = normalized, digest
                if digest and digest != actual:
                    fail("ARTIFACT_CHANGED")
                if sum(len(v) for v in entries.values()) + len(data) > 100_000_000:
                    fail("EXPORT_SIZE_LIMIT")
                entries[name] = data

            if task.get("pdf"):
                add("report.pdf", study.safe_path(task["pdf"]), task["qa"]["sha256"])
            if "docx" in artifacts:
                add("report.docx", study.safe_path(artifacts["docx"]["path"]), artifacts["docx"]["sha256"])
            entries["report.md"] = task["draft"].encode("utf-8")
            for name, artifact in task.get("generated_files", {}).items():
                if not re.fullmatch(r"[\w.-]+", name) or name in {".", ".."}:
                    fail("ARTIFACT_NAME_INVALID")
                add("code/" + name, study.safe_path(artifact["path"]), artifact["sha256"])
            for step_id, result in task.get("tool_results", {}).items():
                if not re.fullmatch(r"[\w-]+", step_id):
                    fail("ARTIFACT_NAME_INVALID")
                for name, artifact in result.get("artifacts", {}).items():
                    if not re.fullmatch(r"[\w.-]+", name) or name in {".", ".."}:
                        fail("ARTIFACT_NAME_INVALID")
                    add(f"artifacts/{step_id}/{name}", study.safe_path(artifact["path"]), artifact["sha256"])
            if include_sources:
                for ref, digest in task.get("source_hashes", {}).items():
                    if ref == "assignment-description":
                        add("sources/assignment-description.txt", workspace / "input/assignment-description.txt", digest,
                            legacy_text=True)
                    elif ref.startswith("crossref:"):
                        research = [result for result in task.get("tool_results", {}).values()
                                    if result.get("provider") == "Crossref"]
                        research.append(task.get("completion_data", {}).get("research", {}))
                        source = next((source for result in research for source in result.get("sources", [])
                                       if source.get("file_id") == ref), None)
                        if source is None:
                            fail("ARTIFACT_CHANGED")
                        content = json.dumps(source, sort_keys=True, ensure_ascii=False).encode()
                        if hashlib.sha256(content).hexdigest() != digest:
                            fail("ARTIFACT_CHANGED")
                        entries[f"sources/{digest}.json"] = content
                    elif ref.startswith("dataset:"):
                        dataset = next((item for item in task.get("completion_data", {}).get("datasets", [])
                                        if item["file_id"] == ref), None)
                        if dataset is None:
                            fail("ARTIFACT_CHANGED")
                        add(f"sources/{digest}.csv", study.safe_path(dataset["path"]), digest)
                    else:
                        suffix = Path(task["source_names"][ref]).suffix.lower()
                        if not re.fullmatch(r"[a-f0-9]{64}", digest) or not re.fullmatch(r"\.[a-z0-9]+", suffix):
                            fail("ARTIFACT_NAME_INVALID")
                        add(f"sources/{digest}{suffix}", workspace / "input" / (digest + suffix), digest)
            manifest = {"schema_version": 3, "task_id": task_id, "exported_at": now(),
                        "human_reviewed": True, "include_sources": bool(include_sources),
                        "omitted_exports": omitted,
                        "source_names": task.get("source_names", {}),
                        "source_hashes": task.get("source_hashes", {}),
                        "unresolved": task.get("report_model", {}).get("unresolved", []),
                        "artifacts": {name: hashlib.sha256(data).hexdigest() for name, data in entries.items()}}
            entries["manifest.json"] = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
            zip_path = output / "workspace.zip"
            pending = output / "workspace.pending.zip"
            try:
                with zipfile.ZipFile(pending, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                    for name, data in entries.items():
                        archive.writestr(name, data)
                with zipfile.ZipFile(pending) as archive:
                    if archive.testzip():
                        fail("ZIP_INVALID")
                pending.replace(zip_path)
            finally:
                pending.unlink(missing_ok=True)
            artifacts["zip"] = {"path": str(zip_path.relative_to(study.root.resolve())),
                                "sha256": hashlib.sha256(zip_path.read_bytes()).hexdigest(),
                                "include_sources": bool(include_sources), "omitted_exports": omitted}
        task["exports"] = artifacts
        study.store.save(user, "task", task_id, task)
        study.store.event(user, task_id, "exported_" + kind)
        return task
