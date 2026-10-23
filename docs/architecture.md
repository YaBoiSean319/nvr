# Architecture and module boundaries

This describes how the backend is organised during the architectural rebuild (#80): domain routers for the HTTP API, and supervised worker processes for background work.

## Processes

The container entrypoint (`app.entrypoint`) runs `app.workers.supervisor`. The supervisor applies schema migrations once, then starts each worker as its own process:

| Worker | Module | Responsibility |
|---|---|---|
| `recorder` | `app.workers.recorder` | Recording tasks, schedule supervision, retention cleanup, storage health, ONVIF events and server motion fallback. |
| `web` | `app.workers.web` | The HTTP API and browser interface (uvicorn serving `app.main:app`). |

Each worker writes a heartbeat to `worker_status` every 5 seconds. The supervisor restarts a worker that exits, or that stops reporting for 30 seconds, after an exponential backoff from 1 to 60 seconds. The failure counter resets after 60 seconds of stable running. While a worker is down, an incident (`worker_recorder` or `worker_web`) is active, and it recovers once the replacement reports liveness. Recorder incidents notify webhooks as `recording.failure`/`recording.recovered`. On SIGTERM the supervisor stops the web worker first and the recorder last, and allows each `NVR_STOP_GRACE_SECONDS` (default 25) before killing it. `GET /api/system/status` lists every worker's liveness, and reports `degraded` when one is not alive.

The database is the source of truth. When the web process saves something the recorder must act on, it sends a best-effort nudge over a Unix socket in the data volume's `run/` directory (`app.workers.control`). The recorder also reconciles every camera from the database every 5 seconds, so a lost nudge only delays the change. Inside the supervised web process, `app.state.recorders` is a `RecorderClient` that sends these nudges in place of the in-process `RecordingManager`.

Without the supervisor (tests, `uvicorn app.main:app --reload`), `create_app()` runs every manager in-process, as before. Export, AI, webhook and heartbeat work still runs in the web process until their own workers are added.

## Composition

- `app.main` is the ASGI app the web worker serves. It only builds the application with `create_app()`.
- `app.application.create_app(start_workers=True)` owns the middleware, the lifespan, and router registration. The lifespan applies schema migrations, then starts the export, webhook, heartbeat, recording and detection managers. `start_workers=False` serves the API without background loops.
- `app.api.ROUTERS` lists every domain router in registration order. The legacy browser interface router (`app.api.web_ui`) is registered last so its catch-all asset paths cannot shadow API routes.

## Domain routers (`app.api`)

| Module | Responsibility |
|---|---|
| `system` | Liveness check used by Compose (`/healthz`). |
| `identity` | First-run administrator, local sign-in/out, Entra OIDC, session administration. |
| `site_settings` | Display (time zone/format) settings and site branding, including public branding. |
| `live_views` | Personal and shared saved views and per-user live workspace state. |
| `policies` | Camera groups and membership; group/camera schedules, retention and storage placement. |
| `cameras` | Camera inventory, stream sources and profiles, credential rotation, audio probing, motion sources. Owns `camera_view`, `insert_camera` and discovery probe bookkeeping. |
| `onboarding` | ONVIF discovery and candidates, CSV import, reviewed bulk updates. Builds on `cameras`. |
| `ai_settings` | Per-camera, per-group and runtime person/vehicle detection settings. |
| `live_media` | Authenticated live video/audio delivery and viewer admission. |
| `footage` | Segments, coverage, motion and AI events, archive/restore, footage deletion, preservation holds. |
| `playback` | Synchronized timeline, segment media, snapshots and frame navigation. |
| `export_jobs` | Export job submission, status, cancellation, download and evidence verification. |
| `alerts` | Incidents, webhooks and the external heartbeat. |
| `operations` | Administrator system status and opt-in diagnostics. |
| `web_ui` | The legacy static browser interface (replaced by the new interface in a later ticket). |

Routers may import public helpers from another router only in one direction (for example `onboarding` → `cameras` → `policies`). They never import `app.main` or `app.application`. Shared constants that several layers need live in small modules (`app.viewing` for live admission limits, `app.version` for the release identifier).

Domain logic that does not depend on HTTP (recording, retention, storage, exports, incidents, webhooks, heartbeat, detection, discovery, scheduling) stays in the existing top-level `app` modules, and routers call into it.

## Authorization

`app.principal` resolves the caller. Every route that needs a signed-in user declares one of two annotated dependencies:

- `CurrentUser`: any signed-in administrator or operator.
- `Administrator`: administrators only (403 otherwise).

Both yield a `Principal` (`id`, `username`, `role`, and for API tokens `scopes`/`token_id`) that also supports `principal["id"]` style access. A bearer API token resolves to its creating administrator, limited by scope: an endpoint marked `@requires_scope("media")` or `@requires_scope("export")` needs that scope, administrator routes need `admin`, other reads need `read`, and other changes need `admin`. The OpenAPI document publishes the result as `x-required-scope`. Unauthenticated routes are deliberate: setup status/bootstrap, sign-in, OIDC, public branding, health, and the static interface. Tests replace identity by overriding `app.principal.current_user` or `app.principal.administrator` in `app.dependency_overrides`.

## Database

- `app.store.connect()` opens the SQLite database named by `NVR_DB_PATH` in autocommit mode, with foreign keys enforced and a 10 second busy timeout. The database runs in WAL mode, set when migrations are applied, so separate processes can read while one writes.
- Keep write transactions short. Use explicit `BEGIN IMMEDIATE … COMMIT` when several statements must apply together.
- Schema changes are made only through versioned migrations in `app.migrations` (see the README's upgrade section). No module creates or alters tables at runtime.
