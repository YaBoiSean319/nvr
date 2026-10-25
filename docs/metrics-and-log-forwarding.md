# Prometheus metrics and log forwarding

## `/metrics`

`GET /metrics` returns the NVR's state in the Prometheus text format (0.0.4). It needs an **API token with the `metrics` scope** (Settings › Users & access › API tokens). A person's session is refused, including an administrator's, and so is a token without the scope. A token holding only `metrics` can read nothing else, and `admin` implies every scope, as it does elsewhere.

```yaml
scrape_configs:
  - job_name: nvr
    scheme: https
    authorization: { credentials_file: /etc/prometheus/nvr-token }   # the nvrt_… secret
    static_configs: [{ targets: ["nvr.example.org"] }]
```

| Series | Type | Labels | Meaning |
|---|---|---|---|
| `nvr_camera_recording_state` | gauge | `camera_id`, `camera`, `state` | 1 for the state each camera's recording is in |
| `nvr_camera_recording_enabled` | gauge | camera | 1 when recording is on for the camera |
| `nvr_camera_last_footage_write_timestamp_seconds` | gauge | camera | when footage was last written |
| `nvr_segments_written_total` | counter | camera | segments that became playable |
| `nvr_segment_write_latency_seconds` | histogram | camera, `le` | seconds from the end of a segment's footage until it was indexed and playable (buckets 1, 2, 5, 10, 30, 60, 120, 300 s) |
| `nvr_segments_dropped_total` | counter | camera | segments the recorder produced and could not keep: a file too small or missing, an unreadable index row, or a motion clip that could not be assembled. Counted once per segment |
| `nvr_live_viewers`, `nvr_live_viewer_slots` | gauge | – | live video streams and the number allowed |
| `nvr_live_audio_listeners`, `nvr_live_audio_listener_slots` | gauge | – | live audio listeners and the number allowed |
| `nvr_worker_up`, `nvr_worker_restarts_total`, `nvr_worker_heartbeat_timestamp_seconds` | gauge/counter | `worker` | liveness, restarts and last heartbeat of each worker |
| `nvr_export_jobs` | gauge | `state` | export jobs by state |
| `nvr_export_oldest_queued_age_seconds` | gauge | – | how long the oldest queued export has waited |
| `nvr_ai_cameras` | gauge | `state` | cameras with analysis on, by state |
| `nvr_ai_backlog_cameras` | gauge | – | cameras whose analysis is delayed or failing |
| `nvr_ai_seconds_since_last_analysis` | gauge | camera | seconds since the camera's last analysed frame |
| `nvr_storage_free_bytes`, `nvr_storage_capacity_bytes` | gauge | `volume` | free space and capacity of each recording volume |
| `nvr_storage_volume_state` | gauge | `volume`, `state` | 1 for each volume's state |
| `nvr_backup_enabled`, `nvr_backup_age_seconds`, `nvr_backup_stale` | gauge | – | configuration backups |
| `nvr_log_forward_enabled`, `nvr_log_forward_sent_total`, `nvr_log_forward_dropped_total` | gauge/counter | – | log forwarding, for the web process |
| `nvr_build_info` | gauge | `version` | the NVR version |

The recorder's counters are kept in the database, so they survive a restart. Archived cameras are not reported. Metrics never carry a camera address, stream URL, credential or password: they read none of the columns that hold them.

## Log forwarding

Set `NVR_LOG_FORWARD_URL` on the container to send the NVR's application logs off the host. The supervisor and every worker forward their own logs.

| URL | Sends |
|---|---|
| `syslog+tcp://host:514` | RFC 5424 syslog over TCP |
| `syslog+tls://host:6514` | RFC 5424 syslog over TLS |
| `otlp+http://host:4318` | OTLP logs, JSON over HTTP (`POST /v1/logs`; a path in the URL replaces it) |
| `otlp+https://host:4318` | the same over HTTPS |

| Variable | Default | |
|---|---|---|
| `NVR_LOG_FORWARD_CA` | system CAs | a CA bundle to trust for TLS and HTTPS |
| `NVR_LOG_FORWARD_LEVEL` | `INFO` | the lowest level sent |
| `NVR_LOG_FORWARD_QUEUE` | `2000` | records that may wait for the endpoint, per process |

Syslog messages use the daemon facility, the application name `nvr-<process>` (`nvr-web`, `nvr-recorder`, …), the logger as the message id, and octet-counting framing (RFC 6587, as RFC 5425 requires over TLS). A traceback is part of its message. OTLP records carry the severity, the logger name, and `service.name=nvr`, `service.version` and `nvr.process` as resource attributes.

**Forwarding never blocks the NVR.** A log call only puts the record on a bounded queue; a thread sends it. When the queue is full, the endpoint is unreachable or it refuses a batch, records are dropped and counted, the connection is retried with a backoff up to 30 seconds, and requests and recording carry on. A connection the collector has closed is replaced before the next batch. A bad `NVR_LOG_FORWARD_URL` is reported at start-up and forwarding stays off.

**What leaves the process is scrubbed.** Before sending, every URL (`rtsp://…`, `http://…`) becomes `[url redacted]`, and so does any occurrence of a configured camera's host or address, ONVIF service address, user name or password (of four characters or more, matched as whole words), read from the cameras every minute. The scrub applies to forwarded logs only; the container's own log output is unchanged. Uvicorn's access log is not forwarded.

## Not covered

- Syslog over UDP, and OTLP over gRPC or protobuf.
- Delivery is at most once: records dropped while the endpoint is down are not replayed, and a batch in flight when a connection breaks may be lost.
- A secret shorter than four characters is not matched by value, though it normally sits inside a URL, which is always removed.
- Metrics for the live viewers and log forwarding describe the web process only.
