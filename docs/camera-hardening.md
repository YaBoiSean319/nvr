# Camera network hardening

Settings > **Camera hardening** is an administrator's report of how exposed the cameras are. It never changes a camera: each finding names the camera, says what is wrong and how to fix it.

## The four checks

| Check | Finding | Severity |
| --- | --- | --- |
| Default password | The login the NVR holds for the camera (in its stream address, or its ONVIF login) is a well-known factory default | High |
| Plain RTSP | The stream is `rtsp://` although the camera completes a TLS handshake on a usual RTSPS port (322, 8322, 7441) | Medium |
| ONVIF over HTTP | The ONVIF device address is `http://` although the camera completes a TLS handshake on a usual HTTPS port (443, 8443) | Medium |
| Outside the camera network | An address the camera is configured with (its stream, substream, ONVIF address or recorded IP) is outside the **Camera networks** you name | Medium |

Names are not placed on a network, so only addresses written as IPs are judged. Until at least one camera network is named, the last check is not made and the page says so. Archived cameras have no findings.

## What is, and is not, kept or sent

- **The default-password check tries nothing against the camera.** The NVR already holds each camera's login (encrypted); the check compares it, in memory, with a bundled list of published factory defaults (such as `admin`/`admin`, `root`/`pass`). The login and the entry it matched are never logged, never stored and never returned: a finding says only that a default is in use, and for the stream, the ONVIF login, or both.
- **The two protocol checks open a TLS connection and stop after the handshake**, on the ports above. The certificate is not verified (cameras use self-signed ones), nothing is sent after the handshake, and nothing is stored about what the camera presented. The probe can be switched off under **Camera networks and probes**; the two protocol checks are then not made.

## How often cameras are contacted

A camera is probed for each kind at most **once an hour** (`NVR_HARDENING_PROBE_GAP_SECONDS`, default 3600). A run inside the hour reuses the last answer, so running the checks by hand repeatedly asks the camera nothing more. Cameras at the same address (a multi-channel encoder) are probed once per run, at most **four probes run at once**, each connection waits at most three seconds, and a changed address is probed at once. Checks run **weekly**, counted from the first time the NVR starts with this feature (so starting the NVR contacts no camera), and on demand with **Run checks now**.

## Acknowledging a finding

**Acknowledge** needs a reason (up to 300 characters). The finding then leaves the list and appears under **Acknowledged** with the reason, who accepted it and when; **Show again** undoes it. An acknowledgement belongs to the condition it was made for, which is recorded without secrets (for a plain stream, the address, port and the secure port offered; for defaults, which logins; for an outside address, the addresses):

- while the condition is unchanged it stays hidden, whatever the time that has passed;
- if the condition changes (another login is a default, the camera moves to another address that is also outside), it is a new finding and is shown;
- if the weakness is fixed, the finding is removed, and if it later comes back it is shown again.

Running the checks, saving the networks, acknowledging and restoring are written to the audit log (`hardening_run`, `hardening_networks_set`, `hardening_probes_set`, `hardening_finding_acknowledged`, `hardening_finding_restored`).

## API

All for administrators.

- `GET /api/hardening`: the report (findings, acknowledged findings, networks, last and next run)
- `POST /api/hardening/run`
- `PUT /api/hardening/settings` with `{"camera_networks": ["192.168.50.0/24"], "probes_enabled": true}` (runs the checks again)
- `PUT` / `DELETE /api/hardening/findings/{camera_id}/{check}/acknowledgement` (`check` is `default_credentials`, `plain_rtsp`, `onvif_http` or `outside_network`; `PUT` takes `{"reason": "…"}`)

## Limits

- The list of defaults is bundled and cannot be edited; a default it does not know is not found.
- A camera that offers RTSPS or HTTPS on an unusual port is not detected as offering it.
- Whether a camera is reachable from outside the building is not tested; the report relies on its configured addresses.
