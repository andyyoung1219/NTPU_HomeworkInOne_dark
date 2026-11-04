"""Bounded read requests; 429 cooldown persists across subsequent calls."""

import threading
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from backend.p0.moodle import probe


class ReadTransport:
    def __init__(self, client=None, clock=time.monotonic, sleep=time.sleep):
        self.client = client or httpx.Client(follow_redirects=False, trust_env=False, timeout=25)
        self.clock, self.sleep = clock, sleep
        self.next_request = 0
        self.retry_at = 0
        self.lock = threading.RLock()

    def request(self, method, url, *, retry=True, max_bytes=8 * 1024 * 1024, **kwargs):
        with self.lock:
            if self.clock() < self.retry_at:
                raise probe.ProbeError("RATE_LIMITED", 429)
            for attempt in range(3 if retry else 1):
                delay = self.next_request - self.clock()
                if delay > 0:
                    self.sleep(delay)
                self.next_request = self.clock() + 0.35
                try:
                    with self.client.stream(method, url, **kwargs) as response:
                        chunks, size = [], 0
                        for chunk in response.iter_bytes():
                            size += len(chunk)
                            if size > max_bytes:
                                raise probe.ProbeError("RESPONSE_TOO_LARGE")
                            chunks.append(chunk)
                        # iter_bytes already decodes content-encoding; do not decode gzip twice.
                        # Preserve wire bytes: Moodle may put a non-ASCII filename
                        # in Content-Disposition. Passing decoded header strings
                        # back to httpx would try to encode them as ASCII.
                        headers = [(k, v) for k, v in response.headers.raw
                                   if k.lower() not in {b"content-encoding", b"content-length"}]
                        result = httpx.Response(response.status_code, headers=headers,
                                                content=b"".join(chunks), request=response.request)
                except (httpx.TimeoutException, httpx.NetworkError):
                    if retry and attempt < 2:
                        self.sleep(2 ** attempt)
                        continue
                    raise probe.ProbeError("NETWORK_UNAVAILABLE") from None
                if result.status_code == 429:
                    raw = result.headers.get("retry-after", "60")
                    try:
                        seconds = max(1, int(raw))
                    except ValueError:
                        try:
                            seconds = max(1, (parsedate_to_datetime(raw) - datetime.now(UTC)).total_seconds())
                        except (ValueError, TypeError, OverflowError):
                            seconds = 60
                    self.retry_at = self.clock() + seconds
                    raise probe.ProbeError("RATE_LIMITED", 429)
                if result.status_code in {502, 503, 504} and retry and attempt < 2:
                    self.sleep(2 ** attempt)
                    continue
                return result

    def close(self):
        self.client.close()
