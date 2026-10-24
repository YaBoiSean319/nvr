# Local person and vehicle detection

AI detection is disabled by default and runs locally on the NVR host. The default provider is OpenVINO CPU and the bundled model is the Open Model Zoo `person-vehicle-bike-detection-2004` FP32 model. The optional OpenVINO GPU provider uses the same CPU-capable package; no GPU-only dependency is required for CPU installations. The model is based on MobileNetV2 with an ATSS detection head at 448×256 and is about 1.8 GFLOPs per frame. The model reports a 0.274 AP score on its own internal test set; this is not a camera compatibility or site accuracy guarantee. Its outputs are vehicle (class 0), person (class 1), and non-vehicle (class 2); this app stores only vehicle and person observations. Open Model Zoo models are Apache-2.0 licensed.

The container pins OpenVINO 2026.2.1 and the model's 2021.4 FP32 XML/BIN files. The Docker build verifies each artifact's SHA-256. Model sources and interface:

- [Open Model Zoo model card](https://github.com/openvinotoolkit/open_model_zoo/tree/master/models/intel/person-vehicle-bike-detection-2004)
- [OpenVINO supported-model list](https://docs.openvino.ai/2026/documentation/compatibility-and-support/supported-models.html)
- [OpenVINO Python API](https://docs.openvino.ai/2026/get-started/learn-openvino/openvino-samples/hello-batch.html)

## Configure

An administrator opens a camera's **Configure person/vehicle detection** controls, chooses whether to analyze it, sets the analysis frame rate (0.1–5 FPS), minimum confidence, normalized rectangular zones, and whether to use the validated lower-resolution stream. Detection uses the selected camera's existing stream profile. Selecting the main stream is supported, but the low-resolution profile is recommended to reduce decoder and model work. The selected profile and AI analysis settings do not change the recording stream or recording policy.

Administrators can configure individual cameras or set inherited AI policy on a camera group. Camera-level overrides remain explicit and can be returned to group inheritance. The runtime defaults to four active camera streams, configurable from 1 to 16. CPU inference uses two OpenVINO threads by default (`NVR_AI_CPU_THREADS` can set 1–8); inference calls remain serialized to protect recording capacity. Each FFmpeg reader is sampled at its configured rate and can buffer only a bounded amount while waiting. Cameras beyond the active limit are marked `skipped`; slow inference is marked `delayed`, and runtime or stream faults are marked `failed`. No unbounded analysis queue is maintained. If AI is disabled, delayed, or fails, it does not stop or change the recorder. The UI and API expose AI coverage state and time of the last successful analysis. `failed`, `delayed`, `skipped`, and `unanalyzed` intervals are unknown coverage, and no detection means only that the configured model did not detect an object in the sampled frames.

## Provider and model selection

Administrators can list OpenVINO CPU/GPU provider availability and compatible models from **AI runtime and model** in camera-group administration. **Validate selected runtime** compiles the model for the selected provider and performs a test inference; **Validate and save AI runtime settings** repeats that validation before changing the active provider/model. A failed validation leaves the active runtime unchanged. Every saved observation and webhook event keeps its provider and model ID/version, including a SHA-256-derived version for local models.

The bundled model is the default model option. To add a compatible replacement, place its OpenVINO IR XML and adjacent BIN files in `/data/models` (or change `NVR_AI_MODEL_DIRECTORY` to a mounted directory). Reload options, validate, and save the selected model. The adapter accepts only models with a fixed `1x3x256x448` input and named `boxes` and `labels` outputs. `boxes` must contain pixel coordinates `[x1,y1,x2,y2]` and confidence; labels must use class 0 for vehicle and class 1 for person. The model must accept BGR planar float input. Incompatible shapes or output names fail validation before activation. The application does not download arbitrary models or infer class semantics from the filename.

GPU availability is host-dependent. The default Compose file does not pass through a GPU device. For Intel GPU testing, expose the host's compatible render device (commonly `/dev/dri`) to the container and install compatible host drivers; other providers require their own compatible container device/runtime configuration. If OpenVINO does not expose a GPU device, validation reports it unavailable and keeps CPU active. Passing validation checks model compilation/inference on that host only; it does not certify the NVR's camera count, recording headroom, or another machine. This workspace has no target GPU, so GPU execution remains untested here. Re-run the benchmark and recording-continuity test on the deployment host before relying on GPU operation.

New observations can be sent as signed `ai.detection` webhook events. Analysis failures and recovery transitions send `ai.failure` and `ai.recovered`. Select these types in each receiver's event filter. Detection results are grouped briefly by camera and label to avoid creating one record for every sampled frame; a new observation group gets a stable event ID for webhook retries. Capacity skips are shown as coverage gaps and do not raise a failure alert.

## Search and playback

Operators can search detections by camera, person/vehicle label, time range, and confidence-bearing observation, then seek to retained footage around the observation. Each observation records its server receipt time in UTC, model ID/version, frame size, confidence, and normalized bounding box. The timestamp is the NVR analysis time; it is not a camera-exposure timestamp. If footage does not cover the observation, the UI reports that no retained clip is available.

Example authenticated API calls:

```http
GET /api/ai/events?camera_id=<camera-id>&label=person&start=2026-10-21T12%3A00%3A00Z&end=2026-10-21T12%3A30%3A00Z
GET /api/cameras/<camera-id>/ai/coverage
```

### Detections and tracks

Alongside the grouped observations above, the AI worker stores every detection as structured data: camera, analysis time (UTC), class, confidence, normalized box (`x`, `y`, `width`, `height` in 0–1 of the frame), attributes and the model that produced it. A lightweight tracker links one camera's detections into **tracks**. It matches each detection to the previous frame's objects by box overlap (IoU) and appearance (a colour histogram of the box), so an object keeps one track ID while it moves and two objects that cross keep theirs. Tracker state is held per camera in the AI worker; after the worker or camera stream restarts, objects already in view start new tracks. Rules, search, object tracks in Playback, insights, redaction and PTZ auto-tracking build on this data.

Detections follow footage retention. Deleting a recorded interval or retention expiry deletes the detections inside that footage (unless other retained footage still covers the same moment). Deleting an archived camera's footage deletes all of its detections and tracks. Detections that no footage covers, for example on a motion-recorded camera between events, expire on the camera's retention schedule. A track whose detections are all gone is deleted.

Detections are written only inside analysed coverage. Each response lists the unanalysed intervals that overlap the request: no detection in such an interval means the frames were not analysed, not that the scene was empty.

```http
GET /api/ai/detections?camera_id=<camera-id>&start=<iso>&end=<iso>[&label=person][&track_id=<id>][&limit=500]
GET /api/ai/tracks?camera_id=<camera-id>&start=<iso>&end=<iso>[&label=vehicle][&limit=500]
GET /api/ai/tracks/<track-id>
```

These are footage reads: any signed-in user, or an API token with the `read` scope. The existing `/api/ai/events` responses and the Playback timeline's `ai_detections` and `ai_unanalyzed` fields are unchanged.

The automated tests use a deterministic detection fixture (`tests/fixtures/detection_scene.py`): synthetic people and a vehicle moving through generated H.264 footage, found by colour from the decoded pixels. Rules, search and tracks are tested against it without model variance. Model accuracy is measured separately and never asserted in CI.

The `GET /api/cameras/{camera_id}/ai` response reports model availability and the analysis state. Configuration changes require the administrator role; operators can search results and inspect coverage.

## CPU benchmark and acceptance limits

Run the reproducible CPU benchmark inside the built image with `python -m scripts.benchmark_ai`. It measures 100 448×256 model inferences after warm-up, reports CPU/runtime details and p50/p95 latency, and estimates the model-only maximum frames per second. It does not include network/FFmpeg decoding, simultaneous recording, storage, browser playback, or scene-dependent accuracy. Treat it as a per-host model measurement, not camera-count capacity certification. Before deployment, run it on the intended NVR host and include active recording workload; freeze the supported rate only after observing that workload. The benchmark currently measures CPU only; it does not certify GPU throughput.

Initial benchmark in the development container (OpenVINO 2026.2.1, two inference threads, x86_64): 100 frames, p50 25.22 ms, p95 33.34 ms, model-only estimate 37.56 FPS. The NVR host still needs its own measurement with recording active before setting an operating envelope.

## Controlled test

Enable a single camera with AI, verify `status=analyzed`, and confirm a controlled person/vehicle scene appears in `/api/ai/events`. Use **Find playback** to seek into the stored recording. Check `/api/cameras/{camera_id}/ai/coverage` for analyzed or failed intervals. Then disable AI, confirm AI status stops updating, and verify that the recording status and footage freshness continue independently. Also verify detections only appear inside configured zones and above the confidence threshold.

AI detections are search aids. This model is not trained or evaluated here for a particular site's cameras, lighting, or safety-critical decisions. Validate the intended device profiles and controlled scenarios before relying on results.
