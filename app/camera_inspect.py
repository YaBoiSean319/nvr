"""Look at a camera from its address and login alone: who it is, how it signs in, what streams it has, and whether its video really decodes.

``inspect_camera`` is what the simplified Add Camera flow runs. It walks the steps an administrator sees (connecting, authenticating,
identifying the device, discovering streams, checking motion events, verifying video) and reports each one's outcome with an actionable
message, plus a capability summary (discovery, authentication, video, audio, motion, PTZ) that is judged separately: a camera whose
motion events are unavailable still has working video, and the report says so. Video is verified by decoding a frame from the stream the
NVR chose, never by the connection opening. Nothing in the report holds a credential or a credentialed address.
"""
from __future__ import annotations

import asyncio
import shutil
import weakref
from typing import Any

import httpx

from .onvif_client import DEFAULT_PORTS, OnvifClient, OnvifError, Services
from .onvif_profiles import MediaProfile, Selection, choose_profiles

STEPS = (
    ("connect", "Connecting to camera"), ("authenticate", "Authenticating"), ("identify", "Identifying device"),
    ("streams", "Discovering streams"), ("motion", "Configuring motion events"), ("video", "Verifying video"), ("ready", "Camera ready"),
)
DECODE_TIMEOUT_SECONDS = 25
STREAM_PROTOCOLS: str | None = None  # Camera protocols only; the tests widen it to read generated files.
MAX_CONCURRENT_INSPECTIONS = 4
_gates: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = weakref.WeakKeyDictionary()


def _gate() -> asyncio.Semaphore:
    """At most a few inspections at a time, so adding many cameras does not open a connection to every one at once (one limit for each event loop)."""
    loop = asyncio.get_running_loop()
    if loop not in _gates:
        _gates[loop] = asyncio.Semaphore(MAX_CONCURRENT_INSPECTIONS)
    return _gates[loop]

BRANDS = {"hanwha": "hanwha", "samsung": "samsung", "wisenet": "hanwha", "axis": "axis", "hikvision": "hikvision", "dahua": "dahua", "bosch": "bosch",
          "reolink": "reolink", "uniview": "uniview", "unv": "uniview", "acti": "acti"}
PORTS_BY_BRAND = {"hikvision": (80, 8000, 8080), "dahua": (80, 8080, 8000), "reolink": (80, 8000), "uniview": (80, 8080, 8000)}


def brand_of(manufacturer: str) -> str:
    """The NVR's brand name for an ONVIF manufacturer string, or ``other`` when it is not one it knows."""
    text = manufacturer.casefold()
    return next((brand for key, brand in BRANDS.items() if key in text), "other" if manufacturer else "auto")


def ports_for(brand: str) -> tuple[int, ...]:
    preferred = PORTS_BY_BRAND.get((brand or "auto").casefold(), (80, 8080))
    return tuple(dict.fromkeys(preferred + DEFAULT_PORTS))


async def verify_video(source: str, protocols: str | None = None) -> dict[str, Any]:
    """Decode one frame from a stream. ``{"decoded": True}``, or ``{"decoded": False, "reason": ...}`` saying whether it opened at all.

    The address (with its login) goes to FFmpeg on standard input, never on a command line."""
    from .recording import _PROTOCOLS, _manifest_bytes, probe_stream
    executable = shutil.which("ffmpeg")
    if not executable:
        return {"decoded": False, "reason": "Video verification is unavailable: FFmpeg is not installed"}
    opened = await probe_stream(source) if (protocols or STREAM_PROTOCOLS) is None else {"reachable": True}
    if not opened.get("reachable"):
        return {"decoded": False, "opened": False, "reason": f"The camera's video stream could not be opened: {opened.get('reason', 'no answer')}"}
    args = [executable, "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-protocol_whitelist", protocols or STREAM_PROTOCOLS or _PROTOCOLS,
            "-i", "pipe:0", "-an", "-frames:v", "1", "-pix_fmt", "gray", "-f", "rawvideo", "-"]
    process = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        frame, _ = await asyncio.wait_for(process.communicate(_manifest_bytes(source)), timeout=DECODE_TIMEOUT_SECONDS)
    except (asyncio.TimeoutError, OSError):
        return {"decoded": False, "opened": True, "reason": "RTSP stream connected but no video frame arrived in time"}
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    if process.returncode != 0:
        return {"decoded": False, "opened": True, "reason": "RTSP stream connected but video decoding failed"}
    if not frame:
        return {"decoded": False, "opened": True, "reason": "RTSP stream connected but it carried no video frames"}
    return {"decoded": True, "opened": True, "video": opened.get("video")}


def _state(state: str, message: str) -> dict[str, str]:
    return {"state": state, "message": message}


def _failure(error: OnvifError) -> str:
    return error.message


async def inspect_camera(host: str, username: str = "", password: str = "", *, device_service_url: str | None = None, brand: str = "auto",
                         transport: httpx.AsyncBaseTransport | None = None, verify: bool = True, protocols: str | None = None) -> dict[str, Any]:
    """The report on one camera. It does not raise for camera faults: each failure is the step's outcome and the capability's state."""
    async with _gate():
        return await _inspect(host, username, password, device_service_url, brand, transport, verify, protocols)


async def _inspect(host: str, username: str, password: str, device_service_url: str | None, brand: str, transport: httpx.AsyncBaseTransport | None,
                   verify: bool, protocols: str | None) -> dict[str, Any]:
    steps: dict[str, dict[str, str]] = {key: {"id": key, "label": label, "state": "pending", "detail": ""} for key, label in STEPS}
    capabilities: dict[str, dict[str, str]] = {name: _state("unknown", "Not checked") for name in ("discovery", "authentication", "video", "audio", "motion", "ptz")}
    report: dict[str, Any] = {"ok": False, "device": {}, "brand": "auto", "device_service_url": None, "services": {}, "auth": {}, "profiles": [], "selection": None,
                              "steps": [], "capabilities": capabilities}

    def finish(client: OnvifClient | None) -> dict[str, Any]:
        for step in steps.values():
            if step["state"] == "pending":
                step["state"] = "skipped"
        report["steps"] = list(steps.values())
        report["auth"] = dict(client.auth_by_service) if client else {}
        report["diagnostics"] = client.diagnostics() if client else []
        return report

    def done(key: str, state: str, detail: str = "") -> None:
        steps[key].update(state=state, detail=detail)

    try:
        client = OnvifClient(host, username, password, transport=transport)
    except ValueError as exc:
        done("connect", "failed", str(exc))
        capabilities["discovery"] = _state("failed", str(exc))
        return finish(None)
    async with client:
        try:
            url = await client.locate_device(device_service_url, ports_for(brand))
            report["device_service_url"] = url
            done("connect", "ok")
            capabilities["discovery"] = _state("ok", "An ONVIF device service answered")
        except OnvifError as exc:
            done("connect", "failed", _failure(exc))
            capabilities["discovery"] = _state("failed", _failure(exc))
            return finish(client)

        info = None
        try:
            info = await client.device_information(url)
            method = client.auth_by_service.get("device", "none")
            if method == "none" and username:
                done("authenticate", "ok", "The camera's device service does not ask for a login; the credentials are checked on its other services")
                capabilities["authentication"] = _state("unknown", steps["authenticate"]["detail"])
            else:
                done("authenticate", "ok", "No login is needed" if method == "none" else f"Signed in with {method.replace('+', ' and ')}")
                capabilities["authentication"] = _state("ok", steps["authenticate"]["detail"])
        except OnvifError as exc:
            if exc.code == "auth_rejected":
                done("authenticate", "failed", _failure(exc))
                capabilities["authentication"] = _state("failed", _failure(exc))
                return finish(client)
            # The camera would not describe itself, but did not reject the login: go on to its streams.
            done("authenticate", "ok", "Credentials were not rejected")
            capabilities["authentication"] = _state("unknown", "Credentials were not rejected, but sign-in could not be confirmed")
            done("identify", "failed", _failure(exc))
        if info is not None:
            report["device"] = {"manufacturer": info.manufacturer, "model": info.model, "firmware": info.firmware}
            report["brand"] = brand_of(info.manufacturer) if info.manufacturer else "auto"
            done("identify", "ok", " ".join(part for part in (info.manufacturer, info.model) if part) or "The camera did not give its make or model")

        services: Services | None = None
        selection: Selection | None = None
        profiles: list[MediaProfile] = []
        try:
            services = await client.services(url)
            report["services"] = dict(services.urls)
            profiles = await client.profiles(services)
            selection = choose_profiles(profiles)
            report["profiles"] = [item.view() for item in profiles]
            if selection.record is None:
                done("streams", "failed", selection.problems[0] if selection.problems else "No compatible video profile found")
                capabilities["video"] = _state("failed", "No compatible video profile found")
            else:
                report["selection"] = {"record_token": selection.record.token, "live_token": selection.live.token if selection.live else None,
                                       "reason": selection.reason, "warnings": list(selection.problems)}
                done("streams", "ok", selection.reason)
        except OnvifError as exc:
            done("streams", "failed", _failure(exc))
            capabilities["video"] = _state("failed", _failure(exc))
            if exc.code == "auth_rejected":
                capabilities["authentication"] = _state("failed", _failure(exc))
                done("authenticate", "failed", _failure(exc))
        if services is not None:
            names = set(services.urls)
            capabilities["ptz"] = _state("available", "The camera has a PTZ service") if "ptz" in names or any(item.has_ptz for item in profiles) else _state("unsupported", "No PTZ service")
            capabilities["audio"] = _state("available", "A profile carries audio") if any(item.has_audio for item in profiles) else _state("unsupported", "No profile carries audio")
            if "events" not in names:
                done("motion", "skipped", "The camera did not report an ONVIF Events service")
                capabilities["motion"] = _state("unsupported", "No ONVIF Events service: motion can use the NVR's own detection instead")
            else:
                try:
                    caps = await client.service_capabilities(services, "events")
                except OnvifError as exc:
                    done("motion", "failed", _failure(exc))
                    capabilities["motion"] = _state("failed", _failure(exc))
                else:
                    if caps.get("WSPullPointSupport", "true").casefold() in {"false", "0"}:
                        done("motion", "skipped", "The camera's Events service does not offer pull-point subscriptions")
                        capabilities["motion"] = _state("unsupported", "No pull-point subscriptions: motion can use the NVR's own detection instead")
                    else:
                        done("motion", "ok", "The camera offers ONVIF pull-point events")
                        capabilities["motion"] = _state("available", "The camera offers ONVIF pull-point events")
        else:
            done("motion", "skipped", "Not checked: the camera's services could not be listed")

        if selection is not None and selection.record is not None and services is not None:
            try:
                uri = await client.stream_uri(services, selection.record)
                source = client.stream_url_with_login(uri)
            except OnvifError as exc:
                done("video", "failed", _failure(exc))
                capabilities["video"] = _state("failed", _failure(exc))
            else:
                if not verify:
                    done("video", "skipped", "Video was not verified")
                    capabilities["video"] = _state("unknown", "Video was not verified")
                else:
                    result = await verify_video(source, protocols)
                    if result.get("decoded"):
                        report["video"] = result.get("video")
                        done("video", "ok", f"Decoded video from {selection.record.name}")
                        capabilities["video"] = _state("ok", f"Decoded video from {selection.record.name}")
                    else:
                        done("video", "failed", str(result["reason"]))
                        capabilities["video"] = _state("failed", str(result["reason"]))
        elif steps["video"]["state"] == "pending":
            done("video", "skipped", "Not checked: no video profile was chosen")

        skipped = steps["video"]["state"] == "skipped" and not verify and selection is not None and selection.record is not None
        report["ok"] = capabilities["video"]["state"] == "ok" or skipped
        done("ready", "ok" if report["ok"] else "failed", "" if report["ok"] else "Video could not be verified; the camera can still be saved and fixed later")
        return finish(client)
