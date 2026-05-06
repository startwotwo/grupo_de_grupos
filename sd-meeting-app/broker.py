#!/usr/bin/env python3
"""
Broker distribuído — sd-meeting-app

Padrões ZeroMQ utilizados
  PULL/PUB   clientes → broker → clientes   (mídia: texto, áudio, vídeo)
  ROUTER/DEALER  controle cliente↔broker    (login, ACK, presença)
  ROUTER/DEALER  inter-broker relay         (mensagens entre brokers)
  PUB/SUB    heartbeat entre brokers
  REQ/REP    registro/heartbeat no registry

Uso:
  python3 broker.py <índice> [host]
  Exemplo: python3 broker.py 0 127.0.0.1

Índice determina portas e conjunto de salas (ver config.yaml).
  Broker 0 → salas A-D,  portas base 5551-5560, inter=5600, hb=5700
  Broker 1 → salas E-H,  portas base 5651-5660, inter=5610, hb=5710
  Broker 2 → salas I-K,  portas base 5751-5760, inter=5620, hb=5720

QoS por canal
  Texto  — broker envia ACK ao remetente via ROUTER; cliente pode retentar
  Áudio  — sem garantia; buffer limitado (drop-oldest)
  Vídeo  — sem garantia; buffer limitado (drop-oldest)

Tolerância a falhas
  Heartbeat PUB/SUB entre brokers (intervalo 2 s, timeout 6 s)
  Registro periódico no registry (heartbeat REQ a cada 2 s)
  Peer morto é removido da malha sem reinicialização
"""

import base64
import json
import queue
import socket
import sys
import threading
import time
import uuid

import yaml
import zmq


# ---------------------------------------------------------------------------
# Helpers de configuração
# ---------------------------------------------------------------------------

def _load_cfg():
    import os
    cfg_path = os.environ.get("BROKER_CONFIG", "config.yaml")
    with open(cfg_path) as f:
        return yaml.safe_load(f)


def _get_local_ip() -> str:
    """
    Detecta o primeiro endereço IPv4 não-loopback disponível na máquina.
    Útil para configurar brokers em LAN sem especificar IP manualmente.
    """
    try:
        # Conecta a um host remoto (não efetivamente envia dados)
        # para determinar qual interface de rede seria usada
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except Exception:
        return "127.0.0.1"


def compute_ports(idx: int, cfg: dict) -> dict:
    """Calcula portas do broker com base no índice e stride."""
    s = cfg["broker"]["port_stride"]
    return {
        "text_pub":     cfg["broker"]["text"]["pub_port"]           + idx * s,
        "text_pull":    cfg["broker"]["text"]["pull_port"]          + idx * s,
        "audio_pub":    cfg["broker"]["audio"]["pub_port"]          + idx * s,
        "audio_pull":   cfg["broker"]["audio"]["pull_port"]         + idx * s,
        "video_pub":    cfg["broker"]["video"]["pub_port"]          + idx * s,
        "video_pull":   cfg["broker"]["video"]["pull_port"]         + idx * s,
        "control":      cfg["broker"]["control_port"]               + idx * s,
        "inter_broker": cfg["broker"]["inter_broker_base_port"]     + idx * 10,
        "heartbeat":    cfg["broker"]["heartbeat_base_port"]        + idx * 10,
    }


def assign_rooms(idx: int, cfg: dict) -> list:
    """Retorna lista de salas gerenciadas por este broker."""
    rooms = cfg["cluster"]["all_rooms"]
    per   = cfg["cluster"]["rooms_per_broker"]
    start = idx * per
    return rooms[start: start + per] if start < len(rooms) else []


# ---------------------------------------------------------------------------
# Gerenciamento de membros de sala (thread-safe)
# ---------------------------------------------------------------------------

class RoomManager:
    def __init__(self):
        self._lock  = threading.Lock()
        self._rooms: dict[str, dict[str, dict[str, float | str]]]
        self._rooms = {}  # room → {client_id: {username, last_seen}}

    def join(self, room: str, client_id: str, username: str):
        with self._lock:
            self._rooms.setdefault(room, {})[client_id] = {
                "username": username,
                "last_seen": time.time(),
            }

    def leave(self, room: str, client_id: str):
        with self._lock:
            if room in self._rooms:
                self._rooms[room].pop(client_id, None)

    def heartbeat(self, room: str, client_id: str):
        with self._lock:
            if room in self._rooms and client_id in self._rooms[room]:
                self._rooms[room][client_id]["last_seen"] = time.time()

    def prune_stale(self, timeout: float) -> list[str]:
        now = time.time()
        changed_rooms: list[str] = []
        with self._lock:
            for room, members in list(self._rooms.items()):
                stale = [cid for cid, data in members.items()
                         if now - float(data.get("last_seen", now)) > timeout]
                if stale:
                    for cid in stale:
                        members.pop(cid, None)
                    changed_rooms.append(room)
        return changed_rooms

    def members(self, room: str) -> dict:
        with self._lock:
            members = self._rooms.get(room, {})
            return {cid: str(data["username"]) for cid, data in members.items()}


# ---------------------------------------------------------------------------
# Classe principal do Broker
# ---------------------------------------------------------------------------

class Broker:
    def __init__(self, idx: int, host: str = "127.0.0.1"):
        self.cfg       = _load_cfg()
        self.idx       = idx
        # Auto-detect local IP if host is "auto"
        self.host      = _get_local_ip() if host.lower() == "auto" else host
        self.broker_id = str(uuid.uuid4())
        self.ports     = compute_ports(idx, self.cfg)
        self.rooms     = assign_rooms(idx, self.cfg)
        self.room_set  = set(self.rooms)
        self.rooms_mgr = RoomManager()

        # peers: broker_id → {info, dealer_socket, last_hb}
        # Acessado APENAS pelo loop principal (thread-unsafe por design ZMQ)
        self.peers: dict[str, dict] = {}
        self._new_peers_q: queue.Queue = queue.Queue()

        reg = self.cfg["registry"]
        self._reg_host = reg["host"]
        self._reg_port = reg["port"]

        cl = self.cfg["cluster"]
        self._hb_interval = cl["heartbeat_interval"]
        self._hb_timeout  = cl["heartbeat_timeout"]

        print(f"[Broker-{idx}] ID={self.broker_id[:8]} host={self.host} "
              f"rooms={self.rooms} ports={self.ports}")

    # ------------------------------------------------------------------
    # Comunicação com o registry (threads independentes, socket próprio)
    # ------------------------------------------------------------------

    def _reg_request(self, payload: dict) -> dict:
        """Envia um REQ ao registry e retorna a resposta. Cria socket temporário."""
        ctx = zmq.Context.instance()
        sock = ctx.socket(zmq.REQ)
        sock.setsockopt(zmq.LINGER, 0)
        sock.setsockopt(zmq.RCVTIMEO, 2000)
        sock.connect(f"tcp://{self._reg_host}:{self._reg_port}")
        try:
            sock.send_string(json.dumps(payload))
            return json.loads(sock.recv_string())
        except Exception as e:
            return {"status": "error", "msg": str(e)}
        finally:
            sock.close()

    def _register_with_registry(self):
        resp = self._reg_request({
            "type":      "register",
            "broker_id": self.broker_id,
            "host":      self.host,
            "ports":     self.ports,
            "rooms":     self.rooms,
        })
        if resp.get("status") == "ok":
            print(f"[Broker-{self.idx}] Registrado no registry")
        else:
            print(f"[Broker-{self.idx}] Falha no registro: {resp}")

    def _registry_hb_thread(self, stop: threading.Event):
        """Envia heartbeat ao registry a cada hb_interval segundos."""
        while not stop.is_set():
            self._reg_request({"type": "heartbeat", "broker_id": self.broker_id})
            stop.wait(self._hb_interval)

    def _discovery_thread(self, stop: threading.Event):
        """Descobre novos peers no registry e os enfileira para o loop principal."""
        known = {self.broker_id}
        while not stop.is_set():
            resp = self._reg_request({"type": "list_brokers"})
            for b in resp.get("brokers", []):
                bid = b["broker_id"]
                if bid not in known:
                    known.add(bid)
                    self._new_peers_q.put(b)
            stop.wait(5)

    # ------------------------------------------------------------------
    # Conexão a peers (executado no loop principal — thread-unsafe OK)
    # ------------------------------------------------------------------

    def _connect_peer(self, ctx: zmq.Context, peer_info: dict, hb_sub: zmq.Socket):
        bid = peer_info["broker_id"]
        if bid == self.broker_id or bid in self.peers:
            return
        h = peer_info["host"]
        P = peer_info["ports"]

        dealer = ctx.socket(zmq.DEALER)
        dealer.setsockopt(zmq.LINGER, 0)
        dealer.setsockopt_string(zmq.IDENTITY, self.broker_id)
        dealer.connect(f"tcp://{h}:{P['inter_broker']}")

        hb_sub.connect(f"tcp://{h}:{P['heartbeat']}")

        self.peers[bid] = {
            "info":    peer_info,
            "dealer":  dealer,
            "last_hb": time.time(),
        }
        print(f"[Broker-{self.idx}] Peer {bid[:8]} conectado "
              f"(rooms={peer_info.get('rooms')})")

    # ------------------------------------------------------------------
    # Roteamento de mensagens
    # ------------------------------------------------------------------

    def _broadcast(self, channel: str, room: str, frames: list,
                   text_pub: zmq.Socket, audio_pub: zmq.Socket,
                   video_pub: zmq.Socket):
        """Publica mensagem para assinantes locais desta sala."""
        topic = f"{channel}:{room}".encode()
        pub = {"text": text_pub, "audio": audio_pub, "video": video_pub}[channel]
        pub.send_multipart([topic] + frames)

    def _forward_to_peer(self, channel: str, room: str, frames: list):
        """Encaminha mensagem ao broker que possui esta sala via DEALER→ROUTER."""
        for peer in self.peers.values():
            if room in peer["info"].get("rooms", []):
                relay_hdr = json.dumps({
                    "v": 1, "type": "relay",
                    "channel": channel,
                    "room":    room,
                    "hop":     1,
                }).encode()
                encoded = [base64.b64encode(f) for f in frames]
                try:
                    peer["dealer"].send_multipart([relay_hdr] + encoded,
                                                  flags=zmq.NOBLOCK)
                except Exception as e:
                    print(f"[Broker-{self.idx}] Erro ao encaminhar: {e}")
                return True
        print(f"[Broker-{self.idx}] Nenhum peer encontrado para sala {room}")
        return False

    def _route(self, channel: str, room: str, frames: list,
               text_pub, audio_pub, video_pub):
        """Roteia mensagem: broadcast local ou encaminha a peer."""
        if room in self.room_set:
            self._broadcast(channel, room, frames, text_pub, audio_pub, video_pub)
        else:
            # If the registry reassigned the room to this broker, local broadcast
            # is the correct behavior even if the static room_set was not updated.
            if not self._forward_to_peer(channel, room, frames):
                self._broadcast(channel, room, frames, text_pub, audio_pub, video_pub)

    # ------------------------------------------------------------------
    # Handlers de eventos do poller
    # ------------------------------------------------------------------

    def _on_text(self, frames, ctrl_router, text_pub, audio_pub, video_pub):
        try:
            meta = json.loads(frames[0])
        except Exception:
            return
        room      = meta.get("room", "")
        client_id = meta.get("sender_id", "").encode()
        msg_id    = meta.get("msg_id", "")
        if not room:
            return
        # ACK ao remetente (via ROUTER usando identity do cliente)
        if client_id and msg_id:
            ack = json.dumps({"v": 1, "type": "text_ack", "msg_id": msg_id}).encode()
            try:
                ctrl_router.send_multipart([client_id, ack])
            except Exception:
                pass
        self._route("text", room, frames, text_pub, audio_pub, video_pub)

    def _on_audio(self, frames, text_pub, audio_pub, video_pub):
        try:
            meta = json.loads(frames[0])
        except Exception:
            return
        room = meta.get("room", "")
        if room:
            self._route("audio", room, frames, text_pub, audio_pub, video_pub)

    def _on_video(self, frames, text_pub, audio_pub, video_pub):
        try:
            meta = json.loads(frames[0])
        except Exception:
            return
        room = meta.get("room", "")
        if room:
            self._route("video", room, frames, text_pub, audio_pub, video_pub)

    def _on_control(self, frames, ctrl_router, text_pub):
        """Trata login e leave de clientes."""
        if len(frames) < 2:
            return
        client_id = frames[0]
        try:
            msg = json.loads(frames[1])
        except Exception:
            return

        t    = msg.get("type")
        room = msg.get("room", "")

        if t == "login":
            username = msg.get("username", f"user-{client_id[:4].hex()}")
            self.rooms_mgr.join(room, client_id.decode(), username)
            members = self.rooms_mgr.members(room)
            ack = json.dumps({
                "v": 1, "type": "login_ack",
                "room":      room,
                "members":   members,
                "broker_id": self.broker_id,
            }).encode()
            ctrl_router.send_multipart([client_id, ack])
            # Notifica presença a toda a sala
            self._publish_presence(room, members, text_pub)
            print(f"[Broker-{self.idx}] {username} entrou na sala {room} "
                  f"(membros: {list(members.values())})")

        elif t == "leave":
            self.rooms_mgr.leave(room, client_id.decode())
            ack = json.dumps({"v": 1, "type": "leave_ack", "room": room}).encode()
            ctrl_router.send_multipart([client_id, ack])
            members = self.rooms_mgr.members(room)
            self._publish_presence(room, members, text_pub)

        elif t == "hb":
            sender_id = msg.get("sender_id", client_id.decode())
            self.rooms_mgr.heartbeat(room, sender_id)

    def _publish_presence(self, room: str, members: dict, text_pub: zmq.Socket):
        msg = json.dumps({
            "v": 1, "type": "presence",
            "room": room, "members": members,
        }).encode()
        text_pub.send_multipart([f"text:{room}".encode(), msg])

    def _on_inter_broker(self, frames, text_pub, audio_pub, video_pub):
        """Processa mensagem de relay recebida de outro broker."""
        # frames: [dealer_identity, relay_hdr, *encoded_payload]
        if len(frames) < 3:
            return
        try:
            relay = json.loads(frames[1])
        except Exception:
            return

        if relay.get("hop", 0) >= 1:
            return  # previne loops (single-hop)

        channel = relay.get("channel", "")
        room    = relay.get("room", "")
        if not channel or not room or room not in self.room_set:
            return

        try:
            payload = [base64.b64decode(f) for f in frames[2:]]
        except Exception:
            return

        self._broadcast(channel, room, payload, text_pub, audio_pub, video_pub)

    def _on_heartbeat(self, frames):
        """Atualiza timestamp de heartbeat do peer."""
        if len(frames) < 2:
            return
        try:
            hb  = json.loads(frames[1])
            bid = hb.get("broker_id")
            if bid and bid in self.peers:
                self.peers[bid]["last_hb"] = time.time()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Loop principal
    # ------------------------------------------------------------------

    def run(self):
        ctx = zmq.Context()
        P   = self.ports

        # Sockets de mídia
        text_pull = ctx.socket(zmq.PULL);  text_pull.bind(f"tcp://*:{P['text_pull']}")
        text_pub  = ctx.socket(zmq.PUB);   text_pub.bind(f"tcp://*:{P['text_pub']}")

        audio_pull = ctx.socket(zmq.PULL); audio_pull.bind(f"tcp://*:{P['audio_pull']}")
        audio_pub  = ctx.socket(zmq.PUB);  audio_pub.bind(f"tcp://*:{P['audio_pub']}")

        video_pull = ctx.socket(zmq.PULL); video_pull.bind(f"tcp://*:{P['video_pull']}")
        video_pub  = ctx.socket(zmq.PUB);  video_pub.bind(f"tcp://*:{P['video_pub']}")

        # Socket de controle (login / leave / ACK)
        ctrl_router = ctx.socket(zmq.ROUTER)
        ctrl_router.bind(f"tcp://*:{P['control']}")

        # Inter-broker: ROUTER recebe de peers, DEALER envia a peers
        ib_router = ctx.socket(zmq.ROUTER)
        ib_router.bind(f"tcp://*:{P['inter_broker']}")

        # Heartbeat entre brokers
        hb_pub = ctx.socket(zmq.PUB)
        hb_pub.bind(f"tcp://*:{P['heartbeat']}")

        hb_sub = ctx.socket(zmq.SUB)
        hb_sub.setsockopt_string(zmq.SUBSCRIBE, "hb")

        # Poller
        poller = zmq.Poller()
        poller.register(text_pull,   zmq.POLLIN)
        poller.register(audio_pull,  zmq.POLLIN)
        poller.register(video_pull,  zmq.POLLIN)
        poller.register(ctrl_router, zmq.POLLIN)
        poller.register(ib_router,   zmq.POLLIN)
        poller.register(hb_sub,      zmq.POLLIN)

        # Registro no registry + threads de background
        self._register_with_registry()
        stop = threading.Event()
        threading.Thread(target=self._registry_hb_thread,
                         args=(stop,), daemon=True).start()
        threading.Thread(target=self._discovery_thread,
                         args=(stop,), daemon=True).start()

        print(f"[Broker-{self.idx}] Rodando — "
              f"text_pub={P['text_pub']} audio_pub={P['audio_pub']} "
              f"video_pub={P['video_pub']} control={P['control']}",flush= True)

        last_hb_send  = 0.0
        last_peer_chk = 0.0
        last_member_chk = 0.0

        try:
            while True:
                # Incorpora novos peers descobertos pelo discovery thread
                while not self._new_peers_q.empty():
                    self._connect_peer(ctx, self._new_peers_q.get_nowait(), hb_sub)

                events = dict(poller.poll(timeout=500))

                if text_pull in events:
                    self._on_text(text_pull.recv_multipart(),
                                  ctrl_router, text_pub, audio_pub, video_pub)

                if audio_pull in events:
                    self._on_audio(audio_pull.recv_multipart(),
                                   text_pub, audio_pub, video_pub)

                if video_pull in events:
                    self._on_video(video_pull.recv_multipart(),
                                   text_pub, audio_pub, video_pub)

                if ctrl_router in events:
                    self._on_control(ctrl_router.recv_multipart(),
                                     ctrl_router, text_pub)

                if ib_router in events:
                    self._on_inter_broker(ib_router.recv_multipart(),
                                         text_pub, audio_pub, video_pub)

                if hb_sub in events:
                    self._on_heartbeat(hb_sub.recv_multipart())

                now = time.time()

                # Envia heartbeat periódico aos peers
                if now - last_hb_send > self._hb_interval:
                    hb_msg = json.dumps({
                        "type": "hb",
                        "broker_id": self.broker_id,
                        "ts": now,
                    }).encode()
                    hb_pub.send_multipart([b"hb", hb_msg])
                    last_hb_send = now

                # Verifica peers mortos
                if now - last_peer_chk > 2.0:
                    dead = [bid for bid, p in self.peers.items()
                            if now - p["last_hb"] > self._hb_timeout]
                    for bid in dead:
                        p = self.peers.pop(bid)
                        p["dealer"].close()
                        print(f"[Broker-{self.idx}] Peer {bid[:8]} morto "
                              f"(sem heartbeat > {self._hb_timeout}s)")
                    last_peer_chk = now

                # Remove membros que pararam de enviar heartbeat
                if now - last_member_chk > 2.0:
                    changed_rooms = self.rooms_mgr.prune_stale(
                        self.cfg["cluster"]["heartbeat_timeout"]
                    )
                    for room in changed_rooms:
                        members = self.rooms_mgr.members(room)
                        self._publish_presence(room, members, text_pub)
                    last_member_chk = now

        except KeyboardInterrupt:
            print(f"\n[Broker-{self.idx}] Encerrando...")
        finally:
            stop.set()


if __name__ == "__main__":
    idx  = int(sys.argv[1])        if len(sys.argv) > 1 else 0
    host = sys.argv[2]             if len(sys.argv) > 2 else "127.0.0.1"
    Broker(idx, host).run()
