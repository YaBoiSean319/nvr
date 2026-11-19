# Working in this repository

Workplace NVR: a self-hosted network video recorder (FastAPI + SQLite backend, React/TypeScript interface, FFmpeg, ONVIF, on-device AI). It runs as one container with a supervisor and several worker processes. **Read `docs/handoff.md` first**: it holds the current status, the open work, the owner's decisions and what is waiting. This file is the stable part: how to work here.

The repository is public. Nothing in a commit, issue, PR or doc may hold a password, token, key, domain name, IP address, tenant or client ID, or other detail of a real site. Examples use documentation ranges (`192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`) and `example.test`. Never print a secrets file (`.env`, key files); when asking the owner for configuration, ask for names only.

## Priorities (the owner's, in this order of concern)

Performance, reliability, stability and security. Put the main server first. The mobile apps are paused. If something seems to contradict the spirit of a ticket, stop and ask the owner before building it.

## Run it

```sh
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python -m unittest discover -s tests -t .       # about 2,000 tests; about 20-25 minutes ("-t ." loads tests/__init__.py, which makes the suite cheaper). Run one module while working: python -m unittest tests.test_bulk_add
ruff check app tools scripts tests               # small rule set on purpose: real errors, not style
npm ci --prefix web && npm run build --prefix web   # type-checks, then writes app/web_dist
npm run unused --prefix web                      # knip: unused files, exports and dependencies fail CI
npx playwright test tests/browser/<spec>.cjs --trace off   # one browser journey; needs the web build. Run one Playwright process at a time (they share a fixture folder)
python -m radon cc app -s -n C                   # not in CI: how complex the biggest functions are (pip install radon)
```

Some browser journeys and tests need the simulated RTSP camera (a MediaMTX container) and MinIO, which only CI starts; they are skipped or fail locally for that reason alone. A few tests need packages that are in `requirements-dev.txt` but may be missing from a sandbox (for example `joserfc`).

## Layout

- `app/`: the backend. `app/api/*.py` are the HTTP routers (registered in `app/api/__init__.py`, order matters: specific paths before `/api/cameras/{id}`). `app/workers/` holds the supervisor and workers (`recorder`, `exporter`, `ai`, `rules`, `background`, `web`); see `docs/architecture.md`. `app/migrations/mNNNN_name.py` are forward-only schema steps, registered in `app/migrations/__init__.py` in **both** the import line and the `(version, module)` list.
- `web/`: the React interface (`web/src/features/...`). `tests/`: Python unit tests (`tests/fixtures/` has a mock ONVIF camera and other stand-ins) and `tests/browser/` (Playwright journeys). `docs/`: one page per feature, plus `docs/rebuild/spec.md`. `mobile/`: the Capacitor app (paused).
- Dependencies are locked with hashes: edit `requirements*.in`, run `scripts/lock-requirements.sh`, commit the `.txt` files. The container installs with `--require-hashes`.

## Rules of the codebase that have bitten before

- **`with connect() as db:` closes the connection when the block ends.** Never use `db` after the block (this crashed a camera's recording loop; see `tests/test_run_continuous.py`).
- **Timestamps:** write new ones with `app.clock.stamp()` (UTC, milliseconds, trailing `Z`). Recording times deliberately keep their own form (see the module docstring).
- **Idle loops must not take the write lock** (#280): read for due work first (`app.store.has_row`), and only then `BEGIN IMMEDIATE`.
- **A killed worker must leave nothing behind** (#322): workers run as process-group leaders and the supervisor ends the group.
- **Privacy masks apply everywhere** detections, tracks, pictures or footage leave the server (`app/track_privacy.py`, `app.privacy`). New readers of detections need the projection.
- **Credentials:** camera passwords are encrypted at rest (`app.credentials`); error text, logs, audit rows and API views never hold one. Tests assert this.
- **Never name a model or tool as an author in a commit, PR or doc**, beyond the attribution lines the harness you run in asks for.

## How the owner wants work done

- **Open a PR; never merge without the owner's explicit go-ahead for that PR**, even when everything is green. Say what you did, what you tested, and what you did not or could not test.
- **Never skip, disable or loosen a test to get green.** Find the root cause. A failing CI job gets at most one re-run, and only when the cause is outside the change. Several past "flakes" were races in the tests: fix the race.
- **Pin behaviour before refactoring.** For the large-function splits (#286): write tests that pass on the old code, split one function per PR, and show the same tests pass.
- **Read a ticket's whole thread before saying it is done or closing it.** Do not close tickets the owner wants kept open for reference.
- **Explain in plain language.** The owner is not a developer. Lead with what happened and what to do; say what you are unsure of.
- **Anything irreversible on the owner's servers** (deleting files, restoring a database, changing credentials) is described, checked with them, and done by them, never assumed. Take a copy first.
- Commit messages: a short imperative first line, a body saying why, and the harness's attribution lines. PR bodies: Summary, Evidence, Not covered.
