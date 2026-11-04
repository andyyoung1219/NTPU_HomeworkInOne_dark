"""Exercise the preparation CLI with synthetic per-user Moodle API responses."""

import json

import httpx
import pytest

from scripts import prepare_live


@pytest.mark.parametrize("case", ["assignment", "no_attachments", "no_assignments", "no_courses"])
def test_preparation_selects_first_course_and_its_first_assignment(tmp_path, monkeypatch, capsys, case):
    app = tmp_path / ".local/app"
    app.mkdir(parents=True)
    (app / "launch-url.txt").write_text(prepare_live.BASE + "/unlock/synthetic")
    (app / "prepared-live.json").write_text(json.dumps({
        "course_id": "other-user-course", "assignment_id": "other-user-assignment",
        "asset_ids": ["old-asset"], "excerpt": "old user excerpt", "requirements": ["old"]}))
    calls = []
    refs = [] if case == "no_attachments" else ["first-asset"]

    def handle(request):
        calls.append(request)
        path = request.url.path
        if path == "/unlock/synthetic":
            return httpx.Response(200)
        if path == "/api/connections/moodle/restore":
            result = [] if case == "no_courses" else [
                {"id": "first-course", "name": "資料結構"},
                {"id": "later-course", "name": "人工智慧代理人原理及實務"}]
        elif path == "/api/sync/moodle":
            assert request.url.params["course_id"] == "first-course"
            result = [] if case == "no_assignments" else [
                {"id": "first-assignment", "title": "排序練習", "asset_ids": refs},
                {"id": "later-assignment", "title": "指定作業1", "asset_ids": ["later-asset"]}]
        elif path == "/api/assignments/first-assignment/download":
            assert json.loads(request.content) == {"selected_asset_ids": refs, "approved": True}
            result = {"excerpt": "synthetic first assignment", "requirements": [{"text": "排序"}]}
        else:
            pytest.fail(f"Unexpected request: {request.method} {path}")
        return httpx.Response(200, json={"data": result})

    client_class = httpx.Client
    monkeypatch.setattr(prepare_live, "ROOT", tmp_path)
    monkeypatch.setattr(prepare_live.httpx, "Client", lambda **kwargs: client_class(
        transport=httpx.MockTransport(handle), **kwargs))
    prepare_live.main()
    state = json.loads((app / "prepared-live.json").read_text(encoding="utf-8"))
    assert state["course_id"] == (None if case == "no_courses" else "first-course")
    assert state["assignment_id"] == ("first-assignment" if case in {"assignment", "no_attachments"} else None)
    assert state["asset_ids"] == (["first-asset"] if case == "assignment" else [])
    assert state["excerpt"] == ("synthetic first assignment" if case == "assignment" else "")
    assert state["requirements"] == ([{"text": "排序"}] if case == "assignment" else [])
    assert len(calls) == {"assignment": 4, "no_attachments": 3, "no_assignments": 3, "no_courses": 2}[case]
    assert json.loads(capsys.readouterr().out)["ai_data_sent"] is False
