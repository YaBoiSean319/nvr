# The security log

The NVR writes a row for nearly every sensitive action: changes to accounts and how they sign in, second factors set up or reset, API tokens made or revoked, camera access granted, settings changed, sign-ins that were slowed, and commands run from the NVR host. Who *watched* which camera is separate: that is the [viewing audit](viewing-audit.md).

## Reading it

**Settings › Security log** (administrators only) lists the rows for a date range, newest first, 100 to a page. Narrow it by **person** (who did it) and by **action**. A row with no person was done by the NVR itself, from the NVR host, or by someone who was not signed in (a throttled sign-in, for example). **Download as CSV** gives every row that matches, up to 100,000; cells that a spreadsheet would run as a formula are neutralised, and the download is itself recorded.

The same is available as `GET /api/admin/security-log` (`from`, `to`, `user_id`, `action`, `limit`, `offset`) and `GET /api/admin/security-log/export.csv`.

## How long it is kept

Two years (730 days) unless changed: **Security log retention** on the same page takes 30 to 3,650 days (`GET`/`PUT /api/admin/security-log/settings`). Older rows are removed by the same background pass that applies footage retention, every few minutes; a pass with nothing to remove does not write to the database. Changing the period is recorded, with how many rows it removed.

## What is and is not in it

- Passwords, token secrets, recovery codes and camera credentials are never written, only that something changed.
- A username that belongs to nobody is kept as a keyed digest (`unknown:…`), not as text: people type their password into the username box by mistake. The same text gives the same digest, so repeated attempts still show as one thing.
- Using an API token does not add a row. Its last-used time, shown in **Settings › Users & access**, is the record; a row per use used to add up to 1,440 rows a day for one token that is polled constantly.
- An administrator ending someone's session (`session_ended`), disabling or enabling an account (`user_disabled`, `user_enabled`), a password change (`password_changed`) and a change to the site's date and time display (`display_settings_changed`) are recorded.

## For developers

`app.audit.record` writes a row in the caller's transaction; `app/security_log.py` reads and prunes them; `app/api/security_log.py` holds the routes. Migration 70 adds the retention setting and the indexes the person and action filters use. Tests: `tests/test_security_log.py`, `tests/browser/security-log.spec.cjs`.
