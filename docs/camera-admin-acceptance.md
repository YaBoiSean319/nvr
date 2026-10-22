# Camera administration acceptance record

Ticket #75 adds authenticated Playwright journeys backed by the running FastAPI application, temporary SQLite/recording/credential stores, deterministic camera fixtures, a loopback ONVIF responder, and generated RTSP test media. CI installs the pinned Playwright package and Chromium, runs the browser journeys, and uploads the test report, traces, screenshots, videos, and sanitized 200-camera measurements as the `camera-admin-browser-acceptance` artifact, including on failure.

## Current verification record

| Check | Result | Evidence |
| --- | --- | --- |
| Local Chromium browser journeys | Not run: this environment has the Playwright package but no Chromium executable. | GitHub Actions browser job is the executable browser gate. |
| Authenticated browser journey inventory | 15 real-app Playwright journeys are configured and discovered by the pinned runner; execution is pending the CI browser job. This includes visible inventory loading/error and deleted-camera recovery states, plus bulk-dialog focus restoration. | `npx playwright test --list` lists 15 tests; the server fixture uses the isolated FastAPI store. |
| CI browser journeys and measured 200-camera feedback | Pending the CI run for this commit. | `test-results/camera-browser-acceptance.json` and `test-results/camera-admin-acceptance-measurements.json` in the CI artifact. |
| API/media and focused repository regressions | Passed: 90 tests across inventory, onboarding, bulk, discovery, groups, scheduling, storage, retention, archive/holds, playback/live/audio/exports, navigation, display, and branding. | Explicit modules: `tests.test_camera_inventory`, `tests.test_camera_onboarding`, `tests.test_camera_bulk_actions`, `tests.test_discovery_candidates`, `tests.test_discovery_range`, `tests.test_camera_groups`, `tests.test_scheduling`, `tests.test_storage`, `tests.test_retention`, `tests.test_retention_api`, `tests.test_camera_archive`, `tests.test_holds`, `tests.test_synchronized_playback`, `tests.test_live_views`, `tests.test_audio`, `tests.test_exports`, `tests.test_console_navigation`, `tests.test_display_settings`, `tests.test_branding`. |
| JavaScript regression suite | Passed: 42 tests. | `node --test tests-js/*.test.cjs`. |
| Python compile and JavaScript syntax | Passed. | `python -m compileall -q app tests/browser`; `node --check app/static/app.js`; `node --check playwright.config.cjs`; `node --check tests/browser/helpers.cjs`; `node --check tests/browser/camera-admin.spec.cjs`. |
| Full unittest discovery | Not run locally because repository acceptance notes identify an Analytics test that attempts an unwanted outbound request. | The CI `verify` job remains configured to run its existing full suite. |

The measurements JSON is initialized before the Playwright server or browser starts. If the 200-camera measurement test does not complete, it explicitly says `not-collected`, includes zero samples, and leaves the deployment gates unchecked; it never substitutes guessed timings. On completion it records application/browser/runner versions, workload, ten filter samples, five selected-panel samples, p50/p95, browser and failed-request counts, inventory media-request count, owner, result, and artifact evidence. The test report JSON is also pre-seeded so a failed web server or earlier journey still leaves a stable artifact path; Playwright replaces it with its normal report when the reporter completes.

## Deployment acceptance gates

| Gate | Status | Owner | Environment and workload | Result | Evidence |
| --- | --- | --- | --- | --- | --- |
| Production camera-count capacity and recording headroom | **Unchecked** | Deployment acceptance owner: unassigned | Target NVR host, real camera/network/codec mix, representative concurrent recording and administration workload | Not run; a 200-row browser fixture is not a capacity certification. | None |
| Original 30-day parallel qualification | **Unchecked** | Deployment acceptance owner: unassigned | Production-like deployment over the full 30-day pilot | Not run. | None |
| Supported browser/device, identity, upgrade/restart, and recording-health walkthrough | **Unchecked** | Deployment acceptance owner: unassigned | Declared target deployment and supported client fleet | Not run. | None |

Keep camera names, addresses, stream URLs, credentials, tenant identifiers, and footage out of committed records and CI artifacts. The fixture manifest and isolated credential key live in the temporary test directory and are removed after the Playwright process exits.
