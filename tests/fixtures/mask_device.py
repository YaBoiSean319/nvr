"""An ONVIF camera with a Media2 service that accepts privacy masks, recording each mask it holds."""
from __future__ import annotations

import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ENVELOPE = ('<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:tds="http://www.onvif.org/ver10/device/wsdl" '
            'xmlns:tr2="http://www.onvif.org/ver20/media/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema"><s:Body>{}</s:Body></s:Envelope>')


class MaskDevice:
    def __init__(self, *, supports_masks: bool = True) -> None:
        self.masks: dict[str, list[tuple[float, float]]] = {}
        self.deleted: list[str] = []
        self.supports_masks = supports_masks
        device = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf-8")
                operation = re.search(r"<s:Body><(?:\w+:)?(\w+)", body).group(1)
                reply = device.answer(operation, body)
                status = 200 if reply is not None else 400
                data = ENVELOPE.format(reply or "").encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/soap+xml")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        origin = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.origin = origin
        self.url = f"{origin}/onvif/device_service"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def answer(self, operation: str, body: str) -> str | None:
        if operation == "GetServices":
            return ("<tds:GetServicesResponse><tds:Service><tds:Namespace>http://www.onvif.org/ver10/device/wsdl</tds:Namespace>"
                    f"<tds:XAddr>{self.url}</tds:XAddr></tds:Service><tds:Service><tds:Namespace>http://www.onvif.org/ver20/media/wsdl</tds:Namespace>"
                    f"<tds:XAddr>{self.origin}/onvif/media2</tds:XAddr></tds:Service></tds:GetServicesResponse>")
        if operation == "GetVideoSourceConfigurations":
            return '<tr2:GetVideoSourceConfigurationsResponse><tr2:Configurations token="source-1"/></tr2:GetVideoSourceConfigurationsResponse>'
        if operation == "GetMaskOptions":
            return "<tr2:GetMaskOptionsResponse><tr2:Options><tt:MaxMasks>8</tt:MaxMasks></tr2:Options></tr2:GetMaskOptionsResponse>" if self.supports_masks else None
        if operation == "DeleteMask":
            token = re.search(r"<(?:\w+:)?Token>([^<]*)<", body).group(1)
            self.masks.pop(token, None)
            self.deleted.append(token)
            return "<tr2:DeleteMaskResponse/>"
        if operation == "CreateMask":
            token = f"mask-{len(self.masks) + len(self.deleted) + 1}"
            self.masks[token] = [(float(x), float(y)) for x, y in re.findall(r'<tt:Point x="([^"]+)" y="([^"]+)"/>', body)]
            return f"<tr2:CreateMaskResponse><tr2:Token>{token}</tr2:Token></tr2:CreateMaskResponse>"
        return None

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
