"""
SuperBroker — interconexão entre todos os grupos da federação.
"""

import json
import threading
import time
import uuid as _uuid
from pathlib import Path

import yaml
import zmq

try:
    import msgpack
    _MSGPACK_OK = True
except ImportError:
    _MSGPACK_OK = False

FED_DIR = Path(__file__).parent
PORTS_FILE = FED_DIR / "ports.yaml"

RELAY_GROUPS = {
    "grupo_i", "expansion", "googlemeet", "sd_meeting",
    "trabalho1", "sd_trabalho", "t1sistemas", "sd_trab1", "videoconf",
    "ufscar",
}


def load_ports():
    with open(PORTS_FILE) as f:
        return yaml.safe_load(f)


def normalize_room(room: str) -> str:
    room = room.upper().strip()
    if room.startswith("ROOM_"):
        room = room[5:]
    return room


def _group_host(gcfg: dict) -> str:
    return gcfg.get("host") or "localhost"


class SuperBroker:
    def __init__(self):
        cfg = load_ports()
        self.groups = cfg["groups"]
        sb = cfg["super_broker"]

        self.ctx = zmq.Context()
        self.running = True

        self.fed_txt_xsub = self._bind(zmq.XSUB, sb["txt_xsub"])
        self.fed_txt_xpub = self._bind(zmq.XPUB, sb["txt_xpub"])
        self.fed_aud_xsub = self._bind(zmq.XSUB, sb["aud_xsub"])
        self.fed_aud_xpub = self._bind(zmq.XPUB, sb["aud_xpub"])
        self.fed_vid_xsub = self._bind(zmq.XSUB, sb["vid_xsub"])
        self.fed_vid_xpub = self._bind(zmq.XPUB, sb["vid_xpub"])

        self.group_subs: dict[str, dict[str, zmq.Socket]] = {}
        for gname, gcfg in self.groups.items():
            host = _group_host(gcfg)
            subs = {}
            seen_ports = set()
            for channel in ("txt", "aud", "vid"):
                port = gcfg.get(f"xpub_{channel}")
                if not port or port in seen_ports:
                    continue
                seen_ports.add(port)
                s = self.ctx.socket(zmq.SUB)
                s.setsockopt(zmq.SUBSCRIBE, b"")
                s.connect(f"tcp://{host}:{port}")
                subs[channel] = s
            if subs:
                self.group_subs[gname] = subs
                print(f"[SuperBroker] {gname}@{host}: {[(ch, gcfg.get(f'xpub_{ch}')) for ch in subs]}")

        self.group_inject: dict[str, zmq.Socket] = {}
        for gname, gcfg in self.groups.items():
            port = gcfg.get("inject_port")
            itype = gcfg.get("inject_type", "PUB")
            if not port:
                continue
            host = _group_host(gcfg)
            if itype == "MSGPACK":
                # ufscar: control port is ROUTER — use DEALER to send text Messages
                s = self.ctx.socket(zmq.DEALER)
                s.setsockopt(zmq.LINGER, 0)
                s.setsockopt(zmq.SNDTIMEO, 100)
            elif itype == "PUSH":
                s = self.ctx.socket(zmq.PUSH)
            else:
                s = self.ctx.socket(zmq.PUB)
            s.connect(f"tcp://{host}:{port}")
            self.group_inject[gname] = s
            print(f"[SuperBroker] inject {gname}: {itype}->{host}:{port}")

        time.sleep(1.0)
        print(f"[SuperBroker] Online — fed txt_xpub={sb['txt_xpub']}")

    def _bind(self, sock_type: int, port: int) -> zmq.Socket:
        s = self.ctx.socket(sock_type)
        s.bind(f"tcp://*:{port}")
        return s

    def _extract_text(self, msg: list, origin_group: str) -> tuple[str, str] | None:
        topic_str = msg[0].decode("utf-8", errors="replace")

        if origin_group == "trabalho1":
            if len(msg) >= 3:
                try:
                    sender = msg[1].decode("utf-8")
                    if sender.startswith("["):
                        return None
                    text = msg[2].decode("utf-8")
                    if not text.strip():
                        return None
                    room = normalize_room(msg[0].decode("utf-8"))
                    return text, room
                except UnicodeDecodeError:
                    return None
            return None

        if origin_group == "t1sistemas":
            if len(msg) >= 2:
                try:
                    raw = msg[1].decode("utf-8")
                    if raw.startswith("[") and "]: " in raw:
                        return None  # é relay de federação
                    result = raw.split(": ", 1)[1] if ": " in raw else raw
                    if not result.strip():
                        return None
                    room_part = topic_str.split(":")[0]
                    room = normalize_room(room_part.replace("sala_", ""))
                    return result, room
                except UnicodeDecodeError:
                    return None
            return None


        if origin_group == "googlemeet":
            parts = topic_str.split("|", 2)
            if len(parts) == 3:
                text = parts[2]
                if text in ("__PRESENCE__", "saiu da ligação", "") or text.startswith("__"):
                    return None
                room_part = parts[0].replace("TXT/", "")
                return text, normalize_room(room_part)
            return None

        if origin_group == "videoconf":
            raw = msg[0]
            pipe = raw.find(b"|")
            if pipe < 0:
                return None
            header = raw[:pipe].decode("utf-8", errors="replace")
            parts = header.split(":")
            if len(parts) < 3:
                return None
            if parts[1] != "TEXTO":
                return None  # filtra AUDIO/VIDEO/HEARTBEAT — binário não deve virar texto
            sender = parts[2]
            if sender == "SISTEMA" or sender.startswith("["):
                return None
            try:
                text = raw[pipe + 1:].decode("utf-8")
            except UnicodeDecodeError:
                return None
            if not text.strip():
                return None
            return text, normalize_room(parts[0])

        if origin_group == "ufscar":
            if len(msg) < 2 or not _MSGPACK_OK:
                return None
            try:
                data = msgpack.unpackb(msg[1], raw=False)
                if data.get("type") != 1:  # MessageType.TEXT = 1
                    return None
                if str(data.get("sender_id", "")).startswith("["):
                    return None  # federation echo
                payload = data.get("payload", b"")
                if isinstance(payload, bytes):
                    payload = payload.decode("utf-8", errors="replace")
                if not payload:
                    return None
                group = data.get("group") or "A"
                return payload, normalize_room(group)
            except Exception:
                return None

        if origin_group == "sd_trabalho":
            if len(msg) < 2 or not _MSGPACK_OK:
                return None
            try:
                data = msgpack.unpackb(msg[1], raw=False)
                if not isinstance(data, dict) or data.get("type") != "text":
                    return None
                if data.get("origin") == "federation":
                    return None
                payload = data.get("data", "")
                if isinstance(payload, bytes):
                    payload = payload.decode("utf-8", errors="replace")
                if not payload:
                    return None
                room = normalize_room(data.get("room", "A"))
                return str(payload), room
            except Exception:
                return None

        if len(msg) < 2:
            return None

        try:
            text = msg[1].decode("utf-8")
        except UnicodeDecodeError:
            return None

        try:
            data = json.loads(text)
            if isinstance(data, dict):
                if data.get("origin") == "federation":
                    return None
                result = data.get("text") or data.get("content") or data.get("msg")
                if result in ("__PRESENCE__", None, ""):
                    return None
                room = normalize_room(data.get("room", "A"))
                return result, room
        except (json.JSONDecodeError, AttributeError):
            pass

        if origin_group == "expansion":
            parts = topic_str.split(":")
            if len(parts) >= 3:
                room = normalize_room(parts[2])
                if ":" in text and not text.startswith("{"):
                    payload = text.split(":", 1)[1]
                    if payload.strip():
                        return payload, room
            return None

        return None

    def _inject(self, target_group: str, origin_group: str, msg: list, canonical_room: str):
        sock = self.group_inject.get(target_group)
        if not sock:
            return

        result = self._extract_text(msg, origin_group)
        if not result:
            return
        text, _ = result

        if target_group == "grupo_i":
            room = f"ROOM_{canonical_room}"
        elif target_group == "sd_trab1":
            room = canonical_room.lower()
        else:
            room = canonical_room

        sender = f"[{origin_group}]"

        try:
            if target_group == "grupo_i":
                meta = json.dumps({
                    "action": "TEXT", "room": room,
                    "user_id": sender, "msg_id": 0,
                    "text": text, "origin": "federation",
                }).encode()
                sock.send_multipart([f"{room} TEXT".encode(), meta], flags=zmq.NOBLOCK)

            elif target_group == "expansion":
                topic = f"text:FED_EXP:{canonical_room}:[{origin_group}]".encode()
                sock.send_multipart([topic, f"{sender}:{text}".encode()], flags=zmq.NOBLOCK)

            elif target_group == "googlemeet":
                topic = f"TXT/{room}|[{origin_group}]|{text}".encode()
                sock.send(topic, flags=zmq.NOBLOCK)

            elif target_group == "sd_meeting":
                meta = json.dumps({
                    "room": canonical_room, "sender_id": sender,
                    "msg_id": f"fed-{time.time()}", "v": 1,
                    "origin": "federation",
                }).encode()
                payload = json.dumps({
                    "v": 1, "type": "text",
                    "msg_id": f"fed-{time.time()}",
                    "room": canonical_room, "sender_id": sender,
                    "username": sender, "content": text,
                    "ts": time.time(), "origin": "federation",
                }).encode()
                sock.send_multipart([meta, payload], flags=zmq.NOBLOCK)

            elif target_group == "trabalho1":
                sock.send_multipart(
                    [canonical_room.encode(), f"[{origin_group}]".encode(), text.encode()],
                    flags=zmq.NOBLOCK
                )

            elif target_group == "sd_trabalho":
                if not _MSGPACK_OK:
                    return
                topic = f"{canonical_room}.text".encode()
                payload = msgpack.packb({
                    "v": 1,
                    "id": str(_uuid.uuid4()),
                    "type": "text",
                    "from": f"[{origin_group}]",
                    "room": canonical_room,
                    "ts": time.time(),
                    "data": text,
                    "origin": "federation",
                }, use_bin_type=True)
                sock.send_multipart([topic, payload], flags=zmq.NOBLOCK)

            elif target_group == "t1sistemas":
                sock.send_multipart(
                    [canonical_room.encode(), f"[{origin_group}]: {text}".encode()],
                    flags=zmq.NOBLOCK
                )


            elif target_group == "sd_trab1":
                topic = f"texto:{room}:[{origin_group}]".encode()
                payload = json.dumps({
                    "id": str(_uuid.uuid4()),
                    "de": sender, "msg": text,
                    "room": room, "ts": time.time(),
                    "origin": "federation",
                }).encode()
                sock.send_multipart([topic, payload], flags=zmq.NOBLOCK)

            elif target_group == "ufscar":
                if not _MSGPACK_OK:
                    return
                payload = msgpack.packb({
                    "type": 1,  # MessageType.TEXT
                    "sender_id": f"[{origin_group}]",
                    "timestamp": time.time(),
                    "payload": text.encode("utf-8"),
                    "message_id": str(_uuid.uuid4()),
                    "group": canonical_room,
                    "recipient": None,
                    "sequence": 0,
                    "qos_level": 0,
                    "control_type": None,
                }, use_bin_type=True)
                sock.send(payload, flags=zmq.NOBLOCK)

            elif target_group == "videoconf":
                frame = f"{room}:TEXTO:[{origin_group}]:0|{text}".encode()
                sock.send(frame, flags=zmq.NOBLOCK)

            else:
                sock.send_multipart([f"FED_{origin_group}/".encode(), text.encode()], flags=zmq.NOBLOCK)

        except zmq.Again:
            pass

    def _relay_loop(self):
        poller = zmq.Poller()

        channel_map: dict[zmq.Socket, tuple[str, str, zmq.Socket]] = {}
        for gname, subs in self.group_subs.items():
            for channel, sock in subs.items():
                fed_pub = {"txt": self.fed_txt_xpub,
                           "aud": self.fed_aud_xpub,
                           "vid": self.fed_vid_xpub}[channel]
                poller.register(sock, zmq.POLLIN)
                channel_map[sock] = (gname, channel, fed_pub)

        poller.register(self.fed_txt_xsub, zmq.POLLIN)
        poller.register(self.fed_aud_xsub, zmq.POLLIN)
        poller.register(self.fed_vid_xsub, zmq.POLLIN)

        fed_proxy = {
            self.fed_txt_xsub: self.fed_txt_xpub,
            self.fed_aud_xsub: self.fed_aud_xpub,
            self.fed_vid_xsub: self.fed_vid_xpub,
        }

        while self.running:
            try:
                events = dict(poller.poll(500))
            except zmq.ZMQError:
                break

            for sock, event in events.items():
                if event != zmq.POLLIN:
                    continue
                try:
                    msg = sock.recv_multipart(flags=zmq.NOBLOCK)
                except zmq.Again:
                    continue

                topic_str = msg[0].decode(errors="replace")
                if "[" in topic_str or topic_str.startswith("FED_"):
                    continue

                if sock in fed_proxy:
                    fed_proxy[sock].send_multipart(msg)
                    continue

                if sock not in channel_map:
                    continue

                gname, channel, fed_pub = channel_map[sock]

                if gname == "trabalho1" and len(msg) >= 3:
                    try:
                        msg[2].decode("utf-8")
                        fed_pub = self.fed_txt_xpub
                    except UnicodeDecodeError:
                        fed_pub = None

                if gname == "grupo_i":
                    if "VIDEO" in topic_str or "AUDIO" in topic_str:
                        fed_pub = None

                if fed_pub is not None:
                    fed_msg = list(msg)
                    fed_msg[0] = f"{gname}/".encode() + msg[0]
                    fed_pub.send_multipart(fed_msg)

                if gname not in RELAY_GROUPS:
                    continue

                result = self._extract_text(msg, gname)
                if result is None:
                    continue

                text_preview, canonical_room = result
                print(f"[relay] {gname} sala={canonical_room} texto={text_preview!r}")

                for other_group in RELAY_GROUPS:
                    if other_group != gname:
                        self._inject(other_group, gname, msg, canonical_room)

    def start(self):
        threading.Thread(target=self._relay_loop, daemon=True).start()
        try:
            while self.running:
                time.sleep(1)
        except KeyboardInterrupt:
            self.running = False
        finally:
            self.ctx.term()


if __name__ == "__main__":
    SuperBroker().start()
