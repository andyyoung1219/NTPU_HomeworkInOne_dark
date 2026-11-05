"""Local P4 views, searchable sources and recoverable wizard inputs.

All queries are scoped to the active account. Building an index only reads
already downloaded files; it never downloads or sends anything to a model.
"""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

from backend.models.store import now
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
    if state not in {"new", "draft", "reopened", "not_submitted", "no_submission"}:
        return "待確認"
    if item.get("due_at") and item["due_at"] < timestamp:
        return "逾期未提交"
    return "未完成"


class LearningCenter:
    def __init__(self, study):
        self.study = study
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

    @locked
    def dashboard(self):
        items = self.assignments()
        current = datetime.now(TAIPEI)
        timestamp = current.timestamp()
        courses = self.records("course")
        active = {c["id"] for c in courses if c.get("scope") != "inactive"}
        items = [a for a in items if a["course_id"] in active]
        tasks = self.records("task")
        timetable = self.study.hub.snapshot("sis:timetable")
        today = []
        for course in (timetable.get("data") or {}).get("courses", []):
            for meeting in course.get("meetings", []):
                if meeting.get("weekday") == current.isoweekday():
                    today.append({**meeting, "name": course["name"], "teacher": course.get("teacher")})
        return {"date": current.date().isoformat(), "courses": courses,
                "today": sorted(today, key=lambda x: str(x.get("period") or "").zfill(3)),
                "timetable": timetable, "assignments": items,
                "upcoming": [a for a in items if a.get("due_at") and timestamp <= a["due_at"]
                             <= timestamp + 7 * 86400 and a["submission_label"] != "已提交"],
                "overdue": [a for a in items if a["submission_label"] == "逾期未提交"],
                "unknown": [a for a in items if a["submission_label"] == "待確認"],
                "review": [t for t in tasks if t.get("draft") and t["status"] != "completed"],
                "recent": tasks[:12]}

    @locked
    def course(self, course_id):
        course = self.study.get("course", course_id)
        assignments = self.assignments(course_id)
        ids = {a["id"] for a in self.records("assignment") if a["course_id"] == course_id}
        snapshots = self.study.hub.course(course_id)
        return {"course": course, "assignments": assignments, "snapshots": snapshots,
                "tasks": [t for t in self.records("task") if t["assignment_id"] in ids],
                "materials": self.materials(course_id),
                "assets": [a for a in self.records("asset") if a["assignment_id"] in ids]}

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
            result = {"course_id": course_id, "assignment_id": assignment_id,
                      "selected_asset_ids": selected, "task_id": task_id, "mode": mode,
                      "page": value.get("page") if value.get("page") in STEPS else "select",
                      "saved_at": now()}
            for field, limit in (("instruction", 4000), ("excerpt", 24000),
                                 ("requirements", 6000), ("editor", 80000)):
                text = value.get(field) or ""
                if not isinstance(text, str) or len(text) > limit:
                    fail("INPUT_SIZE_INVALID")
                result[field] = text
            self.study.store.save(user, "preferences", "workflow", result)
            return result

    @locked
    def progress(self):
        value = self.study.store.get(self.study.require_user(), "preferences", "workflow")
        if not value:
            return None
        value = dict(value)
        assignment = self.study.store.get(self.study.user, "assignment", value.get("assignment_id"))
        if not assignment or assignment.get("removed") or assignment["course_id"] != value.get("course_id"):
            return None
        value["selected_asset_ids"] = [ref for ref in value["selected_asset_ids"] if ref in assignment["asset_ids"]]
        task = self.study.store.get(self.study.user, "task", value.get("task_id"))
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
