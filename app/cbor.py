"""The small part of CBOR (RFC 8949) that WebAuthn uses: integers, byte and text strings,
arrays, maps, booleans and null. Indefinite lengths and tags are refused."""
from __future__ import annotations

from typing import Any

MAX_DEPTH = 8
MAX_ITEMS = 1024


class CborError(ValueError):
    pass


def decode(data: bytes) -> Any:
    """The one value in ``data``; anything after it is an error."""
    value, end = decode_first(data, 0)
    if end != len(data):
        raise CborError("unexpected data after the value")
    return value


def decode_first(data: bytes, offset: int = 0, depth: int = 0) -> tuple[Any, int]:
    """The value starting at ``offset`` and the offset just after it."""
    if depth > MAX_DEPTH:
        raise CborError("too deeply nested")
    if offset >= len(data):
        raise CborError("truncated")
    initial = data[offset]
    major, info = initial >> 5, initial & 0x1F
    offset += 1
    if major == 7:
        if info == 20:
            return False, offset
        if info == 21:
            return True, offset
        if info == 22:
            return None, offset
        raise CborError("unsupported simple value")
    if info < 24:
        argument = info
    elif info in (24, 25, 26, 27):
        size = 1 << (info - 24)
        if offset + size > len(data):
            raise CborError("truncated")
        argument = int.from_bytes(data[offset:offset + size], "big")
        offset += size
    else:
        raise CborError("indefinite lengths are not supported")
    if major == 0:
        return argument, offset
    if major == 1:
        return -1 - argument, offset
    if major in (2, 3):
        if offset + argument > len(data):
            raise CborError("truncated")
        raw = data[offset:offset + argument]
        offset += argument
        if major == 2:
            return bytes(raw), offset
        try:
            return raw.decode("utf-8"), offset
        except UnicodeDecodeError as exc:
            raise CborError("invalid text") from exc
    if argument > MAX_ITEMS:
        raise CborError("too many items")
    if major == 4:
        items = []
        for _ in range(argument):
            item, offset = decode_first(data, offset, depth + 1)
            items.append(item)
        return items, offset
    if major == 5:
        mapping: dict[Any, Any] = {}
        for _ in range(argument):
            key, offset = decode_first(data, offset, depth + 1)
            value, offset = decode_first(data, offset, depth + 1)
            if isinstance(key, (list, dict)) or key in mapping:
                raise CborError("invalid map key")
            mapping[key] = value
        return mapping, offset
    raise CborError("tags are not supported")


def encode(value: Any) -> bytes:
    """The inverse of ``decode``, for tests and for building WebAuthn fixtures."""
    def head(major: int, argument: int) -> bytes:
        if argument < 24:
            return bytes([major << 5 | argument])
        for info, size in ((24, 1), (25, 2), (26, 4), (27, 8)):
            if argument < 1 << (8 * size):
                return bytes([major << 5 | info]) + argument.to_bytes(size, "big")
        raise CborError("integer too large")

    if value is None:
        return b"\xf6"
    if value is True:
        return b"\xf5"
    if value is False:
        return b"\xf4"
    if isinstance(value, int):
        return head(0, value) if value >= 0 else head(1, -1 - value)
    if isinstance(value, (bytes, bytearray)):
        return head(2, len(value)) + bytes(value)
    if isinstance(value, str):
        raw = value.encode("utf-8")
        return head(3, len(raw)) + raw
    if isinstance(value, list):
        return head(4, len(value)) + b"".join(encode(item) for item in value)
    if isinstance(value, dict):
        return head(5, len(value)) + b"".join(encode(key) + encode(item) for key, item in value.items())
    raise CborError(f"cannot encode {type(value).__name__}")
