# External host heartbeat

The NVR can send a signed heartbeat to an independently hosted receiver. Configure it in the administrator's External host heartbeat panel or through `PUT /api/heartbeat`. The interval is 30–900 seconds; the default is 60 seconds. Receiver URLs and signing secrets are encrypted at rest. Public receivers must use HTTPS. A private receiver requires administrator authorization and a literal private IP. The signing secret is shown only when first configured or rotated.

Each heartbeat is a signed JSON POST with event type `nvr.heartbeat`, a persistent NVR instance ID, a unique event ID, send time, and a `test` flag. It uses the same headers and HMAC-SHA256 input as the signed webhooks documented in [webhooks.md](webhooks.md): exact `X-NVR-Timestamp` bytes, a period, and the exact request body. The receiver should verify the signature in constant time, reject timestamps outside an allowed skew, and store the latest valid receipt time for each instance ID.

The status API reports the latest attempt, HTTP status, error, and last successful send. A successful send only means the configured receiver returned a 2xx response. It does not prove that an independent checker is running, configured to alert, or able to reach its notification destination.

## External checker setup

Run the receiver and checker on a different physical or virtual host, or use an independently hosted monitoring service. Do not run the sole checker only inside the NVR host or its Docker Compose stack: it would also disappear when that host stops.

Configure the external checker to alert when no valid heartbeat has arrived for at least three configured intervals plus the receiver's request timeout. For a 60-second interval and a 10-second request timeout, an initial threshold of 190 seconds allows brief network delays while detecting a full host outage. Adjust this using measured network behavior and the operational response target. Send outage and recovery notifications from the external checker itself.

## Exercise

1. Configure the receiver URL and interval in the NVR, save the signing secret at the external receiver, then use **Send test heartbeat**.
2. Confirm the external receiver verifies the signature and stores the heartbeat. Confirm the NVR reports a last successful send.
3. Stop the entire NVR host or disconnect its network; stopping only the application process is a useful secondary check but does not exercise power-loss detection.
4. Wait beyond the external timeout and verify that the checker sends an alert without any request from the NVR.
5. Restore the NVR host and confirm a new signed heartbeat arrives and the checker reports recovery.
6. Test the external alert destination independently. A green NVR heartbeat status is not evidence that alert delivery works.

The repository's automated tests verify signed sends, receiver failure reporting, private-destination authorization, and heartbeat configuration. They cannot certify a separately deployed receiver or demonstrate an off-host alert; that exercise must be completed in the target environment before relying on host-outage notification.
