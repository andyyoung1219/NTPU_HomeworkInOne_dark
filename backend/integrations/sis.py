"""Normal portal login + verified SIS bridge, ported from pinned NTPUOne.

Cookies/CSRF/bridge credentials live only in memory. No JavaScript execution,
no automatic CAPTCHA handling, no persisted raw HTML or student identifier.
"""

import hashlib
import json
import re
import ssl
from codecs import lookup
from urllib.parse import urlencode, urljoin, urlsplit

import httpx
from lxml import html

from backend.integrations.sis_parser import parse_enrolment, parse_report
from backend.integrations.transport import ReadTransport
from backend.p0.moodle import probe

ORIGIN = "https://my.ntpu.edu.tw"
PATH = "/pls/univer/u0001.query_all_course.judge"
TARGETS = {"timetable": PATH + "?func=3_m", "grades": PATH + "?func=5_m",
           "rank": PATH + "?func=8_m", "graduation": "/pls/univer/query_all_course.judge?func=9_m"}
BRIDGE_HOSTS = {"cof.ntpu.edu.tw", "ohs01.ntpu.edu.tw"}


def _sis_transport():
    bridge_tls = httpx.create_ssl_context(trust_env=False)
    bridge_tls.minimum_version = ssl.TLSVersion.TLSv1_2
    # The bridge servers require this TLS 1.2 cipher. Keep the normal
    # certificate and hostname checks and prefer the default suites.
    suites = [cipher["name"] for cipher in bridge_tls.get_ciphers()
              if cipher["protocol"] != "TLSv1.3"]
    bridge_tls.set_ciphers(":".join([*suites, "ECDHE-RSA-AES256-SHA"]))
    mounts = {f"https://{host}": httpx.HTTPTransport(verify=bridge_tls, trust_env=False)
              for host in BRIDGE_HOSTS}
    return ReadTransport(httpx.Client(mounts=mounts, follow_redirects=False,
                                      trust_env=False, timeout=25))


def identity_key(identity):
    return hashlib.sha256(("sis:" + identity.casefold()).encode()).hexdigest()[:24]


def _decode_report(response):
    encoding = response.charset_encoding or "big5"
    try:
        encoding = lookup(encoding).name
        return response.content.decode(encoding)
    except (UnicodeError, LookupError):
        # Taiwanese pages labelled Big5 can include Windows Big5 extensions.
        if encoding == "big5":
            try:
                return response.content.decode("cp950")
            except UnicodeError:
                pass
        raise probe.ProbeError("SIS_REPORT_ENCODING_UNSUPPORTED") from None


def portal_redirect_url(current_url, location):
    """Canonicalize the school's same-origin home without broadening route access.

    The live portal returns Location: https://my.ntpu.edu.tw (no slash).
    Compare URL components rather than two different spellings of that origin.
    """
    if not location or any(ord(c) < 32 for c in location) or "\\" in location:
        raise probe.ProbeError("SIS_REDIRECT_REQUIRES_REVIEW")
    try:
        target = urlsplit(urljoin(current_url, location))
        valid = (target.scheme == "https" and target.hostname == "my.ntpu.edu.tw"
                 and target.port in (None, 443) and not target.username and not target.password
                 and target.path in ("", "/", "/login") and not target.query and not target.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise probe.ProbeError("SIS_REDIRECT_REQUIRES_REVIEW")
    return ORIGIN + (target.path or "/")


def _bridge_url(value, route, role):
    """Accept equivalent HTTPS origins, returning only a canonical allowed URL.

    Error codes describe the failed check without including school response data.
    Never normalize paths or follow a redirect when forwarding credentials.
    """
    def reject(reason):
        raise probe.ProbeError(f"SIS_BRIDGE_{role}_{reason}")

    if (not isinstance(value, str) or not value or value != value.strip()
            or any(ord(c) < 32 or ord(c) == 127 for c in value) or "\\" in value):
        reject("URL_FORMAT")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        reject("URL_FORMAT")
    if (parsed.scheme != "https" or port not in (None, 443)
            or parsed.username is not None or parsed.password is not None or parsed.fragment):
        reject("URL_FORMAT")
    if parsed.hostname not in BRIDGE_HOSTS:
        reject("HOST")
    expected = urlsplit(route)
    if parsed.path != expected.path or parsed.query != expected.query:
        reject("ROUTE")
    return f"https://{parsed.hostname}{route}", parsed.hostname


def validate_bridge(descriptor, kind):
    """Validate both destinations and field shape before any credential POST."""
    fields = descriptor.get("formData")
    try:
        login, host = _bridge_url(descriptor.get("form_url"), "/pls/pm/stud_system.signin", "LOGIN")
        report, report_host = _bridge_url(descriptor.get("base_url"), TARGETS[kind], "REPORT")
        if host != report_host:
            raise probe.ProbeError("SIS_BRIDGE_HOST_MISMATCH")
        if not isinstance(fields, dict):
            raise probe.ProbeError("SIS_BRIDGE_FIELDS_FORMAT")
        if not {"id", "pw"}.issubset(fields):
            raise probe.ProbeError("SIS_BRIDGE_FIELDS_MISSING")
        if set(fields) != {"id", "pw"}:
            raise probe.ProbeError("SIS_BRIDGE_FIELDS_EXTRA")
        if not all(isinstance(v, str) and v for v in fields.values()):
            raise probe.ProbeError("SIS_BRIDGE_CREDENTIALS_EMPTY")
    except probe.ProbeError:
        # Discard rejected credentials too, without persisting the descriptor.
        if isinstance(fields, dict):
            fields.clear()
        raise
    return login, report, fields


class SISClient:
    def __init__(self, transport=None):
        self.transport = transport if transport is not None else _sis_transport()
        self.menus = None
        self.identity = None
        self.expected_identity_key = None
        self.state = "disconnected"

    def reset(self):
        self.transport.client.cookies.clear()
        self.menus = self.identity = None
        self.expected_identity_key = None
        self.state = "disconnected"

    def _portal(self, path, fields=None):
        url = ORIGIN + path
        for hop in range(6):
            response = self.transport.request("POST" if fields is not None else "GET", url,
                data=fields, retry=fields is None,
                headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}
                if path.startswith("/get-projects/") else {})
            if response.status_code in {301, 302, 303}:
                target = portal_redirect_url(url, response.headers.get("location", ""))
                if path.startswith("/get-projects/"):
                    if target == ORIGIN + "/login":
                        self.state = "login_required"
                        raise probe.ProbeError("SIS_LOGIN_REQUIRED")
                    raise probe.ProbeError("SIS_REDIRECT_REQUIRES_REVIEW")
                # A redirect after POST is a fresh GET; never replay credentials.
                url, fields = target, None
                continue
            self._status(response)
            return response.text, url
        raise probe.ProbeError("SIS_REDIRECT_REQUIRES_REVIEW")

    def _status(self, response):
        if response.status_code in {401, 403}:
            self.state = "login_required" if response.status_code == 401 else "permission_denied"
            raise probe.ProbeError("SIS_LOGIN_REQUIRED" if response.status_code == 401 else "SIS_PERMISSION_DENIED")
        if 300 <= response.status_code < 400:
            raise probe.ProbeError("SIS_REDIRECT_REQUIRES_REVIEW")
        if response.status_code != 200:
            raise probe.ProbeError("SIS_SERVICE_UNAVAILABLE", response.status_code)

    def login(self, username, password):
        self.reset()
        try:
            raw, _ = self._portal("/login")
            page = html.fromstring(raw)
            forms = page.xpath("//form[translate(@method,'post','POST')='POST']")
            valid = [form for form in forms if form.get("action") in {"/login", ORIGIN + "/login"}]
            tokens = valid[0].xpath(".//input[@name='_token']/@value") if len(valid) == 1 else []
            fields = valid[0].xpath(".//input/@name") if len(valid) == 1 else []
            if (len(tokens) != 1 or not tokens[0] or not {"username", "password"}.issubset(fields)
                    or re.search(r"captcha|samlrequest|oauthsso", raw, re.I)):
                raise probe.ProbeError("SIS_BROWSER_LOGIN_REQUIRED")
            raw, url = self._portal("/login", {"_token": tokens[0], "username": username, "password": password})
            page = html.fromstring(raw)
            if urlsplit(url).path == "/login" or page.xpath("//input[@name='password']") or not page.xpath("//form[@action='/logout']|//a[@href='/logout']|//form[@action=$url]|//a[@href=$url]", url=ORIGIN + "/logout"):
                raise probe.ProbeError("SIS_LOGIN_REQUIRED")
            match = re.search(r"\bvar arr\s*=\s*(\[.*?\]);", raw, re.S)
            self.menus = json.loads(match[1]) if match else None
            if not isinstance(self.menus, list) or len(self.menus) > 1000:
                raise probe.ProbeError("SIS_SCHEMA_CHANGED")
            self.state = "connected"
        except Exception:
            self.reset()
            self.state = "login_required"
            raise

    def read(self, kind):
        if kind not in TARGETS:
            raise probe.ProbeError("SIS_ENDPOINT_NOT_ALLOWED")
        if self.state != "connected" or self.menus is None:
            raise probe.ProbeError("SIS_LOGIN_REQUIRED")
        target = "https://ohs01.ntpu.edu.tw" + TARGETS[kind]
        matches = [m for m in self.menus if isinstance(m, dict) and m.get("url") == target and m.get("project_id") == 10]
        if len(matches) != 1:
            raise probe.ProbeError("SIS_REPORT_NOT_AVAILABLE")
        raw, _ = self._portal("/get-projects/10?" + urlencode({"url": target}))
        try:
            descriptor = json.loads(raw)
        except ValueError:
            if "password" in raw or "/login" in raw:
                self.state = "login_required"
                raise probe.ProbeError("SIS_LOGIN_REQUIRED") from None
            raise probe.ProbeError("SIS_SCHEMA_CHANGED") from None
        if not isinstance(descriptor, dict):
            raise probe.ProbeError("SIS_SCHEMA_CHANGED")
        login, report, fields = validate_bridge(descriptor, kind)
        identity = fields["id"]
        if ((self.identity is not None and self.identity != identity)
                or (self.expected_identity_key is not None
                    and identity_key(identity) != self.expected_identity_key)):
            fields.clear()
            self.reset()
            raise probe.ProbeError("SIS_ACCOUNT_CHANGED")
        try:
            # Send only the exact descriptor fields to a verified school endpoint.
            response = self.transport.request("POST", login, data=fields, retry=False)
        finally:
            fields.clear()
        self._status(response)
        response = self.transport.request("GET", report)
        self._status(response)
        raw = _decode_report(response)
        if re.search(r"name\s*=\s*['\"]?(?:password|passwd|pw)\b", raw, re.I):
            self.state = "login_required"
            raise probe.ProbeError("SIS_LOGIN_REQUIRED")
        result = parse_enrolment(raw, identity) if kind == "timetable" else parse_report(raw, identity, kind)
        self.identity = identity
        return {**result, "identity_key": identity_key(identity), "source": "sis",
                "source_url": target}

    def close(self):
        self.reset()
        self.transport.close()
