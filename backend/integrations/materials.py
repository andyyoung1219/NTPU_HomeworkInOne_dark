"""Download original teaching files without executing or sending them to AI."""

import hashlib
import io
import json
import re
import zipfile
from pathlib import Path
from urllib.parse import urlunsplit

import pymupdf

from backend.p0.moodle import MAX_BYTES, probe, validate_url

FORMATS = {
    ".pdf": {"application/pdf"},
    ".docx": {"application/vnd.openxmlformats-officedocument.wordprocessingml.document", "application/zip"},
    ".pptx": {"application/vnd.openxmlformats-officedocument.presentationml.presentation", "application/zip"},
    ".xlsx": {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "application/zip"},
    ".zip": {"application/zip", "application/x-zip-compressed"},
    ".png": {"image/png"}, ".jpg": {"image/jpeg"}, ".jpeg": {"image/jpeg"},
    ".txt": {"text/plain"}, ".csv": {"text/csv", "text/plain", "application/vnd.ms-excel"},
    ".tsv": {"text/tab-separated-values", "text/plain"}, ".md": {"text/markdown", "text/plain"},
    ".json": {"application/json", "text/plain"},
    ".ipynb": {"application/x-ipynb+json", "application/json", "text/plain"},
    ".py": {"text/x-python", "text/plain", "application/x-python-code"},
    ".r": {"text/x-r-source", "text/plain"},
}


def inspect_material(data, suffix, mime):
    if not data or len(data) > MAX_BYTES:
        raise probe.ProbeError("attachment_size_invalid")
    if suffix not in FORMATS or mime not in FORMATS[suffix] | {"application/octet-stream"}:
        raise probe.ProbeError("attachment_type_mismatch")
    if suffix == ".pdf":
        if not data.startswith(b"%PDF-"):
            raise probe.ProbeError("attachment_type_mismatch")
        try:
            with pymupdf.open(stream=data, filetype="pdf") as doc:
                if not doc.page_count:
                    raise ValueError()
        except Exception:
            raise probe.ProbeError("MATERIAL_INVALID_DOCUMENT") from None
    elif suffix in {".docx", ".pptx", ".xlsx", ".zip"}:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                entries = archive.infolist()
                if (len(entries) > 2000 or sum(i.file_size for i in entries) > 80 * 1024 * 1024
                        or any(i.file_size > max(i.compress_size, 1) * 200 for i in entries)
                        or any("vbaproject" in i.filename.lower() for i in entries)):
                    raise ValueError()
                required = {".docx": "word/document.xml", ".pptx": "ppt/presentation.xml", ".xlsx": "xl/workbook.xml"}.get(suffix)
                if required and required not in archive.namelist():
                    raise ValueError()
        except (ValueError, zipfile.BadZipFile):
            raise probe.ProbeError("MATERIAL_UNSAFE_ARCHIVE") from None
    elif suffix in {".png", ".jpg", ".jpeg"}:
        signature = b"\x89PNG\r\n\x1a\n" if suffix == ".png" else b"\xff\xd8\xff"
        if not data.startswith(signature):
            raise probe.ProbeError("attachment_type_mismatch")
    else:
        try:
            content = data.decode("utf-8-sig")
            if "\x00" in content or re.match(r"\s*<(?:!doctype\s+html|html)\b", content, re.I):
                raise ValueError()
            if suffix in {".json", ".ipynb"}:
                json.loads(content)
        except (UnicodeError, ValueError):
            raise probe.ProbeError("attachment_type_mismatch") from None


def fetch_material(moodle, asset, directory):
    token, _ = moodle.credentials()
    parts = validate_url(asset["source_url"])
    path = "/webservice" + parts.path if parts.path.startswith("/pluginfile.php/") else parts.path
    url = urlunsplit(parts._replace(path=path, query=""))
    response = moodle.client.transport.request("GET", url, params={"token": token, "forcedownload": "1"}, max_bytes=MAX_BYTES)
    if response.status_code != 200:
        raise probe.ProbeError("attachment_http_error", response.status_code)
    data = response.content
    if isinstance(asset.get("size_bytes"), int) and asset["size_bytes"] != len(data):
        raise probe.ProbeError("attachment_size_mismatch")
    suffix = Path(asset["name"]).suffix.lower()
    inspect_material(data, suffix, response.headers.get("content-type", "").split(";", 1)[0].strip().lower())
    digest = hashlib.sha256(data).hexdigest()
    target = directory / (digest + suffix)
    target.parent.mkdir(parents=True, exist_ok=True)
    pending = target.with_suffix(target.suffix + ".tmp")
    pending.write_bytes(data)
    pending.replace(target)
    return {"path": target, "sha256": digest}
