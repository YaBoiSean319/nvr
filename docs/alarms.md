# Alarms

An **alarm** is raised for every rule event (see [rules](rules.md)): one object meeting one rule. Alarms are separate from incidents, which stay system faults such as a camera that stopped recording.

## The inbox

The **Alarms** chip in the status area opens the inbox from every workspace. The chip shows how many alarms are new, or how many are still open. The inbox lists alarms newest first and filters by state (open, new, acknowledged, assigned, resolved or all), camera and rule. It refreshes every few seconds, so a change one person makes appears for everyone signed in without a reload.

Each alarm shows:

- a **snapshot** from the recorded footage at the alarm time;
- a **clip** from 10 seconds before to 20 seconds after it. Footage appears once the recording that covers it has been indexed, usually within seconds;
- **Open in Playback**, which searches the five minutes either side of the alarm, selects the camera and waits paused at the alarm time;
- comments, and the alarm's history: who acknowledged, assigned, commented and resolved, and when.

## Handling an alarm

| State | How it gets there |
|---|---|
| New | Raised by a rule. |
| Acknowledged | Someone acknowledged it. |
| Assigned | Someone assigned it to a person (this also acknowledges it). It can be reassigned. |
| Resolved | Someone resolved it, optionally with a note (kept as a comment). |

Operators and administrators can acknowledge, assign, comment and resolve. Only administrators can delete an alarm. Every action is recorded in the alarm's history and the audit log, including deletion.

## Footage

The footage around an alarm (its clip) is kept, whatever the camera's retention, until the alarm is resolved and then for a configurable number of days: **Settings → Recording & storage → Alarm footage**, 30 days by default. Deleting all of an archived camera's footage is a separate administrator action and is not blocked by alarms.

## Push notifications

Each person can be notified of alarms on their own devices, desktop or phone (install the app on a phone for the best results):

1. Open the inbox, then **Notification settings → Turn on for this device**, and allow notifications when the browser asks.
2. Choose which rules, and which camera groups, notify you. Everything is ticked at first. With every group ticked, cameras outside any group notify too.
3. Optionally set **Quiet hours**: alarms raised inside them do not notify you. They still appear in the inbox.

A notification shows the rule, camera, object and time, with a thumbnail: the recorded snapshot once the recording is indexed, otherwise the frame the object was first seen in. Clicking it opens the alarm in the inbox. People are only notified about cameras they may see, checked both when the alarm is raised and again when the notification is sent.

Signing out removes the current device's subscription, so a shared device stops getting the previous person's alarms. Each device is listed by a coarse name such as "Chrome on Android".

How it works:
- Notifications are sent through each browser vendor's push service using Web Push. The message is encrypted for the device and signed with this installation's VAPID key.
- The key is created on first use and stored, encrypted with the credential key, in the database, so configuration backups carry it and a restore keeps existing subscriptions working.
- Set `NVR_VAPID_SUBJECT` to a contact address (`mailto:` or an `https://` URL) that push services can use. Without it, `NVR_PUBLIC_URL` is used when it is HTTPS.
- Deliveries run in the background worker and retry with backoff. A device the push service reports gone, or whose keys no longer match (`401`, `403`, `404` or `410`), is removed. Sent and failed deliveries are kept for 7 days.
- The thumbnail is fetched through a link that expires after 2 hours and names only that alarm. It carries no session or API token.

The NVR must be able to reach the push services on the internet (for example `fcm.googleapis.com`, `updates.push.services.mozilla.com` and `web.push.apple.com`) for notifications to arrive. Push endpoints must use HTTPS and are checked like webhook receivers: DNS names are resolved and private, loopback and link-local destinations are rejected before each delivery.

## Webhooks

Alarm state changes are sent as signed webhooks, signed and retried exactly like the existing events. Choose them in each receiver's event filter:

| Event | When |
|---|---|
| `alarm.created` | A rule raised an alarm. |
| `alarm.acknowledged` | Someone acknowledged it. |
| `alarm.assigned` | Someone assigned it. |
| `alarm.resolved` | Someone resolved it. |

The payload carries `event_id`, `event_type`, `occurred_at`, `alarm_id`, `state`, `alarm_time`, `rule_id`, `rule_name`, `condition_type`, `camera_id`, `track_id`, `label`, `actor` and `assigned_to`. Existing event types and payloads are unchanged.

## API

```http
GET    /api/alarms?state=open|new|acknowledged|assigned|resolved|all[&camera_id=][&rule_id=][&limit=100]
GET    /api/alarms/{alarm_id}
POST   /api/alarms/{alarm_id}/acknowledge
POST   /api/alarms/{alarm_id}/assign        {"user_id": 2}
POST   /api/alarms/{alarm_id}/comments      {"body": "…"}
POST   /api/alarms/{alarm_id}/resolve       {"note": "…"}   (note optional)
DELETE /api/alarms/{alarm_id}                                (administrators)
GET    /api/alarms/assignees
GET    /api/alarm-settings
PUT    /api/alarm-settings                  {"resolved_footage_days": 30}   (administrators)
GET    /api/push/public-key
GET    /api/push/preferences
PUT    /api/push/preferences                {"rule_ids": null, "group_ids": null, "quiet_hours": {"start": "22:00", "end": "07:00", "timezone": "Europe/London"}}
POST   /api/push/subscriptions              {"endpoint": "https://…", "keys": {"p256dh": "…", "auth": "…"}, "device_label": "…"}
POST   /api/push/subscriptions/remove       {"endpoint": "https://…"}
```

Reading alarms needs a signed-in user or an API token with the `read` scope. Handling them is for signed-in people, or a token with the `admin` scope.
