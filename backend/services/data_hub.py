"""P2 user-scoped snapshots, partial sync, materials and explicit course mapping."""

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from backend.integrations.materials import FORMATS, fetch_material
from backend.integrations.sis import SISClient, identity_key
from backend.models.store import now
from backend.p0.moodle import probe, validate_url
from backend.services.documents import plain_html

TTL = 3600


def source_fingerprint(value):
    if isinstance(value, dict):
        return {k: source_fingerprint(v) for k, v in value.items() if k not in {
            "last_synced_at", "downloaded_at", "path", "sha256", "status", "stale"}}
    if isinstance(value, list):
        return [source_fingerprint(v) for v in value]
    return value


def fail(code):
    raise probe.ProbeError(code)


def course_metadata(raw):
    values = " ".join(str(raw.get(k, "")) for k in ("shortname", "idnumber", "fullname"))
    terms = set(re.findall(r"(?<!\d)(1\d{2})[-_\s]?([12])(?!\d)", values))
    codes = set(re.findall(r"(?<![A-Z0-9])([A-Z]\d{4})(?!\d)", values.upper()))
    return {"semester": "-".join(next(iter(terms))) if len(terms) == 1 else None,
            "code": next(iter(codes)) if len(codes) == 1 else None,
            "shortname": str(raw.get("shortname", "")), "idnumber": str(raw.get("idnumber", "")),
            "start_at": raw.get("startdate") or None, "end_at": raw.get("enddate") or None}


def normalized_name(name):
    name = re.sub(r"(?<!\d)1\d{2}[-_\s]?[12](?!\d)|[A-Z]\d{4}", "", name.upper())
    return re.sub(r"[\s()（）\[\]【】_\-]+", "", name)


def safe_link(value):
    if not isinstance(value, str) or any(ord(c) < 32 for c in value):
        return None
    try:
        p = urlsplit(value)
        if p.scheme in {"http", "https"} and p.hostname and not p.username and not p.password:
            return value
    except ValueError:
        pass
    return None


class DataHub:
    def __init__(self, study, sis=None):
        self.study = study
        self.sis = sis or SISClient()
        self.sis_user = None

    @property
    def user(self):
        return self.study.require_user()

    @property
    def store(self):
        return self.study.store

    def snapshot(self, identifier, *, user=None):
        saved = self.store.get(user or self.user, "hub", identifier)
        if saved is None:
            return {"id": identifier, "data": None, "status": "not_synced", "stale": True,
                    "last_synced_at": None, "error": None}
        saved["stale"] = saved["stale"] or not saved.get("last_synced_at") or (
            datetime.now(UTC) - datetime.fromisoformat(saved["last_synced_at"])).total_seconds() > TTL
        if saved["stale"] and saved["status"] == "synced":
            saved["status"] = "cached"
        return saved

    def save_snapshot(self, identifier, data, source, url=None):
        previous = self.store.get(self.user, "hub", identifier) or {}
        digest = hashlib.sha256(json.dumps(source_fingerprint(data), sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        saved = {"id": identifier, "data": data, "source": source, "source_url": url,
                 "last_synced_at": now(), "stale": False, "status": "synced", "error": None,
                 "content_sha256": digest, "changed_at": previous.get("changed_at")
                 if digest == previous.get("content_sha256") else now()}
        if identifier == "sis:graduation" and data.get("summary_issues"):
            saved["status"] = "partial"
        self.store.save(self.user, "hub", identifier, saved)
        return saved

    def failed(self, identifier, error, source):
        from backend.services.study import error_code

        saved = self.snapshot(identifier)
        code = error_code(error)
        saved.update(stale=True, status="error", error=code, source=source, last_attempted_at=now())
        self.store.save(self.user, "hub", identifier, saved)
        return saved

    def connect_sis(self, username, password):
        with self.study.lock:
            username = username.strip()
            self.study.verify_identity()
            token, _ = self.study.moodle.credentials()
            info, _ = probe.site_info(self.study.moodle.client, token)
            # Linking school records to a different Moodle account is never implicit.
            expected = str(info.get("username", "")).casefold()
            self.sis_user = None
            self.sis.reset()
            if not expected or username.casefold() != expected:
                fail("SIS_MOODLE_ACCOUNT_MISMATCH")
            self.sis.login(username, password)
            self.sis.expected_identity_key = identity_key(expected)
            self.sis_user = self.user
            try:
                timetable = self.sis.read("timetable")
                binding = self.store.get(self.user, "connection", "sis")
                if (timetable["identity_key"] != identity_key(expected)
                        or binding and binding["identity_key"] != timetable["identity_key"]):
                    fail("SIS_ACCOUNT_CHANGED")
                self.store.save(self.user, "connection", "sis", {"identity_key": timetable["identity_key"]})
                self.sis_user = self.user
                return self.save_snapshot("sis:timetable", timetable, "sis", timetable["source_url"])
            except Exception as exc:
                result = self.failed("sis:timetable", exc, "sis")
                if getattr(exc, "code", "") in {"SIS_ACCOUNT_CHANGED", "SIS_LOGIN_REQUIRED"}:
                    self.disconnect_sis()
                    raise
                # Portal authentication succeeded. A downstream report/host/schema
                # error must not discard that session or be called a bad password.
                return result

    def disconnect_sis(self):
        with self.study.lock:
            self.sis.reset()
            self.sis_user = None

    def sync_sis(self):
        with self.study.lock:
            self.user
            try:
                self.study.verify_identity()
                if self.sis_user != self.user:
                    fail("SIS_LOGIN_REQUIRED")
            except Exception as exc:
                return {kind: self.failed("sis:" + kind, exc, "sis")
                        for kind in ("timetable", "grades", "rank", "graduation")}
            binding = self.store.get(self.user, "connection", "sis")
            result = {}
            for kind in ("timetable", "grades", "rank", "graduation"):
                identifier = "sis:" + kind
                try:
                    data = self.sis.read(kind)
                    expected_key = binding["identity_key"] if binding else self.sis.expected_identity_key
                    if not expected_key or expected_key != data["identity_key"]:
                        self.disconnect_sis()
                        fail("SIS_ACCOUNT_CHANGED")
                    if not binding:
                        binding = {"identity_key": data["identity_key"]}
                        self.store.save(self.user, "connection", "sis", binding)
                    result[kind] = self.save_snapshot(identifier, data, "sis", data["source_url"])
                except Exception as exc:
                    result[kind] = self.failed(identifier, exc, "sis")
                    if getattr(exc, "code", "") == "SIS_ACCOUNT_CHANGED":
                        self.disconnect_sis()
                    if getattr(exc, "code", "") in {"SIS_ACCOUNT_CHANGED", "SIS_LOGIN_REQUIRED", "RATE_LIMITED", "NETWORK_UNAVAILABLE"}:
                        for remaining in ("timetable", "grades", "rank", "graduation"):
                            if remaining not in result:
                                result[remaining] = self.failed("sis:" + remaining, exc, "sis")
                        break
            return result

    def discover(self, semester=None):
        """Current enrolments by default; history only for an explicitly chosen term."""
        with self.study.lock:
            if semester is None or semester == "":
                return self.study.sync_courses()
            if not re.fullmatch(r"1\d{2}-[12]", semester):
                fail("INVALID_SEMESTER")
            self.study.verify_identity()
            token, available = self.study.moodle.credentials()
            function = "core_course_get_enrolled_courses_by_timeline_classification"
            if function not in available:
                fail("MOODLE_PERMISSION_DENIED")
            courses, offset = {}, 0
            for _ in range(40):
                _, result = self.study.moodle.client.call(function, token, {"classification": "all", "limit": 50, "offset": offset})
                if not isinstance(result, dict) or not isinstance(result.get("courses"), list):
                    fail("MOODLE_SCHEMA_CHANGED")
                if not result["courses"]:
                    break
                for raw in result["courses"]:
                    if not isinstance(raw, dict) or type(raw.get("id")) is not int or raw["id"] <= 0 or raw["id"] in courses:
                        fail("MOODLE_SCHEMA_CHANGED")
                    courses[raw["id"]] = raw
                if type(result.get("nextoffset")) is not int or result["nextoffset"] <= offset:
                    fail("MOODLE_PAGINATION_INVALID")
                offset = result["nextoffset"]
            else:
                fail("MOODLE_PAGINATION_LIMIT")
            selected = {k: c for k, c in courses.items() if course_metadata(c)["semester"] == semester}
            self.study.moodle.courses.update(selected)
            records = [("course", str(key), {"id": str(key), "name": raw.get("fullname") or raw.get("shortname") or str(key),
                        **course_metadata(raw), "source": "moodle", "last_synced_at": now(), "stale": False,
                        "scope": semester}) for key, raw in selected.items()]
            self.store.save_many(self.user, records)
            return [record[2] for record in records]

    def sync_moodle(self, course_ids):
        with self.study.lock:
            if not course_ids or len(course_ids) > 30 or len(set(course_ids)) != len(course_ids):
                fail("COURSE_SELECTION_REQUIRED")
            for course_id in course_ids:
                self.study.get("course", course_id)
            try:
                self.study.verify_identity()
                token, available = self.study.moodle.credentials()
            except Exception as exc:
                return {cid: {kind: self.failed(f"moodle:{cid}:{kind}", exc, "moodle")
                              for kind in ("assignments", "contents", "announcements", "grades")}
                        for cid in course_ids}
            self.store.save(self.user, "preferences", "hub", {"course_ids": course_ids})
            results = {}
            for course_id in course_ids:
                course_result = {}
                loaders = {
                    "assignments": lambda: self.study.sync_assignments(course_id),
                    "contents": lambda: self.contents(course_id, token, available),
                    "announcements": lambda: self.announcements(course_id, token, available),
                    "grades": lambda: self.moodle_grades(course_id, token, available),
                }
                for kind, loader in loaders.items():
                    identifier = f"moodle:{course_id}:{kind}"
                    try:
                        data = loader()
                        course_result[kind] = self.save_snapshot(identifier, data, "moodle", f"{probe.MOODLE}/course/view.php?id={course_id}")
                    except Exception as exc:
                        course_result[kind] = self.failed(identifier, exc, "moodle")
                        if getattr(exc, "code", "") in {"RATE_LIMITED", "NETWORK_UNAVAILABLE", "invalidtoken", "account_identity_changed", "ACCOUNT_CHANGED_RECONNECT"}:
                            # Remaining scopes also explicitly become stale, without more HTTP calls.
                            for cid in course_ids:
                                target = results.setdefault(cid, {})
                                if cid == course_id:
                                    target.update(course_result)
                                for pending in loaders:
                                    if pending not in target:
                                        target[pending] = self.failed(f"moodle:{cid}:{pending}", exc, "moodle")
                            return results
                results[course_id] = course_result
            return results

    def call(self, function, token, available, params):
        if function not in available:
            fail("MOODLE_PERMISSION_DENIED")
        _, value = self.study.moodle.client.call(function, token, params)
        if isinstance(value, dict) and value.get("warnings"):
            fail("MOODLE_ACCESS_WARNING")
        return value

    def file_metadata(self, course_id, owner, raw):
        url = raw.get("fileurl", "")
        try:
            validate_url(url, resolve=False)
        except (probe.ProbeError, ValueError):
            return {"name": str(raw.get("filename", "")), "status": "official_page_required"}
        ref = hashlib.sha256(f"material:{course_id}:{owner}:{url}".encode()).hexdigest()[:24]
        name = str(raw.get("filename", ""))
        previous = self.store.get(self.user, "material", ref) or {}
        changed = any(previous.get(k) != raw.get(v) for k, v in (("size_bytes", "filesize"), ("modified_at", "timemodified")))
        value = {**previous, "id": ref, "course_id": course_id, "owner_id": owner, "name": name,
                 "source_url": url, "source": "moodle", "size_bytes": raw.get("filesize"),
                 "mime_type": raw.get("mimetype", ""), "modified_at": raw.get("timemodified"),
                 "last_synced_at": now(), "status": "not_downloaded" if changed else previous.get("status", "not_downloaded")}
        if changed:
            for key in ("path", "sha256", "downloaded_at"):
                value.pop(key, None)
        value["download_supported"] = Path(name).suffix.lower() in FORMATS
        return value

    def contents(self, course_id, token, available):
        sections = self.call("core_course_get_contents", token, available, {"courseid": int(course_id)})
        if not isinstance(sections, list):
            fail("MOODLE_SCHEMA_CHANGED")
        output, files, seen = [], [], set()
        for raw in sections:
            if not isinstance(raw, dict) or type(raw.get("id")) is not int or not isinstance(raw.get("modules"), list):
                fail("MOODLE_SCHEMA_CHANGED")
            modules = []
            for module in raw["modules"]:
                if not isinstance(module, dict) or type(module.get("id")) is not int or module["id"] in seen:
                    fail("MOODLE_SCHEMA_CHANGED")
                seen.add(module["id"])
                owner = f"module:{module['id']}"
                items = []
                for item in module.get("contents", []):
                    if item.get("type") == "file":
                        value = self.file_metadata(course_id, owner, item)
                        items.append(value)
                        if value.get("id"):
                            files.append(("material", value["id"], value))
                    elif item.get("type") == "url":
                        items.append({"type": "url", "name": str(item.get("filename", "")), "url": safe_link(item.get("fileurl"))})
                    elif item.get("type") == "content":
                        items.append({"type": "text", "text": plain_html(str(item.get("content", "")))})
                modules.append({"id": str(module["id"]), "name": str(module.get("name", "")),
                                "type": module.get("modname", "unknown"), "description": plain_html(module.get("description", "")),
                                "url": safe_link(module.get("url")), "contents": items})
            output.append({"id": str(raw["id"]), "name": str(raw.get("name", "")),
                           "summary": plain_html(raw.get("summary", "")), "modules": modules})
        self.store.save_many(self.user, files)
        return output

    def announcements(self, course_id, token, available):
        forums = self.call("mod_forum_get_forums_by_courses", token, available, {"courseids[0]": int(course_id)})
        if not isinstance(forums, list):
            fail("MOODLE_SCHEMA_CHANGED")
        output, files, seen = [], [], set()
        for forum in forums:
            if str(forum.get("course")) != course_id:
                fail("MOODLE_COURSE_SCOPE_MISMATCH")
            if forum.get("type") != "news":
                continue
            for page in range(100):
                result = self.call("mod_forum_get_forum_discussions", token, available,
                    {"forumid": forum["id"], "page": page, "perpage": 50})
                if not isinstance(result, dict) or not isinstance(result.get("discussions"), list):
                    fail("MOODLE_SCHEMA_CHANGED")
                batch = result["discussions"]
                for post in batch:
                    identifier = str(post["discussion"])
                    if identifier in seen:
                        fail("MOODLE_PAGINATION_INVALID")
                    seen.add(identifier)
                    attachments = [self.file_metadata(course_id, "discussion:" + identifier, item) for item in post.get("attachments", [])]
                    files += [("material", item["id"], item) for item in attachments if item.get("id")]
                    output.append({"id": identifier, "forum_id": str(forum["id"]), "title": post.get("name", ""),
                                   "message": plain_html(post.get("message", "")), "modified_at": post.get("timemodified"),
                                   "attachments": attachments, "url": f"{probe.MOODLE}/mod/forum/discuss.php?d={identifier}"})
                if len(batch) < 50:
                    break
            else:
                fail("MOODLE_PAGINATION_LIMIT")
        self.store.save_many(self.user, files)
        return output

    def moodle_grades(self, course_id, token, available):
        info, _ = probe.site_info(self.study.moodle.client, token)
        value = self.call("gradereport_user_get_grade_items", token, available,
                          {"courseid": int(course_id), "userid": info["userid"]})
        if not isinstance(value, dict) or not isinstance(value.get("usergrades"), list) or len(value["usergrades"]) != 1:
            fail("MOODLE_GRADES_NOT_AVAILABLE")
        usergrade = value["usergrades"][0]
        if usergrade.get("userid") != info["userid"] or str(usergrade.get("courseid")) != course_id:
            fail("MOODLE_GRADE_SCOPE_MISMATCH")
        items = usergrade.get("gradeitems")
        if not isinstance(items, list):
            fail("MOODLE_SCHEMA_CHANGED")
        return [{"id": str(item["id"]), "name": plain_html(item.get("itemname") or "課程總分"),
                 "grade": plain_html(item.get("gradeformatted") or "未公布"),
                 "range": plain_html(item.get("rangeformatted") or ""),
                 "percentage": plain_html(item.get("percentageformatted") or ""),
                 "source": "moodle", "course_id": course_id} for item in items if not item.get("gradeishidden")]

    def download_material(self, ref):
        with self.study.lock:
            asset = self.store.get(self.user, "material", ref)
            if not asset:
                fail("NOT_FOUND_OR_NOT_AUTHORIZED")
            # Removed material refs cannot be used as a back door after a fresh sync.
            snapshots = [self.snapshot(f"moodle:{asset['course_id']}:{kind}") for kind in ("contents", "announcements")]
            listed = []
            for section in snapshots[0].get("data") or []:
                listed += [item.get("id") for module in section["modules"] for item in module["contents"]]
            listed += [item.get("id") for post in snapshots[1].get("data") or [] for item in post["attachments"]]
            if ref not in listed:
                fail("MATERIAL_NO_LONGER_LISTED")
            self.study.verify_identity()
            if not asset.get("download_supported"):
                fail("MATERIAL_OFFICIAL_PAGE_REQUIRED")
            if asset.get("path"):
                path = self.study.safe_path(asset["path"])
                if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == asset.get("sha256"):
                    return asset
            result = fetch_material(self.study.moodle, asset, self.study.root / "materials" / self.user)
            asset.update(path=str(result["path"].relative_to(self.study.root)), sha256=result["sha256"],
                         status="downloaded", downloaded_at=now())
            self.store.save(self.user, "material", ref, asset)
            return asset

    def mappings(self, *, user=None, timetable=None):
        user = user or self.user
        if timetable is None:
            timetable = self.snapshot("sis:timetable", user=user).get("data") or {}
        courses = self.store.list(user, "course")
        output = []
        for sis in timetable.get("courses", []):
            saved = self.store.get(user, "course_mapping", sis["id"])
            candidates = [c for c in courses if c.get("semester") in {None, sis["semester"]}
                          and (c.get("code") == sis["code"] or normalized_name(c["name"]) == normalized_name(sis["name"]))]
            exact = [c for c in candidates if c.get("semester") == sis["semester"] and c.get("code") == sis["code"]
                     and normalized_name(c["name"]) == normalized_name(sis["name"])]
            valid_saved = next((c for c in candidates if saved and c["id"] == saved["moodle_id"]), None)
            output.append({"sis_id": sis["id"], "name": sis["name"], "semester": sis["semester"],
                           "candidates": [{"id": c["id"], "name": c["name"], "semester": c.get("semester")} for c in candidates],
                           "moodle_id": valid_saved["id"] if valid_saved else exact[0]["id"] if len(exact) == 1 and len(candidates) == 1 else None,
                           "status": "confirmed" if valid_saved else "matched" if len(exact) == 1 and len(candidates) == 1 else "needs_confirmation" if candidates else "unmatched"})
        return output

    def confirm_mapping(self, sis_id, moodle_id):
        with self.study.lock:
            mapping = next((m for m in self.mappings() if m["sis_id"] == sis_id), None)
            if not mapping or moodle_id not in {c["id"] for c in mapping["candidates"]}:
                fail("COURSE_MAPPING_SCOPE_MISMATCH")
            if any(m["sis_id"] != sis_id and m["moodle_id"] == moodle_id for m in self.mappings()):
                fail("COURSE_MAPPING_CONFLICT")
            value = {"sis_id": sis_id, "moodle_id": moodle_id, "confirmed_at": now()}
            self.store.save(self.user, "course_mapping", sis_id, value)
            return value

    def overview(self):
        user = self.user
        connection = self.sis.state if self.sis_user == user else "login_required"
        snapshots = {kind: self.snapshot("sis:" + kind, user=user)
                     for kind in ("timetable", "grades", "rank", "graduation")}
        return {"sis_connection": connection, "sis": snapshots,
                "preferences": self.store.get(user, "preferences", "hub") or {"course_ids": []},
                "mappings": self.mappings(user=user, timetable=snapshots["timetable"].get("data") or {})}

    def course(self, course_id):
        user = self.user
        self.study.get("course", course_id, user)
        return {kind: self.snapshot(f"moodle:{course_id}:{kind}", user=user)
                for kind in ("assignments", "contents", "announcements", "grades")}
