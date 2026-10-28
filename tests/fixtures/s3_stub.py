"""A small S3-compatible server for tests (path style: PUT, GET, HEAD, DELETE on ``/bucket/key``) that, like a real store, refuses a request whose AWS Signature Version 4 does not
check out and an upload whose body does not hash to the ``x-amz-content-sha256`` it was signed with. It keeps objects in memory and can be told to fail or to answer wrongly.

Its signature check uses ``app.s3.authorization``, which the tests prove against the examples AWS publishes, so a client that signs wrongly is refused here as it would be by S3.
"""
from __future__ import annotations

import hashlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, unquote, urlsplit

from app import s3


class S3Stub:
    def __init__(self, bucket: str = "footage", access_key: str = "TESTKEYID", secret_key: str = "test-secret-key-0123456789") -> None:
        self.bucket, self.access_key, self.secret_key = bucket, access_key, secret_key
        self.objects: dict[str, bytes] = {}
        self.requests: list[tuple[str, str, int]] = []  # method, key, status
        self.fail: dict[str, int] = {}  # method -> status to answer once, instead of doing it
        self.corrupt_gets = False
        self.lock = threading.Lock()
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args) -> None:  # noqa: D401 - silence
                return

            def _answer(self, status: int, body: bytes = b"", headers: dict[str, str] | None = None, key: str = "") -> None:
                with stub.lock:
                    stub.requests.append((self.command, key, status))
                self.send_response(status)
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)) if self.command != "HEAD" or "Content-Length" not in (headers or {}) else headers["Content-Length"])
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _authorised(self, body_hash: str) -> bool:
                header = self.headers.get("Authorization", "")
                try:
                    credential = header.split("Credential=")[1].split(",")[0]
                    names = header.split("SignedHeaders=")[1].split(",")[0].split(";")
                    given = header.split("Signature=")[1].strip()
                except IndexError:
                    return False
                access_key, date, region, service, _ = credential.split("/")
                if access_key != stub.access_key or service != "s3":
                    return False
                parts = urlsplit(self.path)
                headers = {name: self.headers.get(name, "") for name in names}
                expected = s3.authorization(self.command, unquote(parts.path), parse_qsl(parts.query), headers, body_hash, access_key=stub.access_key, secret_key=stub.secret_key,
                                            region=region, service="s3", amz_date=self.headers.get("x-amz-date", ""))
                return expected.endswith("Signature=" + given) and given != "" and body_hash == self.headers.get("x-amz-content-sha256")

            def _key(self) -> str | None:
                path = unquote(urlsplit(self.path).path).lstrip("/")
                bucket, _, key = path.partition("/")
                return key if bucket == stub.bucket else None

            def _handle(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length) if length else b""
                key = self._key()
                declared = self.headers.get("x-amz-content-sha256", "")
                if key is None:
                    return self._answer(404, b"NoSuchBucket")
                if not self._authorised(declared):
                    return self._answer(403, b"SignatureDoesNotMatch", key=key)
                if self.command == "PUT" and hashlib.sha256(body).hexdigest() != declared:
                    return self._answer(400, b"XAmzContentSHA256Mismatch", key=key)
                forced = stub.fail.pop(self.command, None)
                if forced is not None:
                    return self._answer(forced, b"InjectedFailure", key=key)
                with stub.lock:
                    stored = stub.objects.get(key)
                if self.command == "PUT":
                    with stub.lock:
                        stub.objects[key] = body
                    return self._answer(200, key=key)
                if self.command == "DELETE":
                    with stub.lock:
                        stub.objects.pop(key, None)
                    return self._answer(204, key=key)
                if stored is None:
                    return self._answer(404, b"NoSuchKey", key=key)
                if self.command == "HEAD":
                    return self._answer(200, headers={"Content-Length": str(len(stored))}, key=key)
                data = (b"X" + stored[1:]) if stub.corrupt_gets and stored else stored
                return self._answer(200, data, key=key)

            do_PUT = do_GET = do_HEAD = do_DELETE = _handle

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def start(self) -> "S3Stub":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
