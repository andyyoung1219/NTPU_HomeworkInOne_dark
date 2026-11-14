"""Reviewed Word and ZIP exports using explicit artifact lists, never directory walks."""

import hashlib
import html
import io
import json
import re
import zipfile
from pathlib import Path

from docx import Document
from docx.enum.section import WD_ORIENT, WD_SECTION_START
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor
from lxml import etree

from backend.agent.runner import filesystem_path
from backend.models.store import now
from backend.services.learning import fail
from backend.services.reports import clean_inline
from backend.services.submission import (
    require_submission_content,
    submission_export_stale,
    submission_markdown,
)


def readable(text):
    return html.unescape(clean_inline(text))


def math_element(name, *children, **attributes):
    element = OxmlElement('m:' + name)
    for key, value in attributes.items():
        element.set(qn('m:' + key), str(value))
    for child in children:
        if name in {'e', 'num', 'den', 'sub', 'sup', 'deg', 'lim'} and child.tag == qn('m:e'):
            element.extend(list(child))
        else:
            element.append(child)
    return element


def mathml_to_omml(node):
    """Convert presentation MathML to editable Office Math, without external XSLT."""
    tag = etree.QName(node).localname
    children = list(node)
    convert = mathml_to_omml
    if tag in {'mi', 'mn', 'mo', 'mtext', 'ms'}:
        text = ''.join(node.itertext())
        run = math_element('r')
        value = math_element('t')
        value.text = text
        value.set(qn('xml:space'), 'preserve')
        run.append(value)
        return run
    if tag in {'math', 'mrow', 'mstyle', 'mpadded', 'semantics'}:
        if tag == 'mrow' and len(children) >= 2 and etree.QName(children[0]).localname == 'mo' and (''.join(children[0].itertext()) in {'(', '[', '{', '|', '⟨'} or children[0].get('fence') == 'true'):
            last_is_fence = etree.QName(children[-1]).localname == 'mo' and (''.join(children[-1].itertext()) in {')', ']', '}', '|', '⟩'} or children[-1].get('fence') == 'true')
            inner = children[1:-1] if last_is_fence else children[1:]
            return math_element('d', math_element('dPr',
                math_element('begChr', val=''.join(children[0].itertext())),
                math_element('endChr', val=''.join(children[-1].itertext()) if last_is_fence else ''),
                math_element('grow', val='1')),
                math_element('e', *(convert(child) for child in inner)))
        result = math_element('e')
        for child in children:
            if etree.QName(child).localname not in {'annotation', 'annotation-xml'}:
                converted = convert(child)
                if converted.tag == qn('m:e'):
                    result.extend(list(converted))
                else:
                    result.append(converted)
        return result
    if tag == 'mspace':
        run = math_element('r', math_element('t'))
        run[0].text = ' '
        return run
    if tag == 'mfrac':
        return math_element('f', math_element('num', convert(children[0])), math_element('den', convert(children[1])))
    if tag in {'msup', 'msub', 'msubsup'}:
        names = {'msup': ('sSup', 'sup'), 'msub': ('sSub', 'sub'), 'msubsup': ('sSubSup', 'sub', 'sup')}[tag]
        result = math_element(names[0], math_element('e', convert(children[0])))
        for name, child in zip(names[1:], children[1:]):
            result.append(math_element(name, convert(child)))
        return result
    if tag in {'msqrt', 'mroot'}:
        result = math_element('rad')
        if tag == 'msqrt':
            result.append(math_element('radPr', math_element('degHide', val='1')))
            result.append(math_element('deg'))
            result.append(math_element('e', *(convert(child) for child in children)))
        else:
            result.extend([math_element('deg', convert(children[1])), math_element('e', convert(children[0]))])
        return result
    if tag in {'munder', 'mover', 'munderover'}:
        if tag == 'mover' and node.get('accent') == 'true':
            symbol = ''.join(children[1].itertext())
            return math_element('acc', math_element('accPr', math_element('chr', val=symbol)),
                                math_element('e', convert(children[0])))
        base = convert(children[0])
        for name, child in zip(('limLow', 'limUpp') if tag == 'munderover' else (('limLow',) if tag == 'munder' else ('limUpp',)), children[1:]):
            base = math_element(name, math_element('e', base), math_element('lim', convert(child)))
        return base
    if tag == 'mfenced':
        return math_element('d', math_element('dPr', math_element('begChr', val=node.get('open', '(')),
                    math_element('endChr', val=node.get('close', ')'))),
                    *(math_element('e', convert(child)) for child in children))
    if tag == 'mtable':
        return math_element('m', *(math_element('mr', *(math_element('e', convert(cell)) for cell in row)) for row in children))
    if tag in {'mtr', 'mtd'}:
        return math_element('e', *(convert(child) for child in children))
    if tag == 'menclose' and node.get('notation', '') in {'box', 'roundedbox'}:
        return math_element('borderBox', math_element('e', *(convert(child) for child in children)))
    raise ValueError('Unsupported MathML structure')


def equation(latex):
    from latex2mathml.converter import convert

    if len(latex) > 4000:
        raise ValueError('Equation too long')
    try:
        converted = convert(latex)
    except Exception as exc:
        raise ValueError('Invalid LaTeX equation') from exc
    source = etree.fromstring(converted.encode(), parser=etree.XMLParser(resolve_entities=False, no_network=True))
    if any('\\' in ''.join(node.itertext()) for node in source.iter() if etree.QName(node).localname in {'mi', 'mtext'}):
        raise ValueError('Unrecognized LaTeX command')
    content = mathml_to_omml(source)
    result = math_element('oMath')
    result.extend(list(content))
    return result


INLINE = re.compile(r'(!\[[^\]]*\]\([^\n)]+\)|\[[^\]]+\]\([^\n)]+\)|`[^`\n]+`|\$[^$\n]+\$|\\\([^\n]*?\\\)|\*\*.+?\*\*|__.+?__|(?<!\*)\*[^*\n]+\*(?!\*)|(?<!\w)_[^_\n]+_(?!\w))')


def add_inline(paragraph, text, images, width, warnings):
    for token in INLINE.split(html.unescape(text)):
        if not token:
            continue
        image = re.fullmatch(r'!\[([^\]]*)\]\(([^)]+)\)', token)
        if image:
            label, reference = image.groups()
            if reference in images:
                from PIL import Image
                data = images[reference]
                with Image.open(io.BytesIO(data)) as picture:
                    w, h = picture.size
                    if w * h > 25_000_000 or picture.format not in {'PNG', 'JPEG'}:
                        fail('DOCX_IMAGE_INVALID')
                span = min(width, 8.0 * w / h)
                paragraph.add_run().add_picture(io.BytesIO(data), width=Inches(span))
                if label:
                    caption = paragraph.add_run('\n' + label)
                    caption.font.size = Pt(9)
            else:
                paragraph.add_run('[圖片待補：' + label + ']')
                warnings.add('IMAGE_NOT_REGISTERED')
        elif (token.startswith('$') and token.endswith('$')) or token.startswith(r'\('):
            formula = token[1:-1] if token.startswith('$') else token[2:-2]
            try:
                paragraph._p.append(equation(formula))
            except (ValueError, TypeError, KeyError, IndexError, NotImplementedError):
                paragraph.add_run('[公式需核對：' + formula + ']')
                warnings.add('EQUATION_REQUIRES_REVIEW')
        elif token.startswith('`') and token.endswith('`'):
            run = paragraph.add_run(token[1:-1])
            run.font.name, run.font.size = 'Consolas', Pt(10)
        elif token.startswith(('**', '__')):
            paragraph.add_run(token[2:-2]).bold = True
        elif token.startswith(('*', '_')):
            paragraph.add_run(token[1:-1]).italic = True
        elif (link := re.fullmatch(r'\[([^\]]+)\]\(([^)]+)\)', token)):
            # Preserve visible source URLs, but never dereference or fetch them.
            label, target = link.groups()
            paragraph.add_run(label + ' (' + target + ')')
        else:
            paragraph.add_run(token.replace('<br>', '\n').replace('<br/>', '\n').replace('<br />', '\n'))


def set_section(section, landscape=False):
    section.orientation = WD_ORIENT.LANDSCAPE if landscape else WD_ORIENT.PORTRAIT
    section.page_width, section.page_height = (Inches(11.69), Inches(8.27)) if landscape else (Inches(8.27), Inches(11.69))
    section.top_margin = section.bottom_margin = Inches(0.75)
    section.left_margin = section.right_margin = Inches(0.8)
    section.header_distance = section.footer_distance = Inches(0.3)


def usable_width(document):
    section = document.sections[-1]
    return (section.page_width - section.left_margin - section.right_margin) / 914400


def page_field(paragraph, instruction):
    field = OxmlElement('w:fldSimple')
    field.set(qn('w:instr'), instruction)
    run = OxmlElement('w:r')
    text = OxmlElement('w:t')
    text.text = '1'
    run.append(text)
    field.append(run)
    paragraph._p.append(field)


def table_cells(line):
    return [cell.strip().replace(r'\|', '|') for cell in re.split(r'(?<!\\)\|', line.strip().strip('|'))]


def add_table(document, rows, images, warnings):
    count = max(map(len, rows))
    width = usable_width(document)
    table = document.add_table(rows=0, cols=count)
    table.autofit = False
    weights = [max(5, min(32, max(len(r[c]) if c < len(r) else 0 for r in rows))) for c in range(count)]
    widths = [Inches(width * weight / sum(weights)) for weight in weights]
    for column, value in zip(table.columns, widths):
        column.width = value
    borders = OxmlElement('w:tblBorders')
    for edge in ('top', 'left', 'bottom', 'right', 'insideH', 'insideV'):
        border = OxmlElement('w:' + edge)
        for key, value in (('val', 'single'), ('sz', '4'), ('color', 'D9D9D9')):
            border.set(qn('w:' + key), value)
        borders.append(border)
    table._tbl.tblPr.append(borders)
    for row_index, row in enumerate(rows):
        cells = table.add_row().cells
        for c, cell in enumerate(cells):
            cell.width = widths[c]
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            props = cell._tc.get_or_add_tcPr()
            margins = OxmlElement('w:tcMar')
            for edge in ('top', 'left', 'bottom', 'right'):
                margin = OxmlElement('w:' + edge)
                margin.set(qn('w:w'), '100')
                margin.set(qn('w:type'), 'dxa')
                margins.append(margin)
            props.append(margins)
            paragraph = cell.paragraphs[0]
            paragraph.paragraph_format.space_after = Pt(3)
            paragraph.paragraph_format.line_spacing = 1.1
            add_inline(paragraph, row[c] if c < len(row) else '', images, widths[c] / 914400 - 0.15, warnings)
            for run in paragraph.runs:
                run.font.size = Pt(9 if count > 8 else 10)
            if row_index == 0:
                shade = OxmlElement('w:shd')
                shade.set(qn('w:fill'), 'DFEAF2')
                props.append(shade)
                for run in paragraph.runs:
                    run.bold = True
                paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            elif len(row[c] if c < len(row) else '') < 14:
                paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        if row_index == 0:
            table.rows[0]._tr.get_or_add_trPr().append(OxmlElement('w:tblHeader'))
    document.add_paragraph().paragraph_format.space_after = Pt(3)


def render_docx(markdown, output, images=None):
    """Export rich Markdown with trusted image bytes and editable Office Math."""
    images = images or {}
    if any(not isinstance(data, bytes) or len(data) > 20_000_000 for data in images.values()):
        fail('DOCX_IMAGE_INVALID')
    document = Document()
    set_section(document.sections[0])
    for name in ('Normal', 'Title', 'Heading 1', 'Heading 2', 'Heading 3', 'List Bullet', 'List Number', 'Caption', 'Header', 'Footer'):
        style = document.styles[name]
        style.font.name = 'Microsoft JhengHei'
        style.font.color.rgb = RGBColor(0, 0, 0)
        style.element.get_or_add_rPr().get_or_add_rFonts().set(qn('w:eastAsia'), 'Microsoft JhengHei')
    normal = document.styles['Normal']
    normal.font.size = Pt(11)
    normal.paragraph_format.line_spacing = 1.25
    normal.paragraph_format.space_after = Pt(7)
    for name in ('Title', 'Heading 1', 'Heading 2', 'Heading 3'):
        document.styles[name].paragraph_format.keep_with_next = True
        for border in document.styles[name].element.xpath('./w:pPr/w:pBdr'):
            border.getparent().remove(border)
        document.styles[name].font.underline = False
    document.core_properties.author = ''
    document.core_properties.last_modified_by = ''
    title = next((readable(line.lstrip('# ')) for line in markdown.splitlines() if line.strip()), '報告')[:200]
    document.core_properties.title = title
    header = document.sections[0].header.paragraphs[0]
    header.add_run(title[:80]).font.size = Pt(8)
    footer = document.sections[0].footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    footer.add_run('第 ')
    page_field(footer, 'PAGE')
    footer.add_run(' 頁，共 ')
    page_field(footer, 'NUMPAGES')
    footer.add_run(' 頁')
    warnings = set()
    lines, index, in_code = markdown.splitlines(), 0, False
    while index < len(lines):
        line = lines[index]
        index += 1
        if line.startswith('```'):
            in_code = not in_code
            continue
        if in_code:
            paragraph = document.add_paragraph()
            paragraph.paragraph_format.space_after = Pt(0)
            paragraph.paragraph_format.line_spacing = 1.05
            run = paragraph.add_run(line)
            run.font.name, run.font.size = 'Consolas', Pt(9)
            continue
        if not line.strip():
            continue
        if line.strip() in {'<!-- pagebreak -->', r'\newpage', r'\pagebreak'}:
            document.add_page_break()
            continue
        if line.strip().startswith(('$$', r'\[')):
            opener, closer = ('$$', '$$') if line.strip().startswith('$$') else (r'\[', r'\]')
            formula = line.strip()[len(opener):]
            if formula.endswith(closer) and formula:
                formula = formula[:-len(closer)]
            else:
                parts = [formula]
                while index < len(lines) and closer not in lines[index]:
                    parts.append(lines[index])
                    index += 1
                if index < len(lines):
                    parts.append(lines[index].split(closer)[0])
                    index += 1
                formula = '\n'.join(parts)
            paragraph = document.add_paragraph()
            paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            try:
                paragraph._p.append(math_element('oMathPara', equation(formula)))
            except (ValueError, TypeError, KeyError, IndexError, NotImplementedError):
                paragraph.add_run('[公式需核對：' + formula + ']')
                warnings.add('EQUATION_REQUIRES_REVIEW')
            continue
        if line.startswith('|') and index < len(lines) and re.fullmatch(r'[\s|:\-]+', lines[index]):
            rows = [table_cells(line)]
            index += 1
            while index < len(lines) and lines[index].startswith('|'):
                rows.append(table_cells(lines[index]))
                index += 1
            count = max(map(len, rows))
            wide = count > 6
            if wide:
                # Keep the preceding heading with its table in the new section.
                heading = document.paragraphs[-1]._p if document.paragraphs and document.paragraphs[-1].style.name.startswith('Heading') else None
                set_section(document.add_section(WD_SECTION_START.NEW_PAGE), True)
                if heading is not None:
                    document._body._body.remove(heading)
                    document._body._body.insert(-1, heading)
            if count > 12:
                for start in range(1, count, 5):
                    selected = [0, *range(start, min(start + 5, count))]
                    document.add_paragraph(f'寬表欄位 {start + 1} 至 {selected[-1] + 1}（首欄重複供對照）', 'Caption')
                    add_table(document, [[row[c] if c < len(row) else '' for c in selected] for row in rows], images, warnings)
            else:
                add_table(document, rows, images, warnings)
            if wide and any(value.strip() for value in lines[index:]):
                set_section(document.add_section(WD_SECTION_START.NEW_PAGE))
            continue
        heading = re.match(r'^(#{1,6})\s+(.+)$', line)
        if heading:
            level = len(heading[1])
            paragraph = document.add_paragraph(style='Title' if level == 1 else f'Heading {min(level - 1, 3)}')
            text = heading[2]
        elif re.match(r'^\s*[-*]\s+', line):
            paragraph = document.add_paragraph(style='List Bullet')
            text = re.sub(r'^\s*[-*]\s+', '', line)
        elif re.match(r'^\s*\d+[.)]\s+', line):
            paragraph = document.add_paragraph(style='List Number')
            text = re.sub(r'^\s*\d+[.)]\s+', '', line)
        else:
            paragraph = document.add_paragraph()
            text = re.sub(r'^>\s?', '', line)
        add_inline(paragraph, text, images, usable_width(document), warnings)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    pending = output.with_suffix('.pending.docx')
    try:
        document.save(pending)
        check = Document(pending)
        if not any(p.text.strip() for p in check.paragraphs) and not check.tables and not check.inline_shapes and not check.element.xpath('//m:oMath'):
            fail('EMPTY_REPORT')
        with zipfile.ZipFile(pending) as archive:
            if archive.testzip():
                fail('DOCX_INVALID')
        pending.replace(output)
    finally:
        pending.unlink(missing_ok=True)
    return {'sha256': hashlib.sha256(output.read_bytes()).hexdigest(),
            'validation': 'OOXML_STRUCTURE_VERIFIED', 'visual_review_required': True,
            'warnings': sorted(warnings), 'equations': len(check.element.xpath('//m:oMath')),
            'images': len(check.inline_shapes)}


def task_images(study, task):
    """Resolve only registered, hash-verified task artifacts or approved snapshots."""
    workspace = study.workspace(study.user, task['id']).resolve()
    result = {}

    def register(name, path, sha):
        if Path(name).suffix.lower() not in {'.png', '.jpg', '.jpeg'}:
            return
        path = path.resolve()
        if not path.is_relative_to(workspace) or not sha:
            fail('PATH_OUTSIDE_WORKSPACE')
        path = filesystem_path(path)
        if path.stat().st_size > 20_000_000:
            fail('EXPORT_SIZE_LIMIT')
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != sha:
            fail('ARTIFACT_CHANGED')
        if name in result and result[name] != data:
            fail('DOCX_IMAGE_AMBIGUOUS')
        result[name] = data

    for values in [task.get('source_images', {}), task.get('generated_files', {}), *(value.get('artifacts', {}) for value in task.get('tool_results', {}).values())]:
        for name, artifact in values.items():
            register(name, study.safe_path(artifact['path']), artifact.get('sha256'))
    for ref, sha in task.get('source_hashes', {}).items():
        name = task.get('source_names', {}).get(ref, '')
        suffix = Path(name).suffix.lower()
        if suffix in {'.png', '.jpg', '.jpeg'} and re.fullmatch('[a-f0-9]{64}', sha):
            path = workspace / 'input' / (sha + suffix)
            register(name, path, sha)
            register(sha + suffix, path, sha)
    return result


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
        markdown = submission_markdown(task)
        require_submission_content(task, markdown)
        submission_hash = hashlib.sha256(markdown.encode()).hexdigest()
        omitted = {}
        if kind == "zip" and (not task.get("pdf") or
                              submission_export_stale(task, markdown, task.get("qa"), "pdf")):
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
        if kind == "docx" or "docx" not in artifacts or submission_export_stale(task, markdown, artifacts["docx"], "docx"):
            try:
                qa = render_docx(markdown, docx_path, images=task_images(study, task))
            except Exception as exc:
                code = error_code(exc)
                if kind != "zip" or not code.startswith("DOCX_"):
                    raise
                omitted["docx"] = code
                artifacts.pop("docx", None)
            else:
                artifacts["docx"] = {**qa, "path": str(docx_path.relative_to(study.root.resolve())),
                                     "submission_sha256": submission_hash}
        if kind == "zip":
            entries = {}

            def add(name, path, digest=None, legacy_text=False):
                path = path.resolve()
                if not path.is_relative_to(workspace):
                    fail("PATH_OUTSIDE_WORKSPACE")
                path = filesystem_path(path)
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

            if task.get("pdf") and "pdf" not in omitted:
                add("report.pdf", study.safe_path(task["pdf"]), task["qa"]["sha256"])
            if "docx" in artifacts:
                add("report.docx", study.safe_path(artifacts["docx"]["path"]), artifacts["docx"]["sha256"])
            entries["report.md"] = markdown.encode("utf-8")
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
                    elif ref.startswith(("crossref:", "research:")):
                        research = [result for result in task.get("tool_results", {}).values()
                                    if result.get("sources")]
                        research.append(task.get("completion_data", {}).get("research", {}))
                        source = next((source for result in research for source in result.get("sources", [])
                                       if isinstance(source, dict) and source.get("file_id") == ref), None)
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
            direct = task.get("request", {}).get("mode") == "complete_assignment"
            if direct:
                manifest.pop("unresolved")
            if task.get("plan", {}).get("deliverables"):
                if not direct or task.get("completion", {}).get("status") == "complete":
                    manifest["coverage"] = task["coverage"]
                if not direct:
                    manifest["required_actions"] = task.get("completion", {}).get("required_actions", [])
            if revision := task.get("revision"):
                manifest["revision"] = {key: revision[key] for key in
                                        ("parent_task_id", "unit_id", "mode", "affected_unit_ids")}
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
                                "submission_sha256": submission_hash,
                                "separate_review_checklist": direct,
                                "include_sources": bool(include_sources), "omitted_exports": omitted}
        task["exports"] = artifacts
        study.store.save(user, "task", task_id, task)
        study.store.event(user, task_id, "exported_" + kind)
        return task
