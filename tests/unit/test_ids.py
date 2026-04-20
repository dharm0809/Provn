import time
import uuid
from gateway.util.ids import uuid7, uuid7_str


def test_uuid7_version_and_variant() -> None:
    u = uuid7()
    assert u.version == 7
    assert (u.bytes[8] & 0xC0) == 0x80  # variant 10


def test_uuid7_is_time_sortable() -> None:
    a = uuid7_str()
    time.sleep(0.002)
    b = uuid7_str()
    assert a < b


def test_uuid7_is_unique_at_high_rate() -> None:
    ids = {uuid7_str() for _ in range(10_000)}
    assert len(ids) == 10_000
