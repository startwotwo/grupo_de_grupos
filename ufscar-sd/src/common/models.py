from enum import IntEnum
from dataclasses import dataclass, asdict
from typing import Optional
import time
import uuid


class MessageType(IntEnum):
    TEXT = 0x01
    AUDIO = 0x02
    VIDEO = 0x03
    CONTROL = 0x04
    HEARTBEAT = 0x05
    PRESENCE = 0x06


class ControlMessageType(IntEnum):
    LOGIN = 0x01
    LOGOUT = 0x02
    JOIN_GROUP = 0x03
    LEAVE_GROUP = 0x04
    ACK = 0x05
    NACK = 0x06
    LIST_USERS = 0x07
    LIST_GROUPS = 0x08


@dataclass
class Message:
    type: int
    sender_id: str
    timestamp: float
    payload: bytes
    message_id: str = None
    group: Optional[str] = None
    recipient: Optional[str] = None
    sequence: int = 0
    qos_level: int = 0
    control_type: Optional[int] = None

    def __post_init__(self):
        if self.message_id is None:
            self.message_id = str(uuid.uuid4())
        if self.timestamp == 0:
            self.timestamp = time.time()

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> 'Message':
        return cls(**data)

    @classmethod
    def create_text(cls, sender_id: str, text: str, group: Optional[str] = None,
                   recipient: Optional[str] = None, qos_level: int = 1) -> 'Message':
        return cls(
            type=MessageType.TEXT,
            sender_id=sender_id,
            timestamp=time.time(),
            payload=text.encode('utf-8'),
            group=group,
            recipient=recipient,
            qos_level=qos_level
        )

    @classmethod
    def create_control(cls, sender_id: str, control_type: int,
                      payload: bytes = b'') -> 'Message':
        return cls(
            type=MessageType.CONTROL,
            sender_id=sender_id,
            timestamp=time.time(),
            payload=payload,
            control_type=control_type
        )

    @classmethod
    def create_heartbeat(cls, sender_id: str) -> 'Message':
        return cls(
            type=MessageType.HEARTBEAT,
            sender_id=sender_id,
            timestamp=time.time(),
            payload=b''
        )

    @classmethod
    def create_presence(cls, sender_id: str, is_online: bool,
                       group: Optional[str] = None) -> 'Message':
        payload = b'1' if is_online else b'0'
        return cls(
            type=MessageType.PRESENCE,
            sender_id=sender_id,
            timestamp=time.time(),
            payload=payload,
            group=group
        )

    @classmethod
    def create_audio(cls, sender_id: str, audio_data: bytes,
                    group: Optional[str] = None, sequence: int = 0) -> 'Message':
        return cls(
            type=MessageType.AUDIO,
            sender_id=sender_id,
            timestamp=time.time(),
            payload=audio_data,
            group=group,
            sequence=sequence,
            qos_level=0
        )

    @classmethod
    def create_video(cls, sender_id: str, video_data: bytes,
                    group: Optional[str] = None, sequence: int = 0) -> 'Message':
        return cls(
            type=MessageType.VIDEO,
            sender_id=sender_id,
            timestamp=time.time(),
            payload=video_data,
            group=group,
            sequence=sequence,
            qos_level=0
        )


@dataclass
class BrokerInfo:
    broker_id: str
    host: str
    text_port: int
    audio_port: int
    video_port: int
    control_port: int
    load: int = 0
    latency: float = 0.0
    last_heartbeat: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> 'BrokerInfo':
        return cls(**data)

    def is_alive(self, timeout: float = 5.0) -> bool:
        return time.time() - self.last_heartbeat < timeout


@dataclass
class ClientInfo:
    client_id: str
    group: Optional[str] = None
    last_heartbeat: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> 'ClientInfo':
        return cls(**data)

    def is_alive(self, timeout: float = 5.0) -> bool:
        return time.time() - self.last_heartbeat < timeout
