"""
ASTM F3411 (Remote ID) Message Parser
Supports: Basic ID (Type 0), Location/Vector (Type 1), Message Pack (Type 0xF)

Tested against: basic ID, location, message pack, garbage/malformed, edge cases.

Bug fix history:
  - Heading correctly read from byte offset 2 (Direction field), NOT byte 4
    (byte 4 is Vertical Speed — earlier parser conflated the two fields).
  - Message Pack wrapper (type 0xF) now correctly unwrapped before parsing
    individual sub-messages.
"""

import struct
import logging

logger = logging.getLogger(__name__)

MSG_TYPE_BASIC_ID = 0x0
MSG_TYPE_LOCATION = 0x1
MSG_TYPE_AUTH = 0x2
MSG_TYPE_SELF_ID = 0x3
MSG_TYPE_SYSTEM = 0x4
MSG_TYPE_OPERATOR_ID = 0x5
MSG_TYPE_MESSAGE_PACK = 0xF

LOCATION_MSG_LEN = 25
BASIC_ID_MSG_LEN = 25
MESSAGE_PACK_HEADER_LEN = 3


def _decode_lat_lon(raw_int32):
    """Decode signed int32 (deg * 1e7) to float degrees."""
    return raw_int32 / 1e7


def _decode_altitude(raw_uint16):
    """Decode altitude field: value * 0.5 - 1000 (per ASTM F3411)."""
    return (raw_uint16 * 0.5) - 1000.0


def _decode_speed(raw_byte, multiplier_flag):
    """Decode speed byte. If multiplier flag set, use extended range."""
    if raw_byte == 0xFF:
        return None  # invalid/unknown per spec
    if not multiplier_flag:
        return round(raw_byte * 0.25, 2)
    return round(0.25 * 255 + (raw_byte - 255) * 0.75, 2)  # rare edge case


def _decode_heading(direction_byte, ew_flag):
    """
    Decode heading/track from the Direction byte (offset 2 of Location msg).
    0-179 -> direct degrees. If E/W flag set, add 180 (range 180-359).
    """
    if direction_byte > 179:
        return None  # reserved/invalid per spec
    heading = direction_byte
    if ew_flag:
        heading += 180
    return heading % 360


def _parse_basic_id(msg: bytes) -> dict:
    if len(msg) < BASIC_ID_MSG_LEN:
        raise ValueError(f"Basic ID message too short: {len(msg)} bytes")

    id_type = (msg[1] >> 4) & 0x0F
    ua_type = msg[1] & 0x0F
    uas_id_raw = msg[2:22]
    uas_id = uas_id_raw.split(b'\x00', 1)[0].decode('ascii', errors='replace')

    return {
        "id": uas_id,
        "lat": None,
        "lon": None,
        "alt": None,
        "heading": None,
        "speed": None,
        "message_type": "basic_id",
        "id_type": id_type,
        "ua_type": ua_type,
    }


def _parse_location(msg: bytes) -> dict:
    if len(msg) < LOCATION_MSG_LEN:
        raise ValueError(f"Location message too short: {len(msg)} bytes")

    status_byte = msg[1]
    ew_flag = bool((status_byte >> 1) & 0x01)
    speed_multiplier = bool(status_byte & 0x01)

    # --- FIXED: heading comes from byte offset 2, NOT byte 4 ---
    direction_byte = msg[2]
    heading = _decode_heading(direction_byte, ew_flag)

    speed_byte = msg[3]
    speed = _decode_speed(speed_byte, speed_multiplier)

    # byte 4 = vertical speed (signed, 0.5 m/s per unit) — not heading!
    vspeed_raw = struct.unpack('b', msg[4:5])[0]
    vertical_speed = round(vspeed_raw * 0.5, 2)

    lat_raw = struct.unpack('<i', msg[5:9])[0]
    lon_raw = struct.unpack('<i', msg[9:13])[0]
    lat = _decode_lat_lon(lat_raw)
    lon = _decode_lat_lon(lon_raw)

    pressure_alt_raw = struct.unpack('<H', msg[13:15])[0]
    geo_alt_raw = struct.unpack('<H', msg[15:17])[0]
    geo_alt = _decode_altitude(geo_alt_raw)

    return {
        "id": None,
        "lat": lat,
        "lon": lon,
        "alt": geo_alt,
        "heading": heading,
        "speed": speed,
        "message_type": "location",
        "vertical_speed": vertical_speed,
    }


_PARSERS = {
    MSG_TYPE_BASIC_ID: _parse_basic_id,
    MSG_TYPE_LOCATION: _parse_location,
}


def _parse_single_message(msg: bytes) -> dict:
    if len(msg) < 1:
        raise ValueError("Empty message")

    header = msg[0]
    msg_type = (header >> 4) & 0x0F

    parser_fn = _PARSERS.get(msg_type)
    if parser_fn is None:
        return {
            "id": None,
            "lat": None,
            "lon": None,
            "alt": None,
            "heading": None,
            "speed": None,
            "message_type": f"unsupported_0x{msg_type:X}",
        }

    return parser_fn(msg)


def parse_remoteid_message(raw_bytes: bytes) -> dict:
    """
    Parse a raw Remote ID message (single or Message Pack wrapped).

    Returns a dict with keys:
        id, lat, lon, alt, heading, speed, message_type

    For Message Pack input, returns the FIRST Location message found
    (preferred), else the first Basic ID message, else the first
    sub-message of any kind.

    Use parse_remoteid_message_pack() to get ALL sub-messages at once.

    Raises ValueError on malformed/garbage input.
    """
    if not raw_bytes or len(raw_bytes) < 1:
        raise ValueError("Empty or null message")

    header = raw_bytes[0]
    msg_type = (header >> 4) & 0x0F

    if msg_type == MSG_TYPE_MESSAGE_PACK:
        sub_messages = parse_remoteid_message_pack(raw_bytes)
        for m in sub_messages:
            if m["message_type"] == "location":
                return m
        for m in sub_messages:
            if m["message_type"] == "basic_id":
                return m
        if sub_messages:
            return sub_messages[0]
        raise ValueError("Message Pack contained no parseable sub-messages")

    return _parse_single_message(raw_bytes)


def parse_remoteid_message_pack(raw_bytes: bytes) -> list:
    """
    Unwrap a Message Pack (type 0xF) and parse each sub-message.
    Returns a list of parsed dicts (same shape as parse_remoteid_message).
    """
    if len(raw_bytes) < MESSAGE_PACK_HEADER_LEN:
        raise ValueError("Message Pack too short for header")

    header = raw_bytes[0]
    msg_type = (header >> 4) & 0x0F
    if msg_type != MSG_TYPE_MESSAGE_PACK:
        raise ValueError("Not a Message Pack (header type mismatch)")

    msg_size = raw_bytes[1]
    num_messages = raw_bytes[2]

    if msg_size <= 0:
        raise ValueError("Invalid message size in Message Pack header")

    expected_len = MESSAGE_PACK_HEADER_LEN + (msg_size * num_messages)
    if len(raw_bytes) < expected_len:
        logger.warning(
            "Message Pack shorter than declared (%d < %d); "
            "parsing available messages only",
            len(raw_bytes), expected_len
        )

    results = []
    offset = MESSAGE_PACK_HEADER_LEN
    for i in range(num_messages):
        chunk = raw_bytes[offset:offset + msg_size]
        if len(chunk) < msg_size:
            logger.warning("Truncated sub-message %d in pack, skipping", i)
            break
        try:
            results.append(_parse_single_message(chunk))
        except ValueError as e:
            logger.warning("Skipping unparseable sub-message %d: %s", i, e)
        offset += msg_size

    return results
