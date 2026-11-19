"""Two checks made on every request before it reaches a route: how large its body may be, and where a request that changes something came from.

Both are plain ASGI middleware, so a body is counted as it arrives and never held by the check itself.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import network

DEFAULT_MAX_BODY_BYTES = 8 * 1024 * 1024  # More than the largest ordinary request (a branding picture is under 3 MiB as sent)
# The two routes that take an evidence package to check apply their own, much larger limit as they read.
_OWN_LIMIT = re.compile(r"^/api/(?:exports/[^/]+/verify|cases/[^/]+/packages/[^/]+/verify)$")
_CHANGING = {"POST", "PUT", "PATCH", "DELETE"}
# Called by other systems with their own signatures, never by a browser page of this NVR.
_MACHINE_PREFIXES = ("/api/inbound/",)


def max_body_bytes() -> int:
    try:
        return max(64 * 1024, int(os.environ.get("NVR_MAX_REQUEST_BYTES", DEFAULT_MAX_BODY_BYTES)))
    except ValueError:
        return DEFAULT_MAX_BODY_BYTES


async def _answer(send: Send, status: int, detail: str) -> None:
    body = json.dumps({"detail": detail}).encode("utf-8")
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode("ascii")), (b"cache-control", b"no-store")]})
    await send({"type": "http.response.body", "body": body})


class RequestSizeLimitMiddleware:
    """Refuse a request whose body is larger than the limit, before any route (and so before sign-in) reads it.

    The framework reads and parses a whole JSON body before a route's sign-in check runs, so without this anyone who could reach the port could
    make the web process hold a body of any size (#369). A declared length over the limit is refused at once; a body sent without a length is
    cut off once it passes the limit, and the caller is answered 413 whatever the route would have said."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or _OWN_LIMIT.match(scope.get("path", "")):
            await self.app(scope, receive, send)
            return
        limit = max_body_bytes()
        declared = dict(scope.get("headers") or []).get(b"content-length")
        if declared and declared.isdigit() and int(declared) > limit:
            await _answer(send, 413, "The request is too large")
            return
        state: dict[str, Any] = {"received": 0, "refused": False, "answered": False}

        async def counted() -> Message:
            if state["refused"]:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                state["received"] += len(message.get("body") or b"")
                if state["received"] > limit:
                    state["refused"] = True
                    return {"type": "http.request", "body": b"", "more_body": False}  # The route gets no more; its answer is replaced below
            return message

        async def answered(message: Message) -> None:
            if not state["refused"]:
                await send(message)
            elif not state["answered"]:
                state["answered"] = True
                await _answer(send, 413, "The request is too large")

        await self.app(scope, counted, answered)


class SameOriginMiddleware:
    """Refuse a request that changes something and says it came from a page on another site.

    Browsers send ``Origin`` with such requests. The session cookie is not sent from another site, but a page on another host under the same
    parent domain counts as the same site, and could fire the routes that change something without a body (#370). The rule is the one the live
    video sockets already use: the origin must be the host the request was made to (as a trusted proxy reports it), ``NVR_PUBLIC_URL`` or one
    of ``NVR_ALLOWED_ORIGINS``. A request with no ``Origin`` (an API token, a script, the health check) is not affected."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("method") in _CHANGING and not scope.get("path", "").startswith(_MACHINE_PREFIXES):
            connection = HTTPConnection(scope)
            origin = connection.headers.get("origin")
            if origin is not None and not network.origin_allowed(connection, origin):
                await _answer(send, 403, "This request came from a page on another site. If the NVR is reached through a reverse proxy, set NVR_PUBLIC_URL "
                                         "(or NVR_ALLOWED_ORIGINS) to the address people use.")
                return
        await self.app(scope, receive, send)
