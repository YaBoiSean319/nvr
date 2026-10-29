"""A release feed for the guided-update tests (#148): serves GitHub-style releases JSON on this machine and counts the requests it gets."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def release(tag: str, body: str = "", *, draft: bool = False, prerelease: bool = False, schema: int | None = None) -> dict:
    marker = f"\n<!-- nvr-release: {json.dumps({'schema_version': schema})} -->" if schema is not None else ""
    return {"tag_name": tag, "name": tag, "body": body + marker, "draft": draft, "prerelease": prerelease,
            "html_url": f"https://github.com/yaboisean319/nvr/releases/tag/{tag}", "published_at": "2026-10-20T12:00:00Z"}


class ReleaseFeed:
    def __init__(self, releases: list[dict] | None = None, *, status: int = 200) -> None:
        self.releases, self.status = releases if releases is not None else [], status
        self.requests = 0
        feed = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                feed.requests += 1
                body = json.dumps(feed.releases).encode()
                self.send_response(feed.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/releases"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
