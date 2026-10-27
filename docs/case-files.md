# Case files

A case groups everything about one incident: clips (a camera and a time range), marks, notes, alarms, saved searches and exports. Footage of an open case's clips is held, so it is not deleted. Each case has an owner, members and a status, and records who opened and changed it. One evidence package covers the whole case.

**Playback > Add to case…** puts the active camera's marked stretch (or the minute around the playhead) in a case, or marks the playhead; **Cases** opens the list. Cases > a case shows its items, members, notes, packages and audit.

## Footage holds

- Adding a clip holds every recorded interval of that camera overlapping the range, with a hold of its own that records the case. Footage that is not recorded (a gap) cannot be held: the clip says how many seconds of its range were not covered. A clip must be of footage already recorded, at most 24 hours long, and have some recorded footage in range.
- A held interval cannot be deleted (the existing rule), and its hold cannot be released by hand while a case holds it (releasing a hold in the footage list answers 409). Holds placed by hand or by other cases are separate: closing a case or removing a clip releases only the holds that case placed.
- **Removing a clip** releases the holds no other clip of the case needs.
- **Closing a case** asks for a confirmation that says how many recorded intervals it releases. Footage nothing else holds then becomes eligible for deletion under its retention policy. A closed case takes no more items, but can still be opened, audited and packaged (only if its clips' footage still exists: it is no longer protected). There is no reopening.

## Who can open a case

Only the case's members and administrators. Anyone else gets exactly the answer for a case that does not exist, and the attempt is recorded in the administrators' audit log (`case_access_denied`). The owner and administrators add and remove members, rename the case and close it. Anyone in a case adds items and removes their own; the owner and administrators remove anyone's. A person owns at most 25 open cases (holding footage is not free).

**Access grants apply to each clip's camera.** A person cannot add a clip, mark or alarm on a camera they may not see (the answer is that the camera is not found). A case shows them only the items on cameras they may see (the counts leave the others out too), plus how many items they cannot see; they cannot remove those. The audit, and `audit.json` in a package, leave out what an event says about a camera they may not see. Saved searches show each member only the results on cameras that member may see.

## Notes

Notes are only added. What was written, by whom and when stays as it was; to correct one, add another.

## Audit

`case_audit` records, for the case: created, updated, opened (once for each stretch of use, not for every refresh), a clip opened in Playback from the case (`item_viewed`, once for each stretch of use of that clip, with the camera and time),  clips, marks, notes, alarms, searches and exports added, items removed (with the holds released), members added and removed, packages requested, ready, downloaded and verified, and closing. Exports of a clip are also in the viewing audit and audit log as for any export; a package download is a `download` in the viewing audit for each of its cameras.

## The evidence package

**Build evidence package** makes one ZIP of the clips the person may see. It is built in the background by the exporter: one export is made for each clip (as the export queue has room), and when all are done the case's file is written. It contains:

- `clips/NN-<export id>.zip`: the export's own evidence package, with its manifest and signed chain extract;
- `case.json` (the case, its members and items, which file holds each clip, and how much of each clip's range is recorded now), `notes.json`, `notes.md`, `audit.json` (the audit up to that moment), `metadata.json`;
- `integrity.json`: the SHA-256 and size of every other file.

**Verify a file…** (or `POST /api/cases/{id}/packages/{package}/verify` with the file as the body) checks every file against the manifest and each clip's own package, including its signed chain, and then checks the file against what this NVR built: `matches_issued_package` (the file is byte-for-byte the one it wrote, by its SHA-256), `clips_match_request` (the clips are those the package was requested for) and, for a clip exported from signed footage, that its signed chain is still there. `verified` needs all of them. A manifest inside a file proves nothing alone, since whoever changes a file can rewrite it; the check against the NVR's own record is what catches that. A copy of a package made on another installation cannot verify here. It reports whether every clip named in `case.json` is present and valid (`complete`) and which clips have footage missing from their range (`partial_clips`: a clip over a gap in the recording, or whose file was lost since, still verifies, and says how many seconds are missing). A package built by someone with less access verifies but is not complete: it says how many items were left out (`omitted_items`), and does not name them. The checksums are unsigned: they detect changes to the files, not who made the package.

A package is the access of the person who asked for it, so only they and administrators can download it. The clips in it are those the package was requested for. Only one package for a case is built at a time, a case keeps up to five (the person who asked for one, the owner and administrators can delete it), a package not finished in six hours fails, and one is not written when the disk has too little room.

## API

`GET/POST /api/cases`, `GET/PATCH /api/cases/{id}`, `POST /api/cases/{id}/members`, `DELETE /api/cases/{id}/members/{user_id}`, `POST /api/cases/{id}/items` (kind `clip`, `mark`, `note`, `alarm`, `search`, `export`), `DELETE /api/cases/{id}/items/{item}`, `POST /api/cases/{id}/close` (`{"confirm": true}`), `GET /api/cases/{id}/audit`, and the packages routes above. All need the `export` scope for an API token.

## Limits

- A case holds up to 500 items. Items of a camera that is later deleted stay in the case but their footage goes with the camera.
- The exports a package is built from expire after seven days as usual; the assembled case package does not.
- Only clips are put in a package as footage. Alarms, saved searches and exports appear in `case.json` as records; an export added to a case is not bundled (build the package for the clips). A package is all or nothing: if one clip cannot be exported the package fails, with the reason.
- Viewing a clip is recorded when it is opened from the case; watching the same footage in Playback directly is in the viewing audit as ever, with no case attached.
- Footage whose file is already gone cannot be held and is not counted as held; a segment still being recorded when a clip is added is not held, and footage recorded later in the clip's range is not added to the hold.
- A clip's export is limited as any export is (a size and a segment count); a clip too large to export makes the package fail, with the reason.
- Notes and items are visible to every member who may see their cameras. There are no per-item permissions.
