"""Moodle read-only transport using the pinned function/parameter allowlist."""

from backend.integrations.transport import ReadTransport
from backend.p0.moodle import probe


class MoodleClient(probe.MoodleClient):
    def __init__(self, transport=None):
        self.transport = transport or ReadTransport()

    def request(self, path, fields=None, json_body=None):
        if path not in {"/login/token.php", "/webservice/rest/server.php",
                        "/lib/ajax/service.php?info=tool_mobile_get_public_config"}:
            raise probe.ProbeError("endpoint_not_allowed")
        response = self.transport.request("POST", probe.MOODLE + path,
            retry=path != "/login/token.php", **({"json": json_body} if json_body is not None else {"data": fields or {}}))
        if response.status_code != 200:
            raise probe.ProbeError("redirect_blocked" if 300 <= response.status_code < 400 else "http_error", response.status_code)
        try:
            result = response.json()
        except ValueError:
            raise probe.ProbeError("non_json_response") from None
        if isinstance(result, dict) and any(k in result for k in ("error", "exception", "errorcode")):
            code = result.get("errorcode")
            raise probe.ProbeError(code if isinstance(code, str) and code in probe.ERROR_CODES else "remote_error")
        return response.status_code, result
