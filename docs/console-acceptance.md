# Console acceptance and operating limits

This note distinguishes behavior covered by local tests or GitHub CI from checks that require the target installation, a real browser/camera, or representative host workload. Passing the automated suite does not satisfy production acceptance.

## Implemented console and settings

The console provides Live, Footage Search, Camera Settings, and App Settings. Live supports searchable camera/group/view navigation, saved workspaces, 1/4/9/16-tile layouts, focus/restore, status states, and theme selection. Footage Search provides synchronized playback, event navigation, marks, and export. Camera Settings retains discovery, inventory edit, credential/profile and connection controls, recording policy, groups, schedules, retention, storage assignment, AI settings, and archive controls.

App Settings groups system health, active sessions, optional support diagnostics, failure webhooks, external heartbeat, and site time/date display. Local identity and Entra SSO remain configured through first-run setup and environment variables; validate the target tenant's callback and role mappings. AI runtime and storage assignment are available in camera/group administration. The durable Docker volumes can be backed up by the host's approved process; there is no in-app backup/restore workflow. Branding controls are not currently available. These unsupported functions should not be represented as present in the UI.

The UI uses native controls, visible keyboard focus, labeled navigation and live status regions, a collapsible/resizable camera sidebar, and responsive layout. The operator experience and accessibility still require a real browser check as listed below.

## Camera archive and deletion

Archiving a camera disables collection and removes it from live use while keeping its stable camera identity and indexed historical footage searchable and playable. Normal retention continues to apply. Permanent media deletion is a separate administrator action: the camera must already be archived, the exact camera name must be confirmed, and there must be no active preservation hold, queued/running export, or playback lease. Deletion removes stored segment media and index records but keeps the archived camera identity and audit history.

## Enforced resource bounds

These are application admission/resource limits, not hardware performance guarantees:

- Live video admits at most 16 concurrent video viewers; live audio separately admits at most 16 audio listeners. The UI's sixteen-tile layout is a maximum of sixteen selected cameras, not a promise of smooth playback on all hosts. The browser permits one camera to be audible at a time.
- Exports use one worker and allow at most 4 active jobs. Each job can select up to 8 cameras for a range up to 24 hours, with no more than 5,000 indexed source segments or 100 GiB of source footage. The output ZIP is capped at 2 GiB and export processing preserves a 256 MiB free-space reserve.
- Completed ZIP downloads expire after 7 days. Expiry removes the archive while retaining job/evidence metadata and audit history. Queued work can resume after a process restart; interrupted running renders are reported failed rather than falsely marked complete.
- Camera retention defaults to 30 days and can be configured from 1 to 3,650 days. Projected capacity is estimated from observed bitrate and is not a guarantee of retained days.

## Export timing, audio, and evidence limits

Individual camera videos concatenate that camera's recorded spans and compress its own gaps; durations may differ by camera. The synchronized grid uses the union of selected-camera coverage and compresses only intervals empty across every selected camera. While a grid is retained, all tiles share the same source clock. Its tiles show a frozen last image or a dark state before the first image where a camera has no video. Source/output mappings and timestamp overlays explain compressed time.

Export audio is off by default and available only for supported recorded audio enabled by an administrator. Individual outputs use the respective camera's own audio; a grid uses one selected audio camera and remains silent where that source has no audio. Browser, device, proxy, and camera codec behavior still needs verification in the target environment.

The ZIP includes SHA-256 file checksums and byte counts. Verification compares delivered files with the included manifest; it does not prove identity, creation time, authenticity, camera clock accuracy, or chain of custody because the manifest is unsigned and packaged alongside the media. H.264/AVC is the browser playback compatibility target; HEVC/H.265 and vendor codecs may not play in every browser.

## Local and GitHub verification

Deterministic tests cover API permissions, saved views/groups, synchronized playback, archive safeguards, generated individual/grid media, audio, ZIP contents/integrity, settings, storage, retention, alerts, and heartbeat. GitHub CI can run the repository's tests and syntax checks, but it cannot reach a deployment's camera network, identity provider, browser fleet, persistent volumes, or representative production traffic. Use focused tests such as:

```sh
python -m unittest tests.test_console_navigation tests.test_saved_views tests.test_camera_groups
python -m unittest tests.test_synchronized_playback tests.test_camera_archive tests.test_exports tests.test_audio tests.test_live_views
python -m unittest tests.test_display_settings tests.test_storage tests.test_retention tests.test_webhooks tests.test_heartbeat
node --check app/static/app.js
python -m compileall -q app
git diff --check
```

Avoid claiming a test passed if it was not run. Do not use the full suite if it triggers the known Google Analytics network request; report that constraint and rely on the focused suites above.

## Post-merge acceptance checklist

Run these checks against a restored staging installation after merge. Record app version, browser/OS, camera models/codecs, network path, stream profiles, host CPU/RAM, storage type/free space, workload, measurements, and failures. Do not include camera URLs, credentials, or footage in the report.

- [ ] Test local login and configured Entra SSO in the target tenant, including callback, role mapping, sign-out, and recovery login behavior.
- [ ] In supported desktop and mobile browsers, complete login-to-live, group/view management, sixteen-tile layout, tile reorder, expand/restore, search, event navigation, mark/export/download, archive/historical search/playback, and settings journeys. Verify keyboard-only use, responsive panels, themes, visible labels/focus, empty/error/reconnecting states, and no lost controls after expansion.
- [ ] Upgrade an existing installation with database, secrets, and recordings on persistent volumes. Restart the service and confirm migrations, identities, settings, policies, old footage search/playback, preservation holds, and export status behave correctly.
- [ ] Verify administrator/operator permission boundaries at both API and browser boundaries for settings, private/shared views, camera configuration/archive/deletion, media/playback, export creation/status/cancel/download, and preservation. Confirm existing recording, retention, preservation, and deletion protections still apply.
- [ ] Use representative real cameras to verify low-resolution grid profiles, high-quality focused streams, reconnect/empty/no-footage behavior, codec compatibility, archive collection stop and historical search, camera audio probe/recording/listening, individual audio exports, and grid audio synchronization/silence.
- [ ] Verify App Settings and camera/group settings remain accessible. Confirm documented identity/SSO, storage, alerts/webhooks, AI/integrations, diagnostics and heartbeat functions; exercise host volume backup and restore including the database, secrets key, and recordings. Confirm unavailable branding and in-app backup controls are not implied.
- [ ] With low but safe free space, run an export and confirm it fails clearly before consuming the recording reserve, recording remains healthy, job reservations release on terminal paths, and retention does not remove held or in-policy recordings.
- [ ] Measure recording health during sixteen-feed viewing and representative CPU-only 1080p and 4K exports on the target hardware. Record CPU/RAM, export duration/progress, queue delay, live latency/frame drops, disk/free-space pressure, recording freshness/write health, and failures. Stop if recording health degrades. These measurements describe only the tested workload and host.
- [ ] Complete and document the original 30-day production acceptance gate with the declared camera mix and recording policies. Review daily coverage, ingest/storage health, incidents, playback, alert delivery, and export results. A short soak or successful sixteen-feed/4K sample does not satisfy the 30-day gate.

No >16-feed or 200-camera certification is claimed. Capacity beyond the measured workload remains unverified.
