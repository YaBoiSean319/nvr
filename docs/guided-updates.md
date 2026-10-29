# Guided updates

Settings, **Updates** (administrators) tells you when a newer release is out, what it changes, and gives the exact commands to upgrade and to roll back. **The NVR never runs them.** The container has no access to Docker (nothing mounts the Docker socket) and no route can pull an image or restart anything: you copy the commands and run them on the host.

## What it shows

- The running version and database schema.
- After **Check now**: the newest release newer than this one, its published date, its release notes, and **whether it changes the database** (a database copy is made automatically before migrations run). A release that does not say shows "does not say" rather than a guess.
- After an upgrade, **After an upgrade** lists what the start found: the old and new version and schema and the migrations that were applied, and the most recent migrations in the database.

## The check is opt-out

The check asks one address: the repository's published releases (`https://api.github.com/repos/yaboisean319/nvr/releases`). Nothing about the installation is sent. Turn it off with the switch on the page, or for the whole installation with `NVR_UPDATE_CHECK=false`, and then **no request is made at all**: Check now is refused. Looking at the page never makes a request; only Check now does (there is no background checking).

| Setting | What it does |
|---|---|
| `NVR_UPDATE_CHECK=false` | Turns the check off for the whole installation. |
| `NVR_UPDATE_FEED_URL` | A different releases feed (a mirror, for an installation without internet). GitHub's releases JSON format; `https`, or `http` to this machine only. |
| `NVR_UPDATE_IMAGE` | The image the commands name. Default `ghcr.io/yaboisean319/nvr`. |

## Updating

1. **Back up, then show the upgrade steps.** The configuration backup runs first and is listed (file name and where it is) *before* any command is shown. If it fails, the steps are not shown. If backups are not set up (`NVR_BACKUP_PASSPHRASE`), you have to choose **Show the steps without a backup**.
2. **Upgrade, on the host**: set the new image in `compose.yaml`, `docker compose pull nvr`, `docker compose up -d nvr`.
3. **If you need to go back**: stop the container, put back the database copy made just before the migrations (named on the page, the newest one that is there), set the previous image, and start it. Before the first upgrade with migrations there is no such copy yet: it is made when the new version first starts, and then named on the page.

Footage is not part of the backup. A rollback restores the database and configuration as they were before the upgrade; footage recorded while the new version ran is still on disk, but anything the new version changed in the database (cases, settings) is lost.

## Publishing a release (for whoever maintains the repository)

Push a tag `vX.Y.Z` on a commit of `main` whose checks passed, with `APP_VERSION` in `app/version.py` set to `X.Y.Z`. `.github/workflows/release.yml` publishes `ghcr.io/yaboisean319/nvr:X.Y.Z` and creates the release, with generated notes and the line `<!-- nvr-release: {"schema_version": N} -->` that tells installations whether it migrates. Pre-releases and drafts are never offered.

## Limits

- The release workflow could not be run where this was built; its first use is the test of it.
- The commands assume the standard `compose.yaml` with a service named `nvr`.
- "Newer" means a higher `x.y.z`. An installation on the `edge` image reports its `APP_VERSION`, which may be behind or ahead of a tagged release.
