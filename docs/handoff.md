# Handoff: where the work stands

Written 2026-11-17 for whoever continues development (a new Claude Code session on a new account, or a person). `CLAUDE.md` in the repository root says how to work here; this page says where things stand. When something here changes, change this page in the same pull request.

Nothing on this page may hold a secret or a real site's address. The owner's production install is called "the reporting site" below.

## What has shipped recently

- **Security tickets #292-#302**: API tokens cannot mint unrestricted sessions, mobile refresh replay is bounded, browser sessions follow a revoked device, media scope and privacy masks are enforced on thumbnails, plate crops, detections, tracks, search, insights, case packages and face-search pictures, open live and talk streams re-check credentials and grants, ONVIF destinations are constrained. Each ticket's thread says what is **not** covered (documented gaps); read those before claiming a ticket is complete.
- **Face watch review #306-#314**, **dependency locking with hashes and Dependabot (#285)**, **canonical millisecond timestamps (#284)**, **no idle write locks (#280)**, **a split interface bundle (#281)**, **dead-code removal and `knip` in CI (#283)**.
- **Code health (#286), one function per PR with tests pinned first**: `ExportManager._execute`, `validate_target_url`, the capacity estimate, camera inspection, `create_camera`, `RecordingManager._run_motion` and `._run_continuous`.
- **Reliability from a production incident (see Operations below)**: #321 (nightly database check, startup line, clear 503, `/healthz` fails on a damaged file, `docs/database-recovery.md`), #322 (a killed worker no longer leaves FFmpeg processes running), a fix for a stalled recording that killed a camera's recording loop (found while pinning `_run_continuous`).
- **Bulk Add runs as a server-side job (#173, part)** and **cheaper CI**: see `docs/camera-onboarding.md` and the CI section below.

## Open work

**Can start without the owner**
- **#328** backups stage two plaintext copies of the database on the data disk and fail on a nearly full disk: stream the tar into the encryption, check free space first (see the ticket).
- **#286** two functions remain: `live_stream` (`app/api/live_media.py`, complexity F 82; the surrounding tests in `tests/test_live_stream.py`, `test_hevc.py`, `test_stream_credentials.py`, `test_privacy.py`, `test_access_grants.py`, `test_viewing_audit.py` passed as a baseline) and `DetectionManager._run` (`app/detection.py`, F 69). One function per PR; pin first.
- **#256** settings grouping: the in-page sections for Integrations / AI analysis and Email alerts remain.
- **#173** the two parts still not built: (1) select several *discovered* devices and add them with one shared login, and (3) live progress while a single check runs. Design for (1): checkboxes on the Discover list, a panel with a shared username and password, and the existing bulk job taught a `candidate_id` so the server takes the address and service URL from the discovery record, claims the device (`_claim_discovery_add` in `app/api/onboarding.py`), saves with its ONVIF identity, links it, and releases the claim on failure; progress shows in the Bulk Add tab, which already shows the latest job. This needs one new column on `camera_add_items` (a new migration). For (3) a single check could run as the same kind of job with step progress that the page polls.
- **Idea, not agreed with the owner:** `python -m app.backup verify <file>` to decrypt a backup and run the integrity check without touching live data.

**Waiting on the owner**
- Review of the shipped security (#292-#302) and Face watch (#306-#314) tickets; they are open on purpose. #299 (mobile native bridge) is paused with the mobile apps.
- **#217** (workers killed for a blocked event loop): the reporting site's host details were skipped; it stays open. If stalls come back after the fixes above, the next step is that site's CPU, memory and disk numbers.
- Whether the owner wants the server's idle-connection timeout raised for reverse proxies (uvicorn closes idle connections after 5 s): declined for now, "if it becomes a problem".

**Large, not started, need direction first**: #254 (AI feature ideas to rank), #255 (people in wheelchairs and slow-moving people), the rest of #173. **Kept open for reference**: #106, #216, #230, #234, #238, #246, #253. Read a ticket's thread before assuming what is left.

## Owner decisions on record

- #305 keep strict (closed); #307 leave as is; #296 alarms from inside a masked area stay as they are (staff see label and box; the picture stays masked); #278 no connection pool; #279 camera state writes throttled to one per 5 s; #284 millisecond `...Z` timestamps for new writes, recording times left as they are; #285 stay current on dependencies including security patches; #281 accept a main script of about 620 KB; #283 keep the six test-only functions as test seams.
- **Database `synchronous=NORMAL`** (speed) was the owner's choice. After the incident below, `FULL` is worth raising again if damage recurs; it is their decision.
- Backups on, written to a different disk, copied off the machine: recommended, and the owner's to do (they are off unless a passphrase is set).

## CI and releases

- `.github/workflows/ci.yml` ("NVR checks", on pull requests and pushes to `main`): `changes` (does a pull request touch more than docs?), `camera-browser-acceptance` (about 25 min, Playwright with a simulated RTSP camera), `verify` (about 35 min: all unit tests, compile, web type-check and build, `knip`, Compose smoke test, image build and model checks), and `publish` (only on `main`: pushes `ghcr.io/<owner>/nvr:edge`). `mobile.yml` is path-filtered. `release.yml` publishes a versioned image and release on a `vX.Y.Z` tag.
- **Where the unit tests' time went, and what `tests/__init__.py` does about it:** almost every test paid for password hashing at the server's strength (a fifth of the suite) and for replaying every migration to build its empty database (an eighth). The suite now hashes with few rounds and copies the first empty database the real migrations build; `tests/test_suite_speed.py` pins the server's real hashing strength and that a copy equals a replayed database. Locally the suite went from 2,015 s to about 1,300 s. The discover command needs `-t .` so that package is imported first. What is left is spread evenly; the next lever is running test modules side by side on the runner's four processors (not done: timing-sensitive tests would need care).
- A full run was about 60 runner-minutes before that change (unit tests 35, browser journeys 20). A newer push to a pull request cancels its older run; a pull request that only changes docs skips the browser job (`.github/scripts/changed-code.sh`).
- **Billing history:** on a private repository under GitHub Free (2,000 Actions minutes and 500 MB of Packages storage a month) the account's $0 budgets stopped all jobs ("The job was not started because recent account payments have failed or your spending limit needs to be increased"). The repository was then made public, which makes minutes free. If it becomes private again, the Actions and Packages budgets matter. The image is probably far larger than the 500 MB of free private package storage (check its size).
- **Timing races that were fixed in tests, not in the app** (look for the same shape when a journey is flaky: a click or read racing a load): the idle-connection reset in the 200-camera journey, the playback "next appearance" click before the path loaded, the tiering list needing a reload, the failover incident being read after it had already recovered, the expect timeout (now 15 s), a server teardown, and in `tests/test_hevc.py` the startup acceleration probe finishing after a test had set the state itself (it only showed once setUp became quick).
- The unit tests that read `docs/` are why `verify` still runs for docs-only changes.

## After the repository was transferred to another account

The repository moved from `yaboisean319` to `gvsccrc-it`. GitHub moved issues, pull requests and settings but **not** the container package. Every place in the code, workflows, docs and tests that named the old owner now names `gvsccrc-it` (the image is `ghcr.io/gvsccrc-it/nvr`, and the in-app update check reads `api.github.com/repos/gvsccrc-it/nvr/releases`). Still for the owner:

- On GitHub: install the apps on the new account (Dependabot, the Claude GitHub app), re-check Actions settings (require approval for outside contributors' workflows) and secret scanning, check the new package's visibility after the first publish from `main` (a private package means servers cannot pull it without signing in), move the open security tickets to a private repository if the repository stays public.
- The new image only exists after the first successful `publish` job on `main` after this change. Until then the old image is the latest one there is.
- On each deployed server: point the stack's `image:` at `ghcr.io/gvsccrc-it/nvr:edge` (or a version tag) and pull; the old image keeps working until then. An install that never changes it simply stops getting updates, and its Updates page keeps reading the old account's releases until it runs a version with the new address (or `NVR_UPDATE_FEED_URL` and `NVR_UPDATE_IMAGE` are set).

## Operations: what the production incident taught

The reporting site's SQLite database was damaged twice in a week on a Hyper-V virtual machine. The kernel log showed virtual-disk errors at the same time of morning, "no space left on device" on the system disk, and a setup checkpoint that was never checked on the host; the cause was not proven. Lessons now in the product or its docs:

- The nightly self-test checks the database file and raises an incident; startup logs one `THE DATABASE FILE IS DAMAGED` line; requests answer 503 with a clear message; `/healthz` fails. Recovery (stop, copy the data folder, check each `*.pre-migration-*` copy read-only, restore the newest healthy one) is in `docs/database-recovery.md`.
- **Only three pre-migration copies are kept and older ones are removed automatically**: copy a known-good one off the machine before upgrading.
- **Backups are disabled until `NVR_BACKUP_PASSPHRASE` is set** (not a Settings switch). A backup needs about twice the database size free on the data disk until #328 is fixed.
- Leftover test volumes and many database copies filled the system disk. Keep headroom on the disk that holds the database.
- A configuration file (`.env`) can mix `KEY=value` and `KEY: "value"` lines; masking only one form leaks secrets. Ask for names only.

## Not verified on real hardware

The onboarding flow is tested against a mock ONVIF camera modelled on Hanwha's structure, not a capture from a device. The checklist in `docs/camera-onboarding.md` has not been recorded for any model; put results in `docs/acceptance/`. The face and plate models, fall detection and the AI capacity planner have evaluation scripts and documents under `docs/` but their accuracy on the owner's cameras is unmeasured.
