"""Bounded visual transport and real offline attachment scanner checks."""
import base64
import concurrent.futures
import hashlib
import io
import json
import shutil
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw, ImageFont

from backend.p0.ai import MAX_ATTACHMENT_BYTES, AIAdapter, AIError, encode_attachment
from backend.p0.evidence import ROOT

NODE = r'''
const fs = require('node:fs');
const {decodeAttachment} = require('./ntpu-ai-bridge/src/p0.cjs');
const payload = JSON.parse(fs.readFileSync(0, 'utf8'));
(async () => {
  try {
    const image = await decodeAttachment(payload.attachment);
    if (payload.scan) {
      const {AttachmentScanner} = require('./ntpu-ai-bridge/vendor/src/security/attachments');
      const scanner = new AttachmentScanner({run: async () => { const e = new Error('native absent fixture'); e.code = 'ENOENT'; throw e; }});
      const proof = await scanner.scan(image.buffer, image.mimeType);
      console.log(JSON.stringify({ok:true, proof}));
    } else console.log(JSON.stringify({ok:true, name:image.name, mimeType:image.mimeType, bytes:image.buffer.length}));
  } catch (error) { console.log(JSON.stringify({ok:false,error:error.message})); }
})();
'''


def png(text='Synthetic diagram A -> B'):
    image = Image.new('RGB', (1000, 140), 'white')
    font = ImageFont.load_default(size=34)
    ImageDraw.Draw(image).text((18, 45), text, font=font, fill='black')
    stream = io.BytesIO()
    image.save(stream, format='PNG')
    return stream.getvalue()


def approved(data):
    return {'data': data, 'mime_type': 'image/png',
            'sha256': hashlib.sha256(data).hexdigest(), 'approved': True}


def node_decode(value, *, scan=False):
    result = subprocess.run([shutil.which('node'), '-e', NODE], cwd=ROOT,
        input=json.dumps({'attachment': value, 'scan': scan}),
        capture_output=True, text=True, encoding='utf-8', timeout=65)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_stdio_preserves_long_unicode_with_and_without_visual_input(tmp_path, monkeypatch):
    echo = tmp_path / 'echo-chat.cjs'
    echo.write_text("""
const { BrowserBridge } = require(process.cwd() + '/ntpu-ai-bridge/vendor/src/browser');
BrowserBridge.prototype.chat = async (prompt, { attachment }) => ({
  text: JSON.stringify([prompt, attachment?.buffer.length ?? 0]),
});
""", encoding='utf-8')
    monkeypatch.setenv('NODE_OPTIONS', f'--require="{echo.as_posix()}"')

    data = png()
    adapter = AIAdapter()
    try:
        for prompt, attachment in (('中' * 17000, None), ('\x00' * 47997 + '😀\ud800', None),
                                   ('中' * 48000, approved(data))):
            _, result = adapter.begin(prompt, attachment=attachment, timeout_seconds=10)
            assert json.loads(result.result(timeout=10)) == [prompt, len(data) if attachment else 0]
        with pytest.raises(AIError, match='INVALID_REQUEST'):
            adapter.begin('中' * 48001, timeout_seconds=10)
    finally:
        adapter.close()


@pytest.mark.parametrize('phase', ['decode', 'before_upload'])
def test_stdio_cancels_visual_request_before_upload(tmp_path, monkeypatch, phase):
    pause = tmp_path / 'pause-chat.cjs'
    pause.write_text("""
const fs = require('node:fs');
const root = process.cwd() + '/ntpu-ai-bridge';
const { BrowserBridge } = require(root + '/vendor/src/browser');
const sharp = require(root + '/node_modules/sharp');
const waitForRelease = async () => {
  fs.writeFileSync(process.env.TEST_BRIDGE_PAUSED, '');
  while (!fs.existsSync(process.env.TEST_BRIDGE_RELEASE)) {
    await new Promise(resolve => setTimeout(resolve, 5));
  }
};
const original = sharp.prototype.stats;
sharp.prototype.stats = async function () {
  if (process.env.TEST_BRIDGE_PHASE === 'decode') await waitForRelease();
  return original.call(this);
};
BrowserBridge.prototype.chat = async (prompt, options) => {
  if (options.attachment) {
    if (process.env.TEST_BRIDGE_PHASE === 'before_upload') await waitForRelease();
    options.onUpload?.('sources.png');
    fs.writeFileSync(process.env.TEST_BRIDGE_UPLOADED, '');
  }
  return { text: prompt };
};
""", encoding='utf-8')
    paused, release, uploaded = (tmp_path / name for name in ('paused', 'release', 'uploaded'))
    monkeypatch.setenv('NODE_OPTIONS', f'--require="{pause.as_posix()}"')
    monkeypatch.setenv('TEST_BRIDGE_PHASE', phase)
    monkeypatch.setenv('TEST_BRIDGE_PAUSED', str(paused))
    monkeypatch.setenv('TEST_BRIDGE_RELEASE', str(release))
    monkeypatch.setenv('TEST_BRIDGE_UPLOADED', str(uploaded))

    adapter = AIAdapter()
    with concurrent.futures.ThreadPoolExecutor() as pool:
        begin = pool.submit(adapter.begin, 'Approved visual', attachment=approved(png()),
                            task_id='cancel-visual', timeout_seconds=10)
        try:
            deadline = time.monotonic() + 8
            while not paused.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert paused.exists(), 'The bridge did not reach the expected pause'
            assert adapter.rpc('cancel_task', task_id='cancel-visual', timeout_seconds=3)['requested']
            release.touch()
            with pytest.raises(AIError, match='CANCELLED'):
                _, result = begin.result(timeout=5)
                result.result(timeout=5)
            assert not uploaded.exists()
            _, result = adapter.begin('Next task', timeout_seconds=5)
            assert result.result(timeout=5) == 'Next task'
        finally:
            release.touch()
            adapter.close()


def test_cancel_before_pipe_write_does_not_start_chat(monkeypatch):
    cancel = threading.Event()
    cancel.set()
    adapter = AIAdapter()
    adapter.process = SimpleNamespace(stdin=io.StringIO())
    monkeypatch.setattr(adapter, 'start', lambda: None)
    with pytest.raises(AIError, match='CANCELLED'):
        adapter.begin('Approved prompt', task_id='pre-cancelled', cancel=cancel, timeout_seconds=5)
    assert not adapter.jobs and not adapter.requests
    assert not adapter.process.stdin.getvalue()


def test_begin_binds_visual_bytes_to_approved_hash_without_paths(monkeypatch):
    adapter = AIAdapter()
    calls = []
    monkeypatch.setattr(adapter, 'rpc', lambda method, **options: calls.append((method, options)))
    data = png()
    task_id, _ = adapter.begin('Describe the approved diagram', attachment=approved(data))
    assert calls[0][0] == 'start_task'
    visual = calls[0][1]['attachment']
    assert base64.b64decode(visual['data_base64']) == data
    assert visual['sha256'] == hashlib.sha256(data).hexdigest()
    assert visual['approved'] is True
    assert task_id in adapter.jobs


@pytest.mark.parametrize('change,code', [
    ({'approved': False}, 'ATTACHMENT_APPROVAL_REQUIRED'),
    ({'sha256': '0' * 64}, 'ATTACHMENT_CHANGED'),
    ({'mime_type': 'image/jpeg'}, 'INVALID_ATTACHMENT'),
    ({'path': 'arbitrary.png'}, 'INVALID_ATTACHMENT'),
    ({'data': b'not a png'}, 'INVALID_ATTACHMENT'),
    ({'data': b'\x89PNG\r\n\x1a\n' + b'0' * MAX_ATTACHMENT_BYTES}, 'INVALID_ATTACHMENT'),
])
def test_python_rejects_unapproved_changed_or_unbounded_attachment(change, code):
    with pytest.raises(AIError, match=code):
        encode_attachment({**approved(png()), **change})


def test_node_decodes_only_png_and_returns_fixed_name():
    data = png()
    result = node_decode(encode_attachment(approved(data)))
    assert result == {'ok': True, 'name': 'sources.png', 'mimeType': 'image/png', 'bytes': len(data)}


@pytest.mark.parametrize('change,code', [
    ({'approved': False}, 'ATTACHMENT_APPROVAL_REQUIRED'),
    ({'sha256': '0' * 64}, 'ATTACHMENT_CHANGED'),
    ({'data_base64': 'not base64!'}, 'INVALID_ATTACHMENT'),
    ({'data_base64': 'YQ======'}, 'INVALID_ATTACHMENT'),
    ({'data_base64': 'A' * (8 * 1024 * 1024 + 4)}, 'INVALID_ATTACHMENT'),
    ({'mime_type': 'image/jpeg'}, 'INVALID_ATTACHMENT'),
    ({'path': 'arbitrary.png'}, 'INVALID_ATTACHMENT'),
])
def test_node_rejects_malformed_unapproved_and_oversized_visuals(change, code):
    result = node_decode({**encode_attachment(approved(png())), **change})
    assert result == {'ok': False, 'error': code}


def test_node_rejects_png_header_without_decodable_pixels():
    data = b'\x89PNG\r\n\x1a\n' + b'not an image'
    result = node_decode(encode_attachment(approved(data)))
    assert result == {'ok': False, 'error': 'INVALID_ATTACHMENT'}


def test_real_offline_scanner_accepts_safe_synthetic_image():
    result = node_decode(encode_attachment(approved(png())), scan=True)
    assert result['ok'], result
    proof = result['proof']
    assert proof['checked'] is True and proof['pages'] == 1
    assert proof['engine'] == 'Bundled Tesseract WASM'


def test_real_offline_scanner_rejects_image_with_credential_field():
    data = png('password: NeverUploadPrivateValue')
    result = node_decode(encode_attachment(approved(data)), scan=True)
    assert not result['ok']
    assert '憑證' in result['error']


def test_node_rejects_decompression_image_above_pixel_limit():
    image = Image.new('1', (5000, 5000), 1)
    stream = io.BytesIO()
    image.save(stream, format='PNG')
    result = node_decode(encode_attachment(approved(stream.getvalue())))
    assert result == {'ok': False, 'error': 'INVALID_ATTACHMENT'}


def test_unapproved_visual_never_reaches_rpc(monkeypatch):
    calls = []
    adapter = AIAdapter()
    monkeypatch.setattr(adapter, 'rpc', lambda *args, **kwargs: calls.append(args))
    with pytest.raises(AIError, match='ATTACHMENT_APPROVAL_REQUIRED'):
        adapter.begin('private image', attachment={**approved(png()), 'approved': False})
    assert not calls and not adapter.jobs
