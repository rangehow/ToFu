"""Owner-bound TLS HTTP. Credentials stay in headers and transient memory."""
from __future__ import annotations
import json
import ssl
import urllib.request
import urllib.error
from urllib.parse import urlencode
from tofu_environments import EnvironmentError

class PlatformTransport:
    def __init__(self, token_source, *, timeout=30, cancelled=lambda: False):
        self.token_source, self.timeout, self.cancelled = token_source, timeout, cancelled
        self.origin = "https://mlp.sankuai.com"
    def request(self, actor, project, method, path, payload=None, params=None):
        if self.cancelled(): raise EnvironmentError("cancelled", "Cancelled before platform dispatch")
        token = self.token_source(actor, "com.sankuai.data.ml.platform")
        if not isinstance(token, str) or not token or any(c in token for c in "\r\n"):
            raise EnvironmentError("auth_required", "Owned MLP authentication is required")
        url = self.origin + path + ("?" + urlencode(params) if params else "")
        body = json.dumps(payload, allow_nan=False).encode() if payload is not None else None
        headers = {"access-token": token, "Content-Type": "application/json",
                   "Cookie": "ml.projectName=" + project + "; com.sankuai.data.ml.platform_ssoid=" + token,
                   "AIFree-WXProject": project}
        # Never follow a redirect while carrying the service ticket.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs): return None
        opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))
        try:
            with opener.open(urllib.request.Request(url, data=body, headers=headers, method=method), timeout=self.timeout) as response:
                raw = response.read(1048577)
            if len(raw) > 1048576: raise EnvironmentError("response_limit", "Platform response exceeds bound", uncertain=method != "GET")
            result = json.loads(raw)
            if not isinstance(result, dict) or result.get("code") != 0:
                raise EnvironmentError("platform_rejected", "Platform rejected the operation", uncertain=method != "GET")
            return result.get("data")
        except EnvironmentError: raise
        except urllib.error.HTTPError as exc:
            code = "auth_required" if exc.code in (401, 403) else "platform_http_error"
            raise EnvironmentError(code, "Platform request failed", uncertain=method != "GET" and exc.code not in (401, 403)) from exc
        except Exception as exc:
            raise EnvironmentError("platform_transport", "Platform response unavailable", uncertain=method != "GET") from exc
