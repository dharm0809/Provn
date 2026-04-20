"""Gateway-sovereign ID generation.

UUIDv7 (RFC 9562) gives us time-sortable primary keys — records written
in order have IDs that sort in order, which simplifies pagination,
chain-resume queries, and debugging. Python 3.14 will ship uuid.uuid7();
until this project bumps its floor past 3.12, we generate it inline.
"""
from __future__ import annotations
import os
import time
import uuid


def uuid7() -> uuid.UUID:
    ms = int(time.time() * 1000)
    rand = os.urandom(10)
    b = bytearray(16)
    b[0:6] = ms.to_bytes(6, "big")
    b[6] = 0x70 | (rand[0] & 0x0F)   # version 7
    b[7] = rand[1]
    b[8] = 0x80 | (rand[2] & 0x3F)   # variant 10
    b[9:16] = rand[3:10]
    return uuid.UUID(bytes=bytes(b))


def uuid7_str() -> str:
    return str(uuid7())
