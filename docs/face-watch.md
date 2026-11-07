# Face watch (#253)

Find a face in Playback, select it, and be alerted when that face is seen again: for example when someone who has been trespassed returns to campus, in the way a licence plate on a deny list raises an alert. This page is the design and what is built so far.

**Status.** All four parts are built. The models and engine (`app/face_models.py`); the watch list with its switches and administrator API (`app/faces.py`, `app/api/faces.py`); the scanner (`app/face_scan.py`) with the `face` rule condition; and the screens: **Find a face…** in Playback and **Settings › Face watch**. A camera that is switched on, while the list has an enabled entry, reads faces on people the detector follows; only matches are kept (a small picture, the similarity and the box, in `face_sightings`) and raise the rule's alarm. Every camera is off until an administrator switches it on, and the legal question below is the owner's to settle before that.

## Models and their licences

| Job | Model | Licence | Source |
|---|---|---|---|
| Find faces (and five landmarks on each) | YuNet `face_detection_yunet_2023mar` | MIT (Shiqi Yu) | OpenCV Zoo |
| Turn an aligned face into 128 numbers | SFace `face_recognition_sface_2021dec` | Apache-2.0 | OpenCV Zoo |

Both are ONNX files read directly by OpenVINO, downloaded in the image build with pinned SHA-256 checksums (`Dockerfile`, kept in step with `app/face_models.py` by a test) and checked again before use. Neither licence needs anyone's permission or payment, including for commercial use, which is the rule this project follows.

**Not used:** InsightFace's popular ArcFace and `buffalo_l` weights. The code is MIT but its pretrained models are for non-commercial research only (commercial use needs a licence from them).

**What the licences do not cover.** The licence on a model file is not a statement about the pictures it was trained on, and neither OpenCV Zoo model says what that was (SFace's paper describes public research face datasets). If that matters to you, it is a question for your counsel, as is the next section.

## Before it is switched on: this is a legal question

Recognising people by their faces is regulated in many places (biometric privacy laws, workplace and care-home rules, rules about notice and consent), and what is allowed depends on where the cameras are, who is filmed (staff, residents, visitors) and what is done with a match. This document is not legal advice. The owner has decided to enable the feature; the points below are what the design does to keep that decision reversible and accountable, and they are things counsel will ask about:

- **Off by default, per camera.** A camera looks for faces only when an administrator switches it on for that camera.
- **Only people someone chose to put on the list.** Faces are compared with the watch list and nothing else; faces that match nothing are not kept.
- **Each entry says why and for how long.** A reason is required; an expiry date is optional but offered.
- **An audit trail.** Who added, changed and removed each entry, and who viewed matches.
- **Administrators only**, and privacy masks are respected (a face inside a masked area is not read).
- **What is stored.** The numbers that describe a face (the embedding) and a small picture of the face the entry was made from, so a person can check who an entry is. Deleting an entry deletes both.

## Measured with the real models

On a handful of public pictures, as a check that the code uses the models correctly (it is not a measure of accuracy on your cameras, which only your footage can give):

- Six different people in one photograph: every pair scored 0.33 cosine similarity or less (highest 0.327; SFace's own recommended match threshold is 0.363).
- The same person in the same photograph mirrored, halved in size, darkened, heavily JPEG-compressed or cropped: 0.94 to 0.97; overexposed by half again: 0.69.
- A face is called a match at **0.45** by default (stricter than SFace's 0.363, because a match raises an alarm), adjustable.
- Cameras differ from portraits: distance, angle, light and a small face lower the similarity of the same person, so a watch will miss some returns. A face narrower than 32 pixels in the picture it is read from is not used at all.

## The watch list (part 2)

All of it is administrators only, and every change is in the audit log (who, what, and for entries the name and reason).

| What | How |
|---|---|
| See the faces in a recorded picture | `POST /api/faces/find` `{camera_id, at}` returns each face's box, size and whether it can be used (a face inside a privacy mask cannot) |
| Put a face on the list | `POST /api/faces/watch` `{camera_id, at, point or box, name, reason, expires_at?}`; with `entry_id` it adds another picture of someone already on the list (a second picture makes a return likelier to be noticed) |
| Change or remove an entry | `PATCH` / `DELETE /api/faces/watch/{id}`; removing deletes its numbers and its picture |
| Switch cameras on or off | `GET /api/faces/cameras`, `PUT /api/faces/cameras/{id}` `{enabled}`; every camera is off until switched on |
| How alike two faces must be | `PUT /api/faces/settings` `{match_threshold}` between 0.25 and 0.9 (default 0.45) |

A name and a reason are required for every entry. An entry that has expired or been switched off is not compared with anything. The face is chosen from the recorded picture at a moment (the NVR finds the faces in it and you pick one), never uploaded.

## Plan

1. **Models and engine** (built): `app/face_models.py`.
2. **Watch list** (built): entries with a reason and optional expiry, their embeddings and a small picture, the per-camera switches, the threshold, and the administrator API with audit entries.
3. **Looking for faces** (built): on cameras switched on, a scanner like the licence-plate one finds faces on people the AI already tracks, compares them with the list, and raises an alarm through a new rule condition ("a watched face is seen"), with the usual schedule, cooldown, actions and test mode.
4. **Playback and Settings** (built): in Playback, an administrator chooses **Find a face…**, looks at the picture at the playhead (`POST /api/faces/find` returns it with the faces found and which can be used), clicks one, and gives a name and reason (and optionally an expiry) or adds it as another picture of someone already listed. Settings › Face watch lists the people (switch off, remove the expiry, delete), switches cameras on, sets the match threshold, creates the alert rule, and shows recent matches (`GET /api/faces/sightings`, with pictures; looking at them is audited like every other change).
