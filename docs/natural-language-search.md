# Natural-language search

Playback > **Find by attributes** has a box at the top: **Describe what you are looking for**. Type a plain description, such as `white van at the gate after 6 pm`, and press **Ask**. Results are ranked best first, each with a picture, camera, time and score, and open Playback at that moment. Above them the answer says what it understood (cameras, time of day, range), which way the results were ranked, how much of the time was analysed, and which time was not.

## What the description is split into

| Part | Examples | Effect |
| --- | --- | --- |
| Range | `yesterday`, `today`, `last night`, `this morning`, `last 2 hours`, `past week` | Narrows the range (the default is the last 7 days). |
| Time of day | `after 6 pm`, `after 18:30`, `before 9 am`, `between 6 and 8 pm`, `in the evening`, `at night` | Keeps tracks first seen in that part of the day, on any day in range, on the site's clock (the time zone in the display settings, which every time in the application is shown in). |
| Cameras | `at the gate`, `on Dock 1`, `in the yard` | Narrows to the camera or camera group named: the whole name, or a word that tells cameras apart. Only cameras the person may see are recognised. A place that matches no camera stays in the description. |
| Zone | `in the left half of the frame`, `on the right side`, `in the centre` | Keeps objects that passed through that part of the frame (the zones of [attribute search](attribute-search.md): left, right, top, bottom, centre). A bare `at the left` or `heading left` is not a zone. Named zones drawn per camera are not recognised. |
| Kind and attributes | `van`, `man`, `red jacket`, `blue jeans`, `grey truck`, `heading left`, `parked` | The kind (person or vehicle) must match. Colours, vehicle type and direction raise the score of tracks that have them (the attributes of [attribute search](attribute-search.md)). |
| Everything else | `white van` | Matched against each object's picture by the image-text model. |

A number is read as a time only when it says am or pm, has minutes (`18:30`) or can only be a 24-hour hour (`after 14`): `after 6`, `from 3 cameras` and `by 2 trees` are left in the description.

## The image-text model

An administrator selects a CLIP-family model by putting its OpenVINO files in one directory, `NVR_AI_CLIP_MODEL_DIR` (default `/opt/nvr/models/clip`):

```
image_encoder.xml  image_encoder.bin  text_encoder.xml  text_encoder.bin  vocab.json  merges.txt
```

The model id is `clip:<directory name>`: to change model, point the variable at another directory. The image encoder takes a 224x224 RGB crop (CLIP mean and standard deviation) and the text encoder the 77 token ids from `vocab.json` and `merges.txt` (CLIP's byte-pair tokenizer, run in this process). Each output is normalised. The AI worker embeds every new track's first sighting once; the quantized embedding (one signed byte per value) is stored with the model's id, is never returned by any API, and is deleted with its track, so it follows footage retention. Embeddings from different models are never compared.

The score is the cosine of the query and the picture, plus 0.1 for each described attribute the track has, shown as a number out of 100. Tracks with a picture embedding are listed before tracks without one, which are scored on attributes alone. Scores from this model are relative: use them to order results, not as probabilities.

Without a model (or when the files do not load) the same box still works: results are ranked by how many of the attributes named in the description each track has, and the answer says "No image-text model is installed". A description that names nothing recognisable lists the newest objects in range. Objects analysed before the model was installed have no picture embedding; they are counted in the answer and rank only by their attributes.

## Nothing leaves the server

Indexing and searching run in this process. The tokenizer, both encoders and the comparison are local, no description, picture or embedding is sent anywhere, and a test fails the build if either makes a network connection.

## API

`GET /api/ai/search/ask?q=...` (any signed-in user). Optional: `start`, `end` (default the last 7 days, at most 92), `camera_ids`, `utc_offset_minutes` (to read "6 pm" and "yesterday" on another clock; by default the site's), `limit` (default 30, at most 100). A range phrase in the description narrows the range; with no explicit `start` it replaces the default. Cameras the person may not see are not searched and are not recognised by name. The answer has `interpretation`, `mode` (`image-text`, `attributes` or `browse`), `results` (with `score` and `rank`), `unindexed`, `coverage` (the cameras and intervals not analysed, with why) and `coverage_note`. It never has an embedding.

## Honest limits

- The score ranks tracks: a kind (person or vehicle) must match, and the colours, type and direction named add to the score rather than filter, so with a model "white van" can rank a red van above a white one if the picture suits the words.
- The unanalysed time listed is for the whole range searched, not only the time of day asked for ("after 6 pm" also reports the unanalysed daytime). The page shows the duration for each camera; the intervals, with why, are in the API answer.
- The model is found when the first search or analysed frame needs it: files installed afterwards are picked up on the next restart.
- Privacy masks are drawn over the thumbnails, but the picture embedded and the attributes stored are of the unmasked frame, as for [attribute search](attribute-search.md): someone allowed to search can find an object that passed behind a mask, though not see what is behind it.
- Place words that match no camera, camera group or frame zone (for example the name of a zone drawn for detection) stay in the description.

- The model path is tested with a stand-in model that shares one axis per colour; the real CLIP preprocessing (nearest-neighbour resize rather than bicubic) and output handling have not been run against a real model's files. Validate on your own footage before relying on it.
- Only the first sighting of each object is embedded, so an object first seen small, clipped or badly lit is described by that picture.
- Phrases are matched by rules, in English: unusual wording may stay in the description (and is then matched by the model, or ignored without one).
- A time that has not happened yet ("this evening" at nine in the morning) has nothing to look at, and the answer says so. Time of day uses the first time an object was seen, and the site's offset from UTC now, so it can be an hour out for tracks seen on the other side of a change of clocks.
- Up to 5000 candidate tracks in range are scored per request; the answer says when there were more.
