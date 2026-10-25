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
    # The scope is checked for a token; a person's browser session has none, so it is refused here.
    if user.scopes is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Metrics need an API token with the 'metrics' scope")
    return Response(prometheus.render(supervised()), media_type="text/plain; version=0.0.4; charset=utf-8",
                    headers={"Cache-Control": "no-store"})
