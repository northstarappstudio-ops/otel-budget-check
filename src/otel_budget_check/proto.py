"""Minimal protobuf wire-format decoder (zero dependencies).

Decodes OTLP protobuf payloads into plain dicts/lists that mirror the OTLP
JSON mapping, so the analyzer can treat JSON and binary protobuf identically.
Supports varint, fixed64, length-delimited, fixed32. Unknown fields skipped.
"""

import struct

WIRE_VARINT = 0
WIRE_FIXED64 = 1
WIRE_LEN = 2
WIRE_FIXED32 = 5


class ProtoError(ValueError):
    pass


def _read_varint(buf, pos):
    result = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise ProtoError("truncated varint")
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
        if shift > 63:
            raise ProtoError("varint too long")


def decode_message(buf):
    """Decode a protobuf message -> {field_number: [values]}."""
    fields = {}
    pos = 0
    n = len(buf)
    while pos < n:
        key, pos = _read_varint(buf, pos)
        field = key >> 3
        wire = key & 0x7
        if wire == WIRE_VARINT:
            val, pos = _read_varint(buf, pos)
            fields.setdefault(field, []).append(val)
        elif wire == WIRE_FIXED64:
            if pos + 8 > n:
                raise ProtoError("truncated fixed64")
            fields.setdefault(field, []).append(buf[pos:pos + 8])
            pos += 8
        elif wire == WIRE_LEN:
            ln, pos = _read_varint(buf, pos)
            if pos + ln > n:
                raise ProtoError("truncated length-delimited")
            fields.setdefault(field, []).append(buf[pos:pos + ln])
            pos += ln
        elif wire == WIRE_FIXED32:
            if pos + 4 > n:
                raise ProtoError("truncated fixed32")
            fields.setdefault(field, []).append(buf[pos:pos + 4])
            pos += 4
        else:
            raise ProtoError("unsupported wire type %d" % wire)
    return fields


def f64(raw):
    return struct.unpack("<d", raw)[0]


def f32(raw):
    return struct.unpack("<f", raw)[0]


def u64(raw):
    return int.from_bytes(raw, "little", signed=False)