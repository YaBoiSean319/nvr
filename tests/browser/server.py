from __future__ import annotations

import os
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import sys
from threading import Thread

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tests.browser.seed_fixtures import seed_fixtures  # noqa: E402


class FixtureOnvifHandler(BaseHTTPRequestHandler):
    server_version = "NvrBrowserFixture/1.0"

    def do_POST(self) -> None:  # noqa: N802
        size = int(self.headers.get("Content-Length", "0"))
        request = self.rfile.read(size).decode("utf-8", errors="replace")
        if "GetCapabilities" in request and self.path == "/onvif/device_service":
            body = (
                '<tds:GetCapabilitiesResponse><tds:Capabilities>'
                f'<tt:Media><tt:XAddr>http://127.0.0.1:{self.server.server_port}/onvif/media_service</tt:XAddr></tt:Media>'
                '</tds:Capabilities></tds:GetCapabilitiesResponse>'
            )
        elif "GetProfiles" in request and self.path == "/onvif/media_service":
            body = (
                '<trt:GetProfilesResponse>'
                '<trt:Profiles token="main"><tt:Name>Main stream</tt:Name></trt:Profiles>'
                '</trt:GetProfilesResponse>'
            )
        elif "GetStreamUri" in request and self.path == "/onvif/media_service":
            body = (
                '<trt:GetStreamUriResponse><trt:MediaUri>'
                '<tt:Uri>rtsp://127.0.0.1:8554/nvr-test</tt:Uri>'
                '</trt:MediaUri></trt:GetStreamUriResponse>'
            )
        else:
            self.send_error(404)
            return

        response = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
            'xmlns:tds="http://www.onvif.org/ver10/device/wsdl" '
            'xmlns:trt="http://www.onvif.org/ver10/media/wsdl" '
            'xmlns:tt="http://www.onvif.org/ver10/schema">'
            f'<s:Body>{body}</s:Body></s:Envelope>'
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/soap+xml; charset=utf-8")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def main() -> None:
    os.environ.setdefault("NVR_BROWSER_FIXTURE_MANIFEST", str(Path(os.environ["NVR_DB_PATH"]).with_name("fixtures.json")))
    seed_fixtures(0)
    onvif = ThreadingHTTPServer(("127.0.0.1", 8766), FixtureOnvifHandler)
    onvif_thread = Thread(target=onvif.serve_forever, name="browser-fixture-onvif", daemon=True)
    onvif_thread.start()
    import uvicorn

    try:
        uvicorn.run(
            "app.main:app",
            host=os.environ.get("NVR_BROWSER_HOST", "127.0.0.1"),
            port=int(os.environ.get("NVR_BROWSER_PORT", "8765")),
            log_level="warning",
            access_log=False,
        )
    finally:
        onvif.shutdown()
        onvif.server_close()


if __name__ == "__main__":
    main()
