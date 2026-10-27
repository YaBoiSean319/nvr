"""A mock ONVIF camera for the onboarding tests: real HTTP on a loopback port, real Digest and WS-Security checks.

Each service (device, media, media2, events) can ask for its own authentication: ``none``, ``digest``, ``wsse`` (WS-Security
``UsernameToken`` with a ``PasswordDigest``) or ``digest+wsse``, as real cameras do (the Hanwha model asks Media for Digest and Events
for WS-Security). The Digest challenge is answered by the RFC 7616 maths, and a WS-Security token is accepted only when its nonce/created/
password digest is right and its ``Created`` is within five minutes of the camera's own clock, which can be set apart from the real one.

``requests`` records every request (service, operation, the authentication it carried) so tests can say what the client tried and in
what order. The SOAP is written the way ONVIF cameras write it; it is modelled on the structure of Hanwha Wisenet responses, not captured
from a device, and carries no real addresses or credentials.
"""
from __future__ import annotations

import base64
import hashlib
import html
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

NS = ('xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:tds="http://www.onvif.org/ver10/device/wsdl" xmlns:trt="http://www.onvif.org/ver10/media/wsdl" '
      'xmlns:tr2="http://www.onvif.org/ver20/media/wsdl" xmlns:tev="http://www.onvif.org/ver10/events/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema" '
      'xmlns:ter="http://www.onvif.org/ver10/error"')
REALM = "onvif-fixture"
WSSE_WINDOW = timedelta(minutes=5)
PLACEHOLDER_HOST = "192.0.2.77"  # What the camera believes its own address is: the NVR must keep to the address it was given.


def profile(token: str, name: str, encoding: str, width: int, height: int, fps: int = 30, bitrate: int = 4096, audio: bool = False, ptz: bool = False) -> dict:
    return {"token": token, "name": name, "encoding": encoding, "width": width, "height": height, "fps": fps, "bitrate": bitrate, "audio": audio, "ptz": ptz}


def hanwha_profiles() -> list[dict]:
    """The shape of the Hanwha XNV-6120R's profiles: MJPEG listed first (the camera's default), H.264 main and sub, and an H.265 profile."""
    return [profile("profile1", "mjpeg-1080p", "JPEG", 1920, 1080), profile("profile2", "h264-main", "H264", 1920, 1080, audio=True),
            profile("profile3", "h264-sub", "H264", 640, 360, 15, 1024), profile("profile4", "h265-main", "H265", 1920, 1080)]


_EVENTS = "http://www.onvif.org/ver10/events/wsdl/"
_WSN = "http://docs.oasis-open.org/wsn/bw-2/SubscriptionManager/"
EVENT_ACTIONS = {
    "CreatePullPointSubscription": _EVENTS + "EventPortType/CreatePullPointSubscriptionRequest",
    "GetEventProperties": _EVENTS + "EventPortType/GetEventPropertiesRequest",
    "GetServiceCapabilities": _EVENTS + "EventPortType/GetServiceCapabilitiesRequest",
    "PullMessages": _EVENTS + "PullPointSubscription/PullMessagesRequest",
    "Renew": _WSN + "RenewRequest",
    "Unsubscribe": _WSN + "UnsubscribeRequest",
}


def fault(code: str, subcode: str, reason: str) -> str:
    return (f'<s:Fault><s:Code><s:Value>{code}</s:Value><s:Subcode><s:Value>{subcode}</s:Value></s:Subcode></s:Code>'
            f'<s:Reason><s:Text xml:lang="en">{reason}</s:Text></s:Reason></s:Fault>')


class OnvifCamera:
    def __init__(self, *, username: str = "operator", password: str = "S3cret!pass", auth: dict[str, str] | None = None, skew: timedelta = timedelta(),
                 profiles: list[dict] | None = None, media2_profiles: list[dict] | None = None, manufacturer: str = "Hanwha Techwin", model: str = "XNV-6120R",
                 firmware: str = "2.00.00", get_services: bool = True, pull_point: bool = True, services: tuple[str, ...] = ("media", "events", "ptz"),
                 behaviours: dict[str, str] | None = None, subscription_style: str = "path", strict_actions: bool = False,
                 event_topics: tuple[str, ...] = ("RuleEngine/CellMotionDetector/Motion", "VideoSource/MotionAlarm", "VideoAnalytics/MotionDetection", "Device/Temperature")) -> None:
        self.username, self.password = username, password
        self.auth = {"device": "digest", "media": "digest", "media2": "digest", "events": "digest", **(auth or {})}
        self.skew = skew
        self.profiles = hanwha_profiles() if profiles is None else profiles
        self.media2_profiles = media2_profiles
        self.manufacturer, self.model, self.firmware = manufacturer, model, firmware
        self.get_services, self.pull_point = get_services, pull_point
        self.services = services + (("media2",) if media2_profiles is not None else ())
        self.behaviours = behaviours or {}  # "operation" or "service:operation" -> "fault" | "http500" | "html" | "hang" | "doctype"
        self.requests: list[dict] = []
        self.nonces: set[str] = set()
        self.pending: list[str] = []  # Notifications for the next PullMessages
        self.pending_lock = threading.Lock()
        self.strict_actions = strict_actions  # Refuse Events requests whose WS-Addressing action is not the one the ONVIF specification gives, as some real cameras do
        self.subscription_style, self.event_topics = subscription_style, event_topics  # style: "path", "query" (id in the address's query) or "reference" (id in reference parameters)
        self.subscription_alive, self.subscriptions, self.unsubscribed = False, 0, 0
        camera = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf-8")
                camera.handle(self, body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.server.block_on_close = False
        self.port = self.server.server_address[1]
        self.origin = f"http://127.0.0.1:{self.port}"
        self.url = f"{self.origin}/onvif/device_service"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    # ---------------------------------------------------------------------------------------------------------------- the camera's clock

    def now(self) -> datetime:
        return datetime.now(timezone.utc) + self.skew

    # ----------------------------------------------------------------------------------------------------------------------------- auth

    def _digest_ok(self, handler: BaseHTTPRequestHandler) -> bool:
        header = handler.headers.get("Authorization", "")
        if not header.startswith("Digest "):
            return False
        fields = dict(re.findall(r'(\w+)="?([^",]*)"?', header[7:]))
        if fields.get("username") != self.username or fields.get("realm") != REALM or fields.get("nonce") not in self.nonces:
            return False
        ha1 = hashlib.md5(f"{self.username}:{REALM}:{self.password}".encode()).hexdigest()  # noqa: S324
        ha2 = hashlib.md5(f"POST:{fields.get('uri', '')}".encode()).hexdigest()  # noqa: S324
        expected = hashlib.md5(f"{ha1}:{fields['nonce']}:{fields.get('nc', '')}:{fields.get('cnonce', '')}:{fields.get('qop', '')}:{ha2}".encode()).hexdigest()  # noqa: S324
        return fields.get("response") == expected

    def _wsse_ok(self, body: str) -> bool:
        def value(tag: str) -> str | None:
            match = re.search(rf"<(?:\w+:)?{tag}[^>]*>([^<]*)</(?:\w+:)?{tag}>", body)
            return match.group(1) if match else None
        user, digest, nonce, created = html.unescape(value("Username") or ""), value("Password"), value("Nonce"), value("Created")
        if not (user and digest and nonce and created) or user != self.username:
            return False
        try:
            stamp = datetime.strptime(created, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            expected = base64.b64encode(hashlib.sha1(base64.b64decode(nonce) + created.encode() + self.password.encode()).digest()).decode()  # noqa: S324
        except ValueError:
            return False
        return digest == expected and abs(self.now() - stamp) <= WSSE_WINDOW

    # ------------------------------------------------------------------------------------------------------------------------ requests

    def handle(self, handler: BaseHTTPRequestHandler, body: str) -> None:
        path = handler.path
        service = {"/onvif/device_service": "device", "/onvif/media": "media", "/onvif/media2": "media2", "/onvif/events": "events", "/onvif/events/subscription": "events",
                   "/onvif/ptz": "ptz"}.get(path.split("?")[0])
        match = re.search(r"<s:Body>\s*<(?:\w+:)?(\w+)", body)
        operation = match.group(1) if match else ""
        has_digest = handler.headers.get("Authorization", "").startswith("Digest ")
        has_wsse = "UsernameToken" in body
        carried = "digest+wsse" if has_digest and has_wsse else "digest" if has_digest else "wsse" if has_wsse else "none"
        record = {"service": service, "operation": operation, "carried": carried}
        self.requests.append(record)
        if service is None:
            return self.send(handler, 404, "")
        mode = self.auth.get(service, "none")
        if operation != "GetSystemDateAndTime" and mode != "none":
            if "digest" in mode and not self._digest_ok(handler):
                nonce = uuid.uuid4().hex
                self.nonces.add(nonce)
                return self.send(handler, 401, "", {"WWW-Authenticate": f'Digest realm="{REALM}", qop="auth", nonce="{nonce}", algorithm=MD5'})
            if "wsse" in mode and not self._wsse_ok(body):
                return self.send(handler, 400, self.envelope(fault("s:Sender", "ter:NotAuthorized", "Sender not authorized")))
        if self.strict_actions and service == "events":
            given = re.search(r"<(?:\w+:)?Action[^>]*>([^<]*)</", body)
            if (given.group(1) if given else "") != EVENT_ACTIONS.get(operation, given.group(1) if given else ""):
                return self.send(handler, 500, self.envelope(fault("s:Sender", "ter:ActionNotSupported", "The action is not supported")))
        behaviour = self.behaviours.get(f"{service}:{operation}") or self.behaviours.get(operation)
        if behaviour == "hang":
            time.sleep(3)
        if behaviour == "fault":
            return self.send(handler, 500, self.envelope(fault("s:Receiver", "ter:Action", "The camera is busy")))
        if behaviour == "http500":
            return self.send(handler, 500, "")
        if behaviour == "html":
            return self.send(handler, 200, "<html><body>camera web page</body></html>", {"Content-Type": "text/html"})
        if behaviour == "doctype":
            return self.send(handler, 200, '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body/></s:Envelope>')
        self.current = (path, body)
        reply = self.answer(service, operation, body)
        if reply is None:
            return self.send(handler, 500, self.envelope(fault("s:Sender", "ter:ActionNotSupported", "Optional action not implemented")))
        self.send(handler, 200, self.envelope(reply))

    @staticmethod
    def envelope(inner: str) -> str:
        return f'<?xml version="1.0" encoding="UTF-8"?><s:Envelope {NS}><s:Body>{inner}</s:Body></s:Envelope>'

    @staticmethod
    def send(handler: BaseHTTPRequestHandler, status: int, body: str, headers: dict[str, str] | None = None) -> None:
        data = body.encode()
        handler.send_response(status)
        handler.send_header("Content-Type", (headers or {}).get("Content-Type", "application/soap+xml; charset=utf-8"))
        for key, value in (headers or {}).items():
            if key != "Content-Type":
                handler.send_header(key, value)
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        try:
            handler.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass  # The client gave up (a cancelled long poll)

    # ----------------------------------------------------------------------------------------------------------------------- responses

    def answer(self, service: str, operation: str, body: str) -> str | None:
        if operation == "GetSystemDateAndTime":
            now = self.now()
            return (f"<tds:GetSystemDateAndTimeResponse><tds:SystemDateAndTime><tt:DateTimeType>NTP</tt:DateTimeType><tt:UTCDateTime><tt:Time><tt:Hour>{now.hour}</tt:Hour>"
                    f"<tt:Minute>{now.minute}</tt:Minute><tt:Second>{now.second}</tt:Second></tt:Time><tt:Date><tt:Year>{now.year}</tt:Year><tt:Month>{now.month}</tt:Month>"
                    f"<tt:Day>{now.day}</tt:Day></tt:Date></tt:UTCDateTime></tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse>")
        if operation == "GetDeviceInformation":
            return (f"<tds:GetDeviceInformationResponse><tds:Manufacturer>{self.manufacturer}</tds:Manufacturer><tds:Model>{self.model}</tds:Model>"
                    f"<tds:FirmwareVersion>{self.firmware}</tds:FirmwareVersion><tds:SerialNumber>FIXTURE-0001</tds:SerialNumber><tds:HardwareId>fixture</tds:HardwareId></tds:GetDeviceInformationResponse>")
        urls = {"media": f"http://{PLACEHOLDER_HOST}:{self.port}/onvif/media", "media2": f"http://{PLACEHOLDER_HOST}:{self.port}/onvif/media2",
                "events": f"http://{PLACEHOLDER_HOST}:{self.port}/onvif/events", "ptz": f"http://{PLACEHOLDER_HOST}:{self.port}/onvif/ptz"}
        if operation == "GetServices":
            if not self.get_services:
                return None
            spaces = {"media": "http://www.onvif.org/ver10/media/wsdl", "media2": "http://www.onvif.org/ver20/media/wsdl", "events": "http://www.onvif.org/ver10/events/wsdl",
                      "ptz": "http://www.onvif.org/ver20/ptz/wsdl"}
            items = [f"<tds:Service><tds:Namespace>http://www.onvif.org/ver10/device/wsdl</tds:Namespace><tds:XAddr>{self.origin}/onvif/device_service</tds:XAddr></tds:Service>"]
            items += [f"<tds:Service><tds:Namespace>{spaces[name]}</tds:Namespace><tds:XAddr>{urls[name]}</tds:XAddr></tds:Service>" for name in self.services]
            return f"<tds:GetServicesResponse>{''.join(items)}</tds:GetServicesResponse>"
        if operation == "GetCapabilities":
            tags = {"media": "Media", "events": "Events", "ptz": "PTZ"}
            parts = "".join(f"<tt:{tags[name]}><tt:XAddr>{urls[name]}</tt:XAddr></tt:{tags[name]}>" for name in self.services if name in tags)
            return f"<tds:GetCapabilitiesResponse><tds:Capabilities>{parts}</tds:Capabilities></tds:GetCapabilitiesResponse>"
        if service == "events":
            answered = self._events(operation)
            if answered is not None or operation in {"CreatePullPointSubscription", "PullMessages", "Renew", "Unsubscribe", "GetEventProperties"}:
                return answered
        if operation == "GetServiceCapabilities" and service == "events":
            return f'<tev:GetServiceCapabilitiesResponse><tev:Capabilities WSPullPointSupport="{str(self.pull_point).lower()}" WSSubscriptionPolicySupport="false"/></tev:GetServiceCapabilitiesResponse>'
        if operation == "GetProfiles" and service == "media":
            return f"<trt:GetProfilesResponse>{''.join(self._media1(item) for item in self.profiles)}</trt:GetProfilesResponse>"
        if operation == "GetProfiles" and service == "media2":
            return f"<tr2:GetProfilesResponse>{''.join(self._media2(item) for item in (self.media2_profiles or []))}</tr2:GetProfilesResponse>"
        if operation == "GetStreamUri":
            token = re.search(r"<(?:\w+:)?ProfileToken>([^<]*)</", body).group(1)
            known = [item for item in (self.profiles + (self.media2_profiles or [])) if item["token"] == token]
            if not known:
                return None
            uri = f"rtsp://{PLACEHOLDER_HOST}:554/{token}/media.smp"
            if service == "media2":
                return f"<tr2:GetStreamUriResponse><tr2:Uri>{uri}</tr2:Uri></tr2:GetStreamUriResponse>"
            return f"<trt:GetStreamUriResponse><trt:MediaUri><tt:Uri>{uri}</tt:Uri><tt:InvalidAfterConnect>false</tt:InvalidAfterConnect></trt:MediaUri></trt:GetStreamUriResponse>"
        return None

    def motion(self, topic: str, active: bool, source: str | None = None, at: datetime | None = None, item: str = "IsMotion") -> None:
        """Queue a motion notification for the next PullMessages."""
        stamp = (at or self.now()).strftime("%Y-%m-%dT%H:%M:%SZ")
        named = f'<tt:Source><tt:SimpleItem Name="Rule" Value="{source}"/></tt:Source>' if source else ""
        with self.pending_lock:
            self.pending.append(
            f'<wsnt:NotificationMessage><wsnt:Topic>{topic}</wsnt:Topic><wsnt:Message><tt:Message UtcTime="{stamp}">{named}'
            f'<tt:Data><tt:SimpleItem Name="{item}" Value="{str(active).lower()}"/></tt:Data></tt:Message></wsnt:Message></wsnt:NotificationMessage>')

    def drop_subscription(self) -> None:
        """The camera forgets the subscription, as it does when it restarts."""
        self.subscription_alive = False

    def _events(self, operation: str) -> str | None:
        wsnt = 'xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2"'
        path, body = self.current
        if operation == "CreatePullPointSubscription":
            self.subscription_alive = True
            self.subscriptions += 1
            address = f"http://{PLACEHOLDER_HOST}:{self.port}/onvif/events/subscription" + ("?Idx=7" if self.subscription_style == "query" else "")
            refs = ('<wsa:ReferenceParameters><dom:Id xmlns:dom="urn:fixture">sub-7</dom:Id></wsa:ReferenceParameters>' if self.subscription_style == "reference" else "")
            return (f'<tev:CreatePullPointSubscriptionResponse><tev:SubscriptionReference xmlns:wsa="http://www.w3.org/2005/08/addressing"><wsa:Address>{address}</wsa:Address>{refs}'
                    f'</tev:SubscriptionReference></tev:CreatePullPointSubscriptionResponse>')
        named = {"query": "Idx=7" in path, "reference": "sub-7" in body}.get(self.subscription_style, True)
        if operation in {"PullMessages", "Renew", "Unsubscribe"} and not named:
            return None  # The camera cannot tell which subscription is meant
        if operation in {"PullMessages", "Renew"} and not self.subscription_alive:
            return None
        if operation == "PullMessages":
            if not self.pending:
                time.sleep(0.05)
            with self.pending_lock:
                batch, self.pending = self.pending, []
            return f"<tev:PullMessagesResponse {wsnt}>{''.join(batch)}</tev:PullMessagesResponse>"
        if operation == "Renew":
            return f"<wsnt:RenewResponse {wsnt}/>"
        if operation == "Unsubscribe":
            self.subscription_alive = False
            self.unsubscribed += 1
            return f"<wsnt:UnsubscribeResponse {wsnt}/>"
        if operation == "GetEventProperties":
            topics = ""
            for topic in self.event_topics:
                first, *rest = topic.split("/")
                inner = f'<{rest[-1]} wstop:topic="true"/>' if rest else ""
                for part in reversed(rest[:-1]):
                    inner = f"<{part}>{inner}</{part}>"
                topics += f'<tns1:{first} xmlns:tns1="http://www.onvif.org/ver10/topics">{inner}</tns1:{first}>'
            return f'<tev:GetEventPropertiesResponse xmlns:wstop="http://docs.oasis-open.org/wsn/t-1"><wstop:TopicSet>{topics}</wstop:TopicSet></tev:GetEventPropertiesResponse>'
        return None

    @staticmethod
    def _media1(item: dict) -> str:
        audio = '<tt:AudioEncoderConfiguration token="audio1"><tt:Name>audio</tt:Name><tt:Encoding>G711</tt:Encoding></tt:AudioEncoderConfiguration>' if item["audio"] else ""
        ptz = '<tt:PTZConfiguration token="ptz1"><tt:Name>ptz</tt:Name></tt:PTZConfiguration>' if item["ptz"] else ""
        return (f'<trt:Profiles token="{item["token"]}" fixed="true"><tt:Name>{item["name"]}</tt:Name><tt:VideoSourceConfiguration token="vsc1"><tt:Name>source</tt:Name>'
                f'<tt:Bounds x="0" y="0" width="{item["width"]}" height="{item["height"]}"/></tt:VideoSourceConfiguration>'
                f'<tt:VideoEncoderConfiguration token="vec-{item["token"]}"><tt:Name>enc</tt:Name><tt:Encoding>{item["encoding"]}</tt:Encoding>'
                f'<tt:Resolution><tt:Width>{item["width"]}</tt:Width><tt:Height>{item["height"]}</tt:Height></tt:Resolution>'
                f'<tt:RateControl><tt:FrameRateLimit>{item["fps"]}</tt:FrameRateLimit><tt:EncodingInterval>1</tt:EncodingInterval><tt:BitrateLimit>{item["bitrate"]}</tt:BitrateLimit></tt:RateControl>'
                f"</tt:VideoEncoderConfiguration>{audio}{ptz}</trt:Profiles>")

    @staticmethod
    def _media2(item: dict) -> str:
        audio = '<tr2:AudioEncoder token="audio1"><tt:Name>audio</tt:Name><tt:Encoding>AAC</tt:Encoding></tr2:AudioEncoder>' if item["audio"] else ""
        ptz = '<tr2:PTZ token="ptz1"><tt:Name>ptz</tt:Name></tr2:PTZ>' if item["ptz"] else ""
        return (f'<tr2:Profiles token="{item["token"]}" fixed="true"><tr2:Name>{item["name"]}</tr2:Name><tr2:Configurations><tr2:VideoSource token="vsc1"><tt:Name>source</tt:Name></tr2:VideoSource>'
                f'<tr2:VideoEncoder token="vec-{item["token"]}"><tt:Name>enc</tt:Name><tt:Encoding>{item["encoding"]}</tt:Encoding>'
                f'<tt:Resolution><tt:Width>{item["width"]}</tt:Width><tt:Height>{item["height"]}</tt:Height></tt:Resolution>'
                f'<tt:RateControl><tt:FrameRateLimit>{item["fps"]}</tt:FrameRateLimit><tt:BitrateLimit>{item["bitrate"]}</tt:BitrateLimit></tt:RateControl></tr2:VideoEncoder>'
                f"{audio}{ptz}</tr2:Configurations></tr2:Profiles>")
