"""
SuperBroker — interconexão entre todos os grupos da federação.

Para cada grupo configurado em ports.yaml, conecta como SUB nos XPUBs
de texto/áudio/vídeo e republica na federação com prefixo de grupo.

Também expõe XSUBs/XPUBs federados para clientes externos.
"""

import threading
import time
from pathlib import Path

import yaml
import zmq

FED_DIR = Path(__file__).parent
PORTS_FILE = FED_DIR / "ports.yaml"


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

        # Sockets federados (clientes externos conectam aqui)
        self.fed_txt_xsub = self._bind(zmq.XSUB, sb["txt_xsub"])
        self.fed_txt_xpub = self._bind(zmq.XPUB, sb["txt_xpub"])
        self.fed_aud_xsub = self._bind(zmq.XSUB, sb["aud_xsub"])
        self.fed_aud_xpub = self._bind(zmq.XPUB, sb["aud_xpub"])
        self.fed_vid_xsub = self._bind(zmq.XSUB, sb["vid_xsub"])
        self.fed_vid_xpub = self._bind(zmq.XPUB, sb["vid_xpub"])

        # SUBs que escutam os XPUBs de cada grupo
        self.group_subs: dict[str, dict[str, zmq.Socket]] = {}
        for gname, gcfg in self.groups.items():
            subs = {}
            for channel in ("txt", "aud", "vid"):
                port = gcfg.get(f"xpub_{channel}")
                if not port:
                    continue
                s = self.ctx.socket(zmq.SUB)
                s.setsockopt(zmq.SUBSCRIBE, b"")
                s.setsockopt(zmq.RCVTIMEO, 0)
                s.connect(f"tcp://localhost:{port}")
                subs[channel] = s
            if subs:
                self.group_subs[gname] = subs
                print(f"[SuperBroker] {gname}: conectado em {list(subs.keys())}")

        print(f"[SuperBroker] Online — fed txt_xpub={sb['txt_xpub']} aud={sb['aud_xpub']} vid={sb['vid_xpub']}")

    def _bind(self, sock_type: int, port: int) -> zmq.Socket:
        s = self.ctx.socket(sock_type)
        s.bind(f"tcp://*:{port}")
        return s

    def _relay_loop(self):
        """
        Lê mensagens dos grupos e republica no XPUB federado com prefixo do grupo.
        Também faz proxy do XSUB federado → XPUB federado (clientes externos).
        """
        poller = zmq.Poller()

        # Registra todos os SUBs dos grupos
        channel_map: dict[zmq.Socket, tuple[str, str, zmq.Socket]] = {}
        for gname, subs in self.group_subs.items():
            for channel, sock in subs.items():
                fed_pub = {"txt": self.fed_txt_xpub,
                           "aud": self.fed_aud_xpub,
                           "vid": self.fed_vid_xpub}[channel]
                poller.register(sock, zmq.POLLIN)
                channel_map[sock] = (gname, channel, fed_pub)

        # Registra XSUBs federados (mensagens de clientes externos)
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

                if sock in fed_proxy:
                    # Mensagem de cliente externo → republica na federação
                    fed_proxy[sock].send_multipart(msg)

                elif sock in channel_map:
                    gname, channel, fed_pub = channel_map[sock]
                    # Prefixa o tópico com o nome do grupo: b"grupo_i/" + topic
                    if msg:
                        prefix = f"{gname}/".encode()
                        msg[0] = prefix + msg[0]
                    fed_pub.send_multipart(msg)

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
