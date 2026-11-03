"""A stand-in for Microsoft's sign-in service and Graph ``sendMail`` (#231). Like the real ones it refuses a token it did not issue or that has expired, rotates the refresh
token on every refresh, and only accepts the device code it handed out. It keeps every message it was given, and can be told to fail or to expire things."""
from __future__ import annotations

import base64
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


class MicrosoftStub:
    def __init__(self, tenant: str = "contoso.onmicrosoft.com", client_id: str = "11111111-2222-3333-4444-555555555555", account: str = "alerts@contoso.com") -> None:
        self.tenant, self.client_id, self.account = tenant, client_id, account
        self.device_code = f"device-{uuid.uuid4()}"
        self.user_code = "ABCD-EFGH"
        self.pending_polls = 0  # Answer "authorization_pending" this many times before the sign-in succeeds
        self.expires_in = 3600  # Lifetime of each access token issued
        self.interval = 5
        self.access_tokens: dict[str, float] = {}
        self.refresh_token = ""
        self.revoked = False  # The next refresh answers invalid_grant
        self.send_status: list[int] = []  # Statuses to answer sendMail with, once each, before accepting
        self.messages: list[dict] = []
        self.sent_paths: list[str] = []
        self.requests: list[tuple[str, str]] = []
        self.token_requests: list[dict[str, str]] = []
        self.lock = threading.Lock()
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args) -> None:
                return

            def _json(self, status: int, body: dict | None = None) -> None:
                data = json.dumps(body or {}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                path = urlsplit(self.path).path
                with stub.lock:
                    stub.requests.append(("POST", path))
                    if path.endswith("/oauth2/v2.0/devicecode"):
                        form = {key: values[0] for key, values in parse_qs(raw.decode()).items()}
                        if form.get("client_id") != stub.client_id or path != f"/{stub.tenant}/oauth2/v2.0/devicecode":
                            return self._json(400, {"error": "invalid_request"})
                        if "Mail.Send" not in form.get("scope", ""):
                            return self._json(400, {"error": "invalid_scope"})
                        return self._json(200, {"device_code": stub.device_code, "user_code": stub.user_code, "verification_uri": "https://microsoft.com/devicelogin",
                                                "expires_in": 900, "interval": stub.interval})
                    if path.endswith("/oauth2/v2.0/token"):
                        form = {key: values[0] for key, values in parse_qs(raw.decode()).items()}
                        stub.token_requests.append(form)
                        return self._token(form)
                    if path.endswith("/sendMail"):
                        return self._send(path, raw)
                self._json(404)

            def _issue(self) -> dict:
                token = f"access-{uuid.uuid4()}"
                stub.access_tokens[token] = time.time() + stub.expires_in
                stub.refresh_token = f"refresh-{uuid.uuid4()}"
                claims = base64.urlsafe_b64encode(json.dumps({"preferred_username": stub.account}).encode()).decode().rstrip("=")
                return {"access_token": token, "refresh_token": stub.refresh_token, "expires_in": stub.expires_in, "id_token": f"x.{claims}.y", "token_type": "Bearer"}

            def _token(self, form: dict[str, str]) -> None:
                if form.get("client_id") != stub.client_id:
                    return self._json(400, {"error": "invalid_client"})
                if form.get("grant_type") == "urn:ietf:params:oauth:grant-type:device_code":
                    if form.get("device_code") != stub.device_code:
                        return self._json(400, {"error": "bad_verification_code"})
                    if stub.pending_polls > 0:
                        stub.pending_polls -= 1
                        return self._json(400, {"error": "authorization_pending"})
                    return self._json(200, self._issue())
                if form.get("grant_type") == "refresh_token":
                    if stub.revoked or form.get("refresh_token") != stub.refresh_token:
                        return self._json(400, {"error": "invalid_grant", "error_description": "AADSTS70008: expired"})
                    return self._json(200, self._issue())
                self._json(400, {"error": "unsupported_grant_type"})

            def _send(self, path: str, raw: bytes) -> None:
                token = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
                if stub.access_tokens.get(token, 0) <= time.time():
                    return self._json(401, {"error": {"code": "InvalidAuthenticationToken"}})
                if stub.send_status:
                    return self._json(stub.send_status.pop(0), {"error": {"code": "Failure"}})
                stub.sent_paths.append(path)
                stub.messages.append(json.loads(raw))
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def start(self) -> "MicrosoftStub":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
