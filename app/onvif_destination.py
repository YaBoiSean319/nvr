"""Where a camera may send the NVR (#300): a service address a camera advertises is only ever used on the host the administrator entered.

A camera (or something answering in its place) can claim its PTZ, Media or Media2 service lives anywhere: a loopback port, a link-local metadata
address, another machine on the network. The NVR holds the camera's digest credentials and would send them, and a SOAP body, there. So every
advertised address is moved onto the configured host, keeping only the port and path it advertised (cameras do serve on other ports), and
an advertised ``http`` address on a camera configured over ``https`` is refused rather than downgraded.
"""
from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


class UnsafeDestination(ValueError):
    """The camera advertised a service address that cannot be used."""


def confine(configured_url: str, advertised: str) -> str:
    """``advertised`` rehosted onto the host of ``configured_url``; raises ``UnsafeDestination`` when it is not a plain http(s) address."""
    base = urlsplit(configured_url.strip())
    parts = urlsplit(advertised.strip())
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise UnsafeDestination("The camera reported an invalid service address")
    if base.scheme == "https" and parts.scheme != "https":
        raise UnsafeDestination("The camera reported a service address without encryption")
    try:
        port = parts.port
    except ValueError:
        raise UnsafeDestination("The camera reported an invalid service port") from None
    host = base.hostname or ""
    shown = f"[{host}]" if ":" in host else host
    return urlunsplit((parts.scheme, f"{shown}:{port}" if port else shown, parts.path, parts.query[:256], ""))
