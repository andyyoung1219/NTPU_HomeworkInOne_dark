import io
import json
import subprocess
import zipfile

import pymupdf
import pytest
from fastapi.testclient import TestClient

from backend.p0 import evidence
from backend.p0.ai import AIAdapter, AIError
from backend.p0.gate import evaluate
from backend.p0.moodle import MoodleProbe, inspect_document, probe, validate_url
from backend.services.study import error_code


@pytest.mark.parametrize("url", [
    "http://lms3.ntpu.edu.tw/pluginfile.php/1/a.pdf",
    "https://evil.example/pluginfile.php/1/a.pdf",
    "https://lms3.ntpu.edu.tw@127.0.0.1/pluginfile.php/1/a.pdf",
    "https://lms3.ntpu.edu.tw:444/pluginfile.php/1/a.pdf",
    "https://lms3.ntpu.edu.tw/login/index.php",
    "https://lms3.ntpu.edu.tw/pluginfile.php/1/a.pdf?token=SECRET",
])
def test_download_rejects_untrusted_endpoints(url):
    with pytest.raises(probe.ProbeError):
        validate_url(url, resolve=False)


def test_dns_private_address_is_rejected(monkeypatch):
    monkeypatch.setattr("socket.getaddrinfo", lambda *a, **kw: [(2, 1, 6, "", ("127.0.0.1", 443))])
    with pytest.raises(probe.ProbeError, match="dns_not_public"):
        validate_url("https://lms3.ntpu.edu.tw/pluginfile.php/1/a.pdf")


def test_valid_download_path():
    parts = validate_url("https://lms3.ntpu.edu.tw/webservice/pluginfile.php/1/a.pdf?forcedownload=1", resolve=False)
    assert parts.hostname == "lms3.ntpu.edu.tw"


def test_pdf_has_page_provenance_and_html_is_rejected():
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_text((60, 60), "Synthetic P0 document")
        data = pdf.tobytes()
    parsed = inspect_document(data, ".pdf", "application/pdf")
    assert parsed[0]["page"] == 1
    assert "Synthetic P0 document" in parsed[0]["text"]
    with pytest.raises(probe.ProbeError, match="type_mismatch"):
        inspect_document(b"<html>Login</html>", ".pdf", "text/html")


def test_image_only_pdf_is_not_silently_accepted():
    with pymupdf.open() as pdf:
        pdf.new_page()
        data = pdf.tobytes()
    with pytest.raises(probe.ProbeError, match="requires_ocr"):
        inspect_document(data, ".pdf", "application/pdf")


def test_docx_zip_bomb_rejected_before_parser():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", "a" * 1_000_000)
    with pytest.raises(probe.ProbeError, match="unsafe_docx"):
        inspect_document(stream.getvalue(), ".docx", "application/zip")


def test_unselected_asset_cannot_be_downloaded():
    with pytest.raises(probe.ProbeError, match="asset_not_authorized"):
        MoodleProbe().download("other-course-file")


def test_evidence_excludes_arbitrary_sensitive_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(evidence, "LOCAL", tmp_path)
    with pytest.raises(ValueError):
        evidence.record("login", token="SYNTHETIC_SECRET")
    evidence.record("login", passed=True)
    text = (tmp_path / "evidence.jsonl").read_text()
    assert "SYNTHETIC_SECRET" not in text
    assert json.loads(text)["passed"] is True


def test_offline_tests_cannot_pass_live_gate():
    events = [{"event": "offline_tests", "passed": True}]
    assert evaluate(events)["passed"] is False


def test_gate_requires_ten_trials_document_and_cancel():
    names = ["sources_verified", "moodle_login", "moodle_courses", "moodle_assignments",
             "moodle_attachment", "ai_document", "ai_cancel", "evidence_policy_tested"]
    events = [{"event": name, "passed": True} for name in names]
    events += [{"event": "ai_trial", "trial": n, "passed": True} for n in range(1, 10)]
    assert not evaluate(events)["passed"]
    events.append({"event": "ai_trial", "trial": 10, "passed": True})
    assert evaluate(events)["passed"]


def test_stdio_contract_no_approval_means_no_request():
    adapter = AIAdapter()
    try:
        assert adapter.rpc("health")["transport"] == "stdio"
        with pytest.raises(AIError, match="APPROVAL_REQUIRED"):
            adapter.rpc("start_task", task_id="p0-test", prompt="test")
        assert adapter.rpc("login_status")["ready"] is False
        assert adapter.rpc("cancel_task", task_id="unknown")["requested"] is False
        assert adapter.rpc("stream_events")["transport"] == "stdio-notifications"
    finally:
        adapter.close()


def test_stdio_preserves_specific_model_response_failures():
    cases = [
        ({"code": "INCOMPLETE_MODEL_RESPONSE"}, "INCOMPLETE_MODEL_RESPONSE"),
        ({"code": "EMPTY_MODEL_RESPONSE"}, "EMPTY_MODEL_RESPONSE"),
        ({"code": "IDLE_RESPONSE_TIMEOUT"}, "AI_TIMEOUT"),
        ({"code": "TOTAL_RESPONSE_TIMEOUT"}, "AI_TIMEOUT"),
        ({"code": "INCOMPLETE_MODEL_RESPONSE", "status": 429}, "AI_SERVICE_LIMIT"),
        ({"code": "EMPTY_MODEL_RESPONSE", "status": 403}, "AI_AUTH_REQUIRED"),
        ({"code": "TOTAL_RESPONSE_TIMEOUT", "message": "aborted"}, "CANCELLED"),
        ({"code": "UNCLASSIFIED", "diagnostics": {"opened": True}}, "AI_BRIDGE_ERROR"),
    ]
    script = """
const { bridgeErrorCode } = require('./ntpu-ai-bridge/src/p0.cjs');
const cases = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
console.log(JSON.stringify(cases.map(bridgeErrorCode)));
"""
    output = subprocess.run(["node", "-e", script], cwd=evidence.ROOT, check=True, timeout=20,
                            input=json.dumps([case for case, _ in cases]), capture_output=True, text=True)
    codes = json.loads(output.stdout)
    expected = [code for _, code in cases]
    assert codes == expected
    assert [error_code(AIError(code)) for code in codes] == expected


def test_bridge_closes_extra_tabs_before_and_after_background_chat():
    script = """
const assert = require('node:assert/strict');
const { BrowserManager } = require('./ntpu-ai-bridge/vendor/src/browser/manager');
const { WorkspaceBrowserManager, chatWithTabCleanup } = require('./ntpu-ai-bridge/src/p0.cjs');

function makeSession() {
  const tabs = [];
  const newTab = () => {
    let closed = false;
    const page = {
      isClosed: () => closed,
      close: async () => { closed = true; },
    };
    tabs.push(page);
    return page;
  };
  const page = newTab();
  const context = { pages: () => tabs.filter(tab => !tab.isClosed()) };
  return { page, context, newTab };
}

(async () => {
  const originalLaunch = BrowserManager.prototype.launch;
  BrowserManager.prototype.launch = async () => {
    const session = makeSession();
    session.newTab();
    return session;
  };
  try {
    const manager = new WorkspaceBrowserManager();
    const background = await manager.launch({ headless: true });
    assert.deepEqual(background.context.pages(), [background.page]);
    const login = await manager.launch({ headless: false });
    assert.equal(login.context.pages().length, 2);
    const loginBridge = { ...login, mode: 'login', chat: async () => ({ text: 'login' }) };
    await chatWithTabCleanup(loginBridge, 'test', {});
    assert.equal(login.context.pages().length, 2);
  } finally {
    BrowserManager.prototype.launch = originalLaunch;
  }

  const session = makeSession();
  const bridge = { ...session, mode: 'background' };
  for (const error of [null, new Error('timeout'), new Error('cancelled')]) {
    session.newTab();
    bridge.chat = async () => {
      assert.deepEqual(session.context.pages(), [session.page]);
      session.newTab();
      if (error) throw error;
      return { text: 'completed' };
    };
    const result = chatWithTabCleanup(bridge, 'test', {});
    if (error) await assert.rejects(result, error);
    else assert.deepEqual(await result, { text: 'completed' });
    assert.deepEqual(session.context.pages(), [session.page]);
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    subprocess.run(["node", "-e", script], cwd=evidence.ROOT, check=True, timeout=20)


def test_local_ui_requires_cookie_and_rejects_cross_origin():
    from backend.p0.app import ORIGIN, SESSION_KEY, create_app

    with TestClient(create_app(), base_url=ORIGIN) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/").status_code == 401
        assert client.get("/", headers={"host": "evil.example"}).status_code == 403
        assert client.get("/unlock/" + SESSION_KEY).status_code == 200
        assert client.get("/config").status_code == 200
        assert client.post("/", headers={"origin": "https://evil.example"}).status_code == 403


def test_locked_source_commits():
    config = json.loads((evidence.ROOT / "docs/sources.lock.json").read_text())
    for source in config["sources"].values():
        actual = subprocess.check_output(["git", "-C", str(evidence.ROOT / source["path"]),
                                          "rev-parse", "HEAD"], text=True).strip()
        assert actual == source["commit"]


def test_vendored_bridge_matches_pinned_source():
    import hashlib

    vendor = evidence.ROOT / "ntpu-ai-bridge/vendor"
    manifest = json.loads((vendor / "manifest.json").read_text())
    source = evidence.ROOT / ".upstream/ntpu-ai-vscode"
    for name, digest in manifest["files"].items():
        assert hashlib.sha256((vendor / name).read_bytes()).hexdigest() == digest
        assert (vendor / name).read_bytes() == (source / name).read_bytes()
