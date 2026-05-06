import json
import socket
import sys
import threading
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timezone

import zmq


class BrokerConfig:
    def __init__(self, broker_id: str, primary_port: int):
        self.broker_id = broker_id
        self.primary_port = primary_port

        self.video_pub_in  = primary_port + 3
        self.video_sub_out = primary_port + 4
        self.audio_pub_in  = primary_port + 13
        self.audio_sub_out = primary_port + 14
        self.text_pub_in   = primary_port + 23
        self.text_sub_out  = primary_port + 24
        self.control_port  = primary_port + 200
        self.mesh_port     = primary_port + 300

        # Service discovery
        self.discovery_port    = 6000
        self.heartbeat_interval = 2
        self.heartbeat_timeout  = 8
        self.relay_ttl          = 4
        self.seen_ttl_seconds   = 60



class DistributedBroker:
    def __init__(self, broker_id: str, primary_port: int):
        self.config = BrokerConfig(broker_id, primary_port)
        self.ctx = zmq.Context.instance()
        self.stop_event = threading.Event()

        # Service discovery state
        self.known_brokers = {}  # broker_id -> {host, primary_port, mesh_port, control_port, ts}
        self.connected_mesh_endpoints = set()
        self.discovery_lock = threading.Lock()

        # Presence/session state
        self.local_users = {}  # user_id -> {room, last_seen}
        self.local_rooms = defaultdict(set)  # room -> set(user_id)
        self.remote_presence = {}  # broker_id -> {users, rooms, ts}
        self.session_lock = threading.Lock()

        # Text QoS buffer
        self.text_history = defaultdict(lambda: deque(maxlen=500))

        # Relay de-dup cache
        self.seen_messages = {}  # msg_id -> unix_ts
        self.seen_lock = threading.Lock()

        # Mesh sockets managed across threads
        self.mesh_pub = None
        self.mesh_sub_sockets = []
        self.mesh_sub_lock = threading.Lock()

        # Local injection PUB for text reliability path
        self.text_inject_pub = None

        print(f"\n[Broker {self.config.broker_id}] init primary={self.config.primary_port}")

    def _bind_random_port(self, sock):
        return sock.bind_to_random_port("tcp://*")
    
    def _bind_fixed_port(self, sock, port: int) -> int:
        sock.bind(f"tcp://*:{port}")
        return port


    # def _ports_ready(self):
    #     defaults = {
    #         "video_pub_in": self.config.primary_port + 3,
    #         "video_sub_out": self.config.primary_port + 4,
    #         "audio_pub_in": self.config.primary_port + 13,
    #         "audio_sub_out": self.config.primary_port + 14,
    #         "text_pub_in": self.config.primary_port + 23,
    #         "text_sub_out": self.config.primary_port + 24,
    #         "control_port": self.config.primary_port + 200,
    #     }
    #     return all(getattr(self.config, name) != value for name, value in defaults.items())
    def _ports_ready(self):
        return True


    def start(self):
        self.mesh_pub = self.ctx.socket(zmq.PUB)
        self.config.mesh_port = self._bind_fixed_port(self.mesh_pub, self.config.mesh_port)
        # ... resto idêntico ...

        threading.Thread(
            target=self._media_channel_worker,
            args=("video", self.config.video_pub_in, self.config.video_sub_out),
            daemon=True,
        ).start()
        threading.Thread(
            target=self._media_channel_worker,
            args=("audio", self.config.audio_pub_in, self.config.audio_sub_out),
            daemon=True,
        ).start()
        threading.Thread(
            target=self._media_channel_worker,
            args=("texto", self.config.text_pub_in, self.config.text_sub_out),
            daemon=True,
        ).start()

        threading.Thread(target=self._control_server, daemon=True).start()

        for _ in range(50):
            if self._ports_ready():
                break
            time.sleep(0.1)

        threading.Thread(target=self._heartbeat_sender, daemon=True).start()
        threading.Thread(target=self._heartbeat_receiver, daemon=True).start()
        threading.Thread(target=self._heartbeat_cleanup, daemon=True).start()
        threading.Thread(target=self._mesh_connection_manager, daemon=True).start()
        threading.Thread(target=self._seen_cache_cleanup, daemon=True).start()

        threading.Thread(target=self._presence_sender, daemon=True).start()
        threading.Thread(target=self._presence_receiver, daemon=True).start()

        # Small delay to allow XSUB bind before local injection PUB connect.
        time.sleep(0.2)
        self.text_inject_pub = self.ctx.socket(zmq.PUB)
        self.text_inject_pub.connect(f"tcp://127.0.0.1:{self.config.text_pub_in}")

        print(f"[Broker {self.config.broker_id}] discovery UDP={self.config.discovery_port}")
        print(f"[Broker {self.config.broker_id}] control REP={self.config.control_port}")
        print(f"[Broker {self.config.broker_id}] mesh PUB={self.config.mesh_port}")
        print(
            f"[Broker {self.config.broker_id}] channels video({self.config.video_pub_in}->{self.config.video_sub_out}) "
            f"audio({self.config.audio_pub_in}->{self.config.audio_sub_out}) "
            f"texto({self.config.text_pub_in}->{self.config.text_sub_out})"
        )

    # ---------------------------------------------------------------------
    # Service discovery
    # ---------------------------------------------------------------------

    def _heartbeat_sender(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

        while not self.stop_event.is_set():
            payload = json.dumps(
                {
                    "broker_id": self.config.broker_id,
                    "primary_port": self.config.primary_port,
                    "mesh_port": self.config.mesh_port,
                    "control_port": self.config.control_port,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            ).encode("utf-8")

            try:
                sock.sendto(payload, ("255.255.255.255", self.config.discovery_port))
            except OSError:
                sock.sendto(payload, ("127.0.0.1", self.config.discovery_port))

            time.sleep(self.config.heartbeat_interval)

    def _heartbeat_receiver(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", self.config.discovery_port))
        sock.settimeout(1)

        while not self.stop_event.is_set():
            try:
                data, addr = sock.recvfrom(4096)
                msg = json.loads(data.decode("utf-8"))
                broker_id = msg.get("broker_id")
                if not broker_id or broker_id == self.config.broker_id:
                    continue

                with self.discovery_lock:
                    first_seen = broker_id not in self.known_brokers
                    self.known_brokers[broker_id] = {
                        "host": addr[0],
                        "primary_port": int(msg.get("primary_port")),
                        "mesh_port": int(msg.get("mesh_port")),
                        "control_port": int(msg.get("control_port")),
                        "ts": time.time(),
                    }

                if first_seen:
                    print(
                        f"[Discovery] broker={broker_id} host={addr[0]} mesh={msg.get('mesh_port')}"
                    )
            except socket.timeout:
                pass
            except Exception:
                pass

    def _heartbeat_cleanup(self):
        while not self.stop_event.is_set():
            time.sleep(1)
            now = time.time()
            dead = []

            with self.discovery_lock:
                for broker_id, info in self.known_brokers.items():
                    if now - info["ts"] > self.config.heartbeat_timeout:
                        dead.append(broker_id)
                for broker_id in dead:
                    del self.known_brokers[broker_id]

            if dead:
                for broker_id in dead:
                    print(f"[Discovery] broker={broker_id} timeout")

    def _mesh_connection_manager(self):
        while not self.stop_event.is_set():
            with self.discovery_lock:
                brokers_snapshot = dict(self.known_brokers)

            for broker_id, info in brokers_snapshot.items():
                endpoint = f"tcp://{info['host']}:{info['mesh_port']}"
                if endpoint in self.connected_mesh_endpoints:
                    continue

                with self.mesh_sub_lock:
                    for sock in self.mesh_sub_sockets:
                        try:
                            sock.connect(endpoint)
                        except Exception:
                            pass
                self.connected_mesh_endpoints.add(endpoint)
                print(f"[Mesh] connected to broker={broker_id} endpoint={endpoint}")

            time.sleep(1)

    # ---------------------------------------------------------------------
    # Presence and session
    # ---------------------------------------------------------------------

    def _presence_sender(self):
        while not self.stop_event.is_set():
            snapshot = self._build_local_presence_snapshot()
            try:
                self.mesh_pub.send_multipart(
                    [
                        b"presence|snapshot",
                        json.dumps(
                            {
                                "broker_id": self.config.broker_id,
                                "snapshot": snapshot,
                                "ts": time.time(),
                            }
                        ).encode("utf-8"),
                    ]
                )
            except Exception:
                pass
            time.sleep(3)

    def _presence_receiver(self):
        sub = self.ctx.socket(zmq.SUB)
        sub.setsockopt(zmq.SUBSCRIBE, b"presence|")
        sub.setsockopt(zmq.RCVTIMEO, 1000)

        with self.mesh_sub_lock:
            self.mesh_sub_sockets.append(sub)
            for endpoint in self.connected_mesh_endpoints:
                try:
                    sub.connect(endpoint)
                except Exception:
                    pass

        while not self.stop_event.is_set():
            try:
                topic, payload = sub.recv_multipart()
                msg = json.loads(payload.decode("utf-8"))
                broker_id = msg.get("broker_id")
                if not broker_id or broker_id == self.config.broker_id:
                    continue

                with self.session_lock:
                    self.remote_presence[broker_id] = {
                        "users": msg.get("snapshot", {}).get("users", []),
                        "rooms": msg.get("snapshot", {}).get("rooms", {}),
                        "ts": time.time(),
                    }
            except zmq.error.Again:
                pass
            except Exception:
                pass

    def _build_local_presence_snapshot(self):
        with self.session_lock:
            users = sorted(self.local_users.keys())
            rooms = {room: len(users_set) for room, users_set in self.local_rooms.items()}
        return {"users": users, "rooms": rooms}

    def _is_user_taken(self, user_id: str) -> bool:
        with self.session_lock:
            if user_id in self.local_users:
                return True
            for remote in self.remote_presence.values():
                if user_id in remote.get("users", []):
                    return True
        return False

    # ---------------------------------------------------------------------
    # Channel workers: XSUB/XPUB + inter-broker relay
    # ---------------------------------------------------------------------

    def _media_channel_worker(self, stream: str, pub_in_port: int, sub_out_port: int):
        xsub = self.ctx.socket(zmq.XSUB)
        self._bind_fixed_port(xsub, pub_in_port)

        xpub = self.ctx.socket(zmq.XPUB)
        self._bind_fixed_port(xpub, sub_out_port)
        xpub.setsockopt(zmq.XPUB_VERBOSE, 1)
        if stream == "video":
            self.config.video_pub_in = pub_in_port
            self.config.video_sub_out = sub_out_port
        elif stream == "audio":
            self.config.audio_pub_in = pub_in_port
            self.config.audio_sub_out = sub_out_port
        else:
            self.config.text_pub_in = pub_in_port
            self.config.text_sub_out = sub_out_port

        mesh_sub = self.ctx.socket(zmq.SUB)
        mesh_sub.setsockopt(zmq.SUBSCRIBE, f"relay|{stream}|".encode("utf-8"))
        mesh_sub.setsockopt(zmq.RCVTIMEO, 500)

        with self.mesh_sub_lock:
            self.mesh_sub_sockets.append(mesh_sub)
            for endpoint in self.connected_mesh_endpoints:
                try:
                    mesh_sub.connect(endpoint)
                except Exception:
                    pass

        poller = zmq.Poller()
        poller.register(xsub, zmq.POLLIN)
        poller.register(xpub, zmq.POLLIN)
        poller.register(mesh_sub, zmq.POLLIN)

        print(f"[Channel {stream}] XSUB={pub_in_port} XPUB={sub_out_port}")

        while not self.stop_event.is_set():
            try:
                events = dict(poller.poll(500))
            except Exception:
                continue

            # Local publisher -> local subscribers and mesh relay.
            if xsub in events and events[xsub] == zmq.POLLIN:
                try:
                    parts = xsub.recv_multipart()
                    if len(parts) < 2:
                        continue
                    topic = parts[0]
                    payload = parts[1]

                    xpub.send_multipart([topic, payload])

                    msg_meta = self._build_relay_meta(topic, stream)
                    relay_topic = f"relay|{stream}|".encode("utf-8")
                    self.mesh_pub.send_multipart(
                        [relay_topic, topic, payload, json.dumps(msg_meta).encode("utf-8")]
                    )
                except Exception:
                    pass

            # Forward topic subscriptions from local subscribers to publishers.
            # This is required for XSUB to receive topic-filtered data.
            if xpub in events and events[xpub] == zmq.POLLIN:
                try:
                    subscription_frame = xpub.recv()
                    xsub.send(subscription_frame)
                except Exception:
                    pass

            # Mesh relay -> local subscribers and optional re-forward.
            if mesh_sub in events and events[mesh_sub] == zmq.POLLIN:
                try:
                    relay_topic, topic, payload, meta_raw = mesh_sub.recv_multipart()
                    del relay_topic
                    meta = json.loads(meta_raw.decode("utf-8"))
                    msg_id = meta.get("msg_id")
                    if not msg_id:
                        continue

                    if self._is_seen(msg_id):
                        continue
                    self._mark_seen(msg_id)

                    xpub.send_multipart([topic, payload])

                    ttl = int(meta.get("ttl", 0))
                    if ttl > 0 and meta.get("origin_broker") != self.config.broker_id:
                        meta["ttl"] = ttl - 1
                        self.mesh_pub.send_multipart(
                            [
                                f"relay|{stream}|".encode("utf-8"),
                                topic,
                                payload,
                                json.dumps(meta).encode("utf-8"),
                            ]
                        )
                except Exception:
                    pass

    def _build_relay_meta(self, topic: bytes, stream: str):
        msg_id = str(uuid.uuid4())
        room, user = self._parse_topic(topic, stream)

        meta = {
            "msg_id": msg_id,
            "origin_broker": self.config.broker_id,
            "ttl": self.config.relay_ttl,
            "room": room,
            "user": user,
            "stream": stream,
            "ts": time.time(),
        }
        self._mark_seen(msg_id)
        return meta

    @staticmethod
    def _parse_topic(topic: bytes, stream: str):
        try:
            text = topic.decode("utf-8")
            # topic format: <stream>:<room>:<user>
            parts = text.split(":", 2)
            if len(parts) == 3 and parts[0] == stream:
                return parts[1], parts[2]
        except Exception:
            pass
        return "", ""

    def _is_seen(self, msg_id: str) -> bool:
        with self.seen_lock:
            return msg_id in self.seen_messages

    def _mark_seen(self, msg_id: str):
        with self.seen_lock:
            self.seen_messages[msg_id] = time.time()

    def _seen_cache_cleanup(self):
        while not self.stop_event.is_set():
            time.sleep(5)
            now = time.time()
            with self.seen_lock:
                dead = [
                    mid
                    for mid, ts in self.seen_messages.items()
                    if now - ts > self.config.seen_ttl_seconds
                ]
                for mid in dead:
                    del self.seen_messages[mid]

    # ---------------------------------------------------------------------
    # Control plane (login/join/leave/presence/text qos)
    # ---------------------------------------------------------------------

    def _control_server(self):
        rep = self.ctx.socket(zmq.REP)
        self._bind_fixed_port(rep, self.config.control_port)
        # ... resto idêntico ...


        print(f"[Control] REP={self.config.control_port}")

        while not self.stop_event.is_set():
            try:
                req = rep.recv_json()
                resp = self._handle_control_request(req)
                rep.send_json(resp)
            except Exception:
                try:
                    rep.send_json({"ok": False, "error": "internal_error"})
                except Exception:
                    pass

    def _handle_control_request(self, req: dict):
        action = req.get("action")

        if action == "ping":
            return {"ok": True, "broker_id": self.config.broker_id, "ts": time.time()}

        if action == "login":
            user_id = (req.get("user_id") or "").strip()
            room = (req.get("room") or "geral").strip().lower()
            if not user_id:
                return {"ok": False, "error": "invalid_user_id"}
            # if self._is_user_taken(user_id):
            #     return {"ok": False, "error": "user_id_in_use"}

            with self.session_lock:
                self.local_users[user_id] = {"room": room, "last_seen": time.time()}
                self.local_rooms[room].add(user_id)

            return {
                "ok": True,
                "broker_id": self.config.broker_id,
                "room": room,
                "ports": {
                    "video_pub_in": self.config.video_pub_in,
                    "video_sub_out": self.config.video_sub_out,
                    "audio_pub_in": self.config.audio_pub_in,
                    "audio_sub_out": self.config.audio_sub_out,
                    "text_pub_in": self.config.text_pub_in,
                    "text_sub_out": self.config.text_sub_out,
                    "control_port": self.config.control_port,
                },
            }

        if action == "heartbeat_user":
            user_id = (req.get("user_id") or "").strip()
            if not user_id:
                return {"ok": False, "error": "invalid_user_id"}
            with self.session_lock:
                if user_id in self.local_users:
                    self.local_users[user_id]["last_seen"] = time.time()
            return {"ok": True}

        if action == "join_room":
            user_id = (req.get("user_id") or "").strip()
            new_room = (req.get("room") or "").strip().lower()
            if not user_id or not new_room:
                return {"ok": False, "error": "invalid_parameters"}

            with self.session_lock:
                user = self.local_users.get(user_id)
                if not user:
                    return {"ok": False, "error": "user_not_logged"}
                old_room = user["room"]
                if old_room in self.local_rooms:
                    self.local_rooms[old_room].discard(user_id)
                self.local_rooms[new_room].add(user_id)
                user["room"] = new_room
                user["last_seen"] = time.time()

            return {"ok": True, "room": new_room}

        if action == "leave":
            user_id = (req.get("user_id") or "").strip()
            if not user_id:
                return {"ok": False, "error": "invalid_user_id"}

            with self.session_lock:
                user = self.local_users.pop(user_id, None)
                if user:
                    room = user["room"]
                    self.local_rooms[room].discard(user_id)
                    if not self.local_rooms[room]:
                        del self.local_rooms[room]

            return {"ok": True}

        if action == "presence":
            with self.session_lock:
                local_users = sorted(self.local_users.keys())
                local_rooms = {
                    room: sorted(list(users)) for room, users in self.local_rooms.items()
                }
                remote = dict(self.remote_presence)

            aggregated_rooms = defaultdict(set)
            for room, users in local_rooms.items():
                for user in users:
                    aggregated_rooms[room].add(user)
            for broker_data in remote.values():
                for user in broker_data.get("users", []):
                    # user->room mapping is not guaranteed from snapshot; keep online view.
                    pass

            return {
                "ok": True,
                "broker_id": self.config.broker_id,
                "online_local": local_users,
                "rooms_local": local_rooms,
                "remote_presence": remote,
                "rooms_aggregated": {
                    room: sorted(list(users)) for room, users in aggregated_rooms.items()
                },
            }

        if action == "send_text":
            user_id = (req.get("user_id") or "").strip()
            room = (req.get("room") or "").strip().lower()
            text = (req.get("text") or "").strip()
            text_id = (req.get("text_id") or "").strip()
            if not user_id or not room or not text or not text_id:
                return {"ok": False, "error": "invalid_parameters"}

            with self.session_lock:
                if user_id not in self.local_users:
                    return {"ok": False, "error": "user_not_logged"}

            payload = {
                "id": text_id,
                "de": user_id,
                "msg": text,
                "room": room,
                "ts": time.time(),
            }
            raw = json.dumps(payload).encode("utf-8")
            topic = f"texto:{room}:{user_id}".encode("utf-8")

            try:
                self.text_inject_pub.send_multipart([topic, raw])
                self.text_history[room].append(payload)
                return {"ok": True, "text_id": text_id}
            except Exception:
                return {"ok": False, "error": "text_publish_failed"}

        return {"ok": False, "error": "unknown_action"}


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Uso: python broker.py <broker_id> <primary_port>")
        print("Exemplo: python broker.py broker1 5500")
        sys.exit(1)

    broker_id = sys.argv[1]
    try:
        primary_port = int(sys.argv[2])
    except ValueError:
        print("Erro: primary_port deve ser inteiro")
        sys.exit(1)

    broker = DistributedBroker(broker_id, primary_port)
    broker.start()

    print("Broker em execucao. Ctrl+C para encerrar.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Encerrando broker...")
