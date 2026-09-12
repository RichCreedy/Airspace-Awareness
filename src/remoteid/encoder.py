"""
Synthetic ASTM F3411 Remote ID Message Encoder
Used by test harness (tests/test_remoteid_parser.py,
tests/test_capture_injection.py) to generate valid + malformed messages
for parser validation.

Bug fix history:
  - Fixed Message Pack wrapping so num_messages * msg_size always matches
    the actual payload length (previously caused fixed-length mismatch
    and truncated parsing in message-pack tests).
"""

import os
import struct

from src.remoteid.parser import (
    MSG_TYPE_BASIC_ID,
    MSG_TYPE_LOCATION,
    MSG_TYPE_MESSAGE_PACK,
    LOCATION_MSG_LEN,
    BASIC_ID_MSG_LEN,
)

PROTOCOL_VERSION = 0x02


def _header_byte(msg_type: int, version: int = PROTOCOL_VERSION) -> int:
    return ((msg_type & 0x0F) << 4) | (version & 0x0F)


def encode_basic_id(uas_id: str, id_type: int = 1, ua_type: int = 2) -> bytes:
    """Build a Basic ID message (25 bytes)."""
    header = _header_byte(MSG_TYPE_BASIC_ID)
    id_type_ua_type = ((id_type & 0x0F) << 4) | (ua_type & 0x0F)

    id_bytes = uas_id.encode('ascii')[:20].ljust(20, b'\x00')
    reserved = b'\x00' * 3

    msg = bytes([header, id_type_ua_type]) + id_bytes + reserved
    assert len(msg) == BASIC_ID_MSG_LEN, f"Basic ID length mismatch: {len(msg)}"
    return msg


def encode_location(
    lat: float,
    lon: float,
    alt_m: float,
    heading_deg: int,
    speed_mps: float,
    vertical_speed_mps: float = 0.0,
) -> bytes:
    """Build a Location/Vector message (25 bytes)."""
    header = _header_byte(MSG_TYPE_LOCATION)

    ew_flag = 1 if heading_deg >= 180 else 0
    speed_multiplier = 0
    status_byte = (ew_flag << 1) | speed_multiplier

    direction_byte = heading_deg - 180 if ew_flag else heading_deg
    direction_byte = max(0, min(179, direction_byte))

    speed_byte = max(0, min(255, int(round(speed_mps / 0.25))))
    vspeed_byte = struct.pack(
        'b', max(-128, min(127, int(round(vertical_speed_mps / 0.5))))
    )

    lat_raw = struct.pack('<i', int(round(lat * 1e7)))
    lon_raw = struct.pack('<i', int(round(lon * 1e7)))

    pressure_alt_raw = struct.pack('<H', int(round((alt_m + 1000) / 0.5)))
    geo_alt_raw = struct.pack('<H', int(round((alt_m + 1000) / 0.5)))
    height_raw = struct.pack('<H', 0)

    accuracy_byte = 0x00
    speed_accuracy_byte = 0x00
    timestamp_raw = struct.pack('<H', 0)
    timestamp_accuracy = 0x00
    reserved = 0x00

    msg = (
        bytes([header, status_byte, direction_byte, speed_byte])
        + vspeed_byte
        + lat_raw
        + lon_raw
        + pressure_alt_raw
        + geo_alt_raw
        + height_raw
        + bytes([accuracy_byte, speed_accuracy_byte])
        + timestamp_raw
        + bytes([timestamp_accuracy, reserved])
    )
    assert len(msg) == LOCATION_MSG_LEN, f"Location length mismatch: {len(msg)}"
    return msg


def encode_message_pack(messages: list) -> bytes:
    """
    Wrap a list of pre-built single messages (all same length) into
    a Message Pack (type 0xF).

    FIXED: msg_size derived from actual message length; num_messages *
    msg_size always matches concatenated payload length exactly.
    """
    if not messages:
        raise ValueError("Cannot build empty Message Pack")

    msg_size = len(messages[0])
    for m in messages:
        if len(m) != msg_size:
            raise ValueError("All messages in a pack must be equal length")

    header = _header_byte(MSG_TYPE_MESSAGE_PACK)
    num_messages = len(messages)

    pack_header = bytes([header, msg_size, num_messages])
    payload = b''.join(messages)

    return pack_header + payload


def encode_garbage(length: int = 25) -> bytes:
    """Generate non-conforming garbage bytes for negative-path testing."""
    return os.urandom(length)
