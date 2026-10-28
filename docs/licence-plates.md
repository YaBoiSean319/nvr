# Licence plate recognition and watch lists

Cameras designated for it read the licence plates of the vehicles they see. Each read is kept with its vehicle (track), a confidence and a picture of the plate; plates can be searched in full or in part, and **watch lists** (allow and deny, with expiry) are rule conditions. Recognition quality depends on the camera and the site: it is reported (confidence, accuracy on a labelled sample), not asserted. Read a plate against the footage before acting on it.

## Turning it on

1. Install the models (below) beside the other AI models and restart the AI worker. Until both load, nothing is read and **Settings, Licence plates** says which one is missing.
2. Turn **AI analysis** on for the camera: plates are read on the vehicles analysis finds.
3. Under **Settings, Licence plates**, tick **Read plates on** the camera (gates, car parks) and choose the lowest confidence to keep (60% by default; reads below it are dropped).

## How a plate is read

For each vehicle track on a designated camera the AI worker takes a few attempts, three seconds apart and at most four per vehicle until a plate is kept: a full-size (1280×720) picture of the camera is taken from its main stream, the detector finds plates in it, the recogniser reads each plate crop, and a plate that sits on a tracked vehicle (its centre inside the vehicle's box, grown by 15%) is stored with that track. A plate off every tracked vehicle is not kept. Reading never holds up detection: it runs beside it, one picture at a time per camera.

- **Privacy masks.** A plate whose box touches an area the NVR masks is not kept, so a masked area never yields a plate or a picture.
- **One read per plate per vehicle.** Seeing the same plate again on the same track keeps the most confident read and its picture. Two different readings of one car are two reads.
- **Plates are compared in a normal form**: upper case, letters and digits only (`ab-12 cde` is `AB12CDE`), at most 12 characters. A read must have at least 2.

## Searching

Playback, **Find a plate…** (or `GET /api/plates/search`):

| You type | Finds |
|---|---|
| `12CD` | plates *containing* it (a partial plate) |
| `AB12*` | plates starting with `AB12` |
| `*QRS` | plates ending with `QRS` |
| `AB??CDE` | `AB`, any two characters, `CDE` (`?` is exactly one character; with a wildcard the whole plate must match) |

**Look-alikes** (on by default, switch off to be exact) also find plates that differ only in characters cameras confuse: O, Q or D and 0; I or L and 1; S and 5; B and 8; Z and 2. Results are newest first, with the camera, the time (site time), the confidence and the picture; choosing one opens Playback at that moment. A search covers only the cameras the person may see (access grants): asking for another camera finds nothing and does not say it exists, and the picture of a hidden camera's plate does not exist for them. The range defaults to 7 days and is at most 92.

```http
GET /api/plates/search?q=<text>[&start=<iso>&end=<iso>][&camera_ids=<id>...][&lookalikes=true|false][&min_confidence=0.0-1.0][&order=desc|asc][&limit=60]
GET /api/plates/reads/<read-id>/crop      # the picture of the plate (JPEG, never cached)
```

An API token with the `read` scope may search (it is a footage read, like the attribute search).

## Watch lists and rules

A **deny list** is plates of interest; an **allow list** is plates that belong (residents, staff). Under **Settings, Licence plates** make lists and add plates, each with an optional note and expiry. An entry is a whole-plate pattern: `AB12CDE`, `KT55*`, `AB??CDE` (at least two letters or digits; `*` alone is refused). An expired entry matches nothing. A list can treat look-alike characters as the same.

A **rule** (Cameras, Rules: condition **A licence plate is read (watch list)**) names one list and the cameras or groups it watches:

- on a **deny list** it fires when a plate on the camera matches an entry that has not expired;
- on an **allow list** it fires when a plate matches *no* entry. A plate that is on the list does nothing. Note that an empty list, or one whose entries have all expired, raises an alarm for every plate read, and that a one-character misread of an allowed plate counts as a plate that is not on the list unless the list treats look-alike characters as the same.

It fires once for each vehicle (track), with the rule's usual schedule, cooldown and scope, raises an alarm labelled **Plate AB12CDE** (alarm inbox, push notifications, Live pop-in), and carries the vehicle's picture and clip like any alarm. Test mode replays the reads kept over the last 24 hours without raising anything. A list used by a rule cannot be deleted or changed between allow and deny until the rule is changed.

## Retention and access

Plate reads belong to their footage. They are deleted with it: when the footage a read was taken from is deleted (even while the vehicle's track lives on in newer footage), when detections that no footage covers expire, when the track or the camera is deleted. The picture of the plate goes with its read. Reads follow access grants as above; managing settings and lists is for administrators and is recorded in the audit log.

## The models

Neither is bundled (no reviewed checksum is pinned for them yet; see `docs/acceptance/roadmap.md`). Put each model's `.xml` and `.bin` in `/opt/nvr/models` or set the paths:

| Model | Default path | Contract |
|---|---|---|
| Plate detector | `/opt/nvr/models/vehicle-license-plate-detection-barrier-0106.xml` (`NVR_PLATE_DETECTOR_MODEL_PATH`) | input `[1,3,300,300]` BGR; output `[1,1,N,7]`, rows `image_id, label, confidence, x1, y1, x2, y2` as fractions of the picture; label 2 is a plate; confidence at least 0.5 |
| Plate recogniser | `/opt/nvr/models/license-plate-recognition-barrier-0007.xml` (`NVR_PLATE_RECOGNISER_MODEL_PATH`) | input `[1,3,H,W]` BGR (the size is read from the model); output either per-position scores with an axis as long as the symbol list (CTC: best class per position, repeats collapsed, blank dropped; logits are softmaxed) or whole-number indices, one per position, `-1` ending the text |

The recogniser's symbols are read from `<recogniser>.charset.txt` beside it: one symbol per line in class order, a line `<blank>` marking the CTC blank. Without the file they are `0`–`9`, `A`–`Z` and a blank last, which suits a Latin-plate recogniser. **Check that the symbols match the model you install**: the Open Model Zoo's `license-plate-recognition-barrier-0007` reads Chinese plates, and a model for another region needs its own symbols. Neither model's contract could be checked against the real files in this build (they could not be downloaded); the code was run against real OpenVINO models of the same contract built for the tests.

## Measuring accuracy

```sh
python -m app.plates evaluate labels.csv frames/   # labels.csv: image,plate   frames/: PNG, JPEG or BMP pictures
```

prints the share of plates read, right as a whole, right ignoring look-alikes, and characters right, using each picture's most confident read. Record a labelled sample from the cameras in service (day and night, the speeds in use) and put the result in `docs/acceptance/roadmap.md`. That file also holds the measurement made for this feature: the pipeline on a labelled sample of 500 rendered plates at eight picture qualities, with stand-in models (it measures the code, not the Open Model Zoo models or any camera).

## Limits

- A privacy mask added after plates were read does not remove the reads already kept (it applies from then on).
- A recogniser that takes a second `seq_ind` input is given one (0 for the first position, 1 after), as the Open Model Zoo recognisers expect; like the rest of the real-model contract, this was written from the model documentation and not run against the real files.
- Recognition depends on the camera: resolution on the plate, angle, shutter speed, infrared glare, dirt and the plate style. Fast vehicles and plates under about 100 pixels wide read poorly. Place plate cameras for it (low angle, tight view, fast shutter).
- A plate is looked for a few times as a vehicle appears, not continuously: a vehicle whose plate was never in view in those attempts has no read.
- Only plates on a tracked vehicle are kept; a motorcycle's plate is read only if the vehicle detector tracks it as a vehicle.
- A console (multi-site) does not search plates across its sites yet: search each site.
