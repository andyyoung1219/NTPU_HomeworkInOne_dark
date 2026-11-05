"""Bounded visual transport and real offline attachment scanner checks."""
import base64
import hashlib
import io
import json
import shutil
import subprocess

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
