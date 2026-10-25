# Rules

A rule watches cameras for people or vehicles doing one thing, and records a **rule event** when it happens. Rules read the structured detections and tracks the AI worker stores (see [AI detection](ai-detection.md)), so a camera must have AI analysis enabled for its rules to see anything. Administrators manage rules in **Cameras → Rules**; operators do not see the section.

## What a rule contains

- **Cameras.** Individual cameras, and groups: a group includes every camera that uses it as its policy group or appears in it in Live, now and later.
- **Condition**, evaluated on the centre of each object's box:
  - **Enters a zone.** The object is inside the zone. An object that first appears inside it counts as entering.
  - **Crosses a line.** The object crosses the drawn line segment between A and B. Choose *either direction*, *forward* (the direction of the arrow the editor draws) or *reverse*. Crossing the line's extension beyond A or B does not count.
  - **Stays in a zone.** The object stays inside the zone for at least the dwell time. Leaving and coming back starts the time again.
  - **Moves the wrong way.** Inside the zone, the object travels back a tenth of the frame against the expected direction (rightwards, leftwards, downwards or upwards) from the furthest point it reached.
- **Objects**: people, vehicles or both.
- **When** (optional): weekly intervals in a time zone, using the same rules as recording schedules. Outside them the rule does not fire.
- **Cooldown** (optional): no two of the rule's events on the same camera are closer together than this.
- **Pop alarms into Live** (optional): the rule's alarms bring their camera and its neighbours into Live for people who opted in, and onto video walls (see [alarms in Live and video walls](live-alarms-and-video-wall.md)).

Zones and lines use fractions of the frame and are drawn over a recorded frame from the first camera the rule watches. Every coordinate can be typed. With the keyboard, arrows move a zone or a line end (Shift for larger steps), and Alt+arrows resize a zone.

## One event per object

The rules worker evaluates each object's whole track, so an object produces at most one event per rule, however many frames it meets the condition in. The event is the earliest moment that meets the condition, falls inside the schedule and is not within the cooldown of another event on that camera. It records the rule, camera, track, object class, time and box. Each event raises an [alarm](alarms.md).

A saved rule never fires for moments before it was saved. Editing, enabling or disabling a rule applies to the running worker within a second, without a restart. Upgrading an installation does not evaluate detections stored before the upgrade.

## Actions

Besides raising an alarm, each event can run up to 10 actions. Add them in the rule's **Actions** section:

| Action | What it sends |
|---|---|
| **Signed webhook** | The event as JSON (`event_type` `rule.triggered`), signed exactly like other [webhooks](webhooks.md): `X-NVR-Signature: sha256=…` over `<X-NVR-Timestamp>.<body>` with the action's signing secret (at least 16 characters). |
| **MQTT message** | The same JSON, published at QoS 1 to a topic (optionally retained). The broker is `mqtts://user:password@host:8883`, or `mqtt://` for an authorised private IP address. |
| **Slack message** / **Microsoft Teams message** | An incoming-webhook message: "Gate entry: Vehicle on Loading dock at 10:00:00 UTC", with snapshot and alarm links when `NVR_PUBLIC_URL` is set. The snapshot link expires after 2 hours and names only that alarm. |
| **Camera relay output** | An ONVIF relay pulse on a chosen camera: switched on, held for 0.1 to 60 seconds, then switched off. It uses the camera's first relay unless you name its token. The camera needs an ONVIF device service. |

- **Destinations** follow the webhook rules: public HTTPS (or `mqtts://` for brokers), or a literal private IP address when you tick **Allow a private IP address**. Loopback and link-local addresses are always refused. Destinations are checked when saved and again before each delivery.
- **Secrets** are write-only: webhook, Slack and Teams URLs, broker credentials and signing secrets. They are encrypted with the credential key, like camera credentials, and never returned by the API, written to logs or included in diagnostics bundles. A saved action shows only where it delivers (for example `https://hooks.slack.com` or `mqtts://broker:8883`). Leave a secret empty when editing to keep it.
- **Delivery.** The background worker queues each action once per event and runs deliveries side by side, so a long relay hold never delays a message. Delivery is at least once: a receiver that answers too late may get a retry, so use `event_id` to ignore repeats.
  - **Retries:** connection failures, `429` and `5xx` answers are retried with backoff (2 s, 5 s, 15 s, 1 min, 5 min, 30 min, then hourly; 8 attempts in all).
  - **No retry:** other refusals fail at once, for example a `404`, a broker that rejects the credentials (`mqtt_refused_5`) or a camera with no relay outputs. A relay is always switched off again (retried a few times); one that cannot be is reported as `relay_not_released` and never switched on again by a retry.
  - **Log:** the rule's **Action deliveries** log shows each delivery's status, attempts and last error code for 30 days.
- **Test mode** lists the actions each event would run and runs none of them.

## Test mode

**Test against the last 24 hours** replays the stored detections of the last 24 hours through the rule as it is in the editor, saved or not, and lists every event it would have created. It creates nothing and calls nothing. Time a camera was not analysed cannot match, so "would not have fired" covers analysed frames only.

## API

All rule endpoints require an administrator session, or an API token with the `admin` scope.

```http
GET    /api/rules
POST   /api/rules
GET    /api/rules/{rule_id}
PUT    /api/rules/{rule_id}
DELETE /api/rules/{rule_id}
POST   /api/rules/test
GET    /api/rule-events[?rule_id=<id>][&camera_id=<id>][&limit=200]
```

A rule body:

```json
{
  "name": "Corridor eastbound",
  "enabled": true,
  "camera_ids": ["<camera-id>"],
  "group_ids": [],
  "condition": {"type": "line_crossing", "line": {"x1": 0.5, "y1": 0.1, "x2": 0.45, "y2": 0.9}, "direction": "forward"},
  "classes": ["person"],
  "schedule": {"timezone": "Europe/London", "intervals": [{"days": [0, 1, 2, 3, 4], "start": "18:00", "end": "07:00"}]},
  "cooldown_seconds": 60,
  "pop_into_live": false,
  "actions": [
    {"type": "slack", "name": "Slack #security", "url": "https://hooks.slack.com/services/…"},
    {"type": "mqtt", "name": "Building bus", "url": "mqtts://nvr:…@broker.example:8883", "topic": "site/alarms"},
    {"type": "relay", "name": "Gate siren", "camera_id": "<camera-id>", "duration_seconds": 2}
  ]
}
```

Actions are returned with an `id` and a `target`, never their `url` or `signing_secret`. Send an action back with its `id` to keep its saved secrets, and leave an action out to remove it. `GET /api/rules/{id}/deliveries` lists the rule's action deliveries, newest first.

The other conditions are `{"type": "zone_entry", "zone": {"x", "y", "width", "height"}}`, `{"type": "loitering", "zone": {...}, "dwell_seconds": 30}` and `{"type": "wrong_way", "zone": {...}, "direction": "right"}`. Creating, changing and deleting a rule is recorded in the audit log. Deleting a rule keeps its past events, which still carry the rule's name.

## Reliability

Rules run in the supervised `rules` worker process (see [architecture](architecture.md)). If it stops, a `worker_rules` incident is raised and the worker is restarted. Detections stored while it was down are evaluated when it is back, because the worker keeps its place by detection rather than by time.
