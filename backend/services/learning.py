"""Local P4 views, searchable sources and recoverable wizard inputs.

All queries are scoped to the active account. Building an index only reads
already downloaded files; it never downloads or sends anything to a model.
"""

import hashlib
import json
import re
from concurrent.futures import Future
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import wraps
from hmac import digest
from pathlib import Path
from secrets import token_bytes
from threading import Lock

from keyring.errors import KeyringError

from backend.models.store import now
from backend.p0.moodle import probe
from backend.services.data_hub import TTL
from backend.services.documents import parse

TAIPEI = timezone(timedelta(hours=8))
STEPS = {"select", "sources", "configure", "generate", "review"}


def locked(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self.study.lock:
            return method(self, *args, **kwargs)
    return call


def fail(code):
    from backend.services.study import StudyError
    raise StudyError(code)


def due_label(value):
    return datetime.fromtimestamp(value, TAIPEI).strftime("%Y-%m-%d %H:%M") if value else "未提供截止日"


def submission_label(item, timestamp):
    state = item.get("submission_status", "unknown")
    if state == "submitted":
        return "已提交"
    if item.get("stale") or state not in {"new", "draft", "reopened", "not_submitted", "no_submission"}:
        return "待確認"
    if synced_at := item.get("last_synced_at"):
        try:
            if timestamp - datetime.fromisoformat(synced_at).timestamp() > TTL:
                return "待確認"
        except (TypeError, ValueError):
            return "待確認"
    if item.get("due_at") and item["due_at"] < timestamp:
        return "逾期未提交"
    return "未完成"


def latest_course_ids(courses):
    semesters = {}
    for course in courses:
        semester = course.get("semester")
        match = re.fullmatch(r"([0-9]{1,3})-([12])", semester) if isinstance(semester, str) else None
        semesters[course["id"]] = tuple(map(int, match.groups())) if match else (0, 0)
    latest = max(semesters.values(), default=None)
    return {course["id"] for course in courses
            if course.get("scope") != "inactive" and semesters[course["id"]] == latest}


def submission_refresh_error(error):
    if error.code in {"ACCOUNT_CHANGED_RECONNECT", "account_identity_changed"}:
        return "ACCOUNT_CHANGED_RECONNECT"
    if error.code in {"moodle_login_required", "invalidtoken", "requireloginerror"} or error.status == 401:
        return "MOODLE_LOGIN_REQUIRED"
    if error.code in {"RATE_LIMITED", "NETWORK_UNAVAILABLE"}:
        return error.code
    if error.status in {502, 503, 504}:
        return "NETWORK_UNAVAILABLE"
    if error.code in {"accessexception", "webserviceaccess"} or error.status == 403:
        return "MOODLE_PERMISSION_DENIED"
    return "SUBMISSION_STATUS_UNAVAILABLE"


class SubmissionRefreshBatch:
    """Share each observation across views whose live refreshes overlap."""

    def __init__(self, moodle, user):
        self.moodle = moodle
        self.user = user
        self.views = 0
        self.lock = Lock()
        self.requests = {}

    def once(self, key, request):
        with self.lock:
            existing = self.requests.get(key)
            if existing is None:
                result = self.requests[key] = Future()
        if existing is not None:
            return existing.result()
        try:
            result.set_result(request())
        except BaseException as error:
            result.set_exception(error)
        return result.result()

    def credentials(self):
        return self.once(None, lambda: self.moodle.credentials(expected_user=self.user))

    def assignment(self, token, assignment_id):
        def request():
            _, payload = self.moodle.client.call("mod_assign_get_submission_status", token, {
                "assignid": assignment_id,
            })
            observation = probe.submission_observation(payload)
            return observation["individual_state"], payload["lastattempt"].get("teamsubmission") is not None

        return self.once(assignment_id, request)


class LearningCenter:
    def __init__(self, study):
        self.study = study
        self.submission_lock = Lock()
        self.submission_batches = {}
        self.submission_scope_key = token_bytes(32)
        with study.store.connection() as db:
            db.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS learning_search USING fts5(
                user_id UNINDEXED, course_id UNINDEXED, kind UNINDEXED, ref UNINDEXED,
                title, body, metadata UNINDEXED, tokenize='trigram')""")

    def records(self, kind):
        return self.study.store.list(self.study.require_user(), kind)

    def assignments(self, course_id=None):
        timestamp = datetime.now(TAIPEI).timestamp()
        courses = {c["id"]: c for c in self.records("course")}
        result = []
        for item in self.records("assignment"):
            cid = item["course_id"]
            if cid not in courses or item.get("removed") or (course_id and cid != course_id):
                continue
            result.append({**item, "course_name": courses[cid]["name"],
                           "submission_label": submission_label(item, timestamp),
                           "due_label": due_label(item.get("due_at"))})
        return sorted(result, key=lambda a: (a.get("due_at") or float("inf"), a["title"]))

    @contextmanager
    def _submission_batch(self, user):
        with self.submission_lock:
            batch = self.submission_batches.get(user)
            if batch is None:
                batch = self.submission_batches[user] = SubmissionRefreshBatch(self.study.moodle, user)
            batch.views += 1
        try:
            yield batch
        finally:
            with self.submission_lock:
                batch.views -= 1
                if not batch.views:
                    del self.submission_batches[user]

    def _refresh_batch(self, batch, items, timestamp):
        try:
            token, functions = batch.credentials()
        except probe.ProbeError as error:
            return submission_refresh_error(error)
        except (KeyringError, ValueError, OSError):
            return "SUBMISSION_STATUS_UNAVAILABLE"
        if "mod_assign_get_submission_status" not in functions:
            return "MOODLE_PERMISSION_DENIED"
        failure = None
        for item in items:
            if self.study.user != batch.user:
                fail("ACCOUNT_CHANGED_RECONNECT")
            assignment_id = str(item["id"])
            if not re.fullmatch(r"[1-9][0-9]*", assignment_id):
                failure = "SUBMISSION_STATUS_UNAVAILABLE"
                continue
            try:
                state, team_submission = batch.assignment(token, int(assignment_id))
                if item.get("team_submission") or team_submission:
                    continue  # Group submission does not establish individual status.
                item.update(submission_status=state,
                            submission_label=submission_label({"submission_status": state,
                                                               "due_at": item.get("due_at")}, timestamp))
            except probe.ProbeError as error:
                code = submission_refresh_error(error)
                if code in {"RATE_LIMITED", "NETWORK_UNAVAILABLE", "MOODLE_LOGIN_REQUIRED",
                            "ACCOUNT_CHANGED_RECONNECT"}:
                    return code
                failure = failure or code
        return failure

    def refresh_submissions(self, user, items, timestamp):
        """Project Moodle's live response without changing the saved assignment data."""
        for item in items:
            item.update(submission_status="unknown", submission_label="待確認")
        if items:
            with self._submission_batch(user) as batch:
                return self._refresh_batch(batch, items, timestamp)
        return None

    def _refresh_for_view(self, user, items, timestamp):
        # Moodle requests can take time. Keep the app lock free for other work,
        # but discard the view if its account changed before we could return it.
        error = self.refresh_submissions(user, items, timestamp)
        with self.study.lock:
            if self.study.require_user() != user:
                fail("ACCOUNT_CHANGED_RECONNECT")
        return error

    def _submission_view(self, user, items, timestamp, live, pending):
        if not (live or pending):
            return {}
        # A local page and its live response can straddle a login in another tab.
        scope = digest(self.submission_scope_key, user.encode(), "sha256").hex()
        if live:
            return {"submission_scope": scope,
                    "submission_refresh_error": self._refresh_for_view(user, items, timestamp)}
        for item in items:
            item.update(submission_status="unknown", submission_label="更新中")
        return {"submission_scope": scope, "submission_refresh_pending": bool(items)}

    def dashboard(self, live=False, pending=False):
        with self.study.lock:
            user = self.study.require_user()
            courses = self.records("course")
            active = (latest_course_ids(courses) if live or pending else
                      {c["id"] for c in courses if c.get("scope") != "inactive"})
            items = [a for a in self.assignments() if a["course_id"] in active]
            tasks = self.records("task")
            timetable = self.study.hub.snapshot("sis:timetable")
        current = datetime.now(TAIPEI)
        timestamp = current.timestamp()
        refresh = self._submission_view(user, items, timestamp, live, pending)
        today = []
        for course in (timetable.get("data") or {}).get("courses", []):
            for meeting in course.get("meetings", []):
                if meeting.get("weekday") == current.isoweekday():
                    today.append({**meeting, "name": course["name"], "teacher": course.get("teacher")})
        return {**refresh, "date": current.date().isoformat(), "courses": courses,
                "today": sorted(today, key=lambda x: str(x.get("period") or "").zfill(3)),
                "timetable": timetable, "assignments": items,
                "upcoming": [a for a in items if a.get("due_at") and timestamp <= a["due_at"]
                             <= timestamp + 7 * 86400 and a["submission_label"] != "已提交"],
                "overdue": [a for a in items if a["submission_label"] == "逾期未提交"],
                "unknown": [a for a in items if a["submission_label"] == "待確認"],
                "review": [t for t in tasks if t.get("draft") and t["status"] != "completed"],
                "recent": tasks[:12]}

    def course(self, course_id, live=False, pending=False):
        with self.study.lock:
            user = self.study.require_user()
            course = self.study.get("course", course_id)
            assignments = self.assignments(course_id)
            ids = {a["id"] for a in self.records("assignment") if a["course_id"] == course_id}
            snapshots = self.study.hub.course(course_id)
            view = {"course": course, "assignments": assignments, "snapshots": snapshots,
                    "tasks": [t for t in self.records("task") if t["assignment_id"] in ids],
                    "materials": self.materials(course_id),
                    "assets": [a for a in self.records("asset") if a["assignment_id"] in ids]}
        view.update(self._submission_view(user, assignments, datetime.now(TAIPEI).timestamp(), live, pending))
        return view

    def materials(self, course_id):
        snapshots = self.study.hub.course(course_id)
        refs = set()
        for section in snapshots["contents"].get("data") or []:
            for module in section.get("modules", []):
                refs.update(x["id"] for x in module.get("contents", []) if x.get("id"))
        for post in snapshots["announcements"].get("data") or []:
            refs.update(x["id"] for x in post.get("attachments", []) if x.get("id"))
        return [m for m in self.records("material") if m.get("course_id") == course_id and m["id"] in refs]

    def rebuild_index(self):
        with self.study.lock:
            user = self.study.require_user()
            rows, issues = [], []

            def add(cid, kind, ref, title, body="", **meta):
                rows.append((user, cid, kind, ref, title, body,
                             json.dumps(meta, ensure_ascii=False)))

            assignments = {a["id"]: a for a in self.assignments()}
            for course in self.records("course"):
                cid = course["id"]
                add(cid, "course", cid, course["name"], course.get("code") or "")
                for material in self.materials(cid):
                    self.index_file(material, cid, "material", add, issues)
            for a in assignments.values():
                add(a["course_id"], "assignment", a["id"], a["title"], a.get("description", ""),
                    assignment_id=a["id"], due_at=a.get("due_at"))
            for asset in self.records("asset"):
                a = assignments.get(asset["assignment_id"])
                if a and asset["id"] in a.get("asset_ids", []):
                    self.index_file(asset, a["course_id"], "asset", add, issues,
                                    assignment_id=a["id"], due_at=a.get("due_at"))
            for task in self.records("task"):
                a = self.study.store.get(user, "assignment", task["assignment_id"])
                if a:
                    add(a["course_id"], "task", task["id"], task["title"],
                        assignment_id=a["id"], due_at=a.get("due_at"), status=task["status"])
            with self.study.store.connection() as db:
                db.execute("DELETE FROM learning_search WHERE user_id=?", (user,))
                db.executemany("INSERT INTO learning_search VALUES (?,?,?,?,?,?,?)", rows)
            result = {"indexed_at": now(), "rows": len(rows), "issues": issues}
            self.study.store.save(user, "preferences", "search_index", result)
            return result

    def index_file(self, asset, cid, kind, add, issues, **meta):
        ref, name = asset["id"], asset["name"]
        add(cid, kind, ref, name, **meta)
        if not asset.get("path"):
            issues.append({"name": name, "code": "尚未下載，僅索引檔名"})
            return
        if Path(name).suffix.lower() not in {".pdf", ".docx", ".txt", ".csv", ".png", ".jpg", ".jpeg", ".webp"}:
            issues.append({"name": name, "code": "此格式僅索引檔名"})
            return
        try:
            path = self.study.safe_path(asset["path"])
            if path.stat().st_size > 10_000_000:
                fail("INPUT_SIZE_INVALID")
            data = path.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            if digest != asset.get("sha256"):
                fail("ASSET_CHANGED")
            key = kind + ":" + ref
            cached = self.study.store.get(self.study.user, "search_document", key)
            if cached and cached["sha256"] == digest and cached.get("parser_version") == 2:
                chunks, flags = cached["chunks"], cached["flags"]
            else:
                chunks, flags = parse(data, Path(name).suffix.lower(),
                                      asset.get("mime_type") or "application/octet-stream", ref)
                self.study.store.save(self.study.user, "search_document", key,
                                      {"sha256": digest, "chunks": chunks, "flags": flags, "parser_version": 2})
            for chunk in chunks:
                add(cid, kind, ref, name, chunk["text"], **meta, sha256=digest,
                    page=chunk.get("page"), section=chunk.get("section"), flags=flags)
            if flags:
                issues.append({"name": name, "code": ", ".join(flags)})
        except Exception as exc:
            from backend.services.study import error_code
            issues.append({"name": name, "code": error_code(exc)})

    @locked
    def search(self, query, course_id=None, due_from=None, due_to=None):
        query = query.strip()
        if len(query) > 200:
            fail("SEARCH_QUERY_TOO_LONG")
        if course_id:
            self.study.get("course", course_id)
        try:
            lower = datetime.strptime(due_from, "%Y-%m-%d").replace(tzinfo=TAIPEI).timestamp() if due_from else None
            upper = (datetime.strptime(due_to, "%Y-%m-%d").replace(tzinfo=TAIPEI) + timedelta(days=1)).timestamp() if due_to else None
        except ValueError:
            fail("SEARCH_DATE_INVALID")
        if lower and upper and lower >= upper:
            fail("SEARCH_DATE_INVALID")
        # Rebuild atomically, reusing extracted chunks only when the file hash matches.
        # This also removes revoked/deleted sources and stale excerpts from results.
        index = self.rebuild_index()
        where, params = ["user_id=?"], [self.study.require_user()]
        if course_id:
            where.append("course_id=?")
            params.append(course_id)
        if len(query) >= 3:
            where.append("learning_search MATCH ?")
            params.append('"' + query.replace('"', '""') + '"')
        elif query:
            where.append("(instr(lower(title), lower(?)) > 0 OR instr(lower(body), lower(?)) > 0)")
            params.extend([query, query])
        if lower is not None:
            where.append("json_extract(metadata, '$.due_at') >= ?")
            params.append(lower)
        if upper is not None:
            where.append("json_extract(metadata, '$.due_at') < ?")
            params.append(upper)
        with self.study.store.connection() as db:
            rows = db.execute("SELECT * FROM learning_search WHERE " + " AND ".join(where)
                              + " ORDER BY course_id, kind, ref LIMIT 101", params).fetchall()
        hits = []
        for row in rows[:100]:
            text = row["body"]
            at = max(0, text.lower().find(query.lower()) - 70) if query else 0
            hits.append({k: row[k] for k in ("course_id", "kind", "ref", "title")} |
                        json.loads(row["metadata"]) | {"snippet": text[at:at + 300]})
        return {"results": hits, "truncated": len(rows) > 100, "index": index}

    def save_progress(self, value):
        with self.study.lock:
            user = self.study.require_user()
            course_id = value.get("course_id")
            assignment_id = value.get("assignment_id")
            if course_id:
                self.study.get("course", course_id)
            assignment = self.study.get("assignment", assignment_id) if assignment_id else None
            if assignment and assignment["course_id"] != course_id:
                fail("COURSE_SCOPE_MISMATCH")
            selected = list(dict.fromkeys(value.get("selected_asset_ids") or []))
            if len(selected) > 20 or set(selected) - set((assignment or {}).get("asset_ids", [])):
                fail("ASSET_SCOPE_MISMATCH")
            task_id = value.get("task_id") or None
            if task_id and self.study.get("task", task_id)["assignment_id"] != assignment_id:
                fail("COURSE_SCOPE_MISMATCH")
            from backend.services.study import WORKFLOW_MODES
            mode = value.get("mode", "report_draft")
            if mode not in WORKFLOW_MODES:
                fail("INVALID_MODE")
            provider = value.get("research_provider", "crossref")
            if provider not in {"crossref", "europepmc", "combined"}:
                fail("AGENT_OPTIONS_INVALID")
            result = {"course_id": course_id, "assignment_id": assignment_id,
                      "selected_asset_ids": selected, "task_id": task_id, "mode": mode,
                      "page": value.get("page") if value.get("page") in STEPS else "select",
                      "research_provider": provider,
                      "research_full_text": provider != "crossref" and value.get("research_full_text") is True,
                      "saved_at": now()}
            for field, limit in (("instruction", 4000), ("excerpt", 24000),
                                 ("requirements", 6000), ("editor", 80000), ("research_query", 300)):
                text = value.get(field) or ""
                if not isinstance(text, str) or len(text) > limit:
                    fail("INPUT_SIZE_INVALID")
                result[field] = text
            self.study.store.save(user, "preferences", "workflow", result)
            return result

    @locked
    def progress(self):
        user = self.study.require_user()
        value = self.study.store.get(user, "preferences", "workflow")
        if not value:
            return None
        value = dict(value)
        assignment = self.study.store.get(user, "assignment", value.get("assignment_id"))
        if not assignment or assignment.get("removed") or assignment["course_id"] != value.get("course_id"):
            return None
        value["selected_asset_ids"] = [ref for ref in value["selected_asset_ids"] if ref in assignment["asset_ids"]]
        task = self.study.store.get(user, "task", value.get("task_id"))
        if task and task["assignment_id"] == assignment["id"]:
            if not value.get("editor"):
                value["editor"] = task.get("draft", "")
        else:
            value.update(task_id=None, editor="")
        if value["page"] == "generate" and (not task or task["status"] not in {"running", "partial", "cancelled", "blocked_auth", "failed"}):
            value["page"] = "review" if value["editor"] else "configure"
        if value["page"] == "review" and not value["editor"]:
            value["page"] = "configure"
        value.update(data_transfer_approved=False, reviewed=False)
        return value
