import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_mvp import FakeAI, task_request
from test_mvp import service as service

from backend.main import ORIGIN, create_app
from backend.models.store import Store
from backend.services.maintenance import checked_tree
from backend.services.study import StudyError, StudyService, atomic_text


@pytest.fixture(autouse=True)
def isolated_vault(monkeypatch):
    monkeypatch.setattr('backend.p0.moodle.Vault.operation', lambda *args: None)


def prepare_files(service, user):
    for kind in ('assets', 'materials', 'workspaces', 'incoming'):
        path = service.root / kind / user / 'fixture.txt'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('private fixture')


def erase(service, **kwargs):
    return service.maintenance.delete(service.maintenance.preview()['confirmation'], True, **kwargs)


def test_delete_is_scoped_and_removes_search_events(service):
    user, other = service.user, 'c' * 24
    prepare_files(service, user)
    prepare_files(service, other)
    service.store.save(other, 'task', 'other-task', {'id': 'other-task'})
    service.store.event(user, 'task', 'running')
    with service.store.connection() as db:
        db.execute("INSERT INTO learning_search(user_id,body) VALUES (?,?)", (user, 'private'))
    assert erase(service) == {'deleted': True}
    assert service.user is None
    assert not (service.root / 'assets' / user).exists()
    assert (service.root / 'assets' / other / 'fixture.txt').exists()
    assert service.store.list(other, 'task')
    assert not service.store.list(user, 'asset')
    with service.store.connection() as db:
        assert not db.execute('SELECT 1 FROM learning_search WHERE user_id=?', (user,)).fetchall()
        assert not db.execute('SELECT 1 FROM events WHERE user_id=?', (user,)).fetchall()
    with pytest.raises(StudyError, match='LOGIN_REQUIRED'):
        service.get('assignment', '2')


def test_confirmation_bound_to_account_and_records(service):
    token = service.maintenance.preview()['confirmation']
    with pytest.raises(StudyError, match='CONFIRMATION'):
        service.maintenance.delete(token)
    service.store.save(service.user, 'task', 'new', {'id': 'new'})
    with pytest.raises(StudyError, match='CONFIRMATION'):
        service.maintenance.delete(token, True)
    service.user = 'c' * 24
    with pytest.raises(StudyError, match='CONFIRMATION'):
        service.maintenance.delete(token, True)


def test_active_job_blocks_deletion(service):
    service.active_jobs['active'] = {}
    try:
        with pytest.raises(StudyError, match='TASK_RUNNING'):
            erase(service)
    finally:
        service.active_jobs.clear()


def test_delete_one_draft_removes_only_its_local_data(service):
    user, other = service.user, 'c' * 24
    client = TestClient(create_app(service, 'draft-delete'), base_url=ORIGIN)
    client.get('/unlock/draft-delete', follow_redirects=False)
    draft = {'id': 'draft', 'title': 'Unfinished draft', 'status': 'waiting_review',
             'draft': 'Discard this draft', 'assignment_id': '2'}
    service.store.save_many(user, [
        ('task', 'draft', draft),
        ('task', 'child', {**draft, 'id': 'child', 'revision': {'parent_task_id': 'draft'}}),
        ('task', 'exported', {**draft, 'id': 'exported'}),
        ('task', 'running', {**draft, 'id': 'running', 'status': 'running'}),
        ('task', 'failed', {**draft, 'id': 'failed', 'status': 'failed', 'draft': ''}),
        ('preferences', 'workflow', {'assignment_id': '2', 'course_id': '1', 'task_id': 'draft',
                                     'selected_asset_ids': [], 'page': 'review', 'editor': draft['draft']}),
    ])
    service.store.save(other, 'task', 'foreign', {**draft, 'id': 'foreign'})
    for owner, task_id in ((user, 'draft'), (user, 'child'), (user, 'exported'), (other, 'foreign')):
        atomic_text(service.workspace(owner, task_id) / 'work/report.md', task_id)
    service.store.event(user, 'draft', 'waiting_review')
    service.store.event(user, 'exported', 'exported_docx')
    service.learning.rebuild_index()

    tasks = {task['id']: task for task in client.get('/api/tasks').json()['data']}
    assert tasks['draft']['can_delete_draft'] and tasks['failed']['can_delete_draft']
    assert tasks['child']['revision_parent_available']
    assert not tasks['exported']['can_delete_draft'] and not tasks['running']['can_delete_draft']
    assert client.delete('/api/tasks/exported').status_code == 409
    assert client.delete('/api/tasks/running').status_code == 409
    assert client.delete('/api/tasks/foreign').status_code == 404
    assert client.delete('/api/tasks/draft').json()['data'] == {'deleted': True}
    assert client.delete('/api/tasks/draft').json()['data'] == {'deleted': True}
    assert client.get('/api/tasks/draft').status_code == 404
    assert client.get('/api/artifacts/draft/download?kind=markdown').status_code == 404
    assert not service.workspace(user, 'draft').exists()
    assert not service.store.events(user, 'draft')
    assert (service.root / 'source.pdf').is_file()
    for owner, task_id in ((user, 'child'), (user, 'exported'), (other, 'foreign')):
        assert (service.workspace(owner, task_id) / 'work/report.md').read_text() == task_id
    with service.store.connection() as db:
        assert db.execute("SELECT 1 FROM learning_search WHERE kind='task' AND ref='draft'").fetchone() is None
    progress = client.get('/api/workflow/progress').json()['data']
    assert progress['task_id'] is None and progress['editor'] == ''
    child = client.get('/api/tasks/child').json()['data']
    assert child['revision']['parent_task_id'] == 'draft' and not child['revision_parent_available']
    assert client.delete('/api/tasks/failed').json()['data'] == {'deleted': True}


def test_draft_cleanup_resumes_after_restart_and_blocks_replayed_creation(service, monkeypatch):
    import backend.services.maintenance as maintenance

    user = service.user
    request = task_request(service)
    task = service.create_task(request, 'original-request')
    task_id = task['id']
    service.cancel(task_id)
    workspace = service.workspace(user, task_id)

    def locked(*args):
        raise PermissionError('locked')

    with monkeypatch.context() as patch:
        patch.setattr(maintenance.shutil, 'rmtree', locked)
        with pytest.raises(PermissionError):
            service.maintenance.delete_draft(task_id)
    assert workspace.exists()
    assert service.store.get(user, 'task', task_id) is None
    with pytest.raises(StudyError, match='NOT_FOUND_OR_NOT_AUTHORIZED'):
        service.create_task(request, 'original-request')

    restarted = StudyService(service.root, FakeAI())
    try:
        assert not workspace.exists()
        assert restarted.maintenance.delete_draft(task_id) == {'deleted': True}
        with pytest.raises(StudyError, match='NOT_FOUND_OR_NOT_AUTHORIZED'):
            restarted.create_task(request, 'original-request')
        assert restarted.create_task(request, 'new-request')['id'] != task_id
    finally:
        restarted.close()


def test_failed_delete_stays_hidden_and_resumes_after_restart(service, monkeypatch):
    import backend.services.maintenance as maintenance
    user = service.user
    prepare_files(service, user)
    original = maintenance.shutil.rmtree
    monkeypatch.setattr(maintenance.shutil, 'rmtree', lambda *args: (_ for _ in ()).throw(OSError('locked')))
    with pytest.raises(OSError):
        erase(service)
    with pytest.raises(StudyError, match='DELETION_PENDING'):
        service.require_user()
    monkeypatch.setattr(maintenance.shutil, 'rmtree', original)
    restarted = StudyService(service.root, FakeAI())
    try:
        assert restarted.user is None
        assert not restarted.store.list(user, 'asset')
        assert not (service.root / 'workspaces' / user).exists()
    finally:
        restarted.close()


def test_legacy_shared_download_preserved_until_last_reference(service):
    digest = 'a' * 64
    path = service.root / 'incoming' / (digest + '.txt')
    path.parent.mkdir(exist_ok=True)
    path.write_text('shared')
    for user in (service.user, 'c' * 24):
        service.store.save(user, 'asset', 'legacy', {'name': 'a.txt', 'sha256': digest})
    erase(service)
    assert path.exists()
    service.user = 'c' * 24
    erase(service)
    assert not path.exists()


def test_optional_ai_profile_only_uses_dedicated_root(service, monkeypatch, tmp_path):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local'))
    profile = tmp_path / 'local/NTPU HomeworkInOne/p0/browser-profiles'
    profile.mkdir(parents=True)
    (profile / 'cookie').write_text('private')
    ordinary = tmp_path / 'local/Chrome/User Data'
    ordinary.mkdir(parents=True)
    erase(service, clear_ai=True)
    assert not profile.exists()
    assert ordinary.exists()


def test_diagnostics_excludes_raw_payloads_and_other_accounts(service):
    secret = 'sensitive-content-should-never-appear'
    service.store.save(service.user, 'task', 't', {'id': 't', 'status': 'failed',
        'error': secret, 'draft': secret, 'request': {'password': secret}, 'title': secret})
    service.store.event(service.user, 't', secret, secret)
    service.store.save('c' * 24, 'task', 'other', {'id': 'other', 'status': 'running'})
    result = service.maintenance.diagnostics()
    assert secret not in json.dumps(result)
    assert len(result['tasks']) == 1
    assert result['tasks'][0]['error'] == 'OTHER_ERROR'


def test_schema_upgrade_preserves_records_and_rejects_future(tmp_path):
    path = tmp_path / 'db.sqlite3'
    store = Store(path)
    store.save('user', 'task', 't', {'draft': 'keep'})
    with sqlite3.connect(path) as db:
        db.execute('PRAGMA user_version=1')
    assert Store(path).get('user', 'task', 't')['draft'] == 'keep'
    with sqlite3.connect(path) as db:
        db.execute('PRAGMA user_version=99')
    with pytest.raises(RuntimeError, match='VERSION_UNSUPPORTED'):
        Store(path)
    with sqlite3.connect(path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 99


def test_atomic_replace_failure_keeps_original(tmp_path, monkeypatch):
    path = tmp_path / 'draft.txt'
    path.write_text('keep')
    monkeypatch.setattr(Path, 'replace', lambda *args: (_ for _ in ()).throw(OSError('disk error')))
    with pytest.raises(OSError):
        atomic_text(path, 'new')
    assert path.read_text() == 'keep'
    assert list(tmp_path.iterdir()) == [path]


def test_restart_preserves_draft_resets_review_and_never_sends(service):
    service.store.save(service.user, 'task', 'interrupted', {'id': 'interrupted',
        'status': 'exporting', 'draft': 'keep', 'reviewed': True, 'ready_for_export': True})
    ai = FakeAI()
    restarted = StudyService(service.root, ai)
    try:
        task = restarted.get('task', 'interrupted')
        assert task['draft'] == 'keep' and task['status'] == 'waiting_review'
        assert task['reviewed'] is False and task['ready_for_export'] is False
        assert not ai.calls
    finally:
        restarted.close()


@pytest.mark.parametrize("content", ['{broken', '{"user_id": null}', '{"user_id": 123}', '[]'])
def test_corrupt_active_user_does_not_destroy_saved_records(service, content):
    service.active_path.write_text(content)
    restarted = StudyService(service.root, FakeAI())
    try:
        assert restarted.user is None
        assert restarted.store.list(service.user, 'assignment')
    finally:
        restarted.close()


def test_paths_refuse_parent_and_link(service, monkeypatch):
    with pytest.raises(StudyError, match='PATH_OUTSIDE'):
        checked_tree(service.root / '..', service.root)
    with pytest.raises(StudyError, match='PATH_OUTSIDE'):
        service.workspace(service.user, '../outside')
    original = Path.is_junction
    monkeypatch.setattr(Path, 'is_junction', lambda p: p.name == service.user or original(p))
    with pytest.raises(StudyError, match='PATH_OUTSIDE'):
        service.maintenance.preview()


def test_maintenance_api_auth_confirmation_and_pdf_integrity(service):
    with TestClient(create_app(service, 'test-key'), base_url=ORIGIN) as client:
        assert client.get('/api/maintenance/diagnostics').status_code == 401
        client.get('/unlock/test-key')
        assert client.get('/api/maintenance/diagnostics').headers['content-disposition'].startswith('attachment;')
        token = client.get('/api/maintenance/deletion-preview').json()['data']['confirmation']
        assert client.post('/api/maintenance/delete-account', json={'confirmation': token}).status_code == 400
        path = service.workspace(service.user, 'pdf-task') / 'output/report.pdf'
        path.parent.mkdir(parents=True)
        path.write_bytes(b'changed')
        service.store.save(service.user, 'task', 'pdf-task', {'id': 'pdf-task', 'status': 'completed',
            'pdf': str(path.relative_to(service.root)), 'qa': {'sha256': hashlib.sha256(b'original').hexdigest()}})
        response = client.get('/api/artifacts/pdf-task/download')
        assert response.json()['error']['code'] == 'ARTIFACT_CHANGED'


def test_draft_database_failure_rolls_back_on_restart(service, monkeypatch):
    task = {'id': 'draft-crash', 'status': 'waiting_review', 'draft': 'old draft',
            'request': {'mode': 'code_solution'}, 'reviewed': False}
    service.write_draft(service.user, task)
    service.store.save(service.user, 'task', task['id'], task)
    original = service.store._save

    def fail_commit(db, user, kind, identifier, value):
        if kind == 'task':
            raise sqlite3.OperationalError('injected write failure')
        return original(db, user, kind, identifier, value)

    monkeypatch.setattr(service.store, '_save', fail_commit)
    with pytest.raises(sqlite3.OperationalError):
        service.save_draft(task['id'], 'new draft')
    with pytest.raises(StudyError, match='DRAFT_RECOVERY_REQUIRED'):
        service.get('task', task['id'])
    restarted = StudyService(service.root, FakeAI())
    try:
        assert restarted.get('task', task['id'])['draft'] == 'old draft'
        assert (restarted.workspace(service.user, task['id']) / 'work/report.md').read_text() == 'old draft'
        assert not restarted.store.list(service.user, 'draft_recovery')
    finally:
        restarted.close()


def test_timeout_and_storage_failures_have_distinct_safe_codes():
    import errno

    from backend.services.study import error_code
    assert error_code(TimeoutError('private payload')) == 'OPERATION_TIMEOUT'
    assert error_code(OSError(errno.ENOSPC, 'private path')) == 'DISK_FULL'
    assert error_code(PermissionError('private path')) == 'LOCAL_FILE_UNAVAILABLE'
    assert error_code(sqlite3.OperationalError('private query')) == 'DATABASE_UNAVAILABLE'


def test_api_storage_error_is_fixed_code_without_exception_text(service, monkeypatch):
    import errno
    def broken():
        raise OSError(errno.ENOSPC, 'private path and payload')
    monkeypatch.setattr(service.maintenance, 'diagnostics', broken)
    with TestClient(create_app(service, 'storage-key'), base_url=ORIGIN) as client:
        client.get('/unlock/storage-key')
        response = client.get('/api/maintenance/diagnostics')
        assert response.status_code == 500
        assert response.json() == {'error': {'code': 'DISK_FULL'}}
