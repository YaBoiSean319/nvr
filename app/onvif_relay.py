"""Pulse an ONVIF relay output: switch it on, hold it for the configured time, switch it off."""
from __future__ import annotations

import asyncio
import xml.etree.ElementTree as ET

import httpx

from .onvif_stream import DEVICE, SCHEMA, SOAP, _local, _parse_response

GET_RELAYS = "http://www.onvif.org/ver10/device/wsdl/GetRelayOutputs"
SET_STATE = "http://www.onvif.org/ver10/device/wsdl/SetRelayOutputState"


def _envelope(action: str, body: str) -> bytes:
    return (f'<?xml version="1.0" encoding="UTF-8"?><s:Envelope xmlns:s="{SOAP}" xmlns:tds="{DEVICE}" xmlns:tt="{SCHEMA}">'
            f'<s:Header><s:Action s:mustUnderstand="1">{action}</s:Action></s:Header><s:Body>{body}</s:Body></s:Envelope>').encode("utf-8")


async def _call(client: httpx.AsyncClient, url: str, action: str, body: str) -> ET.Element:
    response = await client.post(url, content=_envelope(action, body), headers={"Content-Type": f'application/soap+xml; charset=utf-8; action="{action}"'})
    response.raise_for_status()
    return _parse_response(response.content)


def _escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


async def pulse(device_service_url: str, username: str, password: str, relay_token: str | None, duration_seconds: float) -> str:
    """Returns the relay token pulsed. The relay is switched off again even if holding it is interrupted."""
    auth = httpx.DigestAuth(username, password) if username else None
    async with httpx.AsyncClient(auth=auth, timeout=httpx.Timeout(10, connect=5), trust_env=False, follow_redirects=False) as client:
        token = relay_token
        if not token:
            relays = await _call(client, device_service_url, GET_RELAYS, "<tds:GetRelayOutputs/>")
            token = next((node.get("token") for node in relays.iter() if _local(node.tag) == "RelayOutputs" and node.get("token")), None)
            if not token:
                raise LookupError("The camera reports no relay outputs")

        def state(value: str) -> str:
            return f"<tds:SetRelayOutputState><tds:RelayOutputToken>{_escape(token)}</tds:RelayOutputToken><tds:LogicalState>{value}</tds:LogicalState></tds:SetRelayOutputState>"

        await _call(client, device_service_url, SET_STATE, state("active"))
        try:
            await asyncio.sleep(duration_seconds)
        finally:
            await _call(client, device_service_url, SET_STATE, state("inactive"))
        return token
