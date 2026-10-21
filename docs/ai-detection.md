# Local person and vehicle detection

AI detection is disabled by default and runs locally on the NVR host. It uses OpenVINO's CPU execution provider and the Open Model Zoo `person-vehicle-bike-detection-2004` FP32 model. The model is based on MobileNetV2 with an ATSS detection head at 448×256 and is about 1.8 GFLOPs per frame. The model reports a 0.274 AP score on its own internal test set; this is not a camera compatibility or site accuracy guarantee. Its outputs are vehicle (class 0), person (class 1), and non-vehicle (class 2); this app stores only vehicle and person observations. Open Model Zoo models are Apache-2.0 licensed.

The container pins OpenVINO 2026.2.1 and the model's 2021.4 FP32 XML/BIN files. The Docker build verifies each artifact's SHA-256. Model sources and interface:

- [Open Model Zoo model card](https://github.com/openvinotoolkit/open_model_zoo/tree/master/models/intel/person-vehicle-bike-detection-2004)
- [OpenVINO supported-model list](https://docs.openvino.ai/2026/documentation/compatibility-and-support/supported-models.html)
- [OpenVINO Python API](https://docs.openvino.ai/2026/get-started/learn-openvino/openvino-samples/hello-batch.html)

## Configure

An administrator opens a camera's **Configure CPU person/vehicle detection** controls, chooses whether to analyze it, sets the analysis frame rate (0.1–5 FPS), minimum confidence, normalized rectangular zones, and whether to use the validated lower-resolution stream. Detection uses the selected camera's existing stream profile. Selecting the main stream is supported, but the low-resolution profile is recommended to reduce decoder and model work. The selected profile and AI analysis settings do not change the recording stream or recording policy.

Administrators can configure individual cameras or set inherited AI policy on a camera group. Camera-level overrides remain explicit and can be returned to group inheritance. The runtime defaults to four active camera streams, configurable from 1 to 16, and two OpenVINO inference threads (`NVR_AI_CPU_THREADS` can set 1–8). Active cameras use one serialized inference slot; each FFmpeg reader is sampled at its configured rate and can buffer only a bounded amount while waiting. Cameras beyond the active limit are marked `skipped`; slow inference is marked `delayed`, and runtime or stream faults are marked `failed`. No unbounded analysis queue is maintained. If AI is disabled, delayed, or fails, it does not stop or change the recorder. The UI and API expose AI coverage state and time of the last successful analysis. `failed`, `delayed`, `skipped`, and `unanalyzed` intervals are unknown coverage, and no detection means only that the configured model did not detect an object in the sampled frames.

New observations can be sent as signed `ai.detection` webhook events. Analysis failures and recovery transitions send `ai.failure` and `ai.recovered`. Select these types in each receiver's event filter. Detection results are grouped briefly by camera and label to avoid creating one record for every sampled frame; a new observation group gets a stable event ID for webhook retries. Capacity skips are shown as coverage gaps and do not raise a failure alert.

## Search and playback

Operators can search detections by camera, person/vehicle label, time range, and confidence-bearing observation, then seek to retained footage around the observation. Each observation records its server receipt time in UTC, model ID/version, frame size, confidence, and normalized bounding box. The timestamp is the NVR analysis time; it is not a camera-exposure timestamp. If footage does not cover the observation, the UI reports that no retained clip is available.

Example authenticated API calls:

```http
GET /api/ai/events?camera_id=<camera-id>&label=person&start=2026-10-21T12%3A00%3A00Z&end=2026-10-21T12%3A30%3A00Z
GET /api/cameras/<camera-id>/ai/coverage
```

The `GET /api/cameras/{camera_id}/ai` response reports model availability and the analysis state. Configuration changes require the administrator role; operators can search results and inspect coverage.

## CPU benchmark and acceptance limits

Run the reproducible benchmark inside the built image with `python -m scripts.benchmark_ai`. It measures 100 448×256 model inferences after warm-up, reports CPU/runtime details and p50/p95 latency, and estimates the model-only maximum frames per second. It does not include network/FFmpeg decoding, simultaneous recording, storage, browser playback, or scene-dependent accuracy. Treat it as a per-host model measurement, not camera-count capacity certification. Before deployment, run it on the intended NVR host and include active recording workload; freeze the supported rate only after observing that workload.

Initial benchmark in the development container (OpenVINO 2026.2.1, two inference threads, x86_64): 100 frames, p50 25.22 ms, p95 33.34 ms, model-only estimate 37.56 FPS. The NVR host still needs its own measurement with recording active before setting an operating envelope.

## Controlled test

Enable a single camera with AI, verify `status=analyzed`, and confirm a controlled person/vehicle scene appears in `/api/ai/events`. Use **Find playback** to seek into the stored recording. Check `/api/cameras/{camera_id}/ai/coverage` for analyzed or failed intervals. Then disable AI, confirm AI status stops updating, and verify that the recording status and footage freshness continue independently. Also verify detections only appear inside configured zones and above the confidence threshold.

AI detections are search aids. This model is not trained or evaluated here for a particular site's cameras, lighting, or safety-critical decisions. Validate the intended device profiles and controlled scenarios before relying on results.
