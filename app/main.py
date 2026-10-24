"""ASGI entry point: the production application, assembled by the factory."""
from __future__ import annotations

from .application import create_app

app = create_app()
