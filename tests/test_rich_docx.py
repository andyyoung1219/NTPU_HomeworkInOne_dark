"""Rich document structure and image authorization regressions."""
import hashlib
import io
import zipfile

import pytest
from docx import Document
from PIL import Image
from test_mvp import service as service

from backend.services.exports import render_docx, task_images
from backend.services.study import StudyError


def png_bytes():
    buffer = io.BytesIO()
    Image.new('RGB', (400, 160), 'white').save(buffer, format='PNG')
    return buffer.getvalue()


def test_editable_office_equations_cover_fraction_radical_matrix_and_scripts(tmp_path):
    target = tmp_path / 'equations.docx'
    result = render_docx(r'''# 可編輯公式測試

內文公式 $x_i^2 + \frac{1}{2}$ 與 \(\sqrt{x+1}\)。

$$
\begin{pmatrix}a&b\\c&d\end{pmatrix}
$$

$$\sum_{i=1}^{n} x_i = \frac{n(n+1)}{2}$$
''', target)
    document = Document(target)
    xml = document.element
    assert result['equations'] == 4
    assert not result['warnings']
    assert xml.xpath('//m:f') and xml.xpath('//m:rad') and xml.xpath('//m:m')
    assert xml.xpath('//m:sSubSup') or xml.xpath('//m:sSup')
    assert not xml.xpath('//m:e/m:e')
    assert len(xml.xpath('//m:oMathPara')) == 2
    assert xml.xpath('//m:d/m:dPr/m:grow')
    assert not document.styles['Title'].element.xpath('./w:pPr/w:pBdr')
    assert document.styles['Normal'].font.name == 'Microsoft JhengHei'


def test_bad_latex_is_preserved_and_flagged_instead_of_dropping_content(tmp_path):
    target = tmp_path / 'invalid.docx'
    result = render_docx('# 公式檢查\n\n$\\frac{1}$\n\n$\\unrecognizedcommand{x}$', target)
    assert 'EQUATION_REQUIRES_REVIEW' in result['warnings']
    assert '公式需核對' in '\n'.join(p.text for p in Document(target).paragraphs)


def test_wide_tables_use_landscape_and_extreme_width_splits_without_losing_cells(tmp_path):
    target = tmp_path / 'wide.docx'
    headers = [f'欄位{i}' for i in range(14)]
    values = [f'唯一值{i}' for i in range(14)]
    markdown = '# 寬表測試\n\n## 數據\n|' + '|'.join(headers) + '|\n|' + '|'.join(['---'] * 14) + '|\n|' + '|'.join(values) + '|\n\n## 結論\n資料已完整列出。'
    render_docx(markdown, target)
    document = Document(target)
    assert len(document.tables) == 3
    assert any(s.page_width > s.page_height for s in document.sections)
    assert document.sections[-1].page_width < document.sections[-1].page_height
    text = '\n'.join(cell.text for table in document.tables for row in table.rows for cell in row.cells)
    assert all(value in text for value in values)
    assert all(table.rows[0]._tr.xpath('./w:trPr/w:tblHeader') for table in document.tables)
    assert all(table._tbl.xpath('./w:tblPr/w:tblBorders') for table in document.tables)


def test_inline_format_code_fields_pagebreak_and_registered_images(tmp_path):
    target = tmp_path / 'rich.docx'
    data = png_bytes()
    result = render_docx('''# 格式測試

**粗體**、*斜體*、`inline_code` 與 [來源](https://example.org/paper)。

![合成圖表](chart.png)

![未核准圖](https://example.invalid/private.png)

```python
def solve():
    return '<b>&' ** 2
```

<!-- pagebreak -->

第二頁文字。
''', target, images={'chart.png': data})
    document = Document(target)
    assert len(document.inline_shapes) == 1 and result['images'] == 1
    assert 'IMAGE_NOT_REGISTERED' in result['warnings']
    runs = [run for paragraph in document.paragraphs for run in paragraph.runs]
    assert any(run.bold and run.text == '粗體' for run in runs)
    assert any(run.italic and run.text == '斜體' for run in runs)
    assert any(run.text == "    return '<b>&' ** 2" for run in runs)
    assert document.element.xpath('//w:br[@w:type="page"]')
    assert document.sections[0].footer._element.xpath('.//w:fldSimple[@w:instr="PAGE"]')
    assert document.sections[0].footer._element.xpath('.//w:fldSimple[@w:instr="NUMPAGES"]')
    with zipfile.ZipFile(target) as archive:
        assert len([name for name in archive.namelist() if name.startswith('word/media/')]) == 1
        assert b'TargetMode="External"' not in archive.read('word/_rels/document.xml.rels')


def test_table_inline_equations_and_escaped_pipes_preserve_structure(tmp_path):
    target = tmp_path / 'table.docx'
    render_docx('# 表格\n\n| 名稱 | 公式 |\n|---|---|\n| **A\\|B** | $x^2$ |', target)
    document = Document(target)
    assert len(document.tables[0].columns) == 2
    assert document.tables[0].cell(1, 0).text == 'A|B'
    assert document.tables[0].cell(1, 0).paragraphs[0].runs[0].bold
    assert document.tables[0]._tbl.xpath('.//m:oMath')


def test_task_images_validate_registered_snapshot_and_hash(service):
    data = png_bytes()
    workspace = service.workspace(service.user, 'image-task')
    path = workspace / 'input/source.png'
    path.parent.mkdir(parents=True)
    path.write_bytes(data)
    task = {'id': 'image-task', 'source_images': {'source.png': {
        'path': str(path.relative_to(service.root)), 'sha256': hashlib.sha256(data).hexdigest()}}}
    assert task_images(service, task) == {'source.png': data}
    path.write_bytes(b'tampered')
    with pytest.raises(StudyError, match='ARTIFACT_CHANGED'):
        task_images(service, task)


def test_task_images_refuse_outside_workspace_and_name_collision(service):
    data = png_bytes()
    outside = service.root / 'private.png'
    outside.write_bytes(data)
    artifact = {'path': 'private.png', 'sha256': hashlib.sha256(data).hexdigest()}
    task = {'id': 'image-task', 'source_images': {'image.png': artifact}}
    with pytest.raises(StudyError, match='PATH_OUTSIDE_WORKSPACE'):
        task_images(service, task)
    workspace = service.workspace(service.user, task['id'])
    workspace.mkdir(parents=True)
    for index in range(2):
        path = workspace / f'image{index}.png'
        path.write_bytes(data + bytes([index]))
    task = {'id': 'image-task', 'tool_results': {str(index): {'artifacts': {'image.png': {
        'path': str((workspace / f'image{index}.png').relative_to(service.root)),
        'sha256': hashlib.sha256(data + bytes([index])).hexdigest()}}} for index in range(2)}}
    with pytest.raises(StudyError, match='DOCX_IMAGE_AMBIGUOUS'):
        task_images(service, task)


def test_inline_picture_uses_body_width_and_never_uses_a_caller_path(tmp_path):
    target = tmp_path / 'picture.docx'
    private = tmp_path / 'private.png'
    private.write_bytes(png_bytes())
    result = render_docx(f'# 圖片\n\n![local]({private})', target)
    assert result['images'] == 0 and result['warnings'] == ['IMAGE_NOT_REGISTERED']
    with pytest.raises(StudyError, match='DOCX_IMAGE_INVALID'):
        render_docx('# 圖片\n\n![local](private.png)', target, images={'private.png': private})
