# Workspace overhaul acceptance report

Date: 2026-10-22  
Candidate: `codex/nvr-workspace-overhaul`, based on `088ed9e`  
Scope: NVR issues #10–24 and workspace issues #50–65

This report records local implementation checks separately from deployment qualification. Local tests passed for this candidate. No production, browser-fleet, camera-network, or 30-day acceptance claim is made.

## Local checks completed

| Check | Result | Evidence |
| --- | --- | --- |
| Console navigation, saved views, and camera groups | Passed | `tests.test_console_navigation`, `tests.test_saved_views`, `tests.test_camera_groups` — 14 tests |
| Synchronized playback, archive, exports, audio, and live views | Passed | `tests.test_synchronized_playback`, `tests.test_camera_archive`, `tests.test_exports`, `tests.test_audio`, `tests.test_live_views` — 45 tests |
| Display settings, storage, retention, webhooks, heartbeat, and coverage | Passed | `tests.test_display_settings`, `tests.test_storage`, `tests.test_retention`, `tests.test_webhooks`, `tests.test_heartbeat`, `tests.test_coverage` — 16 tests |
| Branding API, safe asset validation, delivery, and reset | Passed | `tests.test_branding` — 3 tests |
| Workspace layout behavior | Passed | `node --test tests-js/workspace-layout.test.cjs` — 4 tests |
| Browser JavaScript syntax | Passed | `node --check app/static/app.js` |
| Python compilation | Passed | `.venv/bin/python -m compileall -q app tests` |
| Patch whitespace | Passed | `git diff --check` |

The full unittest discovery run was not repeated locally because its analytics test attempted an outbound request that was blocked by the execution review. Docker Compose checks and the container build were not run because Docker is unavailable in this environment. GitHub Actions results are a separate gate and must be checked on the pull request.

Camera and settings draft guards, per-camera warning drill-in, and frame-aligned visual zones were reviewed in the browser UI code. The repository has no DOM test harness for those interactions, so real-browser role journeys remain unchecked below.

## Performance evidence

No browser timing samples were collected. In particular, the following measurements remain unknown: p95 input-to-painted-feedback with 200 configured cameras and exports active; p95 first decoded live frame; committed seek-to-decoded-frame latency; and 1-, 4-, 9-, and 16-feed CPU, memory, network, decoder, and recording-write effects. No p50/p95 values, sample counts, codec/bitrate profile, hardware context, or error rates are available for this candidate.

Do not use synthetic inventory size, a successful HTTP response, a spinner, or a synthetic AI benchmark as evidence for live media latency or deployment capacity. The implemented sixteen-feed limit is an admission/UI limit, not a capacity certification. No 200-camera certification is claimed.

## Deployment acceptance still required

Run on a restored staging installation and the declared reference hardware. Record application version, browser/OS, camera model and codec mix, stream profiles/bitrates, network path, host CPU/RAM, storage type/free space, active workload, sample counts, p50/p95, failures, and test owner. Keep camera names, addresses, URLs, credentials, tenant identifiers, and footage out of the report.

- [ ] Complete admin/operator workflows at 320, 360, 768, 1024, and 1440 px in supported browsers; include keyboard and touch use, theme changes, drawers, dialogs, visual schedule/zone editing, branding, and error recovery.
- [ ] Measure input-to-painted-feedback for clicks, filtering, panels, and layout with at least 200 configured camera entries while exports run; report sample count and p50/p95.
- [ ] Measure p95 first decoded fresh live frame and committed seek to decoded requested footage using healthy supported streams; record profiles, codecs, bitrates, timeouts, and failures.
- [ ] Exercise 1-, 4-, 9-, and 16-feed workloads and representative CPU-only exports. Record CPU/RAM, queue delay, live drops, disk pressure, free space, recording freshness/write health, and failures.
- [ ] Exercise rapid seek and view churn; verify old media connections, decoders, and media leases are released and recording remains healthy.
- [ ] Verify target Entra configuration, camera VLAN discovery/events, codecs, audio, archive behavior, storage pressure, backup/recovery, and permission boundaries on the upgraded persistent installation.
- [ ] Run the original 30-day parallel pilot at the declared camera mix and recording policies; review coverage, storage, incidents, playback, alerts, and exports each day.

Until these checks have evidence, deployment qualification and the performance-acceptance portion of issue #65 remain open.
