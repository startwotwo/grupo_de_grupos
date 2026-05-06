from typing import Protocol, Optional


class IMessageHandler(Protocol):
    def on_text_message(self, sender_id: str, text: str, group: Optional[str] = None) -> None:
        ...

    def on_control_message(self, sender_id: str, control_type: int, payload: bytes) -> None:
        ...

    def on_presence_update(self, sender_id: str, is_online: bool, group: Optional[str] = None) -> None:
        ...


class IConnectionManager(Protocol):
    def connect(self) -> bool:
        ...

    def disconnect(self) -> None:
        ...

    def is_connected(self) -> bool:
        ...

    def reconnect(self) -> bool:
        ...
