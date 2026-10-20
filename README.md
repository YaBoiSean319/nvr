# Workplace NVR

A self-hosted network video recorder for workplace cameras. The current implementation includes secure first-run administration, a persistent Docker Compose application, manual single-camera RTSP onboarding, continuous recording, and authenticated playback of indexed MP4 segments. The broader approved NVR specification remains in progress.

## Requirements

- Docker Engine with the Compose plugin
- A host with a persistent filesystem for the Compose volumes
- Docker Compose 2.24 or newer when using the optional camera-discovery network override

## Start the stack

For a basic install, use the default isolated Docker bridge network:

```sh
docker compose up -d --build
docker compose logs -f nvr
```

Open `http://<server>:8080` on a trusted setup network and create the first local administrator. No default account or password is shipped. The setup endpoint is permanently closed as soon as the first administrator is committed to the database. Configure TLS through the approved workplace gateway and set `NVR_COOKIE_SECURE=true` when the application is served over HTTPS.

The `nvr-data` volume stores the database and account/session state. `nvr-secrets` stores the generated credential-encryption key with owner-only file permissions. `nvr-recordings` stores the MP4 segments. All three survive container replacement and host reboot as long as Docker's volume data is retained. Back up and protect the data and secrets volumes together; a database-only export does not contain the key needed to decrypt camera URLs.

## Configuration

| Variable | Default | Purpose |
|---|---:|---|
| `NVR_PORT` | `8080` | Host port for the web application |
| `NVR_SESSION_HOURS` | `12` | Session lifetime, clamped to 1–168 hours |
| `NVR_COOKIE_SECURE` | `false` | Set `true` behind HTTPS so session cookies are HTTPS-only |
| `NVR_PUBLIC_URL` | unset | Public HTTPS origin used to construct the fixed Entra callback URL |
| `NVR_OIDC_TENANT_ID` | unset | Microsoft Entra workforce tenant GUID |
| `NVR_OIDC_CLIENT_ID` | unset | Entra app registration client ID |
| `NVR_OIDC_CLIENT_SECRET` | unset | Entra app registration client secret; supply through a secret manager |
| `NVR_OIDC_SESSION_SECRET` | unset | Long random key for the short-lived signed OIDC state cookie |
| `NVR_OIDC_ADMIN_ROLES` | unset | Comma-separated Entra app-role values mapped to NVR administrators |
| `NVR_OIDC_OPERATOR_ROLES` | unset | Comma-separated Entra app-role values mapped to NVR operators |
| `NVR_DISCOVERY_INTERFACE` | unset | Optional IPv4 address of the NVR host interface attached to the camera VLAN |

Do not expose the initial HTTP setup screen to untrusted networks. Place it behind the workplace VPN or approved gateway, then use HTTPS and enable secure cookies.

## Health and setup

- `GET /healthz` returns `200` when the app can query its database; Compose uses it as its health check.
- `GET /api/setup/status` reports whether initial setup is still required.
- `POST /api/setup/admin` creates the one-time local administrator. Passwords require at least 14 characters, including a letter and a number.
- `GET /api/system/status` requires an authenticated administrator session.
- `POST /api/cameras/test` checks an RTSP/RTSPS video stream with FFprobe.
- `POST /api/cameras` saves the stream URL encrypted and reports tested video capability; credentials and query parameters are omitted from API responses.
- `GET /api/cameras/{id}/segments` returns indexed coverage; authenticated media routes serve stored MP4 segments.
- Entra OIDC is enabled only when the five `NVR_PUBLIC_URL` and `NVR_OIDC_*` connection values are set and at least one role mapping is configured. The configured public URL must be HTTPS except for localhost development. Register its exact callback URL, `<NVR_PUBLIC_URL>/api/auth/oidc/callback`, as a **Web** redirect URI in the Entra app registration.
- Assign Entra application roles to staff and map their exact role values with `NVR_OIDC_ADMIN_ROLES` and `NVR_OIDC_OPERATOR_ROLES`. Accounts without an explicit mapping are denied. Role mapping is read from the verified ID token; group overage and unassigned accounts do not grant access.
- The OIDC authorization-code flow uses Authlib for discovery, state/nonce validation, ID-token signature and claim validation, and PKCE. Set `NVR_OIDC_SESSION_SECRET` to a long random value and keep it stable across restarts. A provider outage returns a clear sign-in error while the emergency local administrator can still sign in at `/api/auth/login`.
- Operators can list cameras and view indexed footage. Camera configuration, system status, and session administration require an administrator. Administrators can review and terminate active sessions at `/api/admin/sessions`; session lifetimes are capped at 168 hours by `NVR_SESSION_HOURS`.
- Administrators can remove a camera and its stored footage. The API removes footage before deleting the camera record; if footage cleanup fails, the camera record is retained. Operators receive `403` for camera deletion requests.
- ONVIF WS-Discovery is an explicit administrator action at **Scan camera VLAN**. The default Docker bridge does not reliably pass camera VLAN multicast. On a Linux Docker host with a camera VLAN interface, configure `NVR_DISCOVERY_INTERFACE` to that host interface's IPv4 address and run the stack with `docker compose -f compose.yaml -f compose.discovery.yaml up -d --build`. Host networking lets multicast replies reach the app; it removes the normal Docker port isolation and binds the app directly to host port `8080`, so protect that port with the host firewall or approved gateway. Keep both Compose files on future `up`/`down` commands. Cameras which do not answer ONVIF discovery can still be added manually.
- Discovery returns review candidates only and never starts recording. Review each candidate, supply its RTSP/RTSPS stream URL and name, and use **Test and add camera** to validate video before adding it. Device UUIDs deduplicate repeat discovery and link later scans to manually added cameras when the UUID is known. Discovery service URLs are sanitized; video, motion, audio, and PTZ capabilities remain `untested` until the corresponding credentialed device tests exist.

Choose **Continuous** explicitly to start recording. The product default remains **Motion**, but motion detection is not implemented yet; that mode is shown as unsupported and does not silently record continuously. FFmpeg remuxes the source video without re-encoding into target one-minute MP4 segments; actual segment boundaries follow source keyframes and may be longer. Browser playback depends on the camera codec. H.264/AVC in MP4 is a pragmatic compatibility target, while HEVC/H.265 and vendor-specific codecs may not play in every browser or operating system. Audio is omitted in this slice. FFmpeg and FFprobe are included in the image, and the recorder retries interrupted RTSP/TCP connections.

The application encrypts camera URLs before database storage, keeps the key on a separate Docker volume, suppresses FFmpeg diagnostics that could contain credentials, and passes the URL to the media process over stdin rather than exposing it in process arguments. Protect both volumes and restrict access to the Docker host.

Use `docker compose restart` to restart the service. The account remains in the `nvr-data` volume and the health check returns healthy after startup. To intentionally reset the test installation, run `docker compose down -v`; this permanently deletes the local database and any volume data.

## Development

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
uvicorn app.main:app --reload
```

## Current boundary

Motion detection, multi-camera load controls, retention, and the remaining approved NVR features are still to be implemented. Entra sign-in and role enforcement are implemented in this slice, but the installation still needs tenant-specific setup and validation. This recorder path has not been qualified for 200+ cameras or a production retention workload; compatibility and capacity tests are required before replacement use.
