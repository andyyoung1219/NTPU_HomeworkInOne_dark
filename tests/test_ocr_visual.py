import hashlib
import io

import pymupdf
import pytest
from docx import Document
from docx.oxml import parse_xml
from PIL import Image, ImageDraw, ImageFont
from test_mvp import service as service

from backend.p0.moodle import probe
from backend.services.documents import parse
from backend.services.ocr import image_bytes, recognize
from backend.services.visual import ensure_visual_snapshot, task_attachment


def text_image(text='Assignment: compare two methods'):
    image = Image.new('RGB', (1100, 240), 'white')
    font = ImageFont.truetype('C:/Windows/Fonts/arial.ttf', 42)
    ImageDraw.Draw(image).text((30, 50), text, fill='black', font=font)
    stream = io.BytesIO()
    image.save(stream, format='PNG')
    return stream.getvalue()


def source_task(service, data, suffix='.png'):
    digest = hashlib.sha256(data).hexdigest()
    task = {'id': 'visual-test', 'request': {'allow_visual_transfer': True}, 'source_names': {'image-ref': 'question' + suffix},
            'source_hashes': {'image-ref': digest}}
    path = service.workspace(service.user, task['id']) / 'input' / (digest + suffix)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return task, path


def test_actual_local_ocr_reads_pixels_and_returns_confidence():
    result = recognize(text_image())
    assert 'compare two methods' in ' '.join(x['text'] for x in result['lines'])
    assert all(0 <= x['confidence'] <= 1 and len(x['box']) == 4 for x in result['lines'])


def test_actual_scanned_pdf_ocr_preserves_page_sources():
    with pymupdf.open() as document:
        page = document.new_page(width=600, height=200)
        page.insert_image(page.rect, stream=text_image())
        data = document.tobytes()
    chunks, flags = parse(data, '.pdf', 'application/pdf', 'scan')
    assert chunks[0]['file_id'] == 'scan' and chunks[0]['page'] == 1
    assert 'compare' in chunks[0]['text']
    assert 'OCR_TEXT_REQUIRES_REVIEW' in flags
    assert chunks[0]['ocr']['engine'] == 'RapidOCR PP-OCRv6 CPU'


def test_word_embedded_picture_and_native_fraction_are_extracted():
    document = Document()
    document.add_paragraph('Please solve the fraction')
    document.add_picture(io.BytesIO(text_image()))
    paragraph = document.add_paragraph()
    paragraph._p.append(parse_xml('<m:oMath xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math"><m:f><m:num><m:r><m:t>x</m:t></m:r></m:num><m:den><m:r><m:t>2</m:t></m:r></m:den></m:f></m:oMath>'))
    stream = io.BytesIO()
    document.save(stream)
    chunks, flags = parse(stream.getvalue(), '.docx', 'application/octet-stream', 'word')
    assert any(r'\frac{x}{2}' in c['text'] for c in chunks)
    assert any('compare' in c['text'] for c in chunks)
    assert 'VISUAL_CONTENT_AVAILABLE' in flags


def test_image_without_ocr_text_is_honestly_marked_for_vision(monkeypatch):
    monkeypatch.setattr('backend.services.documents.recognize', lambda data: {'lines': [], 'engine': 'test'})
    chunks, flags = parse(text_image(), '.png', 'image/png', 'figure')
    assert '須於核准後' in chunks[0]['text'] and chunks[0]['has_visual']
    assert 'VISUAL_CONTENT_AVAILABLE' in flags


def test_visual_snapshot_is_source_bound_and_contact_sheet_is_valid(service):
    task, _ = source_task(service, text_image())
    result = ensure_visual_snapshot(service, service.user, task)
    assert result['items'][0]['file_id'] == 'image-ref'
    attachment = task_attachment(service, task)
    assert attachment['approved'] is True
    assert hashlib.sha256(attachment['data']).hexdigest() == result['sha256']
    image = Image.open(io.BytesIO(attachment['data']))
    assert image.size == (1100, 1500)
    task['source_hashes']['image-ref'] = 'f' * 64
    with pytest.raises(probe.ProbeError, match='ASSET_CHANGED'):
        task_attachment(service, task)


def test_changed_original_and_contact_sheet_are_refused(service):
    task, path = source_task(service, text_image())
    path.write_bytes(b'changed')
    with pytest.raises(probe.ProbeError, match='ASSET_CHANGED'):
        ensure_visual_snapshot(service, service.user, task)
    task, path = source_task(service, text_image())
    result = ensure_visual_snapshot(service, service.user, task)
    service.safe_path(result['path']).write_bytes(b'changed')
    with pytest.raises(probe.ProbeError, match='ASSET_CHANGED'):
        task_attachment(service, task)


def test_visual_pdf_page_limit_is_explicit(service):
    with pymupdf.open() as document:
        for _ in range(6):
            page = document.new_page(width=600, height=200)
            page.insert_image(page.rect, stream=text_image())
        data = document.tobytes()
    task, _ = source_task(service, data, '.pdf')
    result = ensure_visual_snapshot(service, service.user, task)
    assert len(result['items']) == 4 and result['omitted'] == 2 and result['limitations']


def test_image_validation_rejects_non_image_and_pixel_bomb(monkeypatch):
    with pytest.raises(probe.ProbeError):
        image_bytes(b'not an image')
    monkeypatch.setattr('backend.services.ocr.MAX_PIXELS', 10)
    with pytest.raises(probe.ProbeError, match='IMAGE_INVALID_OR_TOO_LARGE'):
        image_bytes(text_image())


def test_blank_pdf_is_not_fabricated_into_a_question(monkeypatch):
    with pymupdf.open() as document:
        document.new_page()
        data = document.tobytes()
    monkeypatch.setattr('backend.services.documents.recognize', lambda data: {'lines': []})
    with pytest.raises(probe.ProbeError, match='pdf_requires_ocr'):
        parse(data, '.pdf', 'application/pdf', 'blank')


def test_image_transmission_needs_separate_manual_approval(service):
    task, _ = source_task(service, text_image())
    ensure_visual_snapshot(service, service.user, task)
    task['request'] = {'mode': 'report_draft'}
    assert task_attachment(service, task) is None
    task['request']['mode'] = 'complete_assignment'
    assert task_attachment(service, task)['approved'] is True


def test_ocr_cancel_and_document_deadline_stop_before_inference():
    import threading

    from backend.services.ocr import ocr_scope
    cancel = threading.Event()
    cancel.set()
    with ocr_scope(cancel=cancel):
        with pytest.raises(probe.ProbeError, match='CANCELLED'):
            recognize(text_image())
    with ocr_scope(timeout=0):
        with pytest.raises(probe.ProbeError, match='OCR_UNAVAILABLE_OR_TIMEOUT'):
            recognize(text_image())


def test_cancel_kills_an_already_running_ocr_worker(monkeypatch):
    import subprocess
    import sys
    import threading
    import time

    from backend.services.ocr import ocr_scope
    original = subprocess.Popen
    processes = []
    def slow_worker(command, **kwargs):
        process = original([sys.executable, '-c', 'import time; time.sleep(15)'], **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr('backend.services.ocr.subprocess.Popen', slow_worker)
    cancel = threading.Event()
    timer = threading.Timer(0.3, cancel.set)
    timer.start()
    started = time.monotonic()
    try:
        with ocr_scope(cancel=cancel):
            with pytest.raises(probe.ProbeError, match='CANCELLED'):
                recognize(text_image())
    finally:
        timer.cancel()
    assert time.monotonic() - started < 3
    assert processes and processes[0].poll() is not None


def test_native_word_formula_degree_and_matrix_structure_are_not_lost():
    from lxml import etree

    from backend.services.documents import omml_text
    ns='http://schemas.openxmlformats.org/officeDocument/2006/math'
    radical=etree.fromstring(f'<m:rad xmlns:m="{ns}"><m:deg><m:r><m:t>3</m:t></m:r></m:deg><m:e><m:r><m:t>x</m:t></m:r></m:e></m:rad>')
    assert omml_text(radical) == r'\sqrt[3]{x}'
    matrix=etree.fromstring(f'<m:m xmlns:m="{ns}"><m:mr><m:e><m:r><m:t>a</m:t></m:r></m:e><m:e><m:r><m:t>b</m:t></m:r></m:e></m:mr><m:mr><m:e><m:r><m:t>c</m:t></m:r></m:e><m:e><m:r><m:t>d</m:t></m:r></m:e></m:mr></m:m>')
    assert omml_text(matrix) == r'\begin{matrix}a & b \\ c & d\end{matrix}'


def test_word_images_ignore_unreferenced_zip_media():
    import zipfile

    from backend.services.documents import docx_image_entries
    doc=Document()
    doc.add_picture(io.BytesIO(text_image()))
    stream=io.BytesIO()
    doc.save(stream)
    with zipfile.ZipFile(stream,'a') as archive:
        archive.writestr('word/media/orphan.png',text_image('orphan'))
    with zipfile.ZipFile(stream) as archive:
        entries=docx_image_entries(archive)
        assert len(entries)==1 and entries[0][0].endswith('image1.png')


def test_short_selectable_formula_survives_empty_pixel_ocr(monkeypatch):
    with pymupdf.open() as document:
        page=document.new_page()
        page.insert_text((60,70),'x = 2')
        data=document.tobytes()
    monkeypatch.setattr('backend.services.documents.recognize',lambda data:{'lines':[]})
    chunks,_=parse(data,'.pdf','application/pdf','formula')
    assert 'x = 2' in chunks[0]['text']
