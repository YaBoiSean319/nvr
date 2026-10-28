# Events from outside systems

Door controllers, sensors, intercoms and panic buttons can tell the NVR what happened. An administrator ties each event to the cameras that watch the place, and from then on the event:

- shows on the **Playback timeline** of those cameras (a red marker; click it to jump the playback to that moment),
- can raise an alarm through a **rule** whose condition is *An outside event*,
- is kept 90 days, mapped or not, so a mapping can be written from what really arrives (Settings → Event sources → Recent events).

Sources and their secrets are for administrators only. Secrets and broker logins are encrypted, never returned by the API and never shown again after saving.

## Event format

```json
{"event_id": "door-17-000123", "type": "door_forced", "subject": "main-door", "message": "Forced open", "occurred_at": "2026-03-01T08:15:00Z", "data": {"reader": 3}}
```

Only `type` is required. `subject` names the door, sensor or button and is what mappings match (`*` matches every subject). `event_id` makes a retry harmless: a repeat is refused with 409. Always send one: without it, two genuinely different events with the same body in the same second look like a replay. Usual types: `door_granted`, `door_denied`, `door_forced`, `door_held`, `intercom_call`, `panic_button`, `sensor`. Bodies are limited to 16 KiB.

## Signed HTTP

`POST /api/inbound/events/<source id>`, no sign-in. Sign the exact body with the source's secret:

```
X-NVR-Timestamp: <unix seconds>
X-NVR-Signature: sha256=<hex HMAC-SHA256 of "<timestamp>.<body>">
```

```python
import hashlib, hmac, json, time, urllib.request
body = json.dumps({"type": "door_forced", "subject": "main-door"}).encode()
stamp = str(int(time.time()))
sig = hmac.new(SECRET.encode(), stamp.encode() + b"." + body, hashlib.sha256).hexdigest()
urllib.request.urlopen(urllib.request.Request(URL, body, {"Content-Type": "application/json", "X-NVR-Timestamp": stamp, "X-NVR-Signature": "sha256=" + sig}))
```

The same answer (401) is given for an unknown source, a disabled one, a missing or wrong signature and a timestamp more than 5 minutes off. A repeated signature or `event_id` is 409, a body too large 413, more than 600 events a minute from one source 429.

## MQTT

Add an *MQTT subscription* source with the broker address (`mqtt://user:password@host:1883` or `mqtts://…`) and a topic filter such as `building/doors/#`. Whoever can publish to the broker is trusted, so give the NVR's login read access to only those topics. A JSON message in the format above is an event; any other message becomes an `mqtt_message` event whose subject is its topic.

## Home Assistant

Settings → Event sources → Home Assistant. With the broker address set, the NVR publishes retained MQTT discovery messages under `<prefix>/<component>/<node>/<object>/config` (prefix `homeassistant`, node `nvr` by default): a snapshot camera (the picture of the camera's latest alarm, blank until it has one) and an *alarm* binary sensor (device class `safety`, on while the camera has an unresolved alarm) per camera, an *Open alarms* sensor, and availability on `nvr/status`. Events go the other way: publish the event JSON to the events topic (`nvr/events`) and it arrives as the source *Home Assistant*, ready to map.

```yaml
automation:
  - alias: Front door forced to NVR
    trigger: {platform: state, entity_id: binary_sensor.front_door_forced, to: "on"}
    action:
      - service: mqtt.publish
        data:
          topic: nvr/events
          payload: '{"type": "door_forced", "subject": "front-door"}'
```

## Mapping guides

Vendor connectors are not built; most systems can post or publish through one of the above.

- **Door controllers with webhooks** (Brivo, Genea, Verkada Access, Openpath style): point the webhook at a small relay that signs the body, or use their MQTT/Home Assistant bridge. Use the door name as `subject`.
- **Open source access control** (for example through Home Assistant or Node-RED): publish events to the MQTT topic; set `subject` to the door.
- **Alarm panels and sensors via Home Assistant**: automation per sensor as above, `type: sensor`.
- **Intercoms**: send `intercom_call` with the station name as the subject, and map it to the camera that views the entrance.

Map one subject to several cameras to see the same event on each. Archived cameras are never mapped to.
