"""Windows-capable wrapper of the pinned, read-only upstream probe."""

import hashlib
import importlib.util
import io
import ipaddress
import json
import socket
import urllib.parse
import zipfile
from pathlib import Path

import httpx
import keyring
import pymupdf
from docx import Document

from .evidence import LOCAL, ROOT, record

_spec = importlib.util.spec_from_file_location(
    "ntpuone_probe", ROOT / ".upstream/NTPUOne/scripts/academic_probe.py"
)
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)
# The upstream only emits fixed error codes and schema, but we keep it off server stdout.
probe.emit = lambda *args, **kwargs: None

MAX_BYTES = 20 * 1024 * 1024
HOST = "lms3.ntpu.edu.tw"
SERVICE = "NTPU.HomeworkInOne.P0"


class Vault:
    def __init__(self):
        # Only use an OS credential backend, never a plaintext fallback.
        backend = keyring.get_keyring()
        if type(backend).__module__ not in {
            "keyring.backends.Windows", "keyring.backends.macOS", "keyring.backends.SecretService"
        }:
            raise probe.ProbeError("secure_vault_unavailable")
        self.backend = backend

    def operation(self, action, account, payload=None):
        if action == "store":
            self.backend.set_password(SERVICE, account, json.dumps(payload))
        elif action == "load":
            value = self.backend.get_password(SERVICE, account)
            return json.loads(value) if value else None
        elif action == "delete":
            if self.backend.get_password(SERVICE, account):
                self.backend.delete_password(SERVICE, account)
        else:
            raise probe.ProbeError("vault_action_not_allowed")


def validate_url(url, resolve=True):
    parts = urllib.parse.urlsplit(url)
    if (parts.scheme != "https" or parts.hostname != HOST or parts.port not in (443, None)
            or parts.username or parts.password or parts.fragment
            or not parts.path.startswith(("/pluginfile.php/", "/webservice/pluginfile.php/"))):
        raise probe.ProbeError("attachment_url_not_allowed")
    query = urllib.parse.parse_qs(parts.query)
    if set(query) - {"forcedownload"}:
        raise probe.ProbeError("attachment_query_not_allowed")
    if resolve:
        addresses = socket.getaddrinfo(HOST, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
            raise probe.ProbeError("attachment_dns_not_public")
    return parts


def inspect_document(data, suffix, mime):
    suffix = suffix.lower()
    if not data or len(data) > MAX_BYTES:
        raise probe.ProbeError("attachment_size_invalid")
    if suffix == ".pdf":
        if mime not in {"application/pdf", "application/octet-stream"} or not data.startswith(b"%PDF-"):
            raise probe.ProbeError("attachment_type_mismatch")
        with pymupdf.open(stream=data, filetype="pdf") as document:
            if document.is_encrypted or not 0 < len(document) <= 300:
                raise probe.ProbeError("pdf_protected_or_too_large")
            chunks = [{"page": i + 1, "text": page.get_text()} for i, page in enumerate(document)]
        if not any(item["text"].strip() for item in chunks):
            raise probe.ProbeError("pdf_requires_ocr")
    elif suffix == ".docx":
        if mime not in {"application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        "application/octet-stream", "application/zip"} or not data.startswith(b"PK"):
            raise probe.ProbeError("attachment_type_mismatch")
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            if (len(infos) > 2000 or sum(i.file_size for i in infos) > 80 * 1024 * 1024
                    or any(i.file_size > max(i.compress_size, 1) * 200 for i in infos)
                    or any("vbaProject" in i.filename for i in infos)
                    or "word/document.xml" not in archive.namelist()):
                raise probe.ProbeError("unsafe_docx")
        doc = Document(io.BytesIO(data))
        chunks = [{"section": i + 1, "text": p.text} for i, p in enumerate(doc.paragraphs)]
        chunks += [{"section": f"table-{i + 1}", "text": "\n".join(
            " | ".join(c.text for c in row.cells) for row in table.rows
        )} for i, table in enumerate(doc.tables)]
    else:
        raise probe.ProbeError("p0_requires_pdf_or_docx")
    return chunks


class MoodleProbe:
    def __init__(self, supported_suffixes=None, inspector=None, download_dir=None):
        self.client = probe.MoodleClient()
        self.courses = {}
        self.assignments = {}
        self.assets = {}
        self.downloaded = {}
        self.supported_suffixes = supported_suffixes or {".pdf", ".docx"}
        self.inspector = inspector or inspect_document
        self.download_dir = download_dir or LOCAL / "input"

    def credentials(self):
        saved = Vault().operation("load", probe.account_key("p0"))
        if not saved:
            raise probe.ProbeError("moodle_login_required")
        info, available = probe.site_info(self.client, saved["token"])
        if info["userid"] != saved["userid"]:
            raise probe.ProbeError("account_identity_changed")
        return saved["token"], available

    def login(self, username, password):
        self.courses.clear()
        self.assignments.clear()
        self.assets.clear()
        self.downloaded.clear()
        _, config = self.client.public_config()
        if config.get("typeoflogin") != 1 or config.get("enablemobilewebservice") != 1:
            raise probe.ProbeError("browser_login_required")
        if not username or not password:
            raise probe.ProbeError("empty_credential")
        _, response = self.client.request("/login/token.php", fields={
            "username": username, "password": password, "service": "moodle_mobile_app"
        })
        token = response.get("token")
        if not isinstance(token, str) or not token:
            raise probe.ProbeError("token_not_received")
        info, _ = probe.site_info(self.client, token)
        Vault().operation("store", probe.account_key("p0"), {"token": token, "userid": info["userid"]})
        record("moodle_login", passed=True)

    def inventory(self):
        token, _ = self.credentials()
        self.courses = probe.own_courses(self.client, token)
        record("moodle_courses", course_count=len(self.courses), passed=bool(self.courses))
        return [(str(c.get("fullname") or c.get("shortname") or c["id"]), str(c["id"]))
                for c in self.courses.values()]

    def list_assignments(self, course_id):
        course_id = int(course_id)
        if course_id not in self.courses:
            raise probe.ProbeError("course_not_authorized")
        token, _ = self.credentials()
        _, result = self.client.call("mod_assign_get_assignments", token, {"courseids[0]": course_id})
        if result.get("warnings") or not isinstance(result.get("courses"), list):
            raise probe.ProbeError("assignment_access_warnings")
        self.assignments, self.assets = {}, {}
        for course in result["courses"]:
            if course.get("id") != course_id:
                raise probe.ProbeError("assignment_course_scope_mismatch")
            for assignment in course.get("assignments", []):
                key = str(assignment["id"])
                self.assignments[key] = assignment
                for item in assignment.get("introattachments", []):
                    if Path(item.get("filename", "")).suffix.lower() not in self.supported_suffixes:
                        continue
                    ref = hashlib.sha256((key + item["fileurl"]).encode()).hexdigest()[:24]
                    self.assets[ref] = {**item, "assignment_id": key}
        record("moodle_assignments", assignment_count=len(self.assignments),
               asset_count=len(self.assets), passed=bool(self.assignments))
        return [(a.get("name", key), key) for key, a in self.assignments.items()]

    def download(self, asset_id):
        item = self.assets.get(asset_id)
        if not item:
            raise probe.ProbeError("asset_not_authorized")
        token, _ = self.credentials()
        parts = validate_url(item["fileurl"])
        path = parts.path
        if path.startswith("/pluginfile.php/"):
            path = "/webservice" + path
        # Moodle's documented token file endpoint. Do not log this URL or any exception repr.
        url = urllib.parse.urlunsplit(parts._replace(path=path, query=""))
        transport = getattr(self.client, "transport", None)
        if transport:
            response = transport.request("GET", url, params={"token": token, "forcedownload": "1"}, max_bytes=MAX_BYTES)
            status = response.status_code
            if status != 200:
                raise probe.ProbeError("attachment_http_error", status)
            data = response.content
            mime = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        else:
            data, mime = self._download_bytes(url)
        expected_size = item.get("filesize")
        if isinstance(expected_size, int) and expected_size != len(data):
            raise probe.ProbeError("attachment_size_mismatch")
        suffix = Path(item["filename"]).suffix.lower()
        extracted = self.inspector(data, suffix, mime)
        digest = hashlib.sha256(data).hexdigest()
        destination = self.download_dir / (digest + suffix)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        self.downloaded[asset_id] = {"path": destination, "sha256": digest, "chunks": extracted}
        record("moodle_attachment", passed=True, http_status=200, sha256=digest,
               size_bytes=len(data), format=suffix, pages=len(extracted),
               characters=sum(len(c["text"]) for c in extracted))
        return self.downloaded[asset_id]

    def _download_bytes(self, url):
        token, _ = self.credentials()
        with httpx.Client(follow_redirects=False, trust_env=False, timeout=30) as client:
            with client.stream("GET", url, params={"token": token, "forcedownload": "1"}) as response:
                status = response.status_code
                if status != 200:
                    record("moodle_download", passed=False, http_status=status)
                    raise probe.ProbeError("attachment_http_error", status)
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise probe.ProbeError("attachment_too_large")
                    chunks.append(chunk)
                data = b"".join(chunks)
                mime = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        return data, mime
