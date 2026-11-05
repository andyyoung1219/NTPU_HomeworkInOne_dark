"""JSON-RPC-style pipe client. No cookie export and no local unauthenticated AI port."""

import base64
import concurrent.futures
import hashlib
import json
import subprocess
import threading
import time
import uuid

from .evidence import ROOT


class AIError(Exception):
    pass


MAX_ATTACHMENT_BYTES = 6 * 1024 * 1024


def encode_attachment(attachment):
    """Bind an explicitly approved visual upload to exact PNG bytes, never paths."""
    if not isinstance(attachment, dict) or set(attachment) != {"data", "mime_type", "sha256", "approved"}:
        raise AIError("INVALID_ATTACHMENT")
    if attachment["approved"] is not True:
        raise AIError("ATTACHMENT_APPROVAL_REQUIRED")
    data = attachment["data"]
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_ATTACHMENT_BYTES:
        raise AIError("INVALID_ATTACHMENT")
    if attachment["mime_type"] != "image/png" or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise AIError("INVALID_ATTACHMENT")
    digest = hashlib.sha256(data).hexdigest()
    if attachment["sha256"] != digest:
        raise AIError("ATTACHMENT_CHANGED")
    return {"data_base64": base64.b64encode(data).decode("ascii"), "mime_type": "image/png",
            "sha256": digest, "approved": True}


def connection_status(adapter, *, timeout_seconds=60, cancel=None):
    """Restore the existing browser session before deciding that login is needed."""
    deadline = time.monotonic() + timeout_seconds

    def call(method):
        options = {"timeout_seconds": max(0.1, deadline - time.monotonic()), "cancel": cancel}
        return adapter.rpc(method, **options) if isinstance(adapter, AIAdapter) else adapter.rpc(method)

    status = call("login_status")
    if not status.get("ready") and status.get("authentication") == "unknown" and not status.get("loginWindow"):
        status = call("connect")
    return status


class AIAdapter:
    def __init__(self):
        self.process = None
        self.requests = {}
        self.jobs = {}
        self.lock = threading.Lock()

    def start(self):
        with self.lock:
            if self.process and self.process.poll() is None:
                return
            self.process = subprocess.Popen(
                ["node", str(ROOT / "ntpu-ai-bridge/src/p0.cjs")], cwd=ROOT,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            threading.Thread(target=self._read, args=(self.process,), daemon=True).start()

    def _read(self, process):
        for line in process.stdout:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if "id" in message:
                future = self.requests.pop(message["id"], None)
                if future and not future.done():
                    if "error" in message:
                        future.set_exception(AIError(message["error"]["code"]))
                    else:
                        future.set_result(message["result"])
            elif message.get("event") in {"completed", "failed"}:
                future = self.jobs.pop(message["task_id"], None)
                if future and not future.done():
                    if message["event"] == "failed":
                        future.set_exception(AIError(message["code"]))
                    else:
                        future.set_result(message["text"])
        for futures in (self.requests, self.jobs):
            for future in list(futures.values()):
                if not future.done():
                    future.set_exception(AIError("AI_BRIDGE_EXITED"))
            futures.clear()

    def rpc(self, method, *, timeout_seconds=120, cancel=None, **params):
        self.start()
        identifier = str(uuid.uuid4())
        future = concurrent.futures.Future()
        with self.lock:
            self.requests[identifier] = future
            self.process.stdin.write(json.dumps({"id": identifier, "method": method, "params": params}) + "\n")
            self.process.stdin.flush()
        deadline = time.monotonic() + timeout_seconds
        try:
            while True:
                if cancel is not None and cancel.is_set():
                    raise AIError("CANCELLED")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError()
                try:
                    return future.result(timeout=min(0.2, remaining))
                except concurrent.futures.TimeoutError:
                    continue
        finally:
            self.requests.pop(identifier, None)

    def begin(self, prompt, asset_ref=None, *, timeout_seconds=120, cancel=None, task_id=None,
              attachment=None):
        visual = encode_attachment(attachment) if attachment is not None else None
        task_id = task_id or "p0-" + uuid.uuid4().hex
        future = concurrent.futures.Future()
        self.jobs[task_id] = future
        try:
            options = {"attachment": visual} if visual is not None else {}
            self.rpc("start_task", task_id=task_id, prompt=prompt, approved=True, asset_ref=asset_ref,
                     timeout_ms=max(1, min(300000, int(timeout_seconds * 1000))),
                     timeout_seconds=timeout_seconds, cancel=cancel, **options)
        except Exception:
            self.jobs.pop(task_id, None)
            raise
        return task_id, future

    def close(self):
        if self.process and self.process.poll() is None:
            try:
                self.rpc("shutdown")
                self.process.wait(timeout=10)
            except Exception:
                self.process.terminate()
