from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
import struct
import sys
from threading import Event, Thread

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

class FixtureOnvifHandler(BaseHTTPRequestHandler):
    server_version = "NvrBrowserFixture/1.0"

    def do_POST(self) -> None:  # noqa: N802
        size = int(self.headers.get("Content-Length", "0"))
        request = self.rfile.read(size).decode("utf-8", errors="replace")
        if "GetSystemDateAndTime" in request and self.path == "/onvif/device_service":
            now = datetime.now(timezone.utc)
            body = (
                '<tds:GetSystemDateAndTimeResponse><tds:SystemDateAndTime><tt:UTCDateTime>'
                f'<tt:Time><tt:Hour>{now.hour}</tt:Hour><tt:Minute>{now.minute}</tt:Minute><tt:Second>{now.second}</tt:Second></tt:Time>'
                f'<tt:Date><tt:Year>{now.year}</tt:Year><tt:Month>{now.month}</tt:Month><tt:Day>{now.day}</tt:Day></tt:Date>'
                '</tt:UTCDateTime></tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse>'
            )
        elif "GetDeviceInformation" in request and self.path == "/onvif/device_service":
            body = (
                '<tds:GetDeviceInformationResponse><tds:Manufacturer>Fixture</tds:Manufacturer><tds:Model>Browser Camera</tds:Model>'
                '<tds:FirmwareVersion>1.0</tds:FirmwareVersion><tds:SerialNumber>FIXTURE</tds:SerialNumber><tds:HardwareId>fixture</tds:HardwareId></tds:GetDeviceInformationResponse>'
            )
        elif "GetCapabilities" in request and self.path == "/onvif/device_service":
            body = (
                '<tds:GetCapabilitiesResponse><tds:Capabilities>'
                f'<tt:Media><tt:XAddr>http://127.0.0.1:{self.server.server_port}/onvif/media_service</tt:XAddr></tt:Media>'
                '</tds:Capabilities></tds:GetCapabilitiesResponse>'
            )
        elif "GetProfiles" in request and self.path == "/onvif/media_service":
            body = (
                '<trt:GetProfilesResponse>'
                '<trt:Profiles token="main"><tt:Name>Main stream</tt:Name>'
                '<tt:VideoEncoderConfiguration token="main-encoder"><tt:Name>encoder</tt:Name><tt:Encoding>H264</tt:Encoding>'
                '<tt:Resolution><tt:Width>1280</tt:Width><tt:Height>720</tt:Height></tt:Resolution></tt:VideoEncoderConfiguration></trt:Profiles>'
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


class FixtureDiscoveryResponder:
    """Answer real WS-Discovery probes on the isolated loopback test interface."""

    GROUP = "239.255.255.250"
    PORT = 3702
    DEVICE_UUID = "e319da0d-a198-4a96-a243-0e25775ae7d1"

    @staticmethod
    def accepts_datagram(request: bytes, source: tuple[str, int]) -> bool:
        return source[0] == "127.0.0.1" and b"NetworkVideoTransmitter" in request

    def __init__(self) -> None:
        self._stopped = Event()
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("0.0.0.0", self.PORT))
        membership = struct.pack("=4s4s", socket.inet_aton(self.GROUP), socket.inet_aton("127.0.0.1"))
        self._socket.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
        self._socket.settimeout(0.25)

    @staticmethod
    def _message_id(request: bytes) -> str:
        try:
            import xml.etree.ElementTree as ET

            root = ET.fromstring(request)
            for node in root.iter():
                if node.tag.rsplit("}", 1)[-1] == "MessageID" and node.text:
                    return node.text.strip()
        except ET.ParseError:
            pass
        return "urn:uuid:00000000-0000-0000-0000-000000000000"

    def serve_forever(self) -> None:
        while not self._stopped.is_set():
            try:
                request, address = self._socket.recvfrom(64 * 1024)
            except socket.timeout:
                continue
            except OSError:
                return
            if not self.accepts_datagram(request, address):
                continue
            response = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" '
                'xmlns:a="http://www.w3.org/2005/08/addressing" '
                'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery">'
                '<e:Header><a:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/ProbeMatches</a:Action>'
                f'<a:RelatesTo>{self._message_id(request)}</a:RelatesTo></e:Header>'
                '<e:Body><d:ProbeMatches><d:ProbeMatch>'
                f'<a:EndpointReference><a:Address>urn:uuid:{self.DEVICE_UUID}</a:Address></a:EndpointReference>'
                '<d:Types>dn:NetworkVideoTransmitter</d:Types>'
                '<d:Scopes>onvif://www.onvif.org/name/DeterministicFixtureCamera</d:Scopes>'
                '<d:XAddrs>http://127.0.0.1:8766/onvif/device_service</d:XAddrs>'
                '<d:MetadataVersion>1</d:MetadataVersion>'
                '</d:ProbeMatch></d:ProbeMatches></e:Body></e:Envelope>'
            ).encode("utf-8")
            try:
                self._socket.sendto(response, address)
            except OSError:
                return

    def close(self) -> None:
        self._stopped.set()
        self._socket.close()


def main() -> None:
    os.environ.setdefault("NVR_DISCOVERY_INTERFACE", "127.0.0.1")
    onvif = ThreadingHTTPServer(("127.0.0.1", 8766), FixtureOnvifHandler)
    onvif_thread = Thread(target=onvif.serve_forever, name="browser-fixture-onvif", daemon=True)
    onvif_thread.start()
    discovery = FixtureDiscoveryResponder()
    discovery_thread = Thread(target=discovery.serve_forever, name="browser-fixture-discovery", daemon=True)
    discovery_thread.start()
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
        discovery.close()
        discovery_thread.join(timeout=1)
        onvif.shutdown()
        onvif.server_close()


if __name__ == "__main__":
    main()
