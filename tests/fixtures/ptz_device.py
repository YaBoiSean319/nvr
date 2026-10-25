"""An ONVIF camera with a PTZ service, recording each PTZ command it receives."""
from __future__ import annotations

import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ENVELOPE = ('<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:tds="http://www.onvif.org/ver10/device/wsdl" '
            'xmlns:trt="http://www.onvif.org/ver10/media/wsdl" xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl" '
            'xmlns:tt="http://www.onvif.org/ver10/schema"><s:Body>{}</s:Body></s:Envelope>')


def _value(body: str, tag: str) -> str | None:
    match = re.search(rf"<(?:\w+:)?{tag}>([^<]*)</(?:\w+:)?{tag}>", body)
    return match.group(1) if match else None


def _attributes(body: str, tag: str) -> dict[str, str] | None:
    match = re.search(rf"<(?:\w+:)?{tag} ([^>]*)/>", body)
    return dict(re.findall(r'(\w+)="([^"]*)"', match.group(1))) if match else None


class PtzDevice:
    def __init__(self, *, fov: bool = True) -> None:
        self.commands: list[dict] = []
        self.presets: dict[str, str] = {}
        self.fov = fov
        device = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf-8")
                operation = re.search(r"<s:Body><(?:\w+:)?(\w+)", body).group(1)
                reply = device.answer(operation, body)
                if reply is None:
                    self.send_response(400)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                data = ENVELOPE.format(reply).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/soap+xml")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.origin = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.url = f"{self.origin}/onvif/device_service"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def answer(self, operation: str, body: str) -> str | None:
        if operation == "GetCapabilities":
            return (f"<tds:GetCapabilitiesResponse><tds:Capabilities><tt:Media><tt:XAddr>{self.origin}/onvif/media</tt:XAddr></tt:Media>"
                    f"<tt:PTZ><tt:XAddr>{self.origin}/onvif/ptz</tt:XAddr></tt:PTZ></tds:Capabilities></tds:GetCapabilitiesResponse>")
        if operation == "GetProfiles":
            return '<trt:GetProfilesResponse><trt:Profiles token="profile-main"><tt:Name>Main</tt:Name></trt:Profiles></trt:GetProfilesResponse>'
        if operation == "GetNodes":
            fov = (f"<tt:RelativePanTiltTranslationSpace><tt:URI>http://www.onvif.org/ver10/tptz/PanTiltSpaces/TranslationSpaceFov</tt:URI>"
                   "</tt:RelativePanTiltTranslationSpace>") if self.fov else ""
            return ("<tptz:GetNodesResponse><tptz:PTZNode token=\"node-1\"><tt:SupportedPTZSpaces>"
                    "<tt:ContinuousPanTiltVelocitySpace><tt:URI>http://www.onvif.org/ver10/tptz/PanTiltSpaces/VelocityGenericSpace</tt:URI></tt:ContinuousPanTiltVelocitySpace>"
                    "<tt:ContinuousZoomVelocitySpace><tt:URI>http://www.onvif.org/ver10/tptz/ZoomSpaces/VelocityGenericSpace</tt:URI></tt:ContinuousZoomVelocitySpace>"
                    f"{fov}</tt:SupportedPTZSpaces><tt:MaximumNumberOfPresets>64</tt:MaximumNumberOfPresets></tptz:PTZNode></tptz:GetNodesResponse>")
        profile = _value(body, "ProfileToken")
        if operation == "ContinuousMove":
            self.commands.append({"op": "ContinuousMove", "profile": profile, "pan_tilt": _attributes(body, "PanTilt"),
                                  "zoom": _attributes(body, "Zoom"), "timeout": _value(body, "Timeout")})
            return "<tptz:ContinuousMoveResponse/>"
        if operation == "Stop":
            self.commands.append({"op": "Stop", "profile": profile})
            return "<tptz:StopResponse/>"
        if operation == "RelativeMove":
            self.commands.append({"op": "RelativeMove", "profile": profile, "pan_tilt": _attributes(body, "PanTilt")})
            return "<tptz:RelativeMoveResponse/>"
        if operation == "SetPreset":
            token = _value(body, "PresetToken") or f"preset-{len(self.presets) + 1}"
            self.presets[token] = _value(body, "PresetName") or ""
            self.commands.append({"op": "SetPreset", "profile": profile, "name": self.presets[token], "token": token})
            return f"<tptz:SetPresetResponse><tptz:PresetToken>{token}</tptz:PresetToken></tptz:SetPresetResponse>"
        if operation == "GotoPreset":
            self.commands.append({"op": "GotoPreset", "profile": profile, "token": _value(body, "PresetToken")})
            return "<tptz:GotoPresetResponse/>"
        if operation == "RemovePreset":
            token = _value(body, "PresetToken")
            self.presets.pop(token or "", None)
            self.commands.append({"op": "RemovePreset", "profile": profile, "token": token})
            return "<tptz:RemovePresetResponse/>"
        return None

    def ops(self) -> list[str]:
        return [command["op"] for command in self.commands]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
