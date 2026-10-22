"""Private Jupyter bootstrap and service probes for the CodeLab service profile.

Derived origins stay under the provider domain; tokens never enter returned
connection data. Terminal lifetime is scoped to bootstrap and always closed.
"""
from __future__ import annotations
import json
import re
import ssl
import shlex
import time
import urllib.request
import urllib.error
from urllib.parse import quote
from tofu_environments import EnvironmentError

class WorkspaceRuntime:
    def __init__(self, platform, *, timeout=30, websocket_factory=None):
        self.platform, self.timeout, self.websocket_factory = platform, timeout, websocket_factory
    def _origins(self, detail):
        uid = str(detail.get("dockerUid", ""))
        cluster = str(detail.get("cluster", "")).split("_")[0]
        if not re.fullmatch(r"[a-z0-9-]{1,80}", uid) or not re.fullmatch(r"[a-z0-9]{1,32}", cluster):
            raise EnvironmentError("workspace_connection", "Verified platform connection identifiers are missing")
        return (f"https://{uid}-jupyter-{cluster}.mlp.sankuai.com",
                f"https://{uid}-vscode-{cluster}.mlp.sankuai.com")
    def _request(self, url, token, method="GET", payload=None, extra_headers=None):
        if self.platform.cancelled(): raise EnvironmentError("cancelled", "Workspace request cancelled")
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs): return None
        opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))
        headers = {"Authorization": "token " + token, "Content-Type": "application/json", **(extra_headers or {})}
        body = json.dumps(payload, allow_nan=False).encode() if payload is not None else None
        try:
            with opener.open(urllib.request.Request(url, data=body, method=method, headers=headers), timeout=self.timeout) as response:
                raw = response.read(65537)
            if len(raw) > 65536: raise ValueError("response limit")
            return json.loads(raw) if raw else {}
        except Exception as exc:
            raise EnvironmentError("workspace_transport", "Workspace response unavailable", uncertain=method != "GET") from exc
    def start(self, actor, spec, detail, operation_id):
        origin, _ = self._origins(detail)
        token = detail.get("token")
        if not isinstance(token, str) or not token or any(c in token for c in "\r\n"):
            raise EnvironmentError("workspace_auth_required", "Owned workspace token is unavailable")
        terminal = self._request(origin + "/api/terminals", token, "POST", {})
        name = terminal.get("name")
        if not isinstance(name, str) or not name or len(name) > 128:
            raise EnvironmentError("workspace_protocol", "Bootstrap terminal identifier missing", uncertain=True)
        socket = None
        try:
            factory = self.websocket_factory
            if factory is None:
                from websocket import create_connection
                factory = create_connection
            socket = factory(origin.replace("https://", "wss://", 1) + "/terminals/websocket/" + quote(name, safe=""),
                header=["Authorization: token " + token], origin=origin,
                sslopt={"cert_reqs": ssl.CERT_REQUIRED}, timeout=self.timeout)
            marker = "TOFU_ENV_SUBMITTED_" + operation_id
            command = "nohup " + shlex.join(spec["workspace"]["startup_command"]) + " </dev/null >/dev/null 2>&1 & echo " + marker + "\n"
            socket.send(json.dumps(["stdin", command]))
            deadline = time.monotonic() + self.timeout
            acknowledged = False
            output = ""
            while time.monotonic() < deadline:
                if self.platform.cancelled(): raise EnvironmentError("cancelled", "Bootstrap cancelled", uncertain=True)
                frame = json.loads(socket.recv())
                if isinstance(frame, list) and len(frame) == 2 and frame[0] == "stdout":
                    output = (output + str(frame[1]))[-65536:]
                    if any(line.strip() == marker for line in output.splitlines()):
                        acknowledged = True; break
            if not acknowledged: raise EnvironmentError("workspace_bootstrap_unknown", "Bootstrap acknowledgment missing", uncertain=True)
        except EnvironmentError: raise
        except Exception as exc:
            raise EnvironmentError("workspace_bootstrap_unknown", "Bootstrap outcome is unknown", uncertain=True) from exc
        finally:
            if socket is not None:
                try: socket.close()
                except Exception: pass
            try: self._request(origin + "/api/terminals/" + quote(name, safe=""), token, "DELETE")
            except EnvironmentError: pass
    def service_url(self, spec, detail):
        _, origin = self._origins(detail)
        return origin + "/proxy/" + str(spec["workspace"]["service_port"]) + "/"
    def ready(self, actor, spec, detail):
        token = detail.get("token")
        if not isinstance(token, str) or not token: return False
        service = self.service_url(spec, detail)
        path = spec["workspace"].get("readiness_path", "/api/ready")
        platform_token = self.platform.token_source(actor, "com.sankuai.data.ml.platform")
        try:
            result = self._request(service.rstrip("/") + path, token, extra_headers={
                "access-token": platform_token,
                "Cookie": "com.sankuai.data.ml.platform_ssoid=" + platform_token})
            return isinstance(result, dict) and (result.get("ready") is True or isinstance(result.get("data"), dict) and result["data"].get("ready") is True)
        except EnvironmentError: return False
