"""Bounded offline OCR using models bundled in the locked RapidOCR wheel."""
import io
import json
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from pathlib import Path

from PIL import Image

from backend.p0.moodle import MAX_BYTES, probe

MAX_PIXELS = 20_000_000
IMAGE_SUFFIXES = {'.png', '.jpg', '.jpeg', '.webp'}
OCR_CONTEXT = ContextVar('ocr_budget', default=None)


def image_bytes(data):
    if not data or len(data) > MAX_BYTES:
        raise probe.ProbeError('IMAGE_SIZE_INVALID')
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in {'PNG', 'JPEG', 'WEBP'} or getattr(image, 'n_frames', 1) != 1:
                raise ValueError()
            if image.width * image.height > MAX_PIXELS:
                raise ValueError()
            image.load()
            rgb = Image.new('RGB', image.size, 'white')
            if image.mode == 'RGBA':
                rgb.paste(image, mask=image.getchannel('A'))
            else:
                rgb.paste(image.convert('RGB'))
            output = io.BytesIO()
            rgb.save(output, format='PNG')
            return output.getvalue()
    except (OSError, ValueError, Image.DecompressionBombError):
        raise probe.ProbeError('IMAGE_INVALID_OR_TOO_LARGE') from None


@contextmanager
def ocr_scope(timeout=60, cancel=None):
    previous = OCR_CONTEXT.get()
    deadline = time.monotonic() + max(0, timeout)
    if previous:
        deadline = min(deadline, previous[0])
        cancel = cancel or previous[1]
    token = OCR_CONTEXT.set((deadline, cancel))
    try:
        yield
    finally:
        OCR_CONTEXT.reset(token)


def document_budget(method):
    @wraps(method)
    def wrapped(*args, **kwargs):
        with ocr_scope():
            return method(*args, **kwargs)
    return wrapped


def check_budget():
    context = OCR_CONTEXT.get()
    if context:
        if context[1] and context[1].is_set():
            raise probe.ProbeError('CANCELLED')
        if time.monotonic() >= context[0]:
            raise probe.ProbeError('OCR_UNAVAILABLE_OR_TIMEOUT')


def recognize(data):
    """Only normalized bytes cross a cancellable, bounded process boundary."""
    check_budget()
    normalized = image_bytes(data)
    process = None
    input_stream = tempfile.TemporaryFile()
    input_stream.write(normalized)
    input_stream.seek(0)
    deadline = min(time.monotonic() + 40, (OCR_CONTEXT.get() or (float('inf'),))[0])
    try:
        process = subprocess.Popen([sys.executable, '-m', 'backend.services.ocr', '--worker'],
            stdin=input_stream, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        while True:
            check_budget()
            if time.monotonic() >= deadline:
                raise probe.ProbeError('OCR_UNAVAILABLE_OR_TIMEOUT')
            try:
                output, _ = process.communicate(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                continue
        value = json.loads(output.decode('utf-8').splitlines()[-1])
        if process.returncode or not isinstance(value.get('lines'), list):
            raise ValueError()
        return value
    except (OSError, ValueError, IndexError):
        raise probe.ProbeError('OCR_UNAVAILABLE_OR_TIMEOUT') from None
    finally:
        if process:
            if process.poll() is None:
                process.kill()
            process.communicate()
        input_stream.close()


def worker():
    import contextlib

    import rapidocr
    from rapidocr import RapidOCR

    models = Path(rapidocr.__file__).parent / 'models'
    names = {'Det': 'PP-OCRv6_det_small.onnx', 'Cls': 'ch_ppocr_mobile_v2.0_cls_mobile.onnx',
             'Rec': 'PP-OCRv6_rec_small.onnx'}
    params = {'Global.log_level': 'critical', 'EngineConfig.onnxruntime.intra_op_num_threads': 2,
              'EngineConfig.onnxruntime.inter_op_num_threads': 1}
    for kind, name in names.items():
        path = models / name
        if not path.is_file():
            raise RuntimeError('OCR_MODELS_UNAVAILABLE')
        params[kind + '.model_path'] = str(path)
    data = sys.stdin.buffer.read(MAX_BYTES + 1)
    normalized = image_bytes(data)
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        engine = RapidOCR(params=params)
        result = engine(normalized)
    lines = []
    for text, score, box in zip(result.txts or [], result.scores or [],
                                result.boxes if result.boxes is not None else []):
        lines.append({'text': text[:2000], 'confidence': round(float(score), 4),
                      'box': [[round(float(x), 1), round(float(y), 1)] for x, y in box]})
    print(json.dumps({'engine': 'RapidOCR PP-OCRv6 CPU', 'lines': lines[:1000]}, ensure_ascii=True))


if __name__ == '__main__':
    try:
        worker()
    except Exception:
        print('{"error":"OCR_FAILED"}')
        sys.exit(1)
