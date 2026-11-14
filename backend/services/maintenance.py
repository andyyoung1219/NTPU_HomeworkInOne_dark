"""Local draft and account deletion, plus content-free diagnostics."""
import hashlib
import json
import os
import re
import shutil
import sqlite3
from pathlib import Path

from backend.agent.runner import filesystem_path
from backend.models.store import now

CODES = frozenset({
    'AI_AUTH_REQUIRED', 'AI_SERVICE_LIMIT', 'AI_TIMEOUT', 'AI_BRIDGE_ERROR',
    'AI_BRIDGE_EXITED', 'RATE_LIMITED', 'PERMISSION_DENIED', 'NETWORK_UNAVAILABLE',
    'OPERATION_FAILED', 'OPERATION_TIMEOUT', 'TOOL_TIMEOUT', 'CANCELLED',
    'APP_RESTARTED_REVIEW_REQUIRED', 'PDF_QA_FAILED', 'ARTIFACT_CHANGED',
    'ASSET_CHANGED', 'MOODLE_LOGIN_REQUIRED', 'SIS_LOGIN_REQUIRED',
    'SIS_SCHEMA_CHANGED', 'RUNNER_UNAVAILABLE', 'BUDGET_EXCEEDED',
    'ACCOUNT_DELETION_PENDING', 'PATH_OUTSIDE_WORKSPACE',
    'DISK_FULL', 'LOCAL_FILE_UNAVAILABLE', 'DATABASE_UNAVAILABLE',
    'SANDBOX_NOT_AVAILABLE', 'SANDBOX_CONFIG_INVALID', 'SANDBOX_IMAGE_NOT_PINNED',
    'REQUEST_BUDGET_EXCEEDED', 'PROMPT_BUDGET_EXCEEDED', 'TASK_DEADLINE_EXCEEDED',
})
STATES = frozenset({'running', 'planning', 'exporting', 'waiting_review', 'completed',
                    'failed', 'cancelled', 'blocked_auth', 'partial', 'waiting_approval'})
EVENTS = STATES | {'tool_started', 'tool_degraded', 'tool_retry', 'interrupted',
                   'exported_docx', 'exported_zip'}
DRAFT_STATUSES = {'waiting_review', 'partial', 'failed', 'cancelled', 'blocked_auth'}
EXPORT_EVENTS = {'completed', 'exported_docx', 'exported_zip'}


def fail(code):
    from backend.services.study import StudyError
    raise StudyError(code)


def checked_tree(path, boundary):
    """Never traverse junctions or symlinks, including ancestor directories."""
    path, boundary = Path(path).absolute(), Path(boundary).resolve()
    if path == boundary or not path.resolve().is_relative_to(boundary):
        fail('PATH_OUTSIDE_WORKSPACE')
    for parent in [path, *path.parents]:
        if parent == boundary:
            break
        if parent.is_symlink() or parent.is_junction():
            fail('PATH_OUTSIDE_WORKSPACE')
    if path.is_dir():
        for current, dirs, files in os.walk(path, followlinks=False):
            for name in dirs + files:
                item = Path(current) / name
                if item.is_symlink() or item.is_junction():
                    fail('PATH_OUTSIDE_WORKSPACE')
    return path


class Maintenance:
    def __init__(self, study):
        self.study = study
        self.recovery_error = None

    def _user(self):
        user, _ = self.study.require_request_account()
        if not isinstance(user, str) or not re.fullmatch('[a-f0-9]{24}', user):
            fail('MOODLE_LOGIN_REQUIRED')
        return user

    def _paths(self, user):
        study = self.study
        paths = [study.root / kind / user for kind in ('assets', 'materials', 'workspaces', 'incoming')]
        # Older versions used a shared, content-addressed incoming directory.
        with study.store.connection() as db:
            others = [json.loads(r[0]) for r in db.execute(
                "SELECT payload FROM records WHERE user_id<>? AND kind='asset'", (user,))]
        shared = {r.get('sha256') for r in others}
        for asset in study.store.list(user, 'asset'):
            digest = asset.get('sha256', '')
            suffix = Path(asset.get('name', '')).suffix.lower()
            if re.fullmatch('[a-f0-9]{64}', digest) and digest not in shared and suffix in {'.pdf', '.docx', '.txt', '.csv'}:
                paths.append(study.root / 'incoming' / (digest + suffix))
        return [checked_tree(p, study.root) for p in paths]

    def preview(self):
        with self.study.lock:
            user = self._user()
            self._paths(user)
            with self.study.store.connection() as db:
                rows = [tuple(r) for r in db.execute(
                    'SELECT kind,id,updated_at FROM records WHERE user_id=? ORDER BY kind,id', (user,))]
                pending = bool(db.execute('SELECT 1 FROM account_deletions WHERE user_id=?', (user,)).fetchone())
            counts = {}
            for kind, _, _ in rows:
                counts[kind] = counts.get(kind, 0) + 1
            snapshot = hashlib.sha256(json.dumps([user, rows]).encode()).hexdigest()
            return {'confirmation': snapshot, 'counts': counts, 'pending': pending,
                    'scope': '目前帳號的課程、校務快取、附件、教材、草稿、任務、事件與搜尋索引。',
                    'limits': '不刪除校方資料、已另存的匯出檔、手動備份或舊 P0 驗收檔。共享的舊下載副本在其他帳號仍引用時保留。刪除屬邏輯與檔案清除，不保證磁碟鑑識不可復原。',
                    'ai_scope': '可另選清除本應用程式共用的 AI 專用瀏覽器 Profile，所有 AI 登入都會失效；不影響日常瀏覽器。'}

    def delete(self, confirmation, confirmed=False, clear_ai=False):
        with self.study.lock:
            if self.study.active_jobs:
                fail('TASK_RUNNING')
            preview = self.preview()
            if confirmed is not True or confirmation != preview['confirmation']:
                fail('DELETE_CONFIRMATION_REQUIRED')
            user = self._user()
            with self.study.store.connection() as db:
                db.execute('INSERT INTO account_deletions VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET clear_ai=MAX(clear_ai,excluded.clear_ai)',
                           (user, int(clear_ai)))
            self._erase(user)
            return {'deleted': True}

    def _can_delete_draft(self, task, exported):
        return (task['status'] in DRAFT_STATUSES and task['id'] not in self.study.active_jobs
                and not exported and not task.get('pdf') and not task.get('exports'))

    def task_views(self, task_id=None):
        study = self.study
        with study.lock:
            user = study.require_user()
            tasks = [study.get('task', task_id, user)] if task_id is not None else study.store.list(user, 'task')
            with study.store.connection() as db:
                where, params = (' AND task_id=?', (task_id,)) if task_id is not None else ('', ())
                exported = {row[0] for row in db.execute(
                    "SELECT DISTINCT task_id FROM events WHERE user_id=? AND event IN (?, ?, ?)" + where,
                    (user, *sorted(EXPORT_EVENTS), *params))}
            available = {task['id'] for task in tasks} if task_id is None else None
            for task in tasks:
                task['can_delete_draft'] = self._can_delete_draft(task, task['id'] in exported)
                if parent := task.get('revision', {}).get('parent_task_id'):
                    task['revision_parent_available'] = (parent in available if available is not None else
                                                         study.store.get(user, 'task', parent) is not None)
            return tasks

    def delete_draft(self, task_id):
        study = self.study
        with study.lock:
            user = study.require_user()
            pending = study.store.get(user, 'draft_deletion', task_id)
            if not pending:
                task = study.store.get(user, 'task', task_id)
                if task is None:
                    fail('NOT_FOUND_OR_NOT_AUTHORIZED')
                exported = any(item['event'] in EXPORT_EVENTS for item in study.store.events(user, task_id))
                if not self._can_delete_draft(task, exported):
                    fail('DRAFT_NOT_DELETABLE')
                checked_tree(study.workspace(user, task_id), study.root)
                # Commit the deletion intent before removing files. Retry or startup can finish cleanup.
                with study.store.connection() as db:
                    db.execute('PRAGMA secure_delete=ON')
                    study.store._save(db, user, 'draft_deletion', task_id, {'pending': True})
                    db.execute("DELETE FROM records WHERE user_id=? AND id=? AND kind IN ('task', 'draft_recovery')",
                               (user, task_id))
                    db.execute('DELETE FROM events WHERE user_id=? AND task_id=?', (user, task_id))
                    db.execute("DELETE FROM learning_search WHERE user_id=? AND kind='task' AND ref=?", (user, task_id))
                    row = db.execute("SELECT payload FROM records WHERE user_id=? AND kind='preferences' AND id='workflow'",
                                     (user,)).fetchone()
                    if row:
                        workflow = json.loads(row[0])
                        if workflow.get('task_id') == task_id:
                            workflow.update(task_id=None, editor='', page='configure', saved_at=now())
                            study.store._save(db, user, 'preferences', 'workflow', workflow)
            self._erase_draft_workspace(user, task_id)
            return {'deleted': True}

    def _erase_draft_workspace(self, user, task_id):
        study = self.study
        journal = study.store.get(user, 'draft_deletion', task_id)
        if journal and journal['pending']:
            path = checked_tree(study.workspace(user, task_id), study.root)
            if path.exists():
                shutil.rmtree(filesystem_path(path))
            study.store.save(user, 'draft_deletion', task_id, {'pending': False})

    def _erase(self, user):
        from backend.p0.moodle import Vault, probe
        from backend.services.study import atomic_text
        study = self.study
        paths = self._paths(user)
        with study.store.connection() as db:
            clear_ai = db.execute('SELECT clear_ai FROM account_deletions WHERE user_id=?', (user,)).fetchone()[0]
        if clear_ai:
            storage = os.environ.get('NTPU_AI_BRIDGE_STORAGE')
            boundary = (Path(storage) if storage else
                        Path(os.environ.get('LOCALAPPDATA') or Path.home()) / 'NTPU HomeworkInOne' / 'p0')
            paths.append(checked_tree(boundary / 'browser-profiles', boundary))
            study.ai.close()
        saved = Vault().operation('load', probe.account_key('p0'))
        if saved and hashlib.sha256(f"moodle:{saved['userid']}".encode()).hexdigest()[:24] == user:
            Vault().operation('delete', probe.account_key('p0'))
        study.hub.disconnect_sis()
        for path in paths:
            # Revalidate immediately before each irreversible file operation.
            boundary = study.root if path.is_relative_to(study.root.absolute()) else path.parent
            checked_tree(path, boundary)
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
        with study.store.connection() as db:
            db.execute('PRAGMA secure_delete=ON')
            db.execute('DELETE FROM learning_search WHERE user_id=?', (user,))
            db.execute('DELETE FROM events WHERE user_id=?', (user,))
            db.execute('DELETE FROM records WHERE user_id=?', (user,))
        for name in ('courses', 'assignments', 'assets', 'downloaded'):
            getattr(study.moodle, name).clear()
        if study.user == user:
            atomic_text(study.active_path, '{}')
            study.activate_user(None)
        with study.store.connection() as db:
            db.execute('DELETE FROM account_deletions WHERE user_id=?', (user,))
        self.recovery_error = None

    def recover(self):
        from backend.services.study import StudyError

        with self.study.store.connection() as db:
            users = [r[0] for r in db.execute('SELECT user_id FROM account_deletions')]
        for user in users:
            if not re.fullmatch('[a-f0-9]{24}', user):
                fail('INVALID_DELETE_JOURNAL')
            try:
                self._erase(user)
            except Exception:
                self.recovery_error = 'ACCOUNT_DELETION_PENDING'
        with self.study.store.connection() as db:
            drafts = db.execute("SELECT user_id, id FROM records WHERE kind='draft_deletion' "
                                "AND json_extract(payload, '$.pending') = 1 "
                                "AND user_id NOT IN (SELECT user_id FROM account_deletions)").fetchall()
        for user, task_id in drafts:
            try:
                self._erase_draft_workspace(user, task_id)
            except (OSError, sqlite3.Error, StudyError):
                self.recovery_error = self.recovery_error or 'DRAFT_DELETION_PENDING'

    def diagnostics(self):
        with self.study.lock:
            user = self.study.require_user()
            tasks = self.study.store.list(user, 'task')
            result = []
            for index, task in enumerate(tasks, 1):
                events = self.study.store.events(user, task['id'])
                code = task.get('error')
                result.append({'task': index,
                    'status': task.get('status') if task.get('status') in STATES else 'unknown',
                    'error': code if code in CODES else ('OTHER_ERROR' if code else None),
                    'has_draft': bool(task.get('draft')), 'has_pdf': bool(task.get('pdf')),
                    'events': [{'event': e['event'] if e['event'] in EVENTS else 'other',
                                'code': e['code'] if e['code'] in CODES else ('OTHER_CODE' if e['code'] else None)}
                               for e in events[-100:]]})
            return {'schema_version': 1, 'generated_at': now(), 'tasks': result,
                    'scope': '目前帳號；只含狀態、固定錯誤碼、產物存在狀態，無內容、路徑、帳號或憑證。'}
