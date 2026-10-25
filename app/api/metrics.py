"""``/metrics`` in Prometheus text format, for an API token with the ``metrics`` scope."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response, status

from app import prometheus
from app.principal import CurrentUser, requires_scope
from app.workers import supervised

router = APIRouter()


@router.get("/metrics", include_in_schema=False)
@requires_scope("metrics")
def prometheus_metrics(user: CurrentUser) -> Response:
    # A person's session is never a scraper: the scope is one only a token can hold.
    if user.scopes is None or user.wall:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Metrics need an API token with the 'metrics' scope")
    return Response(prometheus.render(supervised()), media_type="text/plain; version=0.0.4; charset=utf-8", headers={"Cache-Control": "no-store"})
