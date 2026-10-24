# Rebuild acceptance report

Scope: the architectural rebuild and new interface, spec #80 (tickets #81–#104), together with #1, #33, #49, #67, #77 and #78.

Version: application `0.1.0`, branch `claude/admiring-cray-xt8wn4` (pull request #105).

Environment for the checks below: Linux container, Python 3.13, Node 22, Chromium headless shell from Playwright 1.62.1, generated RTSP fixture media (MediaMTX and FFmpeg test sources), a loopback ONVIF responder, and temporary SQLite, recordings, credential-key and backup directories. No real cameras, tenant, footage or customer data were used.

This report separates what automated tests prove from what only the target installation can prove. Passing everything in the first part does not qualify the system for production. The items in the second part do.

## Executed suites

| Suite | Command | Result | What it covers |
| --- | --- | --- | --- |
| API, media and migrations | `python -m unittest discover -s tests` | Passed (239 tests) | Every HTTP route and its authorization (roles and API-token scopes), migrations m0001–m0006 on fresh and pre-versioning databases, supervised workers and restarts, the live hub over real RTSP, recording, retention, exports and retry, webhooks, heartbeat, backups and restore, and serving the interface at `/` with the `/next/` redirect. |
| Browser journeys | `npm run test:browser` | RESULT_BROWSER | Real Chromium against the running application: first-run setup, sign-in, role-gated navigation, Live (layouts, views, groups, sockets released on every exit), Playback (synchronized clock, investigation, frame stepping, exports), Cameras (inventory at 0/1/200 cameras, editor tabs, drafts and leave guard, preview, discovery, manual add, CSV import, bulk changes, Groups & Policies), and every Settings category. |
| Responsive and accessibility sweep | `tests/browser/sweep.spec.cjs` (part of the browser run) | Included above | Every workspace and every Settings category at 320, 360, 768, 1024 and 1440 px and at 1920×1080, 1440×900 and 1366×768, as an administrator and as an operator. Checks: no sideways page scroll; every button, link, field, tab, menu item and separator has an accessible name; body, secondary and accent text reach 4.5:1 in light, dark, system and branded themes; reduced motion stops all animation and transitions. |
| Browser harness configuration | `node --test tests-js/*.test.cjs` | Passed | The browser server receives only fixture file paths, never secret values; failure reports stay free of credentials. |
| Interface type check and build | `npm run build --prefix web` | Passed | TypeScript strict mode and the production bundle. |
| Python compilation | `python -m compileall -q app tests` | Passed | |
| Compose configuration | `docker compose config -q` (with and without `compose.discovery.yaml`) | Passed | |

CI on the pull request runs the same suites, plus the container image build. Its result is recorded on the pull request rather than here.

The 200-camera journey records filter and selection feedback times in `test-results/camera-admin-acceptance-measurements.json`. They come from a CI runner with synthetic inventory and are not evidence of performance on reference hardware.

## Found and fixed during acceptance

- Branded accents were adjusted for contrast against white, so text in the accent colour fell just under 4.5:1 on the grey page and raised surfaces (4.47:1 with the default accent). The accent is now checked against the lowest-contrast neutral surface in each theme.
- Reloading straight into Cameras lost the saved pane width, because the sign-in reset ran after the workspace had loaded its preferences. Workspace state is now cleared only when the user signs out or changes.

## Post-merge testing required

None of these can be checked from this repository. Each needs the version under test, the environment, the steps followed, who ran it, and the evidence kept. Tick an item only when its evidence is attached.

### Real cameras, codecs and audio

- [ ] **Unchecked.** Live, recording, playback and export on every camera model in service.
  - Version:
  - Environment: camera models and firmware, main and sub stream codecs and resolutions, audio codec.
  - Steps: add each model through Discover and Manual Add; confirm Live plays through MSE (H.264) and through the MJPEG fallback (H.265 or other); enable audio on one camera per audio codec and confirm live listening and recorded AAC; record motion and continuous footage; play it back and export it.
  - Owner:
  - Evidence:

### Microsoft Entra tenant

- [ ] **Unchecked.** Staff sign-in with the production tenant.
  - Version:
  - Environment: tenant, app registration, `NVR_PUBLIC_URL`, gateway.
  - Steps: sign in as a mapped administrator and as a mapped operator; confirm an unmapped account is refused; confirm the emergency local administrator still signs in during a provider outage.
  - Owner:
  - Evidence:

### Latency on reference hardware

- [ ] **Unchecked.** Responsiveness and live latency on the workstation and server that will be used.
  - Version:
  - Environment: server CPU, memory, disk and network; workstation, browser and display (1920×1080, 1440×900, 1366×768).
  - Steps: measure p50 and p95 for first decoded live frame, glass-to-glass latency, committed seek to decoded frame, and input to painted feedback with 200 configured cameras; repeat with 1, 4, 9 and 16 live tiles.
  - Owner:
  - Evidence:

### 1080p and 4K export load

- [ ] **Unchecked.** Export rendering under load.
  - Version:
  - Environment: server hardware, source resolutions and bitrates.
  - Steps: queue concurrent exports of 1080p and 4K footage with overlays; record render time, CPU, memory and disk use; confirm recording health stays green throughout and a failed export can be retried.
  - Owner:
  - Evidence:

### Recording health under viewing load

- [ ] **Unchecked.** Recording is unaffected by heavy viewing.
  - Version:
  - Environment: production camera count and policies.
  - Steps: hold 16 live tiles and a 16-camera playback for at least an hour; confirm no recording gaps, write-latency warnings or worker restarts.
  - Owner:
  - Evidence:

### Production volume upgrade

- [ ] **Unchecked.** Upgrade of the production installation's volumes.
  - Version: from (current production) to (candidate).
  - Environment: a copy of the production data, secrets and recordings volumes.
  - Steps: with backups enabled, upgrade as described in the README; confirm migrations reach m0006, a pre-migration copy and backup exist, every camera, view, group, schedule and branding asset survives, and footage plays; then rehearse the downgrade by restoring the pre-upgrade copy with the previous image.
  - Owner:
  - Evidence:

### 30-day parallel pilot

- [ ] **Unchecked.** Thirty days alongside the existing system.
  - Version:
  - Environment: production site.
  - Steps: run the candidate in parallel for 30 days; compare recorded coverage daily; record every incident, restart, backup result and operator issue.
  - Owner:
  - Evidence:
