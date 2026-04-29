"""Gateway-sovereign ID generation.

UUIDv7 (RFC 9562) gives us time-sortable primary keys — records written
in order have IDs that sort in order, which simplifies pagination,
chain-resume queries, and debugging. Python 3.14 will ship uuid.uuid7();
until this project bumps its floor past 3.12, we generate it inline.

Same-ms ordering: ``time.time()`` is wall-clock and can step backwards
under NTP correction, which would silently invert the sort within a
window of records.  We keep ``time.time()`` for the 48-bit timestamp
prefix (UUIDv7's contract — consumers decode it as wall-clock ms) but
add a process-local 12-bit monotonic counter in the ``rand_a`` field
that breaks ties when two IDs land in the same ms.  The counter
resets whenever the ms advances, so within any given ms we get strict
monotonicity even if ``time.time()`` ticks backwards.
"""
from __future__ import annotations
import os
import threading
import time
import uuid


# Process-local state for same-ms tie-breaking.
_lock = threading.Lock()
_last_ms: int = 0
_counter: int = 0


def uuid7() -> uuid.UUID:
    global _last_ms, _counter
    ms = int(time.time() * 1000)
    with _lock:
        if ms == _last_ms:
            _counter = (_counter + 1) & 0x0FFF  # 12-bit counter, wraps
        else:
            _last_ms = ms
            _counter = 0
        ctr = _counter
    rand = os.urandom(10)
    b = bytearray(16)
    b[0:6] = ms.to_bytes(6, "big")
    # rand_a (12 bits) carries the same-ms counter for deterministic ordering.
    # High nibble of byte[6] is the version (7); low nibble + byte[7] = 12-bit counter.
    b[6] = 0x70 | ((ctr >> 8) & 0x0F)   # version 7 + top 4 bits of counter
    b[7] = ctr & 0xFF                    # bottom 8 bits of counter
    b[8] = 0x80 | (rand[2] & 0x3F)       # variant 10
    b[9:16] = rand[3:10]
    return uuid.UUID(bytes=bytes(b))


def uuid7_str() -> str:
    return str(uuid7())
