"""ONVIF motion events (#173): one motion state from many detectors, subscriptions that are renewed and recovered, and honest status."""
from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone
from unittest import mock

from app import onvif_events
from app.onvif_client import OnvifClient
from app.onvif_events import CameraMotionEvent, MotionAggregator, normalise_topic, probe_motion, subscribe_and_pull
from tests.fixtures.onvif_camera import OnvifCamera

USER, PASSWORD = "operator", "S3cret!pass"
CELL = "tns1:RuleEngine/CellMotionDetector/Motion"
ALARM = "tns1:VideoSource/MotionAlarm"


def event(topic: str, active: bool, source_key: str = "") -> CameraMotionEvent:
    return CameraMotionEvent(active, datetime(2026, 10, 20, 12, tzinfo=timezone.utc), topic, source_key=source_key)


class AggregatorTests(unittest.TestCase):
    def test_the_same_movement_on_two_topics_is_one_start_and_one_end(self):
        merged = MotionAggregator()
        results = [merged.feed(item) for item in (event(CELL, True), event(ALARM, True), event(CELL, False), event(ALARM, False))]
        self.assertEqual([item.active if item else None for item in results], [True, None, None, False])

    def test_repeats_of_the_same_state_are_not_new_transitions(self):
        merged = MotionAggregator()
        self.assertTrue(merged.feed(event(CELL, True)).active)
        self.assertIsNone(merged.feed(event(CELL, True)))
        self.assertFalse(merged.feed(event(CELL, False)).active)
        self.assertIsNone(merged.feed(event(CELL, False)))

    def test_topics_that_differ_only_by_prefix_and_case_are_one_topic(self):
        self.assertEqual(normalise_topic("tns1:RuleEngine/CellMotionDetector/Motion"), normalise_topic("ruleengine/cellmotiondetector/MOTION"))
        merged = MotionAggregator()
        merged.feed(event("tns1:RuleEngine/CellMotionDetector/Motion", True))
        self.assertFalse(merged.feed(event("ruleengine/cellmotiondetector/motion", False)).active)

    def test_two_detectors_on_one_topic_keep_motion_until_both_end(self):
        merged = MotionAggregator()
        self.assertTrue(merged.feed(event(CELL, True, "Rule=a")).active)
        self.assertIsNone(merged.feed(event(CELL, True, "Rule=b")))
        self.assertIsNone(merged.feed(event(CELL, False, "Rule=a")))
        self.assertFalse(merged.feed(event(CELL, False, "Rule=b")).active)

    def test_a_new_active_detector_is_not_ignored_when_the_table_is_full_of_quiet_ones(self):
        merged = MotionAggregator()
        for number in range(MotionAggregator.MAX_DETECTORS):
            merged.feed(event(CELL, False, f"Rule={number}"))
        self.assertTrue(merged.feed(event(CELL, True, "Rule=new")).active)

    def test_a_camera_cannot_make_the_state_grow_without_limit(self):
        merged = MotionAggregator()
        for number in range(MotionAggregator.MAX_DETECTORS + 20):
            merged.feed(event(CELL, False, f"Rule={number}"))
        self.assertEqual(len(merged.states), MotionAggregator.MAX_DETECTORS)


class SubscriptionCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        patcher = mock.patch.object(onvif_events, "MIN_PULL_SECONDS", 0.05)  # The mock camera answers at once; the real floor is tested separately.
        patcher.start()
        self.addCleanup(patcher.stop)

    def camera(self, **options) -> OnvifCamera:
        camera = OnvifCamera(username=USER, password=PASSWORD, **options)
        self.addCleanup(camera.close)
        return camera

    async def start(self, camera: OnvifCamera, user: str = USER, password: str = PASSWORD):
        seen: list[CameraMotionEvent] = []
        statuses: list[str] = []

        async def on_event(item: CameraMotionEvent) -> None:
            seen.append(item)

        task = asyncio.create_task(subscribe_and_pull(camera.url, user, password, on_event, statuses.append))
        self._task = task
        self.addAsyncCleanup(self.stop, task)
        return task, seen, statuses

    @staticmethod
    async def stop(task: asyncio.Task) -> None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    @staticmethod
    async def until(condition, seconds: float = 6.0) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        while not condition():
            if loop.time() > deadline:
                raise AssertionError("Timed out waiting for the condition")
            await asyncio.sleep(0.05)


class SubscriptionTests(SubscriptionCase):
    async def test_motion_arrives_from_a_camera_that_wants_ws_security_for_events(self):
        camera = self.camera(auth={"device": "digest", "events": "wsse"})
        _, seen, statuses = await self.start(camera)
        await self.until(lambda: "healthy" in statuses)
        camera.motion(CELL, True)
        await self.until(lambda: seen)
        self.assertEqual((seen[0].active, seen[0].topic), (True, CELL))
        self.assertEqual(statuses[-1], "healthy")

    async def test_the_same_movement_on_two_topics_reaches_recording_once(self):
        camera = self.camera()
        _, seen, statuses = await self.start(camera)
        await self.until(lambda: "healthy" in statuses)
        camera.motion(CELL, True)
        camera.motion(ALARM, True)
        await self.until(lambda: not camera.pending)
        camera.motion(CELL, False)
        camera.motion(ALARM, False)
        await self.until(lambda: len(seen) >= 2)
        await asyncio.sleep(0.3)
        self.assertEqual([item.active for item in seen], [True, False])

    async def test_a_camera_that_checks_the_ws_addressing_action_is_subscribed_to_and_ends_cleanly(self):
        camera = self.camera(strict_actions=True)
        _, seen, statuses = await self.start(camera)
        await self.until(lambda: "healthy" in statuses)
        camera.motion(CELL, True)
        await self.until(lambda: seen)
        self.assertNotIn("unavailable", statuses)
        await self.stop(self._task)
        self.assertEqual(camera.unsubscribed, 1)

    async def test_a_hanwha_style_camera_reports_one_movement_on_two_topics_with_different_item_names(self):
        # Modelled on Hanwha Wisenet: VideoSource/MotionAlarm carries "State", VideoAnalytics/MotionDetection carries "IsMotion", both for one movement.
        camera = self.camera(auth={"device": "digest", "events": "wsse"})
        _, seen, statuses = await self.start(camera)
        await self.until(lambda: "healthy" in statuses)
        camera.motion("tns1:VideoSource/MotionAlarm", True, item="State")
        camera.motion("tns1:VideoAnalytics/MotionDetection", True, source="MotionRule")
        await self.until(lambda: not camera.pending)
        camera.motion("tns1:VideoAnalytics/MotionDetection", False, source="MotionRule")
        camera.motion("tns1:VideoSource/MotionAlarm", False, item="State")
        await self.until(lambda: len(seen) >= 2)
        await asyncio.sleep(0.3)
        self.assertEqual([item.active for item in seen], [True, False])

    async def test_a_camera_that_subscribes_but_fails_every_pull_is_never_called_healthy(self):
        camera = self.camera(behaviours={"PullMessages": "fault"})
        _, _, statuses = await self.start(camera)
        details: list[str] = []
        await self.until(lambda: "unavailable" in statuses)
        self.assertNotIn("healthy", statuses)

    async def test_the_reason_is_passed_on_in_words_without_the_login(self):
        camera = self.camera()
        details: list[str] = []
        statuses: list[str] = []
        task = asyncio.create_task(subscribe_and_pull(camera.url, USER, "not-the-password", mock.AsyncMock(), statuses.append, on_detail=details.append))
        self.addAsyncCleanup(self.stop, task)
        await self.until(lambda: "authentication_error" in statuses)
        self.assertTrue(details and "rejected" in details[-1].casefold())
        self.assertNotIn("not-the-password", " ".join(details))

    async def test_a_login_with_a_username_but_no_password_is_a_configuration_error(self):
        statuses: list[str] = []
        await subscribe_and_pull("http://127.0.0.1:1/onvif/device_service", USER, "", mock.AsyncMock(), statuses.append)
        self.assertEqual(statuses, ["configuration_error"])

    async def test_the_subscription_is_renewed_before_it_lapses_and_ended_when_the_task_stops(self):
        camera = self.camera()
        with mock.patch.object(onvif_events, "SUBSCRIPTION_SECONDS", 2):
            task, _, statuses = await self.start(camera)
            await self.until(lambda: any(item["operation"] == "Renew" for item in camera.requests), 5)
        await self.stop(task)
        self.assertGreaterEqual(camera.unsubscribed, 1)
        self.assertFalse(camera.subscription_alive)

    async def test_a_camera_that_forgets_the_subscription_is_subscribed_again_and_open_motion_is_ended(self):
        camera = self.camera()
        with mock.patch.object(onvif_events, "SUBSCRIPTION_SECONDS", 2):
            _, seen, statuses = await self.start(camera)
            await self.until(lambda: "healthy" in statuses)
            camera.motion(CELL, True)
            await self.until(lambda: seen)
            camera.drop_subscription()
            await self.until(lambda: camera.subscriptions == 2, 8)
        await self.until(lambda: len(seen) >= 2)
        self.assertEqual([item.active for item in seen], [True, False])
        self.assertIn(seen[1].topic, {"onvif/subscription-restarted", "onvif/subscription-lost"})

    async def test_a_renewal_the_camera_refuses_makes_a_new_subscription_without_going_unavailable(self):
        camera = self.camera(behaviours={"Renew": "fault"})
        with mock.patch.object(onvif_events, "SUBSCRIPTION_SECONDS", 2):
            _, seen, statuses = await self.start(camera)
            await self.until(lambda: "healthy" in statuses)
            camera.motion(CELL, True)
            await self.until(lambda: seen)
            await self.until(lambda: camera.subscriptions == 2 and len(seen) >= 2, 8)
        self.assertEqual(seen[1].topic, "onvif/subscription-restarted")
        self.assertNotIn("unavailable", statuses)
        self.assertGreaterEqual(camera.unsubscribed, 1)  # The old subscription was ended, not left to lapse

    async def test_motion_open_when_the_camera_goes_away_is_ended_not_left_open(self):
        camera = self.camera()
        _, seen, statuses = await self.start(camera)
        await self.until(lambda: "healthy" in statuses)
        camera.motion(CELL, True)
        await self.until(lambda: seen)
        camera.behaviours["PullMessages"] = "http500"
        await self.until(lambda: len(seen) >= 2, 8)
        self.assertEqual([item.active for item in seen], [True, False])
        self.assertEqual(seen[1].topic, "onvif/subscription-lost")

    async def test_a_camera_that_names_its_subscription_in_the_address_query_is_pulled_correctly(self):
        camera = self.camera(subscription_style="query")
        _, seen, statuses = await self.start(camera)
        await self.until(lambda: "healthy" in statuses)
        camera.motion(CELL, True)
        await self.until(lambda: seen)

    async def test_a_camera_that_wants_reference_parameters_echoed_gets_them(self):
        camera = self.camera(subscription_style="reference")
        _, seen, statuses = await self.start(camera)
        await self.until(lambda: "healthy" in statuses)
        camera.motion(CELL, True)
        await self.until(lambda: seen)

    async def test_a_camera_that_answers_pulls_at_once_is_not_hammered(self):
        camera = self.camera()
        with mock.patch.object(onvif_events, "MIN_PULL_SECONDS", 0.5):
            _, _, statuses = await self.start(camera)
            await self.until(lambda: "healthy" in statuses)
            before = sum(item["operation"] == "PullMessages" for item in camera.requests)
            await asyncio.sleep(1.6)
        pulls = sum(item["operation"] == "PullMessages" for item in camera.requests) - before
        self.assertLessEqual(pulls, 5)

    async def test_a_detector_that_never_ends_does_not_hold_motion_open_for_ever(self):
        camera = self.camera()
        with mock.patch.object(onvif_events, "MAX_ACTIVE_SECONDS", 0.3):
            _, seen, statuses = await self.start(camera)
            await self.until(lambda: "healthy" in statuses)
            camera.motion(CELL, True)
            await self.until(lambda: len(seen) >= 2)
        self.assertEqual([item.active for item in seen], [True, False])

    async def test_a_camera_that_fails_is_reported_unavailable_then_healthy_when_it_recovers(self):
        camera = self.camera(behaviours={"CreatePullPointSubscription": "http500"})
        _, seen, statuses = await self.start(camera)
        await self.until(lambda: "unavailable" in statuses)
        camera.behaviours.clear()
        await self.until(lambda: statuses[-1] == "healthy", 8)
        camera.motion(CELL, True)
        await self.until(lambda: seen)

    async def test_a_refused_login_is_reported_and_not_retried_quickly(self):
        camera = self.camera()
        _, _, statuses = await self.start(camera, password="not-the-password")
        await self.until(lambda: "authentication_error" in statuses)
        before = len(camera.requests)
        await asyncio.sleep(1.5)
        self.assertEqual(len(camera.requests), before)  # The next try is minutes away: repeated failures can lock the account.

    async def test_a_camera_without_an_events_service_is_unsupported(self):
        camera = self.camera(services=("media",))
        task, _, statuses = await self.start(camera)
        await asyncio.wait_for(asyncio.shield(task), 5)
        self.assertEqual(statuses, ["unsupported"])

    async def test_an_address_with_a_login_in_it_is_refused(self):
        statuses: list[str] = []
        await subscribe_and_pull("http://user:pw@127.0.0.1/onvif/device_service", "", "", mock.AsyncMock(), statuses.append)
        self.assertEqual(statuses, ["configuration_error"])


class ProbeTests(SubscriptionCase):
    async def probe(self, camera: OnvifCamera) -> dict:
        client = OnvifClient("127.0.0.1", USER, PASSWORD)
        self.addAsyncCleanup(client.__aexit__)
        url = await client.locate_device(camera.url)
        services = await client.services(url)
        return await probe_motion(client, services.urls["events"])

    async def test_a_probe_subscribes_pulls_ends_and_lists_the_motion_topics(self):
        camera = self.camera()
        result = await self.probe(camera)
        self.assertTrue(result["ok"])
        self.assertEqual(result["topics"], ["ruleengine/cellmotiondetector/motion", "videoanalytics/motiondetection", "videosource/motionalarm"])
        self.assertEqual((camera.subscriptions, camera.unsubscribed), (1, 1))

    async def test_a_camera_that_lists_no_motion_topic_does_not_pass_the_probe(self):
        result = await self.probe(self.camera(event_topics=("Device/Temperature",)))
        self.assertEqual((result["ok"], result["code"]), (False, "no_motion_topic"))

    async def test_a_camera_that_does_not_list_its_topics_still_passes_when_it_subscribes(self):
        result = await self.probe(self.camera(behaviours={"GetEventProperties": "fault"}))
        self.assertTrue(result["ok"])

    async def test_a_probe_works_on_a_camera_that_checks_the_ws_addressing_action(self):
        result = await self.probe(self.camera(strict_actions=True))
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["topics"])

    async def test_a_probe_says_why_a_camera_would_not_subscribe(self):
        camera = self.camera(behaviours={"CreatePullPointSubscription": "fault"})
        result = await self.probe(camera)
        self.assertFalse(result["ok"])
        self.assertIn("CreatePullPointSubscription", result["reason"])


if __name__ == "__main__":
    unittest.main()
