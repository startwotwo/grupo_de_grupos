#!/usr/bin/env python3
"""
Cliente de videoconferência — sd-meeting-app

Padrões ZeroMQ utilizados
  PUSH → broker PULL   envio de mídia (texto, áudio, vídeo)
  SUB  ← broker PUB   recepção de mídia por sala (topic: "texto:sala")
  DEALER ↔ broker ROUTER  controle: login, ACK, presença, leave

Tolerância a falhas
  Thread hb_monitor assina o PUB de heartbeat do broker.
  Se não recebe heartbeat por cluster.heartbeat_timeout segundos,
  sinaliza reconexão. O loop principal fecha sockets, redescobre
  broker no registry e reinicia todas as threads.

QoS por canal
  Texto  — ACK do broker; fila de reenvio com max_retry tentativas
  Áudio  — sem garantia, real-time (packet drop tolerado)
  Vídeo  — qualidade e FPS adaptativos baseados em backpressure

Uso:
  python3 client.py [--username <nome>] [--room <sala>] [--no-av]
  --no-av desativa captura/reprodução de áudio e vídeo (útil para demo)
"""

import argparse
import json
import queue
import select
import sys
import threading
import time
import uuid

import yaml
import zmq

# Importações opcionais — falha graciosa se hardware indisponível
try:
    import pyaudio
    _AUDIO_OK = True
except ImportError:
    _AUDIO_OK = False

try:
    import cv2
    import numpy as np
    _VIDEO_OK = True
except ImportError:
    _VIDEO_OK = False


# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------

def _load_cfg():
    with open("config.yaml") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Service discovery
# ---------------------------------------------------------------------------

class DiscoveryClient:
    """Consulta o registry para obter informações do broker responsável por uma sala."""

    def __init__(self, cfg: dict, registry_host: str | None = None):
        # Use provided registry_host or fall back to config (client.registry_host) or registry.host
        if registry_host:
            self._host = registry_host
        else:
            self._host = cfg.get("client", {}).get("registry_host") or cfg["registry"]["host"]
        self._port = cfg["registry"]["port"]

    def query_room(self, room: str, timeout_ms: int = 2000) -> dict | None:
        ctx  = zmq.Context.instance()
        sock = ctx.socket(zmq.REQ)
        sock.setsockopt(zmq.LINGER, 0)
        sock.setsockopt(zmq.RCVTIMEO, timeout_ms)
        sock.connect(f"tcp://{self._host}:{self._port}")
        try:
            sock.send_string(json.dumps({"type": "query_room", "room": room}))
            resp = json.loads(sock.recv_string())
            return resp.get("broker") if resp.get("status") == "ok" else None
        except Exception:
            return None
        finally:
            sock.close()


# ---------------------------------------------------------------------------
# QoS — Texto (ACK + reenvio)
# ---------------------------------------------------------------------------

class TextQoS:
    """
    Rastreia mensagens de texto não confirmadas e as reenvia até max_retry vezes.
    Toda comunicação com o socket ocorre via fila thread-safe (_out_q),
    garantindo que apenas a thread de envio toque o socket.
    """

    def __init__(self, cfg: dict):
        qos = cfg["qos"]["text"]
        self._max_retry     = qos["max_retry"]
        self._retry_ivl     = qos["retry_interval"]
        self._pending: dict[str, dict] = {}
        self._lock  = threading.Lock()
        self._out_q: queue.Queue = queue.Queue()

    def send(self, msg_dict: dict):
        """Enfileira mensagem para envio e inicia rastreamento de ACK."""
        mid  = msg_dict["msg_id"]
        data = json.dumps(msg_dict).encode()
        with self._lock:
            self._pending[mid] = {"data": data, "retries": 0, "ts": time.time()}
        self._out_q.put(data)

    def ack(self, msg_id: str):
        with self._lock:
            self._pending.pop(msg_id, None)

    def get_next(self, timeout: float = 0) -> bytes | None:
        """Retorna próximo pacote a enviar (novo ou reenvio), ou None."""
        try:
            return self._out_q.get(timeout=timeout)
        except queue.Empty:
            return None

    def retry_loop(self, stop: threading.Event):
        """Thread que reenvia mensagens sem ACK periodicamente."""
        while not stop.is_set():
            now = time.time()
            with self._lock:
                expired = []
                for mid, p in self._pending.items():
                    if now - p["ts"] >= self._retry_ivl:
                        if p["retries"] >= self._max_retry:
                            expired.append(mid)
                        else:
                            self._out_q.put(p["data"])
                            p["retries"] += 1
                            p["ts"] = now
                for mid in expired:
                    del self._pending[mid]
            stop.wait(0.05)


# ---------------------------------------------------------------------------
# QoS — Vídeo (qualidade e FPS adaptativos)
# ---------------------------------------------------------------------------

class VideoQoS:
    def __init__(self, cfg: dict):
        qos = cfg["qos"]["video"]
        self.quality     = qos["jpeg_quality"]
        self.min_quality = qos["min_jpeg_quality"]
        self.fps         = qos["base_fps"]
        self.min_fps     = qos["min_fps"]
        self.base_fps    = qos["base_fps"]
        self._last_send  = 0.0

    def should_send(self) -> bool:
        now = time.time()
        if now - self._last_send >= 1.0 / self.fps:
            self._last_send = now
            return True
        return False

    def encode(self, frame) -> bytes | None:
        ok, buf = cv2.imencode(
            ".jpg", frame,
            [cv2.IMWRITE_JPEG_QUALITY, int(self.quality)]
        )
        return buf.tobytes() if ok else None

    def degrade(self):
        """Reduz qualidade e FPS quando há backpressure."""
        self.quality = max(self.min_quality, self.quality - 10)
        self.fps     = max(self.min_fps, self.fps - 2)

    def recover(self):
        """Recupera qualidade e FPS gradualmente."""
        self.quality = min(70, self.quality + 5)
        self.fps     = min(self.base_fps, self.fps + 1)


# ---------------------------------------------------------------------------
# Sessão GUI — para uso por client_gui.py
# ---------------------------------------------------------------------------

class GUIClientSession:
    """
    Gerencia todas as operações de rede para client_gui.py via fila thread-safe.
    GUI comunica-se apenas através de _gui_q (queue.Queue):
      - Envio: GUI chama send_text() para enviar mensagens
      - Recepção: thread monitora _gui_q e enfileira eventos para GUI processar
    """

    def __init__(self, cfg: dict, client_id: str, gui_q: queue.Queue, stop: threading.Event):
        self.cfg       = cfg
        self.client_id = client_id
        self.username  = None
        self.room      = None
        self.broker    = None

        self._gui_q    = gui_q
        self._stop     = stop
        self._camera_enabled = threading.Event()
        self._camera_enabled.set()  # Camera starts enabled
        self._hb_interval = self.cfg["cluster"].get("heartbeat_interval", 2.0)
        self._threads: list[threading.Thread] = []
        self._socks: dict[str, zmq.Socket] = {}
        self._discovery = DiscoveryClient(cfg)
        self._text_qos  = TextQoS(cfg)
        self._video_qos = VideoQoS(cfg) if _VIDEO_OK else None

    def set_credentials(self, username: str, room: str):
        """Define usuário e sala antes de conectar."""
        self.username = username
        self.room = room

    def set_camera_enabled(self, enabled: bool):
        """Habilita/desabilita a captura da câmera. Pausa/despausa a thread."""
        if enabled:
            self._camera_enabled.set()
        else:
            self._camera_enabled.clear()

    def is_camera_enabled(self) -> bool:
        """Confere se a câmera está atualmente habilitadad."""
        return self._camera_enabled.is_set()

    # ------------------------------------------------------------------
    # Descoberta de broker
    # ------------------------------------------------------------------

    def discover_broker(self, max_retries: int = 15, delay: float = 1.0) -> bool:
        """Descobre o broker para a sala. Retorna True se encontrado."""
        for i in range(max_retries):
            info = self._discovery.query_room(self.room)
            if info:
                self.broker = info
                return True
            time.sleep(delay)
        return False

    # ------------------------------------------------------------------
    # Sockets
    # ------------------------------------------------------------------

    def _make_sockets(self) -> dict[str, zmq.Socket]:
        h = self.broker["host"]
        P = self.broker["ports"]
        ctx = zmq.Context.instance()
        s = {}

        s["text_push"] = ctx.socket(zmq.PUSH)
        s["text_push"].connect(f"tcp://{h}:{P['text_pull']}")

        s["text_sub"] = ctx.socket(zmq.SUB)
        s["text_sub"].connect(f"tcp://{h}:{P['text_pub']}")
        s["text_sub"].setsockopt_string(zmq.SUBSCRIBE, f"text:{self.room}")

        s["audio_push"] = ctx.socket(zmq.PUSH)
        s["audio_push"].connect(f"tcp://{h}:{P['audio_pull']}")

        s["audio_sub"] = ctx.socket(zmq.SUB)
        s["audio_sub"].connect(f"tcp://{h}:{P['audio_pub']}")
        s["audio_sub"].setsockopt_string(zmq.SUBSCRIBE, f"audio:{self.room}")

        s["video_push"] = ctx.socket(zmq.PUSH)
        s["video_push"].connect(f"tcp://{h}:{P['video_pull']}")

        s["video_sub"] = ctx.socket(zmq.SUB)
        s["video_sub"].connect(f"tcp://{h}:{P['video_pub']}")
        s["video_sub"].setsockopt_string(zmq.SUBSCRIBE, f"video:{self.room}")

        s["ctrl"] = ctx.socket(zmq.DEALER)
        s["ctrl"].setsockopt_string(zmq.IDENTITY, self.client_id)
        s["ctrl"].connect(f"tcp://{h}:{P['control']}")

        s["hb_ctrl"] = ctx.socket(zmq.DEALER)
        s["hb_ctrl"].setsockopt_string(zmq.IDENTITY, f"{self.client_id}-hb")
        s["hb_ctrl"].connect(f"tcp://{h}:{P['control']}")

        s["hb_sub"] = ctx.socket(zmq.SUB)
        s["hb_sub"].connect(f"tcp://{h}:{P['heartbeat']}")
        s["hb_sub"].setsockopt_string(zmq.SUBSCRIBE, "hb")

        return s

    def _close_sockets(self):
        for sock in self._socks.values():
            try:
                sock.setsockopt(zmq.LINGER, 0)
                sock.close()
            except Exception:
                pass
        self._socks = {}

    def _do_zmq_login(self) -> bool:
        """Realiza login no broker via socket ctrl. Retorna True se sucesso."""
        msg = json.dumps({
            "v": 1, "type": "login",
            "sender_id": self.client_id,
            "username":  self.username,
            "room":      self.room,
        }).encode()
        self._socks["ctrl"].send(msg)
        poller = zmq.Poller()
        poller.register(self._socks["ctrl"], zmq.POLLIN)
        evts = dict(poller.poll(timeout=4000))
        if self._socks["ctrl"] in evts:
            resp = json.loads(self._socks["ctrl"].recv())
            if resp.get("type") == "login_ack":
                members = resp.get("members", {})
                self._gui_q.put({"type": "presence", "members": members})
                self._gui_q.put({"type": "status", "msg": "● Conectado",
                                 "color": "#00d4aa"})
                return True
        return False

    # ------------------------------------------------------------------
    # Métodos da GUI para enviar dados
    # ------------------------------------------------------------------

    def send_text(self, content: str):
        """GUI chama para enviar mensagem de texto."""
        if not self._socks or "text_push" not in self._socks:
            return
        self._text_qos.send({
            "v": 1, "type": "text",
            "msg_id":    str(uuid.uuid4()),
            "room":      self.room,
            "sender_id": self.client_id,
            "username":  self.username,
            "content":   content,
            "ts":        time.time(),
        })

    def send_audio(self, pcm_data: bytes, muted: bool):
        """Envia áudio capturado. Chamado pelo callback de áudio."""
        if not self._socks or "audio_push" not in self._socks or muted:
            return
        try:
            meta = json.dumps({
                "v": 1, "type": "audio",
                "room": self.room, "sender_id": self.client_id,
            }).encode()
            self._socks["audio_push"].send_multipart([meta, pcm_data], flags=zmq.NOBLOCK)
        except Exception:
            pass

    def send_video(self, frame):
        """Envia vídeo codificado como JPEG."""
        if not self._socks or "video_push" not in self._socks or self._video_qos is None:
            return
        if not self._video_qos.should_send():
            return
        jpeg = self._video_qos.encode(frame)
        if jpeg is None:
            return
        meta = json.dumps({
            "v": 1, "type": "video",
            "room": self.room, "sender_id": self.client_id,
            "quality": int(self._video_qos.quality),
        }).encode()
        try:
            self._socks["video_push"].send_multipart([meta, jpeg], flags=zmq.NOBLOCK)
        except zmq.Again:
            self._video_qos.degrade()

    # ------------------------------------------------------------------
    # Threads ZMQ — comunicação via _gui_q
    # ------------------------------------------------------------------

    def _th_text_send(self):
        """Envia mensagens de texto da fila QoS."""
        while not self._stop.is_set():
            data = self._text_qos.get_next(timeout=0)
            if data:
                try:
                    self._socks["text_push"].send(data, flags=zmq.NOBLOCK)
                except Exception:
                    pass
            else:
                self._stop.wait(0.05)

    def _th_text_recv(self):
        """Recebe mensagens de texto e presença, enfileira na _gui_q."""
        poller = zmq.Poller()
        poller.register(self._socks["text_sub"], zmq.POLLIN)
        while not self._stop.is_set():
            evts = dict(poller.poll(timeout=500))
            if self._socks["text_sub"] not in evts:
                continue
            frames = self._socks["text_sub"].recv_multipart()
            if len(frames) < 2:
                continue
            try:
                msg = json.loads(frames[1])
            except Exception:
                continue
            t = msg.get("type")
            if t == "text" and msg.get("sender_id") != self.client_id:
                self._gui_q.put({
                    "type":     "text",
                    "username": msg.get("username", "?"),
                    "content":  msg.get("content", ""),
                    "ts":       msg.get("ts", time.time()),
                })
            elif t == "presence":
                self._gui_q.put({
                    "type":    "presence",
                    "members": msg.get("members", {}),
                })

    def _th_ctrl_recv(self):
        """Recebe ACKs de controle."""
        poller = zmq.Poller()
        poller.register(self._socks["ctrl"], zmq.POLLIN)
        while not self._stop.is_set():
            evts = dict(poller.poll(timeout=500))
            if self._socks["ctrl"] not in evts:
                continue
            try:
                msg = json.loads(self._socks["ctrl"].recv())
                if msg.get("type") == "text_ack":
                    self._text_qos.ack(msg.get("msg_id", ""))
            except Exception:
                pass

    def _th_client_hb(self):
        """Envia heartbeat periódico do cliente para manter presença ativa."""
        if not self._socks or "hb_ctrl" not in self._socks:
            return
        while not self._stop.is_set():
            try:
                msg = json.dumps({
                    "v": 1,
                    "type": "hb",
                    "room": self.room,
                    "sender_id": self.client_id,
                    "ts": time.time(),
                }).encode()
                self._socks["hb_ctrl"].send(msg, flags=zmq.NOBLOCK)
            except Exception:
                pass
            self._stop.wait(self._hb_interval)

    def _th_audio_zmq_recv(self, recv_queue: queue.Queue):
        """Recebe áudio do broker e enfileira para o callback de saída."""
        if not _AUDIO_OK:
            return
        poller = zmq.Poller()
        poller.register(self._socks["audio_sub"], zmq.POLLIN)
        while not self._stop.is_set():
            evts = dict(poller.poll(timeout=500))
            if self._socks["audio_sub"] not in evts:
                continue
            try:
                frames = self._socks["audio_sub"].recv_multipart()
                if len(frames) < 3:
                    continue
                try:
                    meta = json.loads(frames[1])
                except Exception:
                    continue
                if meta.get("sender_id") == self.client_id:
                    continue
                try:
                    recv_queue.put_nowait(frames[2])
                except queue.Full:
                    try:
                        recv_queue.get_nowait()
                        recv_queue.put_nowait(frames[2])
                    except Exception:
                        pass
            except Exception:
                pass

    def _th_video_send(self):
        """Captura vídeo da câmera e envia via ZMQ. Pausa quando câmera é desligada."""
        if not _VIDEO_OK or self._video_qos is None:
            return
        vcfg = self.cfg["client"]["video"]
        cap = None
        
        while not self._stop.is_set():
            # Check if camera should be enabled
            if not self._camera_enabled.is_set():
                # Camera disabled: release capture and wait
                if cap:
                    cap.release()
                    cap = None
                self._camera_enabled.wait(timeout=0.5)
                continue
            
            # Camera enabled: ensure capture is open
            if not cap:
                try:
                    cap = cv2.VideoCapture(vcfg["device_index"])
                except Exception:
                    self._camera_enabled.wait(timeout=1.0)
                    continue
                if not cap.isOpened():
                    cap = None
                    self._camera_enabled.wait(timeout=1.0)
                    continue
                cap.set(cv2.CAP_PROP_FRAME_WIDTH,  vcfg["frame_width"])
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, vcfg["frame_height"])
            
            # Capture and send frame
            ret, frame = cap.read()
            if not ret:
                continue
            # Envia para GUI para self-preview
            self._gui_q.put({"type": "video_self", "frame": frame.copy()})
            if not self._video_qos.should_send():
                time.sleep(0.01)
                continue
            jpeg = self._video_qos.encode(frame)
            if jpeg is None:
                continue
            meta = json.dumps({
                "v": 1, "type": "video",
                "room": self.room, "sender_id": self.client_id,
                "quality": int(self._video_qos.quality),
            }).encode()
            try:
                self._socks["video_push"].send_multipart([meta, jpeg], flags=zmq.NOBLOCK)
            except zmq.Again:
                self._video_qos.degrade()
        
        # Cleanup on exit
        if cap:
            cap.release()

    def _th_video_recv(self):
        """Recebe vídeo remoto e enfileira para GUI."""
        if not _VIDEO_OK:
            return
        poller = zmq.Poller()
        poller.register(self._socks["video_sub"], zmq.POLLIN)
        frame_count = 0
        while not self._stop.is_set():
            evts = dict(poller.poll(timeout=500))
            if self._socks["video_sub"] not in evts:
                continue
            frames = self._socks["video_sub"].recv_multipart()
            if len(frames) < 3:
                continue
            try:
                meta = json.loads(frames[1])
            except Exception:
                continue
            if meta.get("sender_id") == self.client_id:
                continue
            self._gui_q.put({
                "type": "video_participant",
                "sender_id": meta.get("sender_id", ""),
                "jpeg": frames[2],
            })
            frame_count += 1
            if frame_count % 30 == 0 and self._video_qos:
                self._video_qos.recover()

    def _th_hb_monitor(self):
        """Monitora heartbeat do broker."""
        timeout  = self.cfg["cluster"]["heartbeat_timeout"]
        last_hb  = time.time()
        poller   = zmq.Poller()
        poller.register(self._socks["hb_sub"], zmq.POLLIN)
        while not self._stop.is_set():
            evts = dict(poller.poll(timeout=1000))
            if self._socks["hb_sub"] in evts:
                try:
                    self._socks["hb_sub"].recv_multipart()
                    last_hb = time.time()
                except Exception:
                    pass
            elif time.time() - last_hb > timeout:
                self._gui_q.put({"type": "reconnecting"})
                self._stop.set()
                return

    # ------------------------------------------------------------------
    # Gerenciamento de conexão
    # ------------------------------------------------------------------

    def start(self, audio_recv_queue: queue.Queue) -> bool:
        """
        Conecta ao broker, faz login e inicia todas as threads.
        audio_recv_queue: queue para buffering de áudio recebido (para callback).
        Retorna True se sucesso, False caso contrário.
        """
        self._stop.clear()
        self._socks = self._make_sockets()

        if not self._do_zmq_login():
            self._gui_q.put({"type": "status",
                             "msg": "⚠ Falha no login", "color": "#e94560"})
            self._close_sockets()
            return False

        self._threads = []
        specs = [
            ("text-send",  self._th_text_send),
            ("text-recv",  self._th_text_recv),
            ("ctrl-recv",  self._th_ctrl_recv),
            ("client-hb",   self._th_client_hb),
            ("video-send", self._th_video_send),
            ("video-recv", self._th_video_recv),
            ("hb-monitor", self._th_hb_monitor),
        ]
        for name, fn in specs:
            t = threading.Thread(target=fn, name=name, daemon=True)
            t.start()
            self._threads.append(t)

        # Thread de áudio recv (precisa da fila)
        t = threading.Thread(target=self._th_audio_zmq_recv, args=(audio_recv_queue,),
                             name="audio-zmq-recv", daemon=True)
        t.start()
        self._threads.append(t)

        # Thread de reenvio de texto QoS
        t = threading.Thread(target=self._text_qos.retry_loop,
                             args=(self._stop,), name="text-qos-retry", daemon=True)
        t.start()
        self._threads.append(t)

        return True

    def stop(self):
        """Para todas as threads e fecha sockets."""
        self._stop.set()
        for t in self._threads:
            t.join(timeout=2.0)
        self._close_sockets()


# ---------------------------------------------------------------------------
# Sessão do cliente — CLI
# ---------------------------------------------------------------------------

class ClientSession:
    def __init__(self, username: str | None, room: str | None, no_av: bool, registry_host: str | None = None):
        self.cfg       = _load_cfg()
        self.client_id = str(uuid.uuid4())
        self.username  = username
        self.room      = room
        self.no_av     = no_av
        self.broker    = None  # dict com info do broker atual

        self._stop   = threading.Event()
        self._threads: list[threading.Thread] = []
        self._discovery = DiscoveryClient(self.cfg, registry_host)
        self._text_qos  = TextQoS(self.cfg)
        self._video_qos = VideoQoS(self.cfg) if (_VIDEO_OK and not no_av) else None

    # ------------------------------------------------------------------
    # Login interativo
    # ------------------------------------------------------------------

    def _interactive_login(self):
        if not self.username:
            self.username = input("Username: ").strip() or f"user-{self.client_id[:4]}"
        if not self.room:
            rooms = self.cfg["cluster"]["all_rooms"]
            print(f"Salas disponíveis: {rooms}")
            room = input("Sala: ").strip().upper()
            self.room = room if room in rooms else "A"

    # ------------------------------------------------------------------
    # Descoberta de broker
    # ------------------------------------------------------------------

    def _discover(self, max_retries: int = 30, delay: float = 1.0) -> bool:
        for i in range(max_retries):
            info = self._discovery.query_room(self.room)
            if info:
                self.broker = info
                print(f"[Client] Broker {info['broker_id'][:8]} "
                      f"encontrado para sala {self.room}")
                return True
            print(f"[Client] Sala {self.room} sem broker, "
                  f"aguardando... ({i+1}/{max_retries})")
            time.sleep(delay)
        return False

    # ------------------------------------------------------------------
    # Sockets
    # ------------------------------------------------------------------

    def _make_sockets(self, ctx: zmq.Context) -> dict[str, zmq.Socket]:
        h = self.broker["host"]
        P = self.broker["ports"]
        s = {}

        s["text_push"] = ctx.socket(zmq.PUSH)
        s["text_push"].connect(f"tcp://{h}:{P['text_pull']}")

        s["text_sub"] = ctx.socket(zmq.SUB)
        s["text_sub"].connect(f"tcp://{h}:{P['text_pub']}")
        s["text_sub"].setsockopt_string(zmq.SUBSCRIBE, f"text:{self.room}")

        s["audio_push"] = ctx.socket(zmq.PUSH)
        s["audio_push"].connect(f"tcp://{h}:{P['audio_pull']}")

        s["audio_sub"] = ctx.socket(zmq.SUB)
        s["audio_sub"].connect(f"tcp://{h}:{P['audio_pub']}")
        s["audio_sub"].setsockopt_string(zmq.SUBSCRIBE, f"audio:{self.room}")

        s["video_push"] = ctx.socket(zmq.PUSH)
        s["video_push"].connect(f"tcp://{h}:{P['video_pull']}")

        s["video_sub"] = ctx.socket(zmq.SUB)
        s["video_sub"].connect(f"tcp://{h}:{P['video_pub']}")
        s["video_sub"].setsockopt_string(zmq.SUBSCRIBE, f"video:{self.room}")

        s["ctrl"] = ctx.socket(zmq.DEALER)
        s["ctrl"].setsockopt_string(zmq.IDENTITY, self.client_id)
        s["ctrl"].connect(f"tcp://{h}:{P['control']}")

        s["hb_sub"] = ctx.socket(zmq.SUB)
        s["hb_sub"].connect(f"tcp://{h}:{P['heartbeat']}")
        s["hb_sub"].setsockopt_string(zmq.SUBSCRIBE, "hb")

        return s

    def _close_sockets(self, socks: dict):
        for sock in socks.values():
            try:
                sock.setsockopt(zmq.LINGER, 0)
                sock.close()
            except Exception:
                pass

    def _do_login(self, ctrl: zmq.Socket) -> bool:
        msg = json.dumps({
            "v": 1, "type": "login",
            "sender_id": self.client_id,
            "username":  self.username,
            "room":      self.room,
        }).encode()
        ctrl.send(msg)
        poller = zmq.Poller()
        poller.register(ctrl, zmq.POLLIN)
        evts = dict(poller.poll(timeout=4000))
        if ctrl in evts:
            resp = json.loads(ctrl.recv())
            if resp.get("type") == "login_ack":
                members = resp.get("members", {})
                print(f"[Client] Entrou na sala {self.room}. "
                      f"Membros: {list(members.values())}")
                print("[CONNECTED]")
                return True
        print("[Client] Timeout no login ACK")
        return False

    # ------------------------------------------------------------------
    # Threads de mídia
    # ------------------------------------------------------------------

    def _th_text_send(self, sock: zmq.Socket):
        """Lê stdin e envia mensagens de texto; também processa reenvios do QoS."""
        while not self._stop.is_set():
            # Drena fila de reenvio (não-bloqueante)
            while True:
                data = self._text_qos.get_next(timeout=0)
                if data is None:
                    break
                try:
                    sock.send(data, flags=zmq.NOBLOCK)
                except Exception:
                    pass

            # Verifica stdin com timeout curto
            try:
                r, _, _ = select.select([sys.stdin], [], [], 0.1)
            except Exception:
                time.sleep(0.1)
                continue
            if not r:
                continue
            line = sys.stdin.readline()
            if not line:
                break
            content = line.strip()
            if not content:
                continue
            self._text_qos.send({
                "v": 1, "type": "text",
                "msg_id":    str(uuid.uuid4()),
                "room":      self.room,
                "sender_id": self.client_id,
                "username":  self.username,
                "content":   content,
                "ts":        time.time(),
            })

    def _th_text_recv(self, sock: zmq.Socket):
        poller = zmq.Poller()
        poller.register(sock, zmq.POLLIN)
        while not self._stop.is_set():
            evts = dict(poller.poll(timeout=500))
            if sock not in evts:
                continue
            frames = sock.recv_multipart()
            if len(frames) < 2:
                continue
            try:
                msg = json.loads(frames[1])
            except Exception:
                continue
            t = msg.get("type")
            if t == "text" and msg.get("sender_id") != self.client_id:
                ts = time.strftime("%H:%M:%S",
                                   time.localtime(msg.get("ts", time.time())))
                print(f"\n[{ts}] {msg.get('username','?')}: {msg.get('content','')}")
                print(f"[{self.username}] > ", end="", flush=True)
            elif t == "presence":
                members = msg.get("members", {})
                print(f"\n[Presença] Sala {msg.get('room')}: "
                      f"{list(members.values())}")
                print(f"[{self.username}] > ", end="", flush=True)

    def _th_ctrl_recv(self, sock: zmq.Socket):
        """Recebe ACKs e outras mensagens de controle do broker."""
        poller = zmq.Poller()
        poller.register(sock, zmq.POLLIN)
        while not self._stop.is_set():
            evts = dict(poller.poll(timeout=500))
            if sock not in evts:
                continue
            try:
                msg = json.loads(sock.recv())
            except Exception:
                continue
            if msg.get("type") == "text_ack":
                self._text_qos.ack(msg.get("msg_id", ""))

    def _th_audio_send(self, sock: zmq.Socket):
        if not _AUDIO_OK or self.no_av:
            return
        cfg = self.cfg["client"]["audio"]
        p   = pyaudio.PyAudio()
        try:
            stream = p.open(
                format=pyaudio.paInt16, channels=cfg["channels"],
                rate=cfg["rate"], input=True,
                frames_per_buffer=cfg["chunk"],
            )
        except Exception as e:
            print(f"[Client] Áudio de entrada indisponível: {e}")
            p.terminate()
            return
        meta = json.dumps({
            "v": 1, "type": "audio",
            "room": self.room, "sender_id": self.client_id,
        }).encode()
        while not self._stop.is_set():
            try:
                pcm = stream.read(cfg["chunk"], exception_on_overflow=False)
                sock.send_multipart([meta, pcm], flags=zmq.NOBLOCK)
            except Exception:
                pass
        stream.stop_stream(); stream.close(); p.terminate()

    def _th_audio_recv(self, sock: zmq.Socket):
        if not _AUDIO_OK or self.no_av:
            return
        cfg = self.cfg["client"]["audio"]
        p   = pyaudio.PyAudio()
        try:
            stream = p.open(
                format=pyaudio.paInt16, channels=cfg["channels"],
                rate=cfg["rate"], output=True,
            )
        except Exception as e:
            print(f"[Client] Áudio de saída indisponível: {e}")
            p.terminate()
            return
        poller = zmq.Poller()
        poller.register(sock, zmq.POLLIN)
        while not self._stop.is_set():
            evts = dict(poller.poll(timeout=500))
            if sock not in evts:
                continue
            frames = sock.recv_multipart()
            if len(frames) < 3:
                continue
            try:
                meta = json.loads(frames[1])
            except Exception:
                continue
            if meta.get("sender_id") == self.client_id:
                continue
            try:
                stream.write(frames[2])
            except Exception:
                pass
        stream.stop_stream(); stream.close(); p.terminate()

    def _th_video_send(self, sock: zmq.Socket):
        if not _VIDEO_OK or self.no_av or self._video_qos is None:
            return
        vcfg = self.cfg["client"]["video"]
        cap  = cv2.VideoCapture(vcfg["device_index"])
        if not cap.isOpened():
            print("[Client] Câmera indisponível")
            return
        cap.set(cv2.CAP_PROP_FRAME_WIDTH,  vcfg["frame_width"])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, vcfg["frame_height"])
        while not self._stop.is_set():
            if not self._video_qos.should_send():
                time.sleep(0.01)
                continue
            ret, frame = cap.read()
            if not ret:
                continue
            jpeg = self._video_qos.encode(frame)
            if jpeg is None:
                continue
            meta = json.dumps({
                "v": 1, "type": "video",
                "room": self.room, "sender_id": self.client_id,
                "quality": int(self._video_qos.quality),
            }).encode()
            try:
                sock.send_multipart([meta, jpeg], flags=zmq.NOBLOCK)
            except zmq.Again:
                self._video_qos.degrade()
        cap.release()

    def _th_video_recv(self, sock: zmq.Socket):
        if not _VIDEO_OK or self.no_av:
            return
        poller = zmq.Poller()
        poller.register(sock, zmq.POLLIN)
        window      = f"Sala {self.room} — {self.username}"
        frame_count = 0
        while not self._stop.is_set():
            evts = dict(poller.poll(timeout=500))
            if sock not in evts:
                continue
            frames = sock.recv_multipart()
            if len(frames) < 3:
                continue
            try:
                meta = json.loads(frames[1])
            except Exception:
                continue
            if meta.get("sender_id") == self.client_id:
                continue
            arr   = np.frombuffer(frames[2], dtype=np.uint8)
            frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if frame is not None:
                cv2.imshow(window, frame)
                frame_count += 1
                if frame_count % 30 == 0 and self._video_qos:
                    self._video_qos.recover()
            if cv2.waitKey(1) & 0xFF == ord("q"):
                self._stop.set()
                break
        cv2.destroyAllWindows()

    def _th_hb_monitor(self, sock: zmq.Socket):
        """Detecta falha do broker via timeout de heartbeat."""
        timeout  = self.cfg["cluster"]["heartbeat_timeout"]
        last_hb  = time.time()
        poller   = zmq.Poller()
        poller.register(sock, zmq.POLLIN)
        while not self._stop.is_set():
            evts = dict(poller.poll(timeout=1000))
            if sock in evts:
                try:
                    sock.recv_multipart()
                    last_hb = time.time()
                except Exception:
                    pass
            elif time.time() - last_hb > timeout:
                print(f"\n[Client] Heartbeat do broker perdido. "
                      f"[RECONNECTING]")
                self._stop.set()   # sinaliza reconexão para o loop principal

    # ------------------------------------------------------------------
    # Gerenciamento de threads
    # ------------------------------------------------------------------

    def _start_threads(self, socks: dict):
        self._stop.clear()
        self._threads = []
        specs = [
            ("text-send",  self._th_text_send,  socks["text_push"]),
            ("text-recv",  self._th_text_recv,  socks["text_sub"]),
            ("ctrl-recv",  self._th_ctrl_recv,  socks["ctrl"]),
            ("audio-send", self._th_audio_send, socks["audio_push"]),
            ("audio-recv", self._th_audio_recv, socks["audio_sub"]),
            ("video-send", self._th_video_send, socks["video_push"]),
            ("video-recv", self._th_video_recv, socks["video_sub"]),
            ("hb-monitor", self._th_hb_monitor, socks["hb_sub"]),
        ]
        for name, fn, sock in specs:
            t = threading.Thread(target=fn, args=(sock,),
                                 name=name, daemon=True)
            t.start()
            self._threads.append(t)
        # Thread separada para reenvio de texto (QoS)
        t = threading.Thread(target=self._text_qos.retry_loop,
                             args=(self._stop,),
                             name="text-qos-retry", daemon=True)
        t.start()
        self._threads.append(t)

    def _join_threads(self):
        for t in self._threads:
            t.join(timeout=2.0)

    # ------------------------------------------------------------------
    # Loop principal com reconexão automática
    # ------------------------------------------------------------------

    def run(self):
        self._interactive_login()
        ctx  = zmq.Context()
        quit_flag = False

        while not quit_flag:
            if not self._discover():
                print("[Client] Nenhum broker disponível. Saindo.")
                break

            socks = self._make_sockets(ctx)

            if not self._do_login(socks["ctrl"]):
                self._close_sockets(socks)
                time.sleep(2)
                continue

            self._start_threads(socks)
            print(f"[{self.username}] > ", end="", flush=True)

            try:
                while not self._stop.is_set():
                    time.sleep(0.3)
            except KeyboardInterrupt:
                print("\n[Client] Encerrando.")
                self._stop.set()
                quit_flag = True

            self._join_threads()
            self._close_sockets(socks)

            if not quit_flag:
                # _stop foi setado por hb_monitor → reconectar
                print("[Client] Reconectando em 2 s... [RECONNECTING]")
                time.sleep(2)
                self._stop.clear()
                self.broker = None


# ---------------------------------------------------------------------------
# Ponto de entrada
# ---------------------------------------------------------------------------

def _parse_args():
    p = argparse.ArgumentParser(description="Cliente de videoconferência")
    p.add_argument("--username", default=None, help="Nome do usuário")
    p.add_argument("--room",     default=None, help="Sala (A-K)")
    p.add_argument("--no-av",   action="store_true",
                   help="Desativa áudio e vídeo (modo texto)")
    p.add_argument("--registry-host", default=None,
                   help="Host do registry (override config.yaml)")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    ClientSession(
        username=args.username,
        room=args.room,
        no_av=args.no_av,
        registry_host=args.registry_host,
    ).run()
