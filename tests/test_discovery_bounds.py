"""A scan keeps a bounded number of devices, and a discovered device that was never added can be forgotten (#381)."""
from __future__ import annotations

import socket
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from app import discovery
from app.discovery import DiscoveredDevice, DiscoveryResult
from app.store import connect
from tests.test_access_grants import GrantCase


def answer(count: int, start: int = 0) -> bytes:
    """One discovery datagram naming ``count`` devices."""
    matches = "".join(
        f"<d:ProbeMatch><a:EndpointReference><a:Address>urn:uuid:{uuid.UUID(int=start + number)}</a:Address></a:EndpointReference>"
        f"<d:XAddrs>http://192.0.2.{number % 250 + 1}/onvif/device_service</d:XAddrs></d:ProbeMatch>" for number in range(count))
    return (f'<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
            f'xmlns:a="http://www.w3.org/2005/08/addressing"><s:Body><d:ProbeMatches>{matches}</d:ProbeMatches></s:Body></s:Envelope>').encode()


class FloodingNetwork:
    """Stands in for the socket: every read returns the next datagram, then nothing."""

    def __init__(self, datagrams: list[bytes]) -> None:
        self.datagrams = list(datagrams)

    def __call__(self, *args, **kwargs):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def setsockopt(self, *args): ...
    def bind(self, *args): ...
    def settimeout(self, *args): ...
    def sendto(self, *args): ...
    def close(self): ...

    def recvfrom(self, size: int):
        if self.datagrams:
            return self.datagrams.pop(0), ("192.0.2.9", 3702)
        raise socket.timeout


class ScanBoundsTests(GrantCase):
    def scan(self, datagrams: list[bytes]) -> DiscoveryResult:
        with patch.object(discovery.socket, "socket", FloodingNetwork(datagrams)):
            return discovery._discover_sync(0.3, None)

    def test_an_answer_naming_hundreds_of_devices_contributes_only_a_few(self):
        result = self.scan([answer(300)])
        self.assertEqual(len(result.devices), discovery.MAX_DEVICES_PER_ANSWER)
        self.assertEqual(result.ignored_responses, 1)

    def test_a_flood_of_answers_is_kept_to_the_scans_limit(self):
        per = discovery.MAX_DEVICES_PER_ANSWER
        result = self.scan([answer(per, start=batch * per) for batch in range(discovery.MAX_DEVICES_PER_SCAN // per + 20)])
        self.assertEqual(len(result.devices), discovery.MAX_DEVICES_PER_SCAN)
        self.assertGreater(result.ignored_responses, 0)

    def test_an_ordinary_network_is_found_whole(self):
        result = self.scan([answer(1, start=number) for number in range(40)])
        self.assertEqual((len(result.devices), result.ignored_responses), (40, 0))


class CandidateTests(GrantCase):
    def discovered(self, *devices: DiscoveredDevice) -> dict:
        with patch("app.api.onboarding.discover_onvif", new=AsyncMock(return_value=DiscoveryResult(tuple(devices), 0))):
            response = self.admin.post("/api/discovery/scan", json={"timeout_seconds": 0.5})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    @staticmethod
    def device(number: int) -> DiscoveredDevice:
        return DiscoveredDevice(str(uuid.UUID(int=number)), (f"http://192.0.2.{number}/onvif/device_service",), ())

    def candidates(self) -> list[dict]:
        return self.admin.get("/api/discovery/candidates").json()["candidates"]

    def test_a_device_that_was_never_added_can_be_forgotten_and_comes_back_if_it_answers_again(self):
        self.discovered(self.device(1), self.device(2))
        first = self.candidates()[0]
        self.assertEqual(self.admin.delete(f"/api/discovery/candidates/{first['id']}").status_code, 204)
        self.assertEqual(len(self.candidates()), 1)
        self.assertEqual(self.admin.delete(f"/api/discovery/candidates/{first['id']}").status_code, 404)
        self.discovered(self.device(1), self.device(2))
        self.assertEqual(len(self.candidates()), 2)

    def test_a_device_that_is_a_camera_is_not_forgotten(self):
        self.discovered(self.device(1))
        candidate = self.candidates()[0]
        with connect() as db:
            db.execute("UPDATE discovery_candidates SET linked_camera_id=? WHERE id=?", (self.d1, candidate["id"]))
        self.assertEqual(self.admin.delete(f"/api/discovery/candidates/{candidate['id']}").status_code, 409)
        self.assertEqual(len(self.candidates()), 1)

    def test_forgetting_is_for_administrators(self):
        self.discovered(self.device(1))
        self.assertEqual(self.limited.delete(f"/api/discovery/candidates/{self.candidates()[0]['id']}").status_code, 403)

    def test_devices_not_seen_for_a_month_and_never_added_are_dropped_at_the_next_scan(self):
        self.discovered(self.device(1), self.device(2), self.device(3))
        long_ago = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
        ids = {item["device_uuid"]: item["id"] for item in self.candidates()}
        with connect() as db:
            db.execute("UPDATE discovery_candidates SET last_seen_at=?", (long_ago,))
            db.execute("UPDATE discovery_candidates SET linked_camera_id=? WHERE id=?", (self.d1, ids[str(uuid.UUID(int=3))]))  # This one became a camera
        self.discovered(self.device(1))  # Only the first answers now
        self.assertEqual(sorted(item["device_uuid"] for item in self.candidates()), [str(uuid.UUID(int=1)), str(uuid.UUID(int=3))])
