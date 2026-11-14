import gzip
import io
import zipfile

import httpx
import pymupdf
import pytest
from test_mvp import service as service
from test_sis import transport

from backend.integrations.materials import fetch_material, inspect_material
from backend.p0.moodle import probe
from backend.services.study import StudyError


def archive(name, data=b"<xml/>"):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as zipped:
        zipped.writestr(name, data)
    return output.getvalue()


@pytest.mark.parametrize("extension,entry", [(".pptx", "ppt/presentation.xml"), (".xlsx", "xl/workbook.xml"), (".docx", "word/document.xml"), (".zip", "sample.py")])
def test_original_office_and_zip_files_not_extracted_or_executed(extension, entry):
    inspect_material(archive(entry), extension, "application/octet-stream")
    if extension != ".zip":
        with pytest.raises(probe.ProbeError, match="MATERIAL_UNSAFE_ARCHIVE"):
            inspect_material(archive("wrong.xml"), extension, "application/octet-stream")


def test_material_download_accepts_scanned_pdf_without_claiming_extraction():
    with pymupdf.open() as doc:
        doc.new_page()
        inspect_material(doc.tobytes(), ".pdf", "application/pdf")


@pytest.mark.parametrize("data,suffix,mime", [
    (b"<html>Login required</html>", ".txt", "text/plain"),
    (b"not a png", ".png", "image/png"),
    (b"not a pdf", ".pdf", "application/pdf"),
    (b"{}", ".json", "text/html"),
    (b"MZ dangerous", ".exe", "application/octet-stream"),
    (b"a\x00b", ".csv", "text/csv"),
])
def test_mime_magic_and_login_page_are_checked(data, suffix, mime):
    with pytest.raises(probe.ProbeError, match="attachment_type_mismatch"):
        inspect_material(data, suffix, mime)


def test_macros_and_zip_bombs_rejected():
    with pytest.raises(probe.ProbeError, match="MATERIAL_UNSAFE_ARCHIVE"):
        inspect_material(archive("word/vbaProject.bin"), ".zip", "application/zip")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
        zipped.writestr("bomb.txt", b"A" * 100000)
    with pytest.raises(probe.ProbeError, match="MATERIAL_UNSAFE_ARCHIVE"):
        inspect_material(output.getvalue(), ".zip", "application/zip")


def test_download_hash_filename_and_token_never_written(tmp_path, monkeypatch):
    from types import SimpleNamespace

    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.host == "lms3.ntpu.edu.tw"
        assert request.url.path.startswith("/webservice/pluginfile.php/")
        assert request.url.params["token"] == "synthetic-token"
        return httpx.Response(200, content=b"sample", headers={"content-type": "text/plain"})

    from backend.integrations import materials
    from backend.p0.moodle import validate_url

    monkeypatch.setattr(materials, "validate_url", lambda url: validate_url(url, resolve=False))
    reader = transport(handler)
    moodle = SimpleNamespace(credentials=lambda: ("synthetic-token", set()), client=SimpleNamespace(transport=reader))
    asset = {"name": "../../sample.txt", "source_url": "https://lms3.ntpu.edu.tw/pluginfile.php/1/sample.txt", "size_bytes": 6}
    result = fetch_material(moodle, asset, tmp_path)
    assert result["path"].parent == tmp_path
    assert result["path"].name == result["sha256"] + ".txt"
    assert result["path"].read_bytes() == b"sample"
    assert len(requests) == 1
    with pytest.raises(probe.ProbeError, match="attachment_url_not_allowed"):
        fetch_material(moodle, {**asset, "source_url": "https://evil.example/sample.txt"}, tmp_path)
    assert len(requests) == 1
    reader.close()


def test_compressed_http_responses_are_decoded_once():
    reader = transport(lambda request: httpx.Response(200, content=gzip.compress(b"decoded body"), headers={"content-encoding": "gzip"}))
    assert reader.request("GET", "https://my.ntpu.edu.tw/").content == b"decoded body"
    reader.close()


def test_download_accepts_verified_local_attachment_without_remote_metadata(service, monkeypatch):
    ref = "a" * 24
    asset = service.get("asset", ref)
    monkeypatch.setattr(service, "verify_identity", lambda: None)

    def unexpected_download(_):
        pytest.fail("Local-only attachments must not be downloaded from Moodle")

    monkeypatch.setattr(service.moodle, "download", unexpected_download)
    result = service.download("2", [ref])
    assert "Please submit a report" in result["excerpt"]
    assert result["assets"][0]["sha256"] == asset["sha256"]

    service.safe_path(asset["path"]).write_bytes(b"changed")
    with pytest.raises(StudyError, match="ASSET_CHANGED"):
        service.download("2", [ref])


@pytest.mark.parametrize("encoding", ["utf-8", "big5"])
def test_attachment_only_assignment_with_chinese_download_header(service, monkeypatch, encoding):
    from backend.p0 import moodle

    assignment = service.get("assignment", "2")
    assignment["description"] = ""
    service.store.save(service.user, "assignment", "2", assignment)
    asset = service.get("asset", "a" * 24)
    data = service.safe_path(asset["path"]).read_bytes()
    asset.update(name="課程作業規範.pdf", path=None, status="not_downloaded",
                 source_url="https://lms3.ntpu.edu.tw/pluginfile.php/1/assignment.pdf",
                 size_bytes=len(data))
    service.store.save(service.user, "asset", asset["id"], asset)
    disposition = 'attachment; filename="課程作業規範.pdf"'.encode(encoding)

    def handler(request):
        assert request.url.path == "/webservice/pluginfile.php/1/assignment.pdf"
        return httpx.Response(200, content=data, headers=[
            (b"content-type", b"application/pdf"), (b"content-disposition", disposition)])

    monkeypatch.setattr(service, "verify_identity", lambda: None)
    monkeypatch.setattr(service.moodle, "credentials", lambda: ("synthetic-token", set()))
    validate_url = moodle.validate_url
    monkeypatch.setattr(moodle, "validate_url", lambda url: validate_url(url, resolve=False))
    service.moodle.client.transport.close()
    service.moodle.client.transport = transport(handler)
    parsed = service.download("2", [asset["id"]])
    assert "Please submit a report" in parsed["excerpt"]
    assert parsed["requirements"]
    assert parsed["assets"][0]["status"] == "parsed"
    assert service.safe_path(parsed["assets"][0]["path"]).read_bytes() == data
