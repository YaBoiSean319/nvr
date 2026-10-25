"""Disk health from SMART, and the retention forecast: judging fixture reports, saying "not available" honestly, and forecasts
checked against hand calculations."""
from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from app import forecast, smart
from app.store import connect
from tests.test_access_grants import GrantCase

GB = 10 ** 9
NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def ata(attributes: list[dict] | None = None, passed: bool = True, **extra) -> dict:
    return {"model_name": "Fixture HDD 4TB", "smart_status": {"passed": passed}, "temperature": {"current": 34}, "power_on_time": {"hours": 12000},
            "ata_smart_attributes": {"table": attributes or []}, **extra}


def attribute(identifier: int, name: str, raw: int = 0, when_failed: str = "", value: int = 100, thresh: int = 10) -> dict:
    return {"id": identifier, "name": name, "value": value, "worst": value, "thresh": thresh, "when_failed": when_failed, "raw": {"value": raw}}


def nvme(**log) -> dict:
    base = {"critical_warning": 0, "percentage_used": 10, "available_spare": 100, "available_spare_threshold": 10, "media_errors": 0}
    return {"model_name": "Fixture NVMe 1TB", "smart_status": {"passed": True}, "temperature": {"current": 41}, "nvme_smart_health_information_log": {**base, **log}}


class AssessmentTests(unittest.TestCase):
    def test_a_clean_disk_is_healthy_and_reports_what_it_is(self):
        result = smart.assess(ata([attribute(5, "Reallocated_Sector_Ct"), attribute(197, "Current_Pending_Sector")]))
        self.assertEqual((result.state, result.findings, result.model, result.temperature_c, result.power_on_hours), ("healthy", [], "Fixture HDD 4TB", 34, 12000))

    def test_failing_attributes_are_failing(self):
        for report in (ata(passed=False), ata([attribute(5, "Reallocated_Sector_Ct", raw=2000, when_failed="FAILING_NOW", value=3, thresh=36)]),
                       nvme(critical_warning=4), nvme(percentage_used=100), nvme(percentage_used=130)):
            self.assertEqual(smart.assess(report).state, "failing", report)
        self.assertIn("FAILED", smart.assess(ata(passed=False)).findings[0])
        self.assertIn("Reallocated_Sector_Ct is failing now", smart.assess(ata([attribute(5, "Reallocated_Sector_Ct", when_failed="FAILING_NOW")])).findings[0])

    def test_signs_that_come_before_failure_are_a_warning(self):
        cases = {
            "reallocated": ata([attribute(5, "Reallocated_Sector_Ct", raw=8)]),
            "pending": ata([attribute(197, "Current_Pending_Sector", raw=1)]),
            "uncorrectable": ata([attribute(198, "Offline_Uncorrectable", raw=3)]),
            "past": ata([attribute(1, "Raw_Read_Error_Rate", when_failed="In_the_past")]),
            "worn": nvme(percentage_used=92),
            "spare": nvme(available_spare=5, available_spare_threshold=10),
            "media": nvme(media_errors=3),
        }
        for name, report in cases.items():
            result = smart.assess(report)
            self.assertEqual(result.state, "warning", name)
            self.assertTrue(result.findings, name)
        self.assertEqual(smart.assess(cases["reallocated"]).findings, ["8 reallocated sectors"])

    def test_failing_outranks_a_warning_and_keeps_both_findings(self):
        result = smart.assess(ata([attribute(197, "Current_Pending_Sector", raw=4)], passed=False))
        self.assertEqual(result.state, "failing")
        self.assertEqual(len(result.findings), 2)
        self.assertIn("FAILED", result.findings[0])

    def test_a_healthy_nvme_drive_is_healthy(self):
        self.assertEqual(smart.assess(nvme()).state, "healthy")


class DiscoveryTests(unittest.TestCase):
    def tree(self, partition: bool = True, device: bool = True) -> tuple[Path, Path]:
        root = Path(tempfile.mkdtemp())
        disk = root / "sys" / "devices" / "pci0" / "block" / "sda"
        part = disk / "sda1"
        (part if partition else disk).mkdir(parents=True)
        if partition:
            (part / "partition").write_text("1")
        link = root / "sys" / "dev" / "block"
        link.mkdir(parents=True)
        (link / "8:1").symlink_to(part if partition else disk)
        (root / "dev").mkdir()
        if device:
            (root / "dev" / "sda").write_text("")
        return root / "sys", root / "dev"

    def find(self, sysfs: Path, dev: Path, number: int = os.makedev(8, 1), **extra) -> str:
        return smart.device_for("default", Path("/x"), sysfs=sysfs, dev=dev, environment={}, stat_device=lambda path: number, is_block=lambda path: path.exists(), **extra)

    def test_a_partitions_whole_disk_is_found_through_sysfs(self):
        sysfs, dev = self.tree()
        self.assertEqual(self.find(sysfs, dev), str(dev / "sda"))

    def test_a_whole_disk_volume_is_found_too(self):
        sysfs, dev = self.tree(partition=False)
        self.assertEqual(self.find(sysfs, dev), str(dev / "sda"))

    def test_a_disk_that_is_not_passed_through_says_so(self):
        sysfs, dev = self.tree(device=False)
        with self.assertRaisesRegex(smart.Unavailable, "not passed through to the container"):
            self.find(sysfs, dev)

    def test_an_overlay_filesystem_cannot_be_traced_to_a_disk(self):
        sysfs, dev = self.tree()
        with self.assertRaisesRegex(smart.Unavailable, "cannot see which disk holds this volume"):
            self.find(sysfs, dev, number=os.makedev(0, 53))
        with self.assertRaisesRegex(smart.Unavailable, "cannot see which disk"):
            self.find(sysfs, dev, number=os.makedev(9, 9))

    def test_a_configuration_that_is_not_an_object_is_unavailable_not_a_crash(self):
        for raw in ('["/dev/sda"]', '"default"', "7", "null"):
            with self.assertRaises(smart.Unavailable, msg=raw):
                smart.device_for("default", Path("/tmp"), environment={"NVR_SMART_DEVICES": raw})

    def test_a_configured_device_wins_and_is_validated(self):
        sysfs, dev = self.tree(device=False)
        configured = lambda value: smart.device_for("archive", Path("/x"), sysfs=sysfs, dev=dev, environment={"NVR_SMART_DEVICES": value}, stat_device=lambda path: 0)
        self.assertEqual(configured('{"archive":"/dev/nvme0n1"}'), "/dev/nvme0n1")
        for bad in ('{"archive":"/etc/passwd"}', '{"archive":"/dev/../etc/shadow"}', "not json"):
            with self.assertRaises(smart.Unavailable, msg=bad):
                configured(bad)

    def test_an_unreadable_volume_is_unavailable(self):
        def broken(path):
            raise OSError("gone")
        with self.assertRaisesRegex(smart.Unavailable, "cannot be read"):
            smart.device_for("default", Path("/x"), environment={}, stat_device=broken)


class SmartctlTests(unittest.TestCase):
    def script(self, body: str) -> str:
        path = Path(tempfile.mkdtemp()) / "smartctl"
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)
        return str(path)

    def run_with(self, body: str) -> dict:
        with mock.patch.dict(os.environ, {"NVR_SMARTCTL": self.script(body)}):
            return smart.run_smartctl("/dev/sda")

    def test_a_report_is_read_from_the_real_command_whatever_its_exit_status(self):
        report = self.run_with("echo '" + json.dumps(ata([attribute(5, "Reallocated_Sector_Ct", raw=8)])) + "'; exit 64")  # smartctl's exit status is a bitmask.
        self.assertEqual(smart.assess(report).state, "warning")

    def test_the_command_is_given_the_device_and_json(self):
        arguments = self.run_with('echo "{\\"smart_status\\":{\\"passed\\":true},\\"args\\":\\"$*\\"}"')
        self.assertEqual(arguments["args"], "-j -a /dev/sda")

    def test_without_smartctl_it_says_so(self):
        with mock.patch.dict(os.environ, {"NVR_SMARTCTL": ""}), mock.patch("app.smart.shutil.which", return_value=None):
            with self.assertRaisesRegex(smart.Unavailable, "smartctl is not installed"):
                smart.run_smartctl("/dev/sda")

    def test_refused_access_says_what_to_add(self):
        denied = {"smartctl": {"messages": [{"string": "/dev/sda: Smartctl open device: /dev/sda failed: Permission denied"}]}}
        with self.assertRaisesRegex(smart.Unavailable, "SYS_RAWIO"):
            self.run_with("echo '" + json.dumps(denied) + "'; exit 2")

    def test_a_disk_that_reports_no_smart_data_is_not_available(self):
        usb = {"smartctl": {"messages": [{"string": "Unknown USB bridge [0x0bc2:0x2321]"}]}}
        with self.assertRaisesRegex(smart.Unavailable, "Unknown USB bridge"):
            self.run_with("echo '" + json.dumps(usb) + "'; exit 4")
        with self.assertRaisesRegex(smart.Unavailable, "does not report SMART"):
            self.run_with("echo '{}'")

    def test_garbage_and_a_hang_are_not_available(self):
        with self.assertRaisesRegex(smart.Unavailable, "no readable report"):
            self.run_with("echo 'this is not json'")
        import subprocess
        with mock.patch("app.smart.subprocess.run", side_effect=subprocess.TimeoutExpired("smartctl", 30)), mock.patch.dict(os.environ, {"NVR_SMARTCTL": "/bin/true"}):
            with self.assertRaisesRegex(smart.Unavailable, "did not answer"):
                smart.run_smartctl("/dev/sda")


class DiskCase(GrantCase):
    def reading(self, report: dict | None, reason: str = "The disk is not passed through") -> mock._patch:
        def read(volume_id, path, runner=None, **discovery):
            return ("/dev/sda", smart.assess(report), None) if report is not None else (None, None, reason)
        return mock.patch("app.smart.read_volume", read)

    def refresh(self, report: dict | None, reason: str = "The disk is not passed through") -> dict[str, str]:
        with self.reading(report, reason):
            return smart.refresh({"default": Path("/tmp")})

    def incidents(self, fault_type: str) -> list[dict]:
        with connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM incidents WHERE fault_type=? ORDER BY first_seen_at", (fault_type,))]


class DiskHealthTests(DiskCase):
    def test_failing_attributes_raise_an_incident_that_recovers_when_the_disk_reads_clean(self):
        self.assertEqual(self.refresh(ata([attribute(197, "Current_Pending_Sector", raw=7)], passed=False)), {"default": "failing"})
        (incident,) = self.incidents("disk_health")
        self.assertTrue(incident["fault_active"])
        self.assertIn("recording storage default is failing", incident["summary"])
        self.assertEqual(json.loads(incident["details_json"])["findings"][0], "The drive's own health assessment is FAILED")
        self.assertEqual(self.refresh(ata()), {"default": "healthy"})
        (incident,) = self.incidents("disk_health")
        self.assertEqual((incident["state"], incident["fault_active"]), ("recovered", 0))

    def test_a_disk_that_stops_answering_does_not_clear_its_incident(self):
        self.refresh(ata([attribute(197, "Current_Pending_Sector", raw=7)], passed=False))
        self.refresh(None, "The disk did not answer in time")
        (incident,) = self.incidents("disk_health")
        self.assertTrue(incident["fault_active"])
        (disk,) = self.admin.get("/api/storage/disk-health").json()["disks"]
        self.assertEqual(disk["state"], "failing")  # The last real reading stands, with its findings.
        self.assertIn("The drive's own health assessment is FAILED", disk["findings"])
        self.assertIn("did not answer in time", disk["reason"])
        self.refresh(ata())  # Only a clean reading recovers it.
        self.assertEqual(self.incidents("disk_health")[0]["fault_active"], 0)

    def test_a_volume_whose_path_cannot_be_used_is_still_reported_and_a_new_volume_is_due(self):
        with self.reading(ata()):
            self.assertEqual(smart.refresh({"default": Path("/tmp"), "archive": "The volume's storage path cannot be used (OSError)"}),
                             {"default": "healthy", "archive": "unavailable"})
        with connect() as db:
            self.assertFalse(smart.due(db, volume_ids=["default", "archive"]))
            self.assertTrue(smart.due(db, volume_ids=["default", "archive", "offsite"]))  # A volume added since has no reading yet.

    def test_a_pre_failure_sign_raises_an_incident_too(self):
        self.refresh(ata([attribute(5, "Reallocated_Sector_Ct", raw=12)]))
        (incident,) = self.incidents("disk_health")
        self.assertTrue(incident["fault_active"])
        self.assertIn("warning: 12 reallocated sectors", incident["summary"])

    def test_without_device_access_it_says_not_available_and_raises_nothing(self):
        self.assertEqual(self.refresh(None, "The disk sda is not passed through to the container."), {"default": "unavailable"})
        body = self.admin.get("/api/storage/disk-health").json()["disks"]
        self.assertEqual([(disk["volume_id"], disk["available"], disk["state"]) for disk in body], [("default", False, "unavailable")])
        self.assertIn("not passed through", body[0]["reason"])
        self.assertEqual(self.incidents("disk_health"), [])

    def test_a_clean_disk_reports_its_facts(self):
        self.refresh(ata())
        (disk,) = self.admin.get("/api/storage/disk-health").json()["disks"]
        self.assertEqual((disk["available"], disk["state"], disk["model"], disk["temperature_c"], disk["power_on_hours"], disk["findings"]), (True, "healthy", "Fixture HDD 4TB", 34, 12000, []))

    def test_a_volume_removed_from_the_configuration_is_forgotten_and_its_incident_ends(self):
        with self.reading(ata(passed=False)):
            smart.refresh({"default": Path("/tmp"), "archive": Path("/tmp")})
        self.assertEqual(len(self.incidents("disk_health")), 2)
        with self.reading(ata()):
            smart.refresh({"default": Path("/tmp")})
        with connect() as db:
            self.assertEqual([row[0] for row in db.execute("SELECT volume_id FROM disk_health")], ["default"])
        self.assertEqual(sorted((incident["incident_key"], bool(incident["fault_active"])) for incident in self.incidents("disk_health")), [("disk:archive", False), ("disk:default", False)])

    def test_readings_are_taken_when_they_are_due(self):
        with connect() as db:
            self.assertTrue(smart.due(db))
        self.refresh(ata())
        with connect() as db:
            self.assertFalse(smart.due(db))
            self.assertTrue(smart.due(db, datetime.now(timezone.utc) + timedelta(seconds=smart.REFRESH_SECONDS + 1)))

    def test_only_administrators_see_disk_health_or_ask_for_a_reading(self):
        for method, path in (("get", "/api/storage/disk-health"), ("post", "/api/storage/disk-health/refresh"), ("get", "/api/storage/forecast")):
            self.assertEqual(getattr(self.limited, method)(path).status_code, 403, path)

    def test_an_administrator_can_ask_for_a_reading_now(self):
        with self.reading(ata([attribute(197, "Current_Pending_Sector", raw=2)])):
            response = self.admin.post("/api/storage/disk-health/refresh")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["disks"][0]["state"], "warning")
        (entry,) = self.audit("disk_health_refreshed")
        self.assertEqual(entry["details"]["states"], {"default": "warning"})

    def test_incidents_reach_storage_webhooks(self):
        from app import webhooks
        webhooks.create_endpoint("Storage hook", "https://93.184.216.34/in", ["storage.failure", "storage.recovered"], False, 1)
        self.refresh(ata(passed=False))
        self.refresh(ata())
        with connect() as db:
            self.assertEqual([row[0] for row in db.execute("SELECT event_type FROM webhook_deliveries ORDER BY created_at,id")], ["storage.failure", "storage.recovered"])

    def test_a_forecast_that_cannot_be_made_does_not_take_the_metrics_down(self):
        from app import prometheus
        self.refresh(ata(passed=False))
        with mock.patch("app.forecast.forecast", side_effect=ValueError("bad timestamp")):
            text = prometheus.render(False)
        self.assertIn('nvr_disk_health_state{volume="default",state="failing"} 1', text)
        self.assertIn("nvr_build_info", text)

    def test_the_disk_state_is_a_metric(self):
        self.refresh(ata(passed=False))
        from app import prometheus
        self.assertIn('nvr_disk_health_state{volume="default",state="failing"} 1', prometheus.render(False))


class WaterLevelTests(unittest.TestCase):
    def test_everything_that_fits_keeps_its_policy(self):
        self.assertIsNone(forecast.water_level([10, 5], [30, 14], 400))

    def test_a_shortfall_is_shared_by_giving_every_camera_the_same_days_up_to_its_policy(self):
        # 10 GB/day for 30 days and 5 GB/day for 14 days need 370 GB; with 300 the short camera keeps its 14 and the other (300 - 70) / 10 = 23.
        self.assertAlmostEqual(forecast.water_level([10, 5], [30, 14], 300), 23.0)
        self.assertAlmostEqual(forecast.water_level([10, 5], [30, 14], 150), 10.0)  # 15 GB/day for D days: 150 / 15.
        self.assertAlmostEqual(forecast.water_level([10], [30], 100), 10.0)

    def test_no_room_at_all_keeps_nothing(self):
        self.assertEqual(forecast.water_level([10, 5], [30, 14], 0), 0.0)

    def test_the_days_until_the_footage_stored_exceeds_the_budget(self):
        rates, policies, ages = [10, 5], [30, 14], [2, 2]
        self.assertAlmostEqual(forecast.days_until_shortfall(rates, policies, ages, 120), 6.0)  # 15 * (2 + t) = 120
        self.assertAlmostEqual(forecast.days_until_shortfall(rates, policies, ages, 300), 21.0)  # 70 + 10 * (2 + t) = 300 once the second camera is full
        self.assertIsNone(forecast.days_until_shortfall(rates, policies, ages, 400))
        self.assertEqual(forecast.days_until_shortfall(rates, policies, ages, 20), 0.0)


class ForecastCase(DiskCase):
    """Cameras that have been writing for 48 hours at known rates, on a disk with a known amount free."""

    def setUp(self):
        super().setUp()
        self.env = mock.patch.dict(os.environ, {"NVR_STORAGE_PRESSURE_FREE_BYTES": "0"})
        self.env.start()
        self.addCleanup(self.env.stop)
        with connect() as db:
            db.execute("DELETE FROM recording_segments")

    def write(self, camera: str, gb_per_day: float, hours: int = 48, ends: datetime = NOW) -> None:
        with connect() as db:
            for hour in range(hours):
                start = ends - timedelta(hours=hours - hour)
                db.execute("INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES(?,?,?,?,?)",
                           (camera, f"{camera}/{uuid.uuid4()}.mp4", start.isoformat(), (start + timedelta(hours=1)).isoformat(), int(gb_per_day * GB / 24)))

    def policy(self, camera: str, days: int) -> None:
        response = self.admin.put(f"/api/cameras/{camera}/retention", json={"retention_days": days})
        self.assertEqual(response.status_code, 200, response.text)

    def run_forecast(self, free_gb: float, devices: dict[str, str] | None = None, now: datetime = NOW) -> dict:
        devices = devices or {}
        with connect() as db:
            return forecast.forecast(db, now, usage=lambda path: int(free_gb * GB), device_of=lambda path: devices.get("archive" if path.name == "archive" else "default", "disk"))

    def camera(self, result: dict, camera_id: str) -> dict:
        return next(item for item in result["cameras"] if item["id"] == camera_id)

    def two_cameras(self) -> None:
        self.write(self.d1, 10)
        self.write(self.d2, 5)
        self.policy(self.d1, 30)
        self.policy(self.d2, 14)


class ForecastTests(ForecastCase):
    def test_a_forecast_for_fixture_write_rates_matches_the_hand_calculation(self):
        self.two_cameras()
        # Stored: 20 + 10 = 30 GB. With 270 GB free the budget is 300 GB. Needs 10 * 30 + 5 * 14 = 370 GB. The 5 GB/day camera keeps its 14 days;
        # the 10 GB/day camera keeps (300 - 70) / 10 = 23 days, below its 30-day policy.
        result = self.run_forecast(270)
        first, second = self.camera(result, self.d1), self.camera(result, self.d2)
        self.assertAlmostEqual(first["write_bytes_per_day"] / GB, 10, delta=0.5)
        self.assertAlmostEqual(first["forecast_days"], 23, delta=23 * 0.05)
        self.assertEqual((first["state"], first["below_policy"], first["message"]), ("below_policy", True, "Dock 1's disk can hold about 23 days at the current write rate, below its 30-day policy"))
        self.assertEqual((second["forecast_days"], second["state"], second["below_policy"]), (14.0, "ok", False))
        self.assertEqual(second["message"], "Dock 2 keeps its 14-day policy")
        (disk,) = result["disks"]
        self.assertFalse(disk["fits"])
        for field, expected in (("budget_bytes", 300 * GB), ("needed_bytes", 370 * GB), ("shortfall_bytes", 70 * GB)):
            self.assertAlmostEqual(disk[field], expected, delta=10_000, msg=field)  # Whole bytes per hourly file.

    def test_when_everything_fits_every_camera_keeps_its_policy(self):
        self.two_cameras()
        result = self.run_forecast(1000)
        self.assertEqual([(camera["forecast_days"], camera["below_policy"]) for camera in result["cameras"] if camera["id"] in (self.d1, self.d2)], [(30.0, False), (14.0, False)])
        self.assertTrue(result["disks"][0]["fits"])

    def test_the_days_until_the_shortfall_begins_follow_the_footage_growing_to_its_policy(self):
        self.two_cameras()
        far = self.camera(self.run_forecast(270), self.d1)
        self.assertAlmostEqual(far["days_until_shortfall"], 21.0, delta=21 * 0.05)  # 70 + 10 * (2 + t) = 300
        near = self.camera(self.run_forecast(90), self.d1)  # Budget 120 GB: 15 * (2 + t) = 120.
        self.assertAlmostEqual(near["days_until_shortfall"], 6.0, delta=6 * 0.05)
        self.assertAlmostEqual(near["forecast_days"], 8.0, delta=0.4)  # 120 / 15

    def test_the_reserve_is_not_counted_as_room(self):
        self.two_cameras()
        with mock.patch.dict(os.environ, {"NVR_STORAGE_PRESSURE_FREE_BYTES": str(20 * GB)}):
            result = self.run_forecast(290)  # 290 free less the 20 GB reserve, plus 30 stored: 300.
        self.assertAlmostEqual(result["disks"][0]["budget_bytes"], 300 * GB, delta=10_000)

    def test_volumes_on_one_disk_share_a_budget_and_volumes_on_two_disks_do_not(self):
        with connect() as db:
            db.execute("UPDATE camera_groups SET storage_volume_id='archive' WHERE id=?", (self.yard,))
        self.write(self.d1, 10)
        self.write(self.y1, 10)
        self.policy(self.d1, 20)
        self.policy(self.y1, 20)
        environment = {"NVR_STORAGE_VOLUMES": json.dumps({"default": "", "archive": "archive"})}
        Path(self.environment["NVR_RECORDINGS_PATH"], "archive").mkdir(exist_ok=True)
        with mock.patch.dict(os.environ, environment), mock.patch("app.storage.os.path.ismount", return_value=True):
            shared = self.run_forecast(180, {"default": "one", "archive": "one"})
            apart = self.run_forecast(180, {"default": "one", "archive": "two"})
        # Stored 20 GB each. Shared: one budget of 180 + 40 = 220 for 400 needed. Apart: 200 each for 200 needed.
        self.assertEqual(len(shared["disks"]), 1)
        self.assertAlmostEqual(shared["disks"][0]["budget_bytes"], 220 * GB, delta=10_000)
        self.assertAlmostEqual(self.camera(shared, self.d1)["forecast_days"], 11.0, delta=0.6)
        self.assertEqual(len(apart["disks"]), 2)
        self.assertTrue(all(disk["fits"] for disk in apart["disks"]))

    def test_a_default_volume_that_does_not_exist_yet_uses_the_disk_it_will_be_created_on(self):
        self.two_cameras()
        root = Path(self.environment["NVR_RECORDINGS_PATH"])
        with mock.patch("app.storage.volume_path", return_value=root / "not" / "yet"):
            result = self.run_forecast(270)
        self.assertEqual(self.camera(result, self.d1)["state"], "below_policy")
        self.assertEqual(len(result["disks"]), 1)

    def test_a_camera_without_enough_history_is_not_forecast(self):
        self.write(self.d1, 10, hours=3, ends=NOW)
        camera = self.camera(self.run_forecast(100), self.d1)
        self.assertEqual((camera["state"], camera["forecast_days"], camera["below_policy"]), ("not_forecast", None, False))
        self.assertIn("at least 6 hours", camera["message"])

    def test_an_archived_camera_is_not_forecast_but_its_footage_still_counts(self):
        self.write(self.d1, 10)
        self.write(self.d2, 5)
        self.policy(self.d2, 14)
        with connect() as db:
            db.execute("UPDATE cameras SET archived_at=? WHERE id=?", (NOW.isoformat(), self.d1))
        result = self.run_forecast(100)
        self.assertEqual(self.camera(result, self.d1)["state"], "not_forecast")
        self.assertAlmostEqual(result["disks"][0]["budget_bytes"], 130 * GB, delta=10_000)

    def test_a_camera_that_is_not_writing_is_not_below_policy(self):
        self.write(self.d1, 10)
        self.write(self.d2, 5, ends=NOW - timedelta(days=5))  # Recording was switched off five days ago: it writes nothing now.
        self.policy(self.d1, 30)
        self.policy(self.d2, 14)
        camera = self.camera(self.run_forecast(100), self.d2)
        self.assertEqual((camera["state"], camera["below_policy"], camera["forecast_days"]), ("not_forecast", False, None))
        self.assertIn("Not writing", camera["message"])

    def test_footage_under_a_hold_is_not_room_the_disk_gets_back(self):
        self.two_cameras()
        before = self.run_forecast(270)["disks"][0]["budget_bytes"]
        with connect() as db:
            segment = db.execute("SELECT id,size_bytes FROM recording_segments WHERE camera_id=? LIMIT 1", (self.d1,)).fetchone()
            db.execute("INSERT INTO recording_holds(id,segment_id,camera_id) VALUES(?,?,?)", (str(uuid.uuid4()), segment["id"], self.d1))
        self.assertEqual(before - self.run_forecast(270)["disks"][0]["budget_bytes"], segment["size_bytes"])

    def test_motion_cameras_are_forecast_from_what_they_write_not_from_time_recording(self):
        self.write(self.d1, 2, hours=12)  # Twelve segments in two days' window of history: quiet most of the time.
        with connect() as db:
            db.execute("UPDATE recording_segments SET starts_at=?,ends_at=? WHERE id=(SELECT MIN(id) FROM recording_segments)", ((NOW - timedelta(hours=48)).isoformat(), (NOW - timedelta(hours=47)).isoformat()))
        camera = self.camera(self.run_forecast(100), self.d1)
        self.assertLess(camera["write_bytes_per_day"] / GB, 1.0)  # Its 12 hourly files over 48 hours, not 2 GB a day.


class SuggestionTests(ForecastCase):
    def test_a_short_camera_is_told_how_to_fit(self):
        self.two_cameras()
        suggestions = self.camera(self.run_forecast(270), self.d1)["suggestions"]
        self.assertTrue(any("Lower Dock 1's bitrate by about 23%" in text and "GB a day" in text for text in suggestions), suggestions)
        self.assertTrue(any(text.startswith("Shorten the retention of") for text in suggestions) or len(suggestions) >= 1)

    def test_another_disk_with_room_is_suggested(self):
        with connect() as db:
            db.execute("UPDATE camera_groups SET storage_volume_id='archive' WHERE id=?", (self.yard,))
        self.write(self.d1, 10)
        self.policy(self.d1, 30)
        environment = {"NVR_STORAGE_VOLUMES": json.dumps({"default": "", "archive": "archive"})}
        Path(self.environment["NVR_RECORDINGS_PATH"], "archive").mkdir(exist_ok=True)
        with mock.patch.dict(os.environ, environment), mock.patch("app.storage.os.path.ismount", return_value=True), connect() as db:
            result = forecast.forecast(db, NOW, usage=lambda path: 100 * GB if path.name != "archive" else 900 * GB, device_of=lambda path: path.name or "default")
        suggestions = self.camera(result, self.d1)["suggestions"]
        self.assertTrue(any(text.startswith("Move Dock 1 to the volume archive") for text in suggestions), suggestions)

    def test_cameras_that_fit_get_no_suggestions(self):
        self.two_cameras()
        self.assertEqual(self.camera(self.run_forecast(1000), self.d1)["suggestions"], [])


class ForecastIncidentTests(ForecastCase):
    def sync(self, free_gb: float) -> None:
        with connect() as db:
            forecast.sync_incidents(db, forecast.forecast(db, NOW, usage=lambda path: int(free_gb * GB), device_of=lambda path: "disk"))

    def incident(self, camera: str) -> dict | None:
        rows = [row for row in self.incidents("retention_forecast") if row["camera_id"] == camera]
        return rows[0] if rows else None

    def test_a_camera_that_will_fall_below_policy_within_a_week_raises_an_incident_that_recovers_when_fixed(self):
        self.two_cameras()
        self.sync(90)  # Shortfall in 6 days.
        incident = self.incident(self.d1)
        self.assertTrue(incident["fault_active"])
        self.assertEqual(incident["summary"], "Dock 1's disk can hold about 8 days at the current write rate, below its 30-day policy")
        self.assertIn("Lower Dock 1's bitrate", json.loads(incident["details_json"])["suggestions"][0])
        self.assertTrue(self.incident(self.d2)["fault_active"])  # The shorter-policy camera is held to the same level (8 of its 14 days).
        self.sync(1000)  # Fixed: more room.
        incident = self.incident(self.d1)
        self.assertEqual((incident["state"], incident["fault_active"]), ("recovered", 0))
        self.sync(90)
        self.assertEqual(self.incident(self.d1)["recurrence_count"], 2)

    def test_a_shortfall_further_away_than_a_week_is_shown_but_raises_nothing(self):
        self.two_cameras()
        self.sync(270)  # Below policy, but 21 days away.
        self.assertIsNone(self.incident(self.d1))
        with connect() as db:
            shown = forecast.forecast(db, NOW, usage=lambda path: 270 * GB, device_of=lambda path: "disk")
        self.assertTrue(next(camera for camera in shown["cameras"] if camera["id"] == self.d1)["below_policy"])

    def test_shortening_the_policy_fixes_it(self):
        self.two_cameras()
        self.sync(90)
        self.assertTrue(self.incident(self.d1)["fault_active"])
        self.policy(self.d1, 7)
        self.policy(self.d2, 7)
        self.sync(90)  # 15 GB/day for 7 days is 105 GB, within the 120 GB budget.
        self.assertFalse(self.incident(self.d1)["fault_active"])

    def test_the_incident_ends_when_the_camera_is_archived(self):
        self.two_cameras()
        self.sync(90)
        with connect() as db:
            db.execute("UPDATE cameras SET archived_at=? WHERE id=?", (NOW.isoformat(), self.d1))
        self.sync(90)
        self.assertFalse(self.incident(self.d1)["fault_active"])

    def test_a_forecast_incident_reaches_storage_webhooks(self):
        from app import webhooks
        webhooks.create_endpoint("Storage hook", "https://93.184.216.34/in", ["storage.failure", "storage.recovered"], False, 1)
        self.two_cameras()
        self.sync(90)  # Both cameras share the shortfall: an incident each.
        self.sync(1000)
        with connect() as db:
            events = [row[0] for row in db.execute("SELECT event_type FROM webhook_deliveries ORDER BY created_at,id")]
        self.assertEqual((events.count("storage.failure"), events.count("storage.recovered"), len(events)), (2, 2, 4))

    def test_the_recorders_pass_runs_the_forecast_and_the_disk_reading_and_survives_their_failure(self):
        from app.recording import RecordingManager
        self.two_cameras()
        with mock.patch("app.smart.refresh", return_value={}) as refresh, mock.patch("app.forecast.forecast", wraps=forecast.forecast):
            RecordingManager._watch_disks()
        self.assertTrue(refresh.called)  # Never read: it was due.
        with mock.patch("app.forecast.forecast", side_effect=RuntimeError("broke")), self.assertLogs("nvr.recording", "ERROR"):
            RecordingManager._watch_disks()  # Does not raise: the retention pass that shares the loop goes on.


class CameraApiTests(ForecastCase):
    def test_a_camera_reads_its_own_forecast_and_disk(self):
        now = datetime.now(timezone.utc)  # The route forecasts from the real clock.
        self.write(self.d1, 10, ends=now)
        self.write(self.d2, 5, ends=now)
        self.policy(self.d1, 30)
        self.policy(self.d2, 14)
        with mock.patch("app.forecast.shutil.disk_usage", return_value=mock.Mock(free=270 * GB)):
            response = self.admin.get(f"/api/cameras/{self.d1}/retention-forecast")
            restricted = self.limited.get(f"/api/cameras/{self.d1}/retention-forecast")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        # Someone who may see this camera but not all of them learns nothing of the others: no disk totals, no suggestions that name them.
        self.assertEqual(restricted.status_code, 200, restricted.text)
        self.assertEqual((restricted.json()["camera"]["id"], restricted.json()["camera"]["suggestions"], restricted.json()["disk"]), (self.d1, [], None))
        self.assertTrue(body["camera"]["suggestions"], body["camera"])
        self.assertEqual(body["camera"]["id"], self.d1)
        self.assertIn("can hold", body["camera"]["message"])
        self.assertEqual(body["disk"]["volume_ids"], ["default"])
        self.assertEqual(body["warn_days"], 7)

    def test_a_camera_someone_cannot_see_does_not_exist_for_them(self):
        hidden = self.limited.get(f"/api/cameras/{self.y1}/retention-forecast")
        missing = self.limited.get("/api/cameras/no-such-camera/retention-forecast")
        self.assertEqual((hidden.status_code, hidden.json()), (404, missing.json()))

    def test_the_whole_forecast_is_for_administrators(self):
        self.two_cameras()
        response = self.admin.get("/api/storage/forecast")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual({camera["id"] for camera in response.json()["cameras"]} >= {self.d1, self.d2}, True)

    def test_forecasts_are_metrics(self):
        self.two_cameras()
        from app import prometheus
        text = prometheus.render(False)
        self.assertIn(f'nvr_camera_retention_policy_days{{camera_id="{self.d1}",camera="Dock 1"}} 30', text)
        self.assertIn("nvr_camera_retention_forecast_days", text)


class UpgradeTests(unittest.TestCase):
    def test_the_migration_creates_the_table(self):
        import sqlite3
        from app.migrations import m0022_disk_health
        db = sqlite3.connect(":memory:")
        m0022_disk_health.upgrade(db)
        db.execute("INSERT INTO disk_health(volume_id,available,state,checked_at) VALUES('default',0,'unavailable','2026-01-01')")
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("INSERT INTO disk_health(volume_id,available,state,checked_at) VALUES('x',1,'great','2026-01-01')")


if __name__ == "__main__":
    unittest.main()
