"""Automatic completion through service/API entry points using synthetic evidence."""

import concurrent.futures
import hashlib
import json
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient
from test_mvp import await_task
from test_mvp import service as service

from backend.agent import completion
from backend.agent.contracts import digest
from backend.main import ORIGIN, create_app
from backend.services.exports import export_extra
from backend.services.study import StudyError


class CompletionAI:
    def __init__(self, dataset_id=None, execution=False, report_factory=None):
        self.calls = []
        self.dataset_id = dataset_id
        self.execution = execution
        self.report_factory = report_factory
        self.plan_blockers = []
        self.plan_blocker_requirements = []
        self.plan_options = {}

    def rpc(self, method, **kwargs):
        return {"ready": True}

    def begin(self, prompt):
        payload = json.loads(prompt.rsplit("\n", 1)[1])
        self.calls.append(payload)
        if "dataset_catalog" in payload:
            result = {
                "objective": "Compare measured values and explain the selected approach.",
                "deliverable_type": "report", "dataset_id": self.dataset_id,
                "needs_execution": self.execution, "research_query": "", "blockers": self.plan_blockers,
                "blocker_requirements": self.plan_blocker_requirements,
                **self.plan_options,
            }
        elif "previous_code" in payload:
            result = {"code": "import json\nassert sum([1, 2, 3]) == 6\nprint(json.dumps({'sum': 6}))\n"}
        else:
            result = {
                "title": "Completed comparison",
                "sections": [{"heading": "Findings", "content": "The supplied sources establish the comparison and its limitations."}],
                "sources": list(payload["allowed_sources"]), "unresolved": [],
                "report_labels": {"sources_heading": "References"},
                "completion": {"status": "complete", "blockers": []},
            }
            if self.report_factory:
                result = self.report_factory(payload, result)
        future = concurrent.futures.Future()
        future.set_result(json.dumps(result, ensure_ascii=False))
        return "synthetic-completion", future

    def close(self):
        pass


@pytest.fixture
def automatic(service, monkeypatch):
    monkeypatch.setattr(service, "verify_identity", lambda: None)
    service.ai = CompletionAI()
    return service


def test_automatic_completion_reads_all_attachments_and_delivers_clean_report(automatic, monkeypatch):
    service = automatic
    asset = service.get("asset", "a" * 24)
    data = service.safe_path(asset["path"]).read_bytes()
    asset.update(path=None, source_url="https://lms3.ntpu.edu.tw/pluginfile.php/1/assignment.pdf",
                 size_bytes=len(data))
    service.store.save(service.user, "asset", asset["id"], asset)
    downloaded = []

    def download(identifier):
        downloaded.append(identifier)
        return {"path": service.root / "source.pdf", "sha256": hashlib.sha256(data).hexdigest()}

    monkeypatch.setattr(service.moodle, "download", download)
    task = service.complete_assignment("2", "completion-entry")
    result = await_task(service, task["id"])
    assert result["status"] == "waiting_review", result.get("error")
    assert downloaded == [asset["id"]]
    assert result["completion"] == {"status": "complete", "blockers": [], "required_actions": []}
    assert result["request"]["selected_asset_ids"] == [asset["id"]]
    assert result["source_hashes"][asset["id"]] == hashlib.sha256(data).hexdigest()
    assert "Please submit a report" in result["completion_inputs"]["excerpt"]
    assert service.get("asset", asset["id"])["status"] == "parsed"
    assert "# Completed comparison" in result["draft"]
    assert "## References" in result["draft"]
    assert "TODO" not in result["draft"] and "AI 協作草稿" not in result["draft"]
    assert not result["reviewed"] and not result.get("pdf")
    assert len(service.ai.calls) == 2
    assert (service.workspace(service.user, task["id"]) / "input" / (asset["sha256"] + ".pdf")).exists()


def test_pdf_submission_and_missing_filename_identity_do_not_block_report(automatic, monkeypatch):
    service = automatic
    assignment = service.get("assignment", "2")
    assignment.update(asset_ids=[], description=(
        "Write an English comparison report. Convert it to PDF, name it studentID_hw1.pdf "
        "and upload to E-learning 3.0 before the deadline."
    ))
    service.store.save(service.user, "assignment", "2", assignment)
    monkeypatch.setattr(service.agent.runner, "status", lambda: {"available": False})
    original_begin = service.ai.begin
    observed = []

    def begin(prompt):
        payload = json.loads(prompt.rsplit("\n", 1)[1])
        capabilities = payload["application_capabilities"]
        assert "pdf" in capabilities["reviewed_local_exports"]
        assert capabilities["export_requires_human_review"]
        assert not capabilities["automatic_submission"]
        assert "省略該欄並繼續完成正文" in prompt
        observed.append(payload)
        return original_begin(prompt)

    monkeypatch.setattr(service.ai, "begin", begin)
    task = service.complete_assignment("2", "delivery-scope")
    result = await_task(service, task["id"])
    assert result["status"] == "waiting_review", result.get("error")
    assert len(observed) == 2  # Planning and final report receive the same delivery scope.
    assert result["completion"]["blockers"] == []
    assert "studentID" not in result["draft"]
    assert not result["reviewed"] and not result.get("pdf")


def test_missing_content_becomes_an_action_and_preserves_the_available_report(automatic):
    service = automatic
    assignment = service.get("assignment", "2")
    requirement = "Provide the required interview data and add its analysis."
    assignment.update(asset_ids=[], description=requirement)
    service.store.save(service.user, "assignment", "2", assignment)
    service.ai.plan_blocker_requirements = [{"requirement_id": "REQ-01", "quote": requirement}]
    task = service.complete_assignment("2", "required-content")
    result = await_task(service, task["id"])
    assert result["status"] == "waiting_review" and result["error"] is None
    assert result["completion"]["status"] == "needs_action"
    assert result["completion"]["blockers"] == []
    assert result["checkpoint"]["cursor"] == 5 and result["checkpoint"]["consecutive_errors"] == 0
    assert len(service.ai.calls) == 2
    assert any("interview data" in action for action in result["completion"]["required_actions"])
    assert "## Required user actions" not in result["draft"]
    assert "The supplied sources establish" in result["draft"]
    assert not result["ready_for_export"]
    service.save_draft(result["id"], result["draft"], reviewed=True)
    with pytest.raises(StudyError, match="INCOMPLETE_ACCEPTANCE_REQUIRED"):
        service.export(result["id"])
    accepted = service.save_draft(result["id"], result["draft"], reviewed=True, accept_incomplete=True)
    assert accepted["ready_for_export"]


def test_completion_idempotency_and_account_scope(automatic):
    service = automatic
    task = service.complete_assignment("2", "repeat-completion")
    result = await_task(service, task["id"])
    assert result["status"] == "waiting_review", result.get("error")
    requests = len(service.ai.calls)
    assert service.complete_assignment("2", "repeat-completion")["id"] == task["id"]
    assert len(service.ai.calls) == requests
    with pytest.raises(StudyError, match="IDEMPOTENCY_CONFLICT"):
        service.complete_assignment("different-assignment", "repeat-completion")
    user = service.user
    try:
        service.user = "c" * 24
        with pytest.raises(StudyError, match="NOT_FOUND_OR_NOT_AUTHORIZED"):
            service.complete_assignment("2", "repeat-completion")
        assert service.store.list(service.user, "task") == []
    finally:
        service.user = user


def test_execution_without_sandbox_generates_report_with_unverified_execution_action(automatic, monkeypatch):
    service = automatic
    service.ai.execution = True
    monkeypatch.setattr(service.agent.runner, "status", lambda: {"available": False})
    task = service.complete_assignment("2", "missing-completion-runner")
    result = await_task(service, task["id"])
    assert result["status"] == "waiting_review" and result["error"] is None
    assert result["completion"]["status"] == "needs_action"
    assert result["checkpoint"]["cursor"] == 5
    assert not result["ready_for_export"]
    assert len(service.ai.calls) == 3
    assert any("execution has not been verified" in action for action in result["completion"]["required_actions"])
    assert "analysis.py" in result["generated_files"]
    code = service.safe_path(result["generated_files"]["analysis.py"]["path"]).read_bytes()
    assert code == result["completion_execution"]["code"].encode()
    assert not result.get("runs")
    audit = next(a for a in result["tool_audit"] if a["tool"] == "execute_completion")
    assert not audit["success"] and audit["warning_code"] == "SANDBOX_NOT_AVAILABLE"


def test_public_dataset_profile_and_citations_are_real_tool_evidence(automatic, monkeypatch):
    service = automatic
    service.ai.dataset_id = "wine-red"
    csv_data = ("feature;quality\n" + "".join(f"{i};{i % 5}\n" for i in range(1200))).encode()
    fetched = []
    original_client = httpx.Client

    def handler(request):
        fetched.append(str(request.url))
        assert request.url.host == "archive.ics.uci.edu"
        assert not request.url.params
        return httpx.Response(200, content=csv_data, headers={"content-type": "text/csv"})

    def client(**kwargs):
        return original_client(transport=httpx.MockTransport(handler), **kwargs)

    def report(payload, result):
        profile = payload["data"]["datasets"][0]["profile"]
        result["sections"][0]["content"] = f"The downloaded dataset contains {profile['records']} records. The feature mean is {profile['attributes']['feature']['mean']}."
        return result

    service.ai.report_factory = report
    monkeypatch.setattr(completion.httpx, "Client", client)
    task = service.complete_assignment("2", "public-completion-data")
    result = await_task(service, task["id"])
    assert result["status"] == "waiting_review", result.get("error")
    dataset = result["completion_data"]["datasets"][0]
    assert len(fetched) == 1
    assert dataset["profile"]["records"] == 1200
    assert dataset["profile"]["attributes"]["feature"]["mean"] == 599.5
    assert len(dataset["profile"]["examples"]) == 5
    assert service.safe_path(dataset["path"]).read_bytes() == csv_data
    assert "dataset:wine-red" in result["report_model"]["sources"]
    assert dataset["url"] in result["draft"]
    assert "1200 records" in result["draft"] and "599.5" in result["draft"]


def test_recorded_sandbox_result_is_sent_to_final_report(automatic):
    service = automatic
    service.ai.execution = True
    executed = []

    class RecordedRunner:
        def status(self):
            return {"available": True}

        def run(self, code, inputs, directory, cancel, timeout):
            executed.append((code, inputs))
            return {"run_id": "synthetic-completion-run", "exit_code": 0,
                    "stdout": '{"sum": 6, "tests_passed": true}',
                    "code_sha256": hashlib.sha256(code.encode()).hexdigest(),
                    "source_hashes": {name: hashlib.sha256(data).hexdigest() for name, data in inputs.items()},
                    "image_id": "synthetic-only", "artifacts": {}}

    def report(payload, result):
        evidence = payload["execution"]
        assert evidence["exit_code"] == 0
        assert evidence["run_id"] == "synthetic-completion-run"
        result["sections"][0]["content"] = "The program verified that the sum is 6."
        return result

    service.agent.runner = RecordedRunner()
    service.ai.report_factory = report
    task = service.complete_assignment("2", "executed-completion")
    result = await_task(service, task["id"])
    assert result["status"] == "waiting_review", result.get("error")
    assert len(executed) == 1 and len(service.ai.calls) == 3
    assert "sum([1, 2, 3])" in executed[0][0]
    assert result["runs"][0]["run_id"] == "synthetic-completion-run"
    generated = result["generated_files"]["analysis.py"]
    assert service.safe_path(generated["path"]).read_text(encoding="utf-8") == executed[0][0]
    assert "The program verified that the sum is 6." in result["draft"]


def test_completion_api_requires_explicit_opt_in(automatic):
    service = automatic
    with TestClient(create_app(service, key="completion-test"), base_url=ORIGIN) as client:
        assert client.post("/api/assignments/2/complete", json={"approved": True},
                           headers={"Idempotency-Key": "api-completion"}).status_code == 401
        client.get("/unlock/completion-test")
        rejected = client.post("/api/assignments/2/complete", json={"approved": False},
                               headers={"Idempotency-Key": "api-completion"})
        assert rejected.json()["error"]["code"] == "COMPLETION_APPROVAL_REQUIRED"
        assert service.store.list(service.user, "task") == [] and not service.ai.calls
        accepted = client.post("/api/assignments/2/complete", json={"approved": True},
                               headers={"Idempotency-Key": "api-completion"})
        assert accepted.status_code == 202
        result = await_task(service, accepted.json()["data"]["id"])
        assert result["status"] == "waiting_review", result.get("error")
        assert result["request"]["mode"] == "complete_assignment"


def test_placeholder_report_stops_after_three_failures(automatic):
    service = automatic
    def scaffold(payload, result):
        result["sections"][0]["content"] = "Dataset: TODO. Results: TBD."
        return result
    service.ai.report_factory = scaffold
    task = service.complete_assignment("2", "reject-scaffold")
    result = await_task(service, task["id"], timeout=10)
    assert result["status"] == "partial" and not result["reviewed"]
    assert result["error"] == "COMPLETION_INCOMPLETE"
    assert result["checkpoint"]["consecutive_errors"] == 3
    assert "REPORT_CONTAINS_PLACEHOLDERS" in result["completion"]["blockers"]
    assert len(service.ai.calls) == 4  # One plan, three report attempts.
    assert sum(a["tool"] == "prepare_completion" for a in result["tool_audit"]) == 1


def test_failed_thread_start_leaves_terminal_task_and_releases_lock(automatic, monkeypatch):
    def failed_start(self):
        raise RuntimeError("thread unavailable")
    monkeypatch.setattr(completion.threading.Thread, "start", failed_start)
    with pytest.raises(RuntimeError):
        automatic.complete_assignment("2", "thread-failure")
    task = automatic.store.list(automatic.user, "task")[0]
    assert task["status"] == "failed" and task["error"] == "COMPLETION_START_FAILED"
    assert not automatic.active_jobs
    assert automatic.ai_lock.acquire(blocking=False)
    automatic.ai_lock.release()


def test_zip_contains_verified_runtime_research_metadata(automatic):
    service = automatic
    task = service.complete_assignment("2", "completion-zip")
    task = await_task(service, task["id"])
    assert task["status"] == "waiting_review"
    source = {"title": "Synthetic public article", "doi": "10.0000/synthetic", "file_id": "crossref:synthetic"}
    source_hash = digest(source)
    task["source_hashes"][source["file_id"]] = source_hash
    task["source_names"][source["file_id"]] = source["title"]
    task["completion_data"]["research"] = {"provider": "Crossref", "sources": [source]}
    service.store.save(service.user, "task", task["id"], task)
    service.save_draft(task["id"], task["draft"], reviewed=True)
    exported = export_extra(service, task["id"], "zip", include_sources=True)
    with zipfile.ZipFile(service.safe_path(exported["exports"]["zip"]["path"])) as archive:
        assert json.loads(archive.read(f"sources/{source_hash}.json")) == source
        assert "sources/assignment-description.txt" in archive.namelist()
