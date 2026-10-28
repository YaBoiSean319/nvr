"""An ONVIF camera with Profile G recording search and replay, serving the footage it "recorded on its card" (#131).

The Search service lists one recording and the span of time it holds; the Replay service returns the address to replay it from, which this
fixture serves over HTTP with byte ranges (FFmpeg seeks in it as it would in a replay stream). ``replay_requests`` records each request
and its Range header so tests can see how the footage was fetched. The SOAP is written the way ONVIF cameras write it, not captured from one.
"""
from __future__ import annotations

import re
import subprocess
import shutil
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from tests.fixtures.onvif_camera import OnvifCamera, PLACEHOLDER_HOST

SEARCH = "http://www.onvif.org/ver10/search/wsdl"
REPLAY = "http://www.onvif.org/ver10/replay/wsdl"


def make_footage(path: Path, seconds: int, fps: int = 10) -> Path:
    """``seconds`` of test video with a key frame every second, so a copy can start where it is asked to."""
    subprocess.run([shutil.which("ffmpeg") or "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc=size=640x360:rate={fps}", "-t", str(seconds), "-an",
                    "-c:v", "libx264", "-g", str(fps), "-keyint_min", str(fps), "-sc_threshold", "0", "-pix_fmt", "yuv420p", "-y", str(path)], check=True, timeout=120)
    return path


class RecordingCamera(OnvifCamera):
    EXTRA_SERVICES = {"search": ("/onvif/search", SEARCH), "replay": ("/onvif/replay", REPLAY)}

    def __init__(self, *, footage: Path, earliest: datetime, seconds: float, token: str = "recording-1", **options) -> None:
        options.setdefault("services", ("media", "events", "ptz", "search", "replay"))
        self.footage, self.earliest, self.seconds, self.token = footage, earliest, seconds, token
        self.replay_requests: list[dict] = []
        self.searches = 0
        self.recordings_present = True  # False: the camera has no recording (its card was removed)
        super().__init__(**options)

    @property
    def latest(self) -> datetime:
        return self.earliest + timedelta(seconds=self.seconds)

    def answer_extra(self, service: str, operation: str, body: str) -> str | None:
        if service == "search":
            if operation == "FindRecordings":
                self.searches += 1
                return f'<tse:FindRecordingsResponse xmlns:tse="{SEARCH}"><tse:SearchToken>search-{self.searches}</tse:SearchToken></tse:FindRecordingsResponse>'
            if operation == "GetRecordingSearchResults":
                info = ""
                if self.recordings_present:
                    info = (f"<tt:RecordingInformation><tt:RecordingToken>{self.token}</tt:RecordingToken>"
                            f"<tt:EarliestRecording>{self.earliest.strftime('%Y-%m-%dT%H:%M:%SZ')}</tt:EarliestRecording>"
                            f"<tt:LatestRecording>{self.latest.strftime('%Y-%m-%dT%H:%M:%SZ')}</tt:LatestRecording><tt:RecordingStatus>Stopped</tt:RecordingStatus></tt:RecordingInformation>")
                return (f'<tse:GetRecordingSearchResultsResponse xmlns:tse="{SEARCH}"><tse:ResultList><tt:SearchState>Completed</tt:SearchState>{info}'
                        "</tse:ResultList></tse:GetRecordingSearchResultsResponse>")
            if operation == "EndSearch":
                return f'<tse:EndSearchResponse xmlns:tse="{SEARCH}"><tse:Endpoint>{self.earliest.strftime("%Y-%m-%dT%H:%M:%SZ")}</tse:Endpoint></tse:EndSearchResponse>'
        if service == "replay" and operation == "GetReplayUri":
            return f'<trp:GetReplayUriResponse xmlns:trp="{REPLAY}"><trp:Uri>http://{PLACEHOLDER_HOST}:{self.port}/replay/{self.token}.mp4</trp:Uri></trp:GetReplayUriResponse>'
        return None

    def get(self, handler: BaseHTTPRequestHandler) -> None:
        if handler.path.split("?")[0] != f"/replay/{self.token}.mp4" or not self.footage.is_file():
            return self.send(handler, 404, "")
        data = self.footage.read_bytes()
        header = handler.headers.get("Range", "")
        self.replay_requests.append({"path": handler.path, "range": header})
        match = re.match(r"bytes=(\d+)-(\d*)", header)
        start = int(match.group(1)) if match else 0
        stop = int(match.group(2)) if match and match.group(2) else len(data) - 1
        stop = min(stop, len(data) - 1)
        chunk = data[start:stop + 1]
        handler.send_response(206 if match else 200)
        handler.send_header("Content-Type", "video/mp4")
        handler.send_header("Accept-Ranges", "bytes")
        if match:
            handler.send_header("Content-Range", f"bytes {start}-{stop}/{len(data)}")
        handler.send_header("Content-Length", str(len(chunk)))
        handler.end_headers()
        try:
            handler.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
