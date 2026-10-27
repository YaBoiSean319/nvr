# Find similar

Choose a person or vehicle in the attribute search results (Playback > **Find by attributes**) and press **Find similar** to see the closest-looking objects on every camera you may see, in time order, each with how alike it looks. It is meant for following someone or something through a site. It is a lead, not proof: similarity is how alike two pictures look, not whether it is the same person.

## How it works

- When the AI worker first sees an object it keeps a picture of it (the keyframe) and works out an **appearance embedding** from that picture: a list of numbers that is close to another list when the two objects look alike.
- For people, if the OpenVINO `person-reidentification-retail-0287` model files are installed (`NVR_AI_REID_MODEL_PATH`, default `/opt/nvr/models/person-reidentification-retail-0287.xml` and its `.bin`), that model makes the embedding. Without it, and for vehicles, a built-in description from the pixels is used (`appearance-colour-v1`): the colours of four bands from top to bottom and how light or dark the plain parts are. That tells apart people and vehicles that look different (clothes, paint) and cannot tell apart people dressed alike. Embeddings from different models are never compared.
- Embeddings are stored in the NVR's database as one signed byte per value, with the model that made them. They are never returned by any API, and are deleted with their object when the footage it was found in is deleted.
- The closest ones are found with the [`sqlite-vec`](https://github.com/asg017/sqlite-vec) extension when it can be loaded, and by comparing in memory when it cannot. Both give the same similarity (cosine, from the stored values) and so the same answer. `NVR_SIMILAR_EXTENSION=off` forces the in-memory comparison.
- Only people are compared with people, and vehicles with vehicles. Cameras you may not see are not searched and are not distinguishable from cameras that do not exist.

## The screen

Each result in the attribute search has a **Find similar** button. The list that follows is titled "Similar to <camera> <time>", shows each object's picture, camera, time and similarity (for example "91% similar"), and each opens Playback at that moment. A note says when some cameras were not analysed for the whole time, so that absence is not mistaken for proof.

## The API

`GET /api/ai/tracks/{track_id}/similar` (any signed-in user; the object's camera must be one they may see)

| Parameter | Meaning |
| --- | --- |
| `start`, `end` | Time stamps with a time zone. Default: the last 7 days. At most 92 days. |
| `camera_ids` | Cameras to search (repeat the parameter). Default: all the person may see. |
| `limit` | How many of the closest to keep, 1 to 100 (default 30). |
| `min_similarity` | 0 to 1 (default 0.6). |
| `sort` | `time` (default; ascending) or `similarity`. |

The answer lists the results (the usual search result fields plus `similarity` from 0 to 1 and `rank`, 1 being the closest) and the analysis coverage for the cameras searched. An object with no embedding (for example, recorded before this existed) answers 409.

## Limits

- Similarity with the built-in description is as good as colours are: two people in similar clothes look alike to it. Install the re-identification model for better results on people.
- Only objects the AI worker has analysed and kept a keyframe for are searched; time that was not analysed is reported, not guessed.
- Nothing here identifies a person: no names, no faces, no cross-referencing.
