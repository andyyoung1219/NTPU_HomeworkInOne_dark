"""Build hash-bound visual evidence solely from this task's selected snapshots."""
import hashlib
import io
import re
import zipfile
from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw

from backend.p0.moodle import probe
from backend.services.ocr import IMAGE_SUFFIXES, image_bytes

MAX_VISUALS = 4
VISUAL_GUIDANCE = (
    '若附有來源影像，請實際判讀圖表、圖片與公式，引用影像標籤的 file_id 及頁／節。'
    '公式轉為 LaTeX（行內 $...$ 或獨立 $$...$$）；圖表說明須根據可見圖例、軸與數值，'
    '不可編造無法辨識的內容。OCR 文字僅供輔助，影像不清楚或被截斷時列為待核對。'
    '報告若需放入來源圖，使用 ![圖說](image_name)，image_name 必須是 visual_input.items 登錄的名稱。'
)


def ensure_visual_snapshot(study, user, task):
    workspace = study.workspace(user, task['id'])
    if task.get('visual_input'):
        return task['visual_input']
    from backend.services.documents import docx_image_bytes, docx_image_entries
    selected = []
    omitted = 0
    images = {}
    for ref, digest in task.get('source_hashes', {}).items():
        if not re.fullmatch('[a-f0-9]{64}', digest):
            raise probe.ProbeError('ASSET_CHANGED')
        suffix = Path(task['source_names'].get(ref, '')).suffix.lower()
        if suffix not in IMAGE_SUFFIXES | {'.pdf', '.docx'}:
            continue
        path = workspace / 'input' / (digest + suffix)
        if not path.resolve().is_relative_to(workspace.resolve()) or not path.is_file():
            raise probe.ProbeError('ASSET_CHANGED')
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise probe.ProbeError('ASSET_CHANGED')
        candidates = []
        if suffix in IMAGE_SUFFIXES:
            candidates.append((1, image_bytes(data)))
        elif suffix == '.pdf':
            with pymupdf.open(stream=data, filetype='pdf') as document:
                for number, page in enumerate(document, 1):
                    if not (page.get_images() or page.get_drawings() or
                            re.search(r'[=∑∫√∂∞]', page.get_text())):
                        continue
                    if len(candidates) + len(selected) >= MAX_VISUALS:
                        omitted += 1
                        continue
                    scale = min(2.0, 1400 / max(page.rect.width, page.rect.height))
                    png = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).tobytes('png')
                    candidates.append((number, png))
        else:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                names = docx_image_entries(archive)
                for number, (name, crop) in enumerate(names, 1):
                    if len(candidates) + len(selected) >= MAX_VISUALS:
                        omitted += 1
                        continue
                    candidates.append((f'image-{number}', docx_image_bytes(archive, name, crop)))
        for position, png in candidates:
            if len(selected) >= MAX_VISUALS:
                omitted += 1
                continue
            name = f'source-{digest[:16]}-{position}.png'
            target = workspace / 'input' / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(png)
            artifact = {'path': str(target.relative_to(study.root)),
                        'sha256': hashlib.sha256(png).hexdigest(),
                        'file_id': ref, 'source_sha256': digest, 'image_name': name,
                        'page' if isinstance(position, int) else 'section': position}
            images[name] = artifact
            selected.append((artifact, png))
    if not selected:
        return None
    width, height = 1100, 1500
    columns = min(2, len(selected))
    rows = (len(selected) + columns - 1) // columns
    canvas = Image.new('RGB', (columns * width, rows * height), 'white')
    draw = ImageDraw.Draw(canvas)
    for index, (artifact, png) in enumerate(selected):
        image = Image.open(io.BytesIO(png)).convert('RGB')
        image.thumbnail((width - 24, height - 55))
        x, y = (index % columns) * width, (index // columns) * height
        label = f"file_id={artifact['file_id']} position={artifact.get('page', artifact.get('section'))}"
        draw.text((x + 12, y + 8), label, fill='black')
        canvas.paste(image, (x + (width - image.width) // 2, y + 45))
    stream = io.BytesIO()
    canvas.save(stream, format='PNG', optimize=True)
    data = stream.getvalue()
    if len(data) > 6 * 1024 * 1024:
        raise probe.ProbeError('VISUAL_INPUT_TOO_LARGE')
    target = workspace / 'input/visual-evidence.png'
    target.write_bytes(data)
    task['source_images'] = images
    task['visual_input'] = {'path': str(target.relative_to(study.root)),
        'sha256': hashlib.sha256(data).hexdigest(), 'items': [item for item, _ in selected],
        'omitted': omitted, 'limitations': (['Only the first four visual pages/images are attached; review the remaining sources.'] if omitted else [])}
    return task['visual_input']


def task_attachment(study, task):
    visual = task.get('visual_input')
    request = task.get('request', {})
    permitted = request.get('mode') == 'complete_assignment' or request.get('allow_visual_transfer') is True
    if not permitted:
        return None
    if not visual:
        return None
    for item in visual['items']:
        if task['source_hashes'].get(item['file_id']) != item['source_sha256']:
            raise probe.ProbeError('ASSET_CHANGED')
    path = study.safe_path(visual['path'])
    if not path.resolve().is_relative_to(study.workspace(study.require_user(), task['id']).resolve()):
        raise probe.ProbeError('PATH_OUTSIDE_WORKSPACE')
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != visual['sha256']:
        raise probe.ProbeError('ASSET_CHANGED')
    return {'data': data, 'mime_type': 'image/png', 'sha256': visual['sha256'], 'approved': True}
