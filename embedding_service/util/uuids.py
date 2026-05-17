import uuid


def new_uuid_bytes() -> bytes:
    return uuid.uuid4().bytes


def bytes_to_hex(b: bytes) -> str:
    return b.hex()


def hex_to_bytes(h: str) -> bytes:
    h = h.replace("-", "").strip()
    return bytes.fromhex(h)
