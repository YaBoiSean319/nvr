# Workplace NVR

A self-hosted network video recorder for workplace cameras. This repository is in its initial implementation phase; the first slice establishes secure first-run administration and a persistent Docker Compose application. Camera recording and playback are planned follow-on slices and are not included yet.

## Requirements

- Docker Engine with the Compose plugin
- A host with a persistent filesystem for the Compose volumes

## Start the stack

```sh
docker compose up -d --build
docker compose logs -f nvr
```

Open `http://<server>:8080` on a trusted setup network and create the first local administrator. No default account or password is shipped. The setup endpoint is permanently closed as soon as the first administrator is committed to the database. Configure TLS through the approved workplace gateway and set `NVR_COOKIE_SECURE=true` when the application is served over HTTPS.

The `nvr-data` volume stores the database and account/session state. `nvr-recordings` is mounted at `/recordings` for later recording slices. Both survive container replacement and host reboot as long as Docker's volume data is retained.

## Configuration

| Variable | Default | Purpose |
|---|---:|---|
| `NVR_PORT` | `8080` | Host port for the web application |
| `NVR_SESSION_HOURS` | `12` | Session lifetime, clamped to 1–168 hours |
| `NVR_COOKIE_SECURE` | `false` | Set `true` behind HTTPS so session cookies are HTTPS-only |

Do not expose the initial HTTP setup screen to untrusted networks. Place it behind the workplace VPN or approved gateway, then use HTTPS and enable secure cookies.

## Health and setup

- `GET /healthz` returns `200` when the app can query its database; Compose uses it as its health check.
- `GET /api/setup/status` reports whether initial setup is still required.
- `POST /api/setup/admin` creates the one-time local administrator. Passwords require at least 14 characters, including a letter and a number.
- `GET /api/system/status` requires an authenticated administrator session.

Use `docker compose restart` to restart the service. The account remains in the `nvr-data` volume and the health check returns healthy after startup. To intentionally reset the test installation, run `docker compose down -v`; this permanently deletes the local database and any volume data.

## Development

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
pip install httpx
python -m unittest discover -s tests -v
uvicorn app.main:app --reload
```

## Current boundary

This initial slice does not yet configure cameras, record or play video, provide Entra ID sign-in, or claim support for a production camera count. Those are subsequent implementation tickets and require separate compatibility and load validation.
