"""JSON-RPC-style pipe client. No cookie export and no local unauthenticated AI port."""

import concurrent.futures
import json
import subprocess
import threading
import uuid

from .evidence import ROOT


class AIError(Exception):
    pass


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

    def rpc(self, method, **params):
        self.start()
        identifier = str(uuid.uuid4())
        future = concurrent.futures.Future()
        with self.lock:
            self.requests[identifier] = future
            self.process.stdin.write(json.dumps({"id": identifier, "method": method, "params": params}) + "\n")
            self.process.stdin.flush()
        return future.result(timeout=120)

    def begin(self, prompt, asset_ref=None):
        task_id = "p0-" + uuid.uuid4().hex
        future = concurrent.futures.Future()
        self.jobs[task_id] = future
        try:
            self.rpc("start_task", task_id=task_id, prompt=prompt, approved=True, asset_ref=asset_ref)
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
