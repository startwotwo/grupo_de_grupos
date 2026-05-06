"""
SuperBroker — interconexão entre todos os grupos da federação.
"""

import json
import threading
import time
from pathlib import Path

import yaml
import zmq

FED_DIR = Path(__file__).parent
PORTS_FILE = FED_DIR / "ports.yaml"

# Grupos com relay bidirecional ativo.
# Adicione aqui para habilitar relay entre novos pares.
RELAY_GROUPS = {
    "grupo_i", "expansion", "googlemeet", "sd_meeting",
    "trabalho1", "sd_trabalho", "t1sistemas",
}


def load_ports():
    with open(PORTS_FILE) as f:
        return yaml.safe_load(f)


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

        # SUBs de leitura — um por porta única por grupo
        self.group_subs: dict[str, dict[str, zmq.Socket]] = {}
        for gname, gcfg in self.groups.items():
            subs = {}
            seen_ports = set()
            for channel in ("txt", "aud", "vid"):
                port = gcfg.get(f"xpub_{channel}")
                if not port or port in seen_ports:
                    continue
                seen_ports.add(port)
                s = self.ctx.socket(zmq.SUB)
                s.setsockopt(zmq.SUBSCRIBE, b"")
                s.connect(f"tcp://localhost:{port}")
                subs[channel] = s
            if subs:
                self.group_subs[gname] = subs

        # Sockets de injeção
        self.group_inject: dict[str, zmq.Socket] = {}
        for gname, gcfg in self.groups.items():
            port = gcfg.get("inject_port")
            itype = gcfg.get("inject_type", "PUB")
            if not port or itype == "MSGPACK":
                continue
            s = self.ctx.socket(zmq.PUSH if itype == "PUSH" else zmq.PUB)
            s.connect(f"tcp://localhost:{port}")
            self.group_inject[gname] = s
            print(f"[SuperBroker] inject {gname}: {itype}->{port}")

        time.sleep(1.0)
        print(f"[SuperBroker] Online — fed txt_xpub={sb['txt_xpub']}")

    def _bind(self, sock_type: int, port: int) -> zmq.Socket:
        s = self.ctx.socket(sock_type)
        s.bind(f"tcp://*:{port}")
        return s

    def _extract_text(self, payload: bytes, origin_group: str) -> str | None:
        """Extrai o texto puro do payload, independente do formato do grupo."""
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            return None  # bytes binários (áudio/vídeo) — ignora

        # GrupoI: JSON com campo "text"
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                # Ignora mensagens de presença e heartbeat
                if data.get("text") in ("__PRESENCE__", None):
                    return None
                if data.get("origin") == "federation":
                    return None  # já é relay — ignora
                return data.get("text") or data.get("content") or data.get("msg")
        except (json.JSONDecodeError, AttributeError):
            pass

        # Expansion: "sender:texto"
        if ":" in text and not text.startswith("{"):
            parts = text.split(":", 1)
            if len(parts) == 2:
                return parts[1]

        # Texto puro
        if text.strip() and not text.startswith("{"):
            return text

        return None

    def _inject(self, target_group: str, origin_group: str, raw_payload: bytes):
        """Reinjeta mensagem de texto no target_group no formato que ele espera."""
        sock = self.group_inject.get(target_group)
        if not sock:
            return

        text = self._extract_text(raw_payload, origin_group)
        if not text or not text.strip():
            return

        gcfg = self.groups[target_group]
        itype = gcfg.get("inject_type", "PUB")
        sender = f"[{origin_group}]"

        try:
            if target_group == "grupo_i":
                # Espera: [topic, json] onde json tem action/room/user_id/text
                meta = json.dumps({
                    "action": "TEXT", "room": "ROOM_A",
                    "user_id": sender, "msg_id": 0,
                    "text": text, "origin": "federation",
                }).encode()
                sock.send_multipart([b"ROOM_A TEXT", meta], flags=zmq.NOBLOCK)

            elif target_group == "expansion":
                # Espera: [topic, "sender:texto"]
                # Tópico com [origem] para o filtro anti-loop funcionar
                topic = f"text:FED_EXP:A:[{origin_group}]".encode()
                payload = f"{sender}:{text}".encode()
                sock.send_multipart([topic, payload], flags=zmq.NOBLOCK)

            elif target_group == "googlemeet":
                # XSUB/XPUB — tópico com [origem] para anti-loop
                topic = f"TXT/ROOM_A|[{origin_group}]|{text}".encode()
                sock.send_multipart([topic, text.encode()], flags=zmq.NOBLOCK)

            elif target_group == "sd_meeting":
                # PUSH com [meta_json, payload_json]
                meta = json.dumps({
                    "room": "A", "sender_id": sender,
                    "msg_id": f"fed-{time.time()}", "v": 1,
                    "origin": "federation",
                }).encode()
                payload = json.dumps({
                    "v": 1, "type": "text",
                    "msg_id": f"fed-{time.time()}",
                    "room": "A", "sender_id": sender,
                    "username": sender, "content": text,
                    "ts": time.time(), "origin": "federation",
                }).encode()
                sock.send_multipart([meta, payload], flags=zmq.NOBLOCK)

            elif target_group == "trabalho1":
                # XSUB — tópico "SALA:TIPO:sender", payload texto puro
                topic = f"ROOM_A:TEXTO:[{origin_group}]".encode()
                sock.send_multipart([topic, text.encode()], flags=zmq.NOBLOCK)

            elif target_group == "sd_trabalho":
                # XSUB/XPUB — tópico com [origem] para anti-loop
                topic = f"fed_room:[{origin_group}]".encode()
                sock.send_multipart([topic, text.encode()], flags=zmq.NOBLOCK)

            elif target_group == "t1sistemas":
                # PUSH — [topic, payload]
                topic = f"sala_A:[{origin_group}]".encode()
                sock.send_multipart([topic, text.encode()], flags=zmq.NOBLOCK)

            else:
                topic = f"FED_{origin_group}/".encode()
                sock.send_multipart([topic, text.encode()], flags=zmq.NOBLOCK)

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

                # Anti-loop: ignora mensagens que já passaram pelo relay
                topic_str = msg[0].decode(errors="replace")
                if "[" in topic_str or topic_str.startswith("FED_"):
                    continue

                if sock in fed_proxy:
                    fed_proxy[sock].send_multipart(msg)
                    continue

                if sock not in channel_map:
                    continue

                gname, channel, fed_pub = channel_map[sock]

                # Publica no XPUB federado
                fed_msg = list(msg)
                fed_msg[0] = f"{gname}/".encode() + msg[0]
                fed_pub.send_multipart(fed_msg)

                # Relay de texto para todos os outros grupos ativos
                if gname not in RELAY_GROUPS:
                    continue

                # Detecta se é mensagem de texto pelo tópico
                is_text = (
                    "TEXT" in topic_str.upper() or
                    topic_str.startswith("text:") or
                    topic_str.startswith("texto:") or
                    topic_str.startswith("TXT/") or
                    topic_str.startswith("sala_") or
                    topic_str.startswith("fed_room:")
                )
                if not is_text:
                    continue

                payload = msg[1] if len(msg) > 1 else b""

                for other_group in RELAY_GROUPS:
                    if other_group != gname:
                        self._inject(other_group, gname, payload)

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
