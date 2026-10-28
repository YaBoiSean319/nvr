"""Home Assistant over MQTT (#138): the NVR announces its cameras and alarms by MQTT discovery, publishes their state and snapshots, and accepts events from it.

With a broker address and a discovery prefix (``homeassistant`` unless changed) set, the NVR publishes retained *discovery* messages that Home Assistant turns
into entities, one set per live camera:

- a **camera** entity whose picture is the snapshot of the camera's latest alarm (``<node>/<camera id>/snapshot``);
- a **binary sensor** ("alarm") that is ON while the camera has an alarm not yet resolved, with the latest alarm's details as attributes;

and one **sensor** for the number of open alarms, which is the device the cameras hang from. Availability follows ``<node>/status``. The messages follow
Home Assistant's MQTT discovery schema (topic ``<prefix>/<component>/<node>/<object>/config``, JSON config with ``unique_id``, ``device`` and the topics).

Events come back the other way: anything Home Assistant (an automation, say) publishes to the *events topic* as a JSON event is taken in like any other source's
events (see ``external_events``), so doors, motion sensors and buttons it knows can be mapped to cameras and raise alarms.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

from . import mqtt, rule_actions
from .credentials import decrypt, encrypt
from .external_events import HOME_ASSISTANT_SOURCE, SourceError
from .store import connect

logger = logging.getLogger("nvr.home_assistant")

PREFIX_PATTERN = re.compile(r"^[A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)*$")
NODE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
MANUFACTURER = "Workplace NVR"
SYNC_SECONDS = 5.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- settings ---------------------------------------------------------------------------------------------------------------------------------

def settings_view(db: sqlite3.Connection) -> dict[str, Any]:
    """The settings as the administrator sees them: never the broker address or login."""
    row = db.execute("SELECT * FROM home_assistant_settings WHERE singleton_id=1").fetchone()
    return {"enabled": bool(row["enabled"]), "configured": row["broker_ciphertext"] is not None, "broker": _target(row), "allow_private": bool(row["allow_private"]),
            "discovery_prefix": row["discovery_prefix"], "node_id": row["node_id"], "events_topic": row["events_topic"], "updated_at": row["updated_at"]}


def _target(row: sqlite3.Row) -> str | None:
    if row["broker_ciphertext"] is None:
        return None
    try:
        return rule_actions.mqtt_target(decrypt(row["broker_ciphertext"]))
    except ValueError:
        return None


def save_settings(db: sqlite3.Connection, fields: dict[str, Any]) -> None:
    """Change the Home Assistant settings inside the caller's transaction. A broker address left out is kept as saved."""
    row = db.execute("SELECT * FROM home_assistant_settings WHERE singleton_id=1").fetchone()
    prefix = str(fields.get("discovery_prefix", row["discovery_prefix"])).strip().strip("/")
    node = str(fields.get("node_id", row["node_id"])).strip()
    events = str(fields.get("events_topic", row["events_topic"])).strip().rstrip("/")
    if not PREFIX_PATTERN.match(prefix):
        raise SourceError("The discovery prefix is topic words separated by /, usually homeassistant")
    if not NODE_PATTERN.match(node):
        raise SourceError("The node id is letters, digits, _ and - (up to 32)")
    try:
        mqtt.validate_topic(events)
    except ValueError as exc:
        raise SourceError(str(exc)) from exc
    allow_private = bool(fields.get("allow_private", row["allow_private"]))
    ciphertext = row["broker_ciphertext"]
    if fields.get("url"):
        try:
            rule_actions._check_broker(fields["url"].strip(), allow_private)
        except ValueError as exc:
            raise SourceError(str(exc)) from exc
        ciphertext = encrypt(fields["url"].strip())
    enabled = bool(fields.get("enabled", row["enabled"]))
    if enabled and ciphertext is None:
        raise SourceError("Enter the broker as mqtt://host:1883 or mqtts://host:8883 before turning Home Assistant on")
    db.execute("UPDATE home_assistant_settings SET enabled=?,broker_ciphertext=?,allow_private=?,discovery_prefix=?,node_id=?,events_topic=?,updated_at=? WHERE singleton_id=1",
               (int(enabled), ciphertext, int(allow_private), prefix, node, events, _now()))
    # Its events arrive as a source of their own, so they can be mapped to cameras like any other's.
    db.execute("INSERT OR IGNORE INTO event_sources(id,name,kind,enabled,settings_json) VALUES(?,?,?,?,?)", (HOME_ASSISTANT_SOURCE, "Home Assistant", "home_assistant", int(enabled), "{}"))
    db.execute("UPDATE event_sources SET enabled=?,target=?,updated_at=? WHERE id=?", (int(enabled), _target(db.execute("SELECT * FROM home_assistant_settings").fetchone()), _now(), HOME_ASSISTANT_SOURCE))


# --- what is published ------------------------------------------------------------------------------------------------------------------------

def _device(node: str, identifier: str, name: str, *, hub: bool = False) -> dict[str, Any]:
    device: dict[str, Any] = {"identifiers": [f"{node}_{identifier}"], "name": name, "manufacturer": MANUFACTURER, "model": "NVR" if hub else "Camera"}
    if not hub:
        device["via_device"] = f"{node}_hub"
    return device


def discovery(db: sqlite3.Connection, settings: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """The retained discovery messages as (topic, config): the hub's open-alarm sensor, and a camera and an alarm binary sensor for each live camera."""
    node, prefix = settings["node_id"], settings["discovery_prefix"]
    availability = {"availability_topic": f"{node}/status", "payload_available": "online", "payload_not_available": "offline"}
    messages: list[tuple[str, dict[str, Any]]] = [(
        f"{prefix}/sensor/{node}/open_alarms/config",
        {"name": "Open alarms", "unique_id": f"{node}_open_alarms", "state_topic": f"{node}/alarms/open", "unit_of_measurement": "alarms", "icon": "mdi:alarm-light",
         "device": _device(node, "hub", "Workplace NVR", hub=True), **availability})]
    for camera in db.execute("SELECT id,name FROM cameras WHERE archived_at IS NULL ORDER BY name COLLATE NOCASE,id"):
        device = _device(node, camera["id"], camera["name"])
        messages.append((f"{prefix}/camera/{node}/{camera['id']}/config",
                         {"name": f"{camera['name']} snapshot", "unique_id": f"{node}_{camera['id']}_snapshot", "topic": f"{node}/{camera['id']}/snapshot", "device": device, **availability}))
        messages.append((f"{prefix}/binary_sensor/{node}/{camera['id']}_alarm/config",
                         {"name": f"{camera['name']} alarm", "unique_id": f"{node}_{camera['id']}_alarm", "state_topic": f"{node}/{camera['id']}/alarm", "payload_on": "ON", "payload_off": "OFF",
                          "device_class": "safety", "json_attributes_topic": f"{node}/{camera['id']}/alarm/attributes", "device": device, **availability}))
    return messages


def alarm_states(db: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """For each live camera: whether it has an alarm not yet resolved, and the latest one."""
    states = {row["id"]: {"on": False, "open": 0, "latest": None} for row in db.execute("SELECT id FROM cameras WHERE archived_at IS NULL")}
    for row in db.execute("SELECT id,camera_id,rule_name,label,occurred_at,state FROM alarms WHERE state!='resolved' ORDER BY occurred_at,id"):
        if row["camera_id"] in states:
            state = states[row["camera_id"]]
            state["on"], state["open"] = True, state["open"] + 1
            state["latest"] = {"alarm_id": row["id"], "rule_name": row["rule_name"], "label": row["label"], "occurred_at": row["occurred_at"], "state": row["state"]}
    return states


def _broker(db: sqlite3.Connection) -> tuple[mqtt.Broker, dict[str, Any]] | None:
    row = db.execute("SELECT * FROM home_assistant_settings WHERE singleton_id=1").fetchone()
    if not row["enabled"] or row["broker_ciphertext"] is None:
        return None
    try:
        return mqtt.parse_broker(decrypt(row["broker_ciphertext"])), settings_view(db)
    except ValueError:
        return None


class HomeAssistantManager:
    """Publishes the discovery messages and keeps the alarm states and snapshots current, republishing everything when the settings or cameras change."""

    def __init__(self, sync_seconds: float = SYNC_SECONDS) -> None:
        self.sync_seconds = sync_seconds
        self.task: asyncio.Task[None] | None = None
        self.config_hash: str | None = None
        self.published: dict[str, str] = {}
        self.snapshots: dict[str, str] = {}
        self.broker: mqtt.Broker | None = None
        self.node = "nvr"

    async def start(self) -> None:
        self.task = asyncio.create_task(self._run(), name="home-assistant")

    async def stop(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.task = None
        if self.broker is not None:
            try:
                await mqtt.publish(self.broker, f"{self.node}/status", b"offline", qos=1, retain=True)
            except (OSError, ConnectionError, asyncio.TimeoutError, mqtt.MqttRefused):
                pass

    async def sync(self) -> int:
        """One pass: publish what changed. Returns the number of messages published."""
        def read() -> tuple[tuple[mqtt.Broker, dict[str, Any]] | None, list[tuple[str, dict[str, Any]]], dict[str, dict[str, Any]]]:
            with connect() as db:
                found = _broker(db)
                if found is None:
                    return None, [], {}
                return found, discovery(db, found[1]), alarm_states(db)
        found, configs, states = await asyncio.to_thread(read)
        if found is None:
            self.config_hash = None
            return 0
        broker, settings = found
        node = settings["node_id"]
        digest = hashlib.sha256(json.dumps([settings, configs], sort_keys=True).encode()).hexdigest()
        sent = 0
        async def publish(topic: str, payload: bytes | str, *, retain: bool = True) -> None:
            nonlocal sent
            await mqtt.publish(broker, topic, payload if isinstance(payload, bytes) else payload.encode("utf-8"), qos=1, retain=retain)
            sent += 1
        if digest != self.config_hash:
            self.published.clear()  # A new broker or new discovery: everything is said again
            for topic, config in configs:
                await publish(topic, json.dumps(config, sort_keys=True, separators=(",", ":")))
            await publish(f"{node}/status", "online")
            self.config_hash, self.broker, self.node = digest, broker, node
        total = sum(state["open"] for state in states.values())
        for key, payload in {f"{node}/alarms/open": str(total), **{f"{node}/{camera}/alarm": "ON" if state["on"] else "OFF" for camera, state in states.items()},
                             **{f"{node}/{camera}/alarm/attributes": json.dumps({"open_alarms": state["open"], **(state["latest"] or {})}, sort_keys=True) for camera, state in states.items()}}.items():
            if self.published.get(key) != payload:
                await publish(key, payload)
                self.published[key] = payload
        for camera, state in states.items():
            latest = state["latest"]["alarm_id"] if state["latest"] else None
            if latest and self.snapshots.get(camera) != latest:
                from .push import thumbnail
                image = await asyncio.to_thread(thumbnail, latest)
                if image:
                    await publish(f"{node}/{camera}/snapshot", image)
                self.snapshots[camera] = latest
        return sent

    async def _run(self) -> None:
        delay = self.sync_seconds
        while True:
            try:
                await self.sync()
                delay = self.sync_seconds
            except asyncio.CancelledError:
                raise
            except (OSError, ConnectionError, asyncio.TimeoutError, mqtt.MqttRefused) as exc:
                self.config_hash = None  # Say everything again once the broker is back
                delay = min(delay * 2, 60.0)
                logger.warning("Home Assistant's broker could not be reached (%s)", type(exc).__name__)
            except Exception:
                logger.exception("Home Assistant sync failed")
            await asyncio.sleep(delay)
