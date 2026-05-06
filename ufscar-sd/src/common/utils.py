import msgpack
from typing import Dict, Any
from .models import Message


def serialize_message(message: Message) -> bytes:
    data = message.to_dict()
    return msgpack.packb(data, use_bin_type=True)


def deserialize_message(data: bytes) -> Message:
    unpacked = msgpack.unpackb(data, raw=False)
    return Message.from_dict(unpacked)


def serialize_dict(data: Dict[str, Any]) -> bytes:
    return msgpack.packb(data, use_bin_type=True)


def deserialize_dict(data: bytes) -> Dict[str, Any]:
    return msgpack.unpackb(data, raw=False)
