"""Domain routers, registered by the application factory in this order."""
from __future__ import annotations

from fastapi import APIRouter

from . import system

ROUTERS: tuple[APIRouter, ...] = (
    system.router,
)
