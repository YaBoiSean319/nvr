"""Incidents, signed webhooks and the external heartbeat."""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Query, Response, status
from pydantic import BaseModel, Field, field_validator

from app.principal import Administrator, CurrentUser
from app.store import connect
from app.incidents import acknowledge_incident, incident_view, list_incidents
from app.webhooks import EVENT_TYPES as WEBHOOK_EVENT_TYPES, create_endpoint as create_webhook_endpoint, delete_endpoint as delete_webhook_endpoint, enqueue_test as enqueue_webhook_test, list_deliveries as list_webhook_deliveries, list_endpoints as list_webhook_endpoints, rotate_secret as rotate_webhook_secret, update_endpoint as update_webhook_endpoint
from app.heartbeat import configure_heartbeat, disable_heartbeat, heartbeat_status, queue_heartbeat_test, rotate_heartbeat_secret

router = APIRouter()


class WebhookCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    url: str = Field(min_length=8, max_length=2048)
    events: list[str] = Field(min_length=1, max_length=8)
    allow_private_receiver: bool = False

    @field_validator("events")
    @classmethod
    def validate_events(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value) or any(event not in WEBHOOK_EVENT_TYPES - {"webhook.test"} for event in value):
            raise ValueError("Select one or more supported incident event types")
        return value


class WebhookUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    url: str | None = Field(default=None, min_length=8, max_length=2048)
    events: list[str] | None = Field(default=None, min_length=1, max_length=8)
    allow_private_receiver: bool | None = None
    enabled: bool | None = None

    @field_validator("events")
    @classmethod
    def validate_events(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and (len(set(value)) != len(value) or any(event not in WEBHOOK_EVENT_TYPES - {"webhook.test"} for event in value)):
            raise ValueError("Select one or more supported incident event types")
        return value


class HeartbeatConfigRequest(BaseModel):
    target_url: str = Field(min_length=8, max_length=2048)
    interval_seconds: int = Field(default=60, ge=30, le=900)
    allow_private_receiver: bool = False


@router.get("/api/incidents")
def get_incidents(
    _: CurrentUser,
    state: str | None = Query(default=None, pattern="^(active|acknowledged|recovered)$"),
) -> dict[str, object]:
    with connect() as db:
        return {"incidents": list_incidents(db, state=state)}


@router.post("/api/incidents/{incident_id}/acknowledge")
def post_incident_acknowledgement(
    incident_id: str,
    user: CurrentUser,
) -> dict[str, object]:
    with connect() as db:
        incident = acknowledge_incident(db, incident_id, int(user["id"]))
        if incident is None:
            raise HTTPException(status_code=404, detail="Incident not found")
        if incident.pop("acknowledgement_conflict", False):
            raise HTTPException(status_code=409, detail="Incident has already recovered")
        row = db.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
        return incident_view(db, row)


@router.get("/api/webhooks/event-types")
def get_webhook_event_types(_: Administrator) -> dict[str, object]:
    return {"event_types": sorted(WEBHOOK_EVENT_TYPES)}


@router.get("/api/heartbeat")
def get_heartbeat_status(_: Administrator) -> dict[str, object]:
    return heartbeat_status()


@router.put("/api/heartbeat")
def put_heartbeat_config(payload: HeartbeatConfigRequest, user: Administrator) -> dict[str, object]:
    try:
        result, secret = configure_heartbeat(payload.target_url, payload.interval_seconds, payload.allow_private_receiver, int(user["id"]))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    with connect() as db:
        db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                   (int(user["id"]), "heartbeat_configured", "heartbeat", "1", json.dumps({"interval_seconds": payload.interval_seconds, "allow_private_receiver": payload.allow_private_receiver})))
    return {"heartbeat": result, "signing_secret": secret, "secret_notice": "A new signing secret is shown once. Store it at the external receiver." if secret else None}


@router.post("/api/heartbeat/test", status_code=status.HTTP_202_ACCEPTED)
def post_heartbeat_test(user: Administrator) -> dict[str, str]:
    if not queue_heartbeat_test():
        raise HTTPException(status_code=409, detail="Configure and enable an external heartbeat receiver first")
    with connect() as db:
        db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                   (int(user["id"]), "heartbeat_test_requested", "heartbeat", "1", "{}"))
    return {"status": "queued"}


@router.post("/api/heartbeat/rotate-secret")
def post_heartbeat_secret_rotation(user: Administrator) -> dict[str, str]:
    secret = rotate_heartbeat_secret()
    if secret is None:
        raise HTTPException(status_code=409, detail="Configure an enabled heartbeat receiver first")
    with connect() as db:
        db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                   (int(user["id"]), "heartbeat_secret_rotated", "heartbeat", "1", "{}"))
    return {"signing_secret": secret, "secret_notice": "This secret is shown once. Update the receiver before the next heartbeat."}


@router.delete("/api/heartbeat", status_code=status.HTTP_204_NO_CONTENT)
def delete_heartbeat_config(user: Administrator) -> Response:
    disable_heartbeat()
    with connect() as db:
        db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                   (int(user["id"]), "heartbeat_disabled", "heartbeat", "1", "{}"))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/api/webhooks")
def get_webhooks(_: Administrator) -> dict[str, object]:
    return {"webhooks": list_webhook_endpoints()}


@router.post("/api/webhooks", status_code=status.HTTP_201_CREATED)
def post_webhook(payload: WebhookCreateRequest, user: Administrator) -> dict[str, object]:
    try:
        endpoint, secret = create_webhook_endpoint(payload.name, payload.url, payload.events, payload.allow_private_receiver, int(user["id"]))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    with connect() as db:
        db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                   (int(user["id"]), "webhook_created", "webhook", endpoint["id"], json.dumps({"name": endpoint["name"], "events": endpoint["events"], "allow_private_receiver": endpoint["allow_private_receiver"]})))
    return {"webhook": endpoint, "signing_secret": secret, "secret_notice": "This secret is shown once. Store it at the receiver to verify signed deliveries."}


@router.patch("/api/webhooks/{webhook_id}")
def patch_webhook(webhook_id: str, payload: WebhookUpdateRequest, user: Administrator) -> dict[str, object]:
    fields = payload.model_dump(exclude_unset=True)
    try:
        endpoint = update_webhook_endpoint(webhook_id, fields)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if endpoint is None:
        raise HTTPException(status_code=404, detail="Webhook endpoint not found")
    with connect() as db:
        db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                   (int(user["id"]), "webhook_updated", "webhook", webhook_id, json.dumps({"fields": sorted(fields), "events": endpoint["events"], "enabled": endpoint["enabled"]})))
    return {"webhook": endpoint}


@router.post("/api/webhooks/{webhook_id}/rotate-secret")
def post_webhook_secret_rotation(webhook_id: str, user: Administrator) -> dict[str, str]:
    secret = rotate_webhook_secret(webhook_id)
    if secret is None:
        raise HTTPException(status_code=404, detail="Webhook endpoint not found")
    with connect() as db:
        db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                   (int(user["id"]), "webhook_secret_rotated", "webhook", webhook_id, "{}"))
    return {"signing_secret": secret, "secret_notice": "This secret is shown once. Update the receiver before the next delivery."}


@router.delete("/api/webhooks/{webhook_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_webhook(webhook_id: str, user: Administrator) -> Response:
    if not delete_webhook_endpoint(webhook_id):
        raise HTTPException(status_code=404, detail="Webhook endpoint not found")
    with connect() as db:
        db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                   (int(user["id"]), "webhook_deleted", "webhook", webhook_id, "{}"))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/api/webhooks/{webhook_id}/test", status_code=status.HTTP_202_ACCEPTED)
def post_webhook_test(webhook_id: str, user: Administrator) -> dict[str, object]:
    event_id = enqueue_webhook_test(webhook_id, int(user["id"]))
    if event_id is None:
        raise HTTPException(status_code=404, detail="Enabled webhook endpoint not found")
    with connect() as db:
        db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                   (int(user["id"]), "webhook_test_requested", "webhook", webhook_id, json.dumps({"event_id": event_id})))
    return {"event_id": event_id, "status": "queued"}


@router.get("/api/webhooks/deliveries")
def get_webhook_deliveries(
    _: Administrator,
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, object]:
    return {"deliveries": list_webhook_deliveries(limit)}
