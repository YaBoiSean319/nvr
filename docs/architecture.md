# Architecture and module boundaries

This describes how the backend is organised after the route split in the architectural rebuild (#80). Later rebuild tickets move background work into supervised worker processes; this page covers the web application as it stands.

## Composition

- `app.main` is the ASGI entry point. It only builds the application with `create_app()`.
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

Both yield a `Principal` (`id`, `username`, `role`) that also supports `principal["id"]` style access. Unauthenticated routes are deliberate: setup status/bootstrap, sign-in, OIDC, public branding, health, and the static interface. Tests replace identity by overriding `app.principal.current_user` or `app.principal.administrator` in `app.dependency_overrides`.

## Database

- `app.store.connect()` opens the SQLite database named by `NVR_DB_PATH` in autocommit mode, with foreign keys enforced and a 10 second busy timeout. The database runs in WAL mode, set when migrations are applied, so separate processes can read while one writes.
- Keep write transactions short. Use explicit `BEGIN IMMEDIATE … COMMIT` when several statements must apply together.
- Schema changes are made only through versioned migrations in `app.migrations` (see the README's upgrade section). No module creates or alters tables at runtime.
