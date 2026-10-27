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
| Configuring motion events | Whether the camera will deliver motion: a real pull-point subscription is made, pulled once and ended, and the camera's motion topics are listed. |
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

## Motion events

Video and motion events are separate: if events fail, recording and live view carry on, and the NVR can use its own motion detection instead.

- Inspection tries a real subscription (subscribe, pull once, end), so "Configuring motion events" says whether the camera will deliver motion, and lists its motion topics where it names them.
- While recording, the NVR keeps a PullPoint subscription, renews it halfway through its lifetime, and subscribes again if the camera forgets it (a restart). It ends the subscription when recording stops.
- Cameras often report one movement on several topics (a cell detector and a video-source alarm) or from several rules. These are merged: motion starts when the first detector goes active and ends when the last goes inactive. Topic prefixes and case are ignored.
- When a subscription is made again, or the camera goes out of reach, motion that was open is ended, since its end may have been missed. Motion that a detector reports for ten minutes without an end is ended too. Clips use the existing pre-roll and post-roll settings.
- The camera's subscription address is used as given (including a query and WS-Addressing reference parameters), on the host that was entered. The camera's granted lifetime sets the renewal time. A camera that answers pulls at once is asked at most once a second.
- The source is `healthy` only after events have been pulled successfully, and the reason it is not is shown in the camera's last error (prefixed "Motion events:"). A motion fault never changes the video's recording status.
- After a fault the NVR waits 1 second, doubling to at most 30 seconds, with jitter, and reports the source as `unavailable`. A refused login is reported as `authentication_error` and retried only every five minutes, to avoid locking the account. A camera with no Events service is `unsupported`.

## Capability health and saving a camera that is not fully working

Each saved camera has a state and a message for discovery, authentication, video, audio, motion events and PTZ (`GET /api/cameras/{id}/capabilities`). States are `ok`, `available`, `unsupported`, `failed` and `unknown`. Messages are the NVR's own plain sentences and never hold a login or a credentialed address.

- `POST /api/cameras` accepts `save_anyway: true`. If the camera's video will not open, or ONVIF will not give a stream address at all (a media service error, say), the camera is still saved, with recording off, the video marked `failed` with the reason, and no live sub-stream, instead of the request being refused. When no address could be had, a stand-in address with no login is stored and the real one is asked for again in the background. Without `save_anyway`, a stream that will not open is refused as before.
- A camera added over ONVIF starts with discovery, login, audio, PTZ and motion events taken from a real inspection, not assumed. Cameras saved before this existed are checked once, then kept up to date. A source change refreshes video and audio health.
- A failed video or motion check, or one not yet made, is tested again in the background (in the background worker): after 30 seconds, then doubling to at most 30 minutes, with jitter, and at most four rechecks per 15-second poll, so a site with many unwell cameras is not hit at once. A success stops the rechecks. Recording is never switched on by a recheck: an administrator decides.
- `POST /api/cameras/{id}/capabilities/recheck` (administrators) tests video and motion events now.
- Inspection also decodes a frame from the lower-resolution live stream. If it cannot, that is a warning in the report (`selection.live_verified` is false) and live view can use the main stream.

## Adding cameras in the web interface

Cameras > Add Cameras has two ways that need only an address and a login:

- **Quick Add**: enter the camera's address (an IP address or host name; add `:port` if its ONVIF service is not on a usual port) and its username and password, then **Add camera**: the camera is checked and, if everything works, added in that one step. If something does not work, the result is shown and the camera waits for a decision. **Check only** shows the result without adding. The result lists the steps (Connecting to camera, Authenticating, Identifying device, Discovering streams, Configuring motion events, Verifying video, Camera ready), each with its outcome and, where it did not work, why; the camera's make and model; the recording and live streams the NVR chose; and the six capabilities separately. **Diagnostics** lists each request made (no passwords, no addresses with a login) and downloads as a file. The name is filled in from the camera's make and model. A camera that needs attention can still be added with **Add camera anyway** (recording off, rechecked in the background). New cameras start with recording off; the box **Turn on motion recording when the camera checks out** changes that for a camera that checks out completely. Changing the address or login after a check requires checking again. **Advanced** holds the ONVIF device service address and the brand, for the rare camera that needs them. The steps are shown when the check finishes, not live: a check can take up to a minute.
- **Bulk Add**: one camera per line, `address,username,password` and an optional name (a password with a comma goes in double quotes; lines starting with `#` and a header line are ignored). Each camera is checked and added, three at a time, however long the list (up to 500), and each line shows its own result: added, added but needs attention, not added with the reason, invalid, or not run. Cameras that need attention are added with recording off unless the box is unticked, and a box turns motion recording on for those that check out completely. Each camera is added by the browser one after another, so the page has to stay open: a long list takes a while (each check can take up to about a minute when a camera is slow), progress is lost if the page is closed, and the server also runs only four checks at a time. Selecting several discovered cameras and giving them shared credentials is not built yet. **Stop** leaves the rest not run, and **Retry** runs only the ones not added. Passwords stay in the page only while it is open.

Discover, Manual Add and CSV Import are unchanged. Checking, adding and listing profiles for a camera now accept a host name or IPv6 address as well as IPv4 (Quick Add, Bulk Add and the camera requests); the Discover, Manual Add and CSV Import screens are otherwise unchanged.

## Host names

A camera can be given by host name or IPv6 address as well as IPv4. The address is stored in one spelling (lower case, without brackets or spaces). The NVR does not resolve a host name to check where it points: the administrator who enters it is trusted to, as with a stream address. The weekly hardening check "outside the camera networks" looks at IP addresses only, so a camera saved by name is not covered by it, and the camera's other addresses are still checked.

## Not in this slice

Live progress while a check runs (steps appear when it finishes), and a stored history of diagnostics, are not built; the report from a check is the diagnostics.

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
