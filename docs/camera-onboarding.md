# Camera onboarding (ONVIF)

An administrator needs to give the NVR only a camera's address, a username and a password. The NVR works out the rest.

`POST /api/cameras/inspect` (administrators only) takes `host`, `username`, `password` and, optionally, `device_service_url` and `brand`. It returns a report and never a credential or a credentialed address.

## What the inspection does

| Step | What happens |
| --- | --- |
| Connecting | Finds the ONVIF device service, on the address given or by trying the usual ports. |
| Authenticating | Negotiates sign-in per service: none, HTTP Digest, WS-Security UsernameToken with PasswordDigest, or both. The camera's clock offset (GetSystemDateAndTime) is applied to the WS-Security timestamp. |
| Identifying device | GetDeviceInformation: manufacturer, model, firmware. |
| Discovering streams | GetServices (GetCapabilities where that is missing) gives the real Media, Media2, Events and PTZ addresses. Profiles come from Media2 or Media1. |
| Checking motion events | Whether the Events service offers pull-point subscriptions. |
| Verifying video | One frame is decoded from the chosen stream. A stream that opens but does not decode is a failure. |
| Camera ready | Video is verified. |

## Stream choice

Recording uses the best H.264 profile (then H.265, then MJPEG). MJPEG is chosen only when nothing else is offered, and the report says so. Live view uses a lower-resolution H.264 profile when one exists.

## Capabilities

Discovery, authentication, video, audio, motion and PTZ are reported separately, each as ok, available, unsupported, failed or unknown. Unavailable motion events do not make working video unhealthy.

## Sign-in attempts and secrets

Some cameras lock an account after a few failed logins. Once every sign-in method has been refused, the NVR stops asking that camera, and the report says the login was rejected. Text a camera puts in a SOAP fault has the username and password removed before it is shown. Diagnostics list each request (operation, service, method tried, outcome, status, time) and never a header, body, credential or credentialed address. Link-local addresses are refused as camera addresses.

`NVR_ONVIF_TIMEOUT_SECONDS` sets how long the NVR waits for each ONVIF request.

## Profile lists

`/api/cameras/onvif-profiles` now also returns each profile's codec, resolution, frame rate, whether it has audio, which Media service listed it, and `suggested` (`record` or `live`) for the ones the NVR would choose. The report's `video` field gives the decoded stream's codec and size once verified. H.265 is chosen only when no H.264 is offered, with a warning that playback may be limited.

## Not in this slice

Verifying the live sub-stream, ONVIF motion subscriptions, retries with backoff, the Add Camera screen and bulk onboarding follow in later slices of #173.

## Limits

Inspections run at most four at a time, so adding many cameras does not connect to all of them at once.

## Testing and honesty note

Tests run against a mock ONVIF camera (`tests/fixtures/onvif_camera.py`). Its SOAP is modelled on the structure Hanwha Wisenet cameras are known to use (Digest on the device service, WS-Security on the media services, separate Media1 and Media2). It is not a capture from a real device. It proves the NVR's handling of these shapes, not any model's behaviour.

## Hardware validation checklist

Record the result for each camera model in `docs/acceptance/`:

- [ ] Inspection finishes with the camera ready from only address, username and password.
- [ ] The device is identified correctly.
- [ ] The recording profile is H.264 and the live profile is lower resolution.
- [ ] The decoded-frame check passes.
- [ ] A wrong password reports an authentication failure, not a connection failure.
- [ ] The report and the application logs contain no password.
