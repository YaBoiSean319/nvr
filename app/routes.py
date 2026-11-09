"""Every route the application serves, whatever FastAPI version builds it.

Up to FastAPI 0.136 ``app.routes`` lists each included route. From 0.137 an included router stays nested (an ``_IncludedRouter`` holding the
router it came from), so code that walks ``app.routes`` finds nothing: the scopes written into the API schema and the tests that check every
route's access rule would silently cover no routes at all (#285).

Flattening through ``original_router`` is only right while routers are included without a prefix or dependencies, which is how every router is
included here (``application.create_app``). If one ever is, this raises instead of reporting wrong paths: such routes would need the include's
prefix and dependencies applied, and that is the moment to look at what FastAPI offers then.
"""
from __future__ import annotations

from typing import Iterable

from fastapi import FastAPI
from starlette.routing import BaseRoute


def _flatten(routes: Iterable[BaseRoute]) -> list[BaseRoute]:
    found: list[BaseRoute] = []
    for route in routes:
        router = getattr(route, "original_router", None)
        if router is None:
            found.append(route)
            continue
        context = getattr(route, "include_context", None)
        if getattr(context, "prefix", "") or getattr(context, "dependencies", None):
            raise RuntimeError("A router is included with a prefix or dependencies; all_routes() has to apply them before it can list its routes")
        found.extend(_flatten(router.routes))
    return found


def all_routes(app: FastAPI) -> list[BaseRoute]:
    return _flatten(app.routes)
