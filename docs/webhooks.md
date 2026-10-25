# Signed failure webhooks

Administrators manage receivers at `/api/webhooks` or in the Failure webhooks panel. Choose one or more event types: `recording.failure`, `recording.uncertain`, `recording.recovered`, `storage.failure`, `storage.recovered`, `ai.detection`, `ai.failure`, `ai.recovered`, `tamper.detected`, `tamper.recovered`, `system.failure`, `system.recovered` and `coverage.weekly_report`. The weekly coverage report is sent once a week as `coverage.weekly_report`, with the report as `report` in the payload (see [coverage and self-test](coverage-and-self-test.md)); a failing nightly self-test check is reported as `system.failure`. A recorder process crash is reported as `recording.failure`/`recording.recovered`, and an AI process crash as `ai.failure`/`ai.recovered`. A covered, moved or defocused camera is reported as `tamper.detected` and, when its view returns, `tamper.recovered` (see [camera tamper detection](tamper-detection.md)). A crash of the web, export, or webhook/heartbeat process is reported as `system.failure`/`system.recovered`, because none of these stops recording. Deliveries queued while the webhook process is down are sent after it restarts. The test-delivery action sends `webhook.test` regardless of the event selection. Detection and recording coverage use the same recording health events; an uncertain event means the NVR cannot establish that motion coverage was complete.

The receiver URL and signing secret are encrypted at rest. The secret is returned only when a receiver is created or its secret is rotated. The UI and list API show only the URL origin; URL paths and queries are kept private because they may contain receiver tokens. Secret rotation invalidates the prior secret immediately, so update the receiver before relying on further deliveries.

Each JSON delivery includes a stable `event_id`. The request also includes:

- `X-NVR-Event-ID`: stable event ID; use it to deduplicate retries.
- `X-NVR-Event-Type`: selected event type.
- `X-NVR-Timestamp`: Unix time in seconds for this delivery attempt.
- `X-NVR-Signature`: `sha256=` followed by the lowercase hexadecimal HMAC-SHA256.

The signature input is the exact timestamp header bytes, one period byte (`.`), then the exact HTTP request body bytes. A receiver should compare signatures in constant time, reject timestamps outside its allowed clock-skew window, and persist processed event IDs so a retry is not applied twice. Retries keep the same event ID and body but use a fresh attempt timestamp and signature.

The worker stores delivery jobs before sending, recovers in-flight jobs after restart, and exposes attempts, response status, errors, and final outcome at `/api/webhooks/deliveries`. Network failures, HTTP 429, and HTTP 5xx responses retry up to eight total attempts with bounded delays. Other non-2xx responses fail without retry. Redirects are not followed. Response bodies and complete receiver URLs are not stored in delivery errors.

Public destinations must use HTTPS. An administrator may explicitly authorize a receiver on a private IP address; private receivers must use a literal private IP, while loopback, link-local, multicast, reserved, and mixed public/private DNS destinations are rejected. DNS names are resolved and checked before delivery. Use network egress controls as an additional boundary for hosts running the NVR.

HMAC signing proves that a request was produced by someone holding the configured shared secret and that the signed body was not modified. It does not provide non-repudiation: the receiver and NVR share the secret. Event delivery is at least once when retries occur; receiver-side deduplication is required.
