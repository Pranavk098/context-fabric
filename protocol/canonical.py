"""RFC 8785 JSON Canonicalization Scheme (JCS) + sha256 helpers.

Implements the subset of JCS needed by CPP v1.0: object-key ordering by UTF-16 code
units, no insignificant whitespace, JSON string escaping, and ECMAScript-style number
serialization for the value types used in packets.

ponytail: number formatting covers integers and finite short decimals (repr-based); it
does not implement full ECMAScript exponent formatting (e.g. 1e21 -> "1e+21"). Packet
values never reach that range. Upgrade to the `rfc8785` package if that ever changes.
"""

from __future__ import annotations

import hashlib
import math
from typing import Any

_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


def _escape_string(s: str) -> str:
    out = ['"']
    for ch in s:
        if ch in _ESCAPES:
            out.append(_ESCAPES[ch])
        elif ch < "\x20":
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _dumps_number(n: int | float) -> str:
    if isinstance(n, int):
        return str(n)
    if math.isnan(n) or math.isinf(n):
        raise ValueError("JCS forbids NaN/Infinity")
    if n == 0:
        return "0"  # normalizes -0.0 -> "0"
    s = repr(float(n))
    if s.endswith(".0"):
        s = s[:-2]
    return s


def _utf16_units(s: str) -> list[int]:
    b = s.encode("utf-16-le")
    return [b[i] | (b[i + 1] << 8) for i in range(0, len(b), 2)]


def _serialize(obj: Any) -> str:
    if obj is None:
        return "null"
    if isinstance(obj, bool):  # must precede int (bool is subclass of int)
        return "true" if obj else "false"
    if isinstance(obj, int):
        return _dumps_number(obj)
    if isinstance(obj, float):
        return _dumps_number(obj)
    if isinstance(obj, str):
        return _escape_string(obj)
    if isinstance(obj, (list, tuple)):
        return "[" + ",".join(_serialize(v) for v in obj) + "]"
    if isinstance(obj, dict):
        for k in obj:
            if not isinstance(k, str):
                raise TypeError("JCS object keys must be strings")
        keys = sorted(obj.keys(), key=_utf16_units)
        return "{" + ",".join(f"{_escape_string(k)}:{_serialize(obj[k])}" for k in keys) + "}"
    raise TypeError(f"JCS cannot serialize type {type(obj).__name__}")


def canonicalize(obj: Any) -> bytes:
    """Return the RFC 8785 canonical UTF-8 byte encoding of ``obj``."""
    return _serialize(obj).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def content_hash(obj: Any) -> str:
    """sha256 (hex) of the canonical form of ``obj``."""
    return sha256_hex(canonicalize(obj))


def packet_content_hash(packet: Any) -> str:
    """Content hash of a ContextPacket (ADR-0003 §5).

    The ``hashes`` block is emptied before hashing: both ``signature`` and the
    ``content_hash`` field itself are excluded, otherwise the hash would depend on its
    own value (a self-reference) and would not be idempotent. ``packet`` may be a
    pydantic model or a plain dict.
    """
    payload = packet.model_dump(mode="json") if hasattr(packet, "model_dump") else dict(packet)
    payload["hashes"] = {}
    return content_hash(payload)


if __name__ == "__main__":
    # Self-check: JCS spec vectors + determinism.
    assert canonicalize({"b": 1, "a": 2}) == b'{"a":2,"b":1}'
    assert canonicalize({"a": [1, 2, 3]}) == b'{"a":[1,2,3]}'
    assert canonicalize(1.0) == b"1"
    assert canonicalize(0.5) == b"0.5"
    assert canonicalize(-0.0) == b"0"
    assert canonicalize({"s": 'he said "hi"\n'}) == b'{"s":"he said \\"hi\\"\\n"}'
    assert canonicalize("\u20ac") == '"\u20ac"'.encode("utf-8")  # non-ASCII emitted literally
    # key order: UTF-16 code units ("\u20ac" is 0x20AC > 'z' 0x7A)
    assert canonicalize({"\u20ac": 1, "z": 2}) == '{"z":2,"\u20ac":1}'.encode("utf-8")
    # h(1.0) style determinism
    assert content_hash({"x": 1}) == content_hash({"x": 1})
    print("canonical.py self-check: OK")
