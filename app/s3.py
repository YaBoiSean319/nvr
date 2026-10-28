"""A small S3-compatible object store client (AWS S3, MinIO, Ceph, Backblaze B2, Wasabi...) for the cold storage tier.

Only what tiering needs: put a file, read an object (whole or a range), ask about one, delete one. Requests are signed with AWS Signature Version 4, the payload hash of an
upload is the real SHA-256 of the file (so the store itself refuses a body that does not match), and addressing is path style (``endpoint/bucket/key``), which every
S3-compatible store accepts. No other library is needed.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import quote, urlsplit

import httpx

ALGORITHM = "AWS4-HMAC-SHA256"
CHUNK = 1024 * 1024
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
TIMEOUT = httpx.Timeout(60.0, connect=5.0)


class S3Error(Exception):
    """The store refused, or could not be reached; ``status`` is the HTTP status when there was an answer."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def signing_key(secret_key: str, date: str, region: str, service: str) -> bytes:
    return _hmac(_hmac(_hmac(_hmac(("AWS4" + secret_key).encode("utf-8"), date), region), service), "aws4_request")


def canonical_uri(path: str) -> str:
    """The path with each segment percent-encoded once (what S3 signs), keeping the slashes."""
    return quote(path or "/", safe="/-_.~")


def canonical_query(pairs: list[tuple[str, str]]) -> str:
    return "&".join(f"{quote(name, safe='-_.~')}={quote(value, safe='-_.~')}" for name, value in sorted(pairs))


def authorization(method: str, path: str, query: list[tuple[str, str]], headers: dict[str, str], payload_hash: str, *, access_key: str, secret_key: str, region: str,
                  service: str, amz_date: str) -> str:
    """The Authorization header for a request whose headers (including host and x-amz-date) are given."""
    names = sorted(name.lower() for name in headers)
    lowered = {name.lower(): " ".join(value.split()) for name, value in headers.items()}
    canonical = "\n".join([method.upper(), canonical_uri(path), canonical_query(query), "".join(f"{name}:{lowered[name]}\n" for name in names), ";".join(names), payload_hash])
    date = amz_date[:8]
    scope = f"{date}/{region}/{service}/aws4_request"
    to_sign = "\n".join([ALGORITHM, amz_date, scope, hashlib.sha256(canonical.encode("utf-8")).hexdigest()])
    signature = hmac.new(signing_key(secret_key, date, region, service), to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{ALGORITHM} Credential={access_key}/{scope}, SignedHeaders={';'.join(names)}, Signature={signature}"


class S3Client:
    def __init__(self, endpoint: str, bucket: str, access_key: str, secret_key: str, *, region: str = "us-east-1", prefix: str = "", timeout: httpx.Timeout = TIMEOUT,
                 transport: httpx.BaseTransport | None = None) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.bucket, self.region, self.prefix = bucket, region, prefix.strip("/")
        self.access_key, self.secret_key = access_key, secret_key
        self.http = httpx.Client(timeout=timeout, follow_redirects=False, transport=transport)
        parts = urlsplit(self.endpoint)
        self.host, self.origin, self.base_path = parts.netloc, f"{parts.scheme}://{parts.netloc}", parts.path.rstrip("/")

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> "S3Client":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def key(self, name: str) -> str:
        return f"{self.prefix}/{name}" if self.prefix else name

    def _request(self, method: str, name: str, *, payload_hash: str = EMPTY_SHA256, headers: dict[str, str] | None = None, content: Any = None, stream: bool = False,
                 query: list[tuple[str, str]] | None = None) -> httpx.Response:
        path = f"{self.base_path}/{self.bucket}" + (f"/{self.key(name)}" if name else "")
        amz_date = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        signed = {"host": self.host, "x-amz-content-sha256": payload_hash, "x-amz-date": amz_date}
        auth = authorization(method, path, query or [], signed, payload_hash, access_key=self.access_key, secret_key=self.secret_key, region=self.region, service="s3", amz_date=amz_date)
        url = f"{self.origin}{canonical_uri(path)}" + (f"?{canonical_query(query)}" if query else "")
        request = self.http.build_request(method, url, headers={**signed, "Authorization": auth, **(headers or {})}, content=content)
        try:
            return self.http.send(request, stream=stream)
        except httpx.HTTPError as exc:
            raise S3Error(f"The object store could not be reached: {type(exc).__name__}") from None

    @staticmethod
    def _check(response: httpx.Response, *accepted: int) -> None:
        if response.status_code not in accepted:
            detail = ""
            try:
                detail = response.read().decode("utf-8", "replace")[:200]
            except httpx.HTTPError:
                pass
            raise S3Error(f"The object store answered {response.status_code}" + (f": {detail}" if detail else ""), response.status_code)

    # --- operations -----------------------------------------------------------------------------------------------------------------------------

    def put_file(self, name: str, path: Path, sha256: str, size: int, *, rate: Callable[[int], None] | None = None) -> None:
        """Upload a file. The signed payload hash is the file's SHA-256, so the store rejects a body that is not exactly this file."""
        def body() -> Iterator[bytes]:
            with path.open("rb") as handle:
                while chunk := handle.read(CHUNK):
                    if rate:
                        rate(len(chunk))
                    yield chunk

        response = self._request("PUT", name, payload_hash=sha256, headers={"Content-Length": str(size), "Content-Type": "application/octet-stream"}, content=body())
        try:
            self._check(response, 200)
        finally:
            response.close()

    def head(self, name: str) -> int | None:
        """The object's size, or None when there is no such object."""
        response = self._request("HEAD", name)
        try:
            if response.status_code == 404:
                return None
            self._check(response, 200)
            return int(response.headers.get("content-length", "0"))
        finally:
            response.close()

    def download(self, name: str, target: Path, *, rate: Callable[[int], None] | None = None) -> tuple[str, int]:
        """Read the object into a file; returns its SHA-256 and size."""
        response = self._request("GET", name, stream=True)
        try:
            self._check(response, 200)
            digest, size = hashlib.sha256(), 0
            with target.open("wb") as handle:
                for chunk in response.iter_bytes(CHUNK):
                    if rate:
                        rate(len(chunk))
                    digest.update(chunk)
                    size += len(chunk)
                    handle.write(chunk)
                handle.flush()
            return digest.hexdigest(), size
        finally:
            response.close()

    def read_digest(self, name: str, *, rate: Callable[[int], None] | None = None) -> tuple[str, int]:
        """Read the object through and return its SHA-256 and size, keeping nothing."""
        response = self._request("GET", name, stream=True)
        try:
            self._check(response, 200)
            digest, size = hashlib.sha256(), 0
            for chunk in response.iter_bytes(CHUNK):
                if rate:
                    rate(len(chunk))
                digest.update(chunk)
                size += len(chunk)
            return digest.hexdigest(), size
        finally:
            response.close()

    def delete(self, name: str) -> None:
        response = self._request("DELETE", name)
        try:
            self._check(response, 200, 204, 404)  # Already gone is gone
        finally:
            response.close()

    def check(self) -> None:
        """Prove the bucket can be written, read and emptied with these credentials, with a small object of its own."""
        probe = f".nvr-check-{int(time.time() * 1000)}"
        data = b"nvr cold tier check"
        digest = hashlib.sha256(data).hexdigest()
        response = self._request("PUT", probe, payload_hash=digest, headers={"Content-Length": str(len(data))}, content=data)
        try:
            self._check(response, 200)
        finally:
            response.close()
        try:
            if self.head(probe) != len(data):
                raise S3Error("The object store did not keep what was written to it")
        finally:
            self.delete(probe)
