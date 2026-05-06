import json
import os
import queue
import socket
import sys
import threading
import time
import uuid
from datetime import datetime, timezone

import cv2
import numpy as np
import tkinter as tk
from tkinter import scrolledtext
import zmq

try:
    from PIL import Image, ImageTk
except Exception:
    Image = None
    ImageTk = None

try:
    import pyaudio
except Exception:
    pyaudio = None


BROKER_HOST = os.environ.get("BROKER_HOST", "127.0.0.1")  # multi-PC: setar BROKER_HOST se discovery falhar
DISCOVERY_PORT = int(os.environ.get("DISCOVERY_PORT", "6000"))
DISCOVERY_TIMEOUT = 8

ctx = zmq.Context.instance()
stop_event = threading.Event()

state_lock = threading.Lock()
chat_lock = threading.Lock()
frames_lock = threading.Lock()

CHAT = []
SEEN_TEXT_IDS = set()
REMOTE_FRAMES = {}

USER_ID = ""
ROOM = "geral"

CURRENT_BROKER = None
BROKER_EPOCH = 0

VIDEO_RAW_Q = queue.Queue(maxsize=5)
AUDIO_RAW_Q = queue.Queue(maxsize=12)

AUDIO_CAPTURE_ENABLED = False
AUDIO_PLAYBACK_ENABLED = False
VIDEO_CAPTURE_ENABLED = True


class BrokerDiscovery:
    def __init__(self):
        self.running = True
        self.lock = threading.Lock()
        self.round_robin_index = -1
        self.brokers = {}  # broker_id -> {host, primary_port, control_port, mesh_port, latency, ts}

        threading.Thread(target=self._listen_heartbeats, daemon=True).start()
        threading.Thread(target=self._cleanup, daemon=True).start()

    def _listen_heartbeats(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", DISCOVERY_PORT))
        sock.settimeout(1)

        while self.running:
            try:
                data, addr = sock.recvfrom(4096)
                now = datetime.now(timezone.utc)
                msg = json.loads(data.decode("utf-8"))
                broker_id = msg.get("broker_id")
                if not broker_id:
                    continue

                heartbeat_ts = datetime.fromisoformat(msg.get("timestamp"))
                if heartbeat_ts.tzinfo is None:
                    heartbeat_ts = heartbeat_ts.replace(tzinfo=timezone.utc)
                latency_ms = max((now - heartbeat_ts).total_seconds() * 1000, 0.0)

                with self.lock:
                    first_seen = broker_id not in self.brokers
                    self.brokers[broker_id] = {
                        "host": addr[0],
                        "primary_port": int(msg.get("primary_port")),
                        "control_port": int(msg.get("control_port")),
                        "mesh_port": int(msg.get("mesh_port")),
                        "latency": latency_ms,
                        "ts": time.time(),
                    }
                if first_seen:
                    print(
                        f"[Discovery] broker={broker_id} host={addr[0]} control={msg.get('control_port')}"
                    )
            except socket.timeout:
                pass
            except Exception:
                pass

    def _cleanup(self):
        while self.running:
            time.sleep(1)
            now = time.time()
            with self.lock:
                dead = [
                    broker_id
                    for broker_id, info in self.brokers.items()
                    if now - info["ts"] > DISCOVERY_TIMEOUT
                ]
                for broker_id in dead:
                    del self.brokers[broker_id]
                    print(f"[Discovery] broker={broker_id} removido (timeout)")

    def list_brokers(self):
        with self.lock:
            return dict(self.brokers)

    def pick_round_robin(self, exclude_id=None):
        with self.lock:
            items = [item for item in self.brokers.items() if item[0] != exclude_id]
            if not items:
                return None, None
            self.round_robin_index = (self.round_robin_index + 1) % len(items)
            broker_id, info = items[self.round_robin_index]
            return broker_id, info

    def pick_lowest_latency(self, exclude_id=None):
        with self.lock:
            items = [item for item in self.brokers.items() if item[0] != exclude_id]
            if not items:
                return None, None
            broker_id, info = min(items, key=lambda item: item[1]["latency"])
            return broker_id, info


def get_current_broker():
    with state_lock:
        return CURRENT_BROKER, BROKER_EPOCH


def set_current_broker(broker_info):
    global CURRENT_BROKER, BROKER_EPOCH
    with state_lock:
        CURRENT_BROKER = broker_info
        BROKER_EPOCH += 1
        return BROKER_EPOCH


def control_request(broker_info, payload, timeout_ms=1200):
    hosts = []
    discovered_host = broker_info.get("host")
    if discovered_host:
        hosts.append(discovered_host)
    if BROKER_HOST not in hosts:
        hosts.append(BROKER_HOST)
    hosts.extend(["127.0.0.1", "localhost"])

    unique_hosts = []
    for host in hosts:
        if host not in unique_hosts:
            unique_hosts.append(host)

    last_exc = None
    for host in unique_hosts:
        req = ctx.socket(zmq.REQ)
        req.setsockopt(zmq.RCVTIMEO, timeout_ms)
        req.setsockopt(zmq.SNDTIMEO, timeout_ms)
        req.connect(f"tcp://{host}:{broker_info['control_port']}")
        try:
            req.send_json(payload)
            response = req.recv_json()
            # Persist a working host for next requests/failover operations.
            broker_info["host"] = host
            return response
        except Exception as exc:
            last_exc = exc
        finally:
            req.close(0)

    if last_exc is not None:
        raise last_exc
    raise RuntimeError("control_request failed without exception")


def try_login(broker_id, broker_info):
    resp = control_request(
        broker_info,
        {
            "action": "login",
            "user_id": USER_ID,
            "room": ROOM,
        },
        timeout_ms=1500,
    )
    if not resp.get("ok"):
        return False, resp

    merged = dict(broker_info)
    merged["broker_id"] = broker_id
    merged["ports"] = resp.get("ports", {})
    set_current_broker(merged)
    print(
        f"[Broker] conectado broker={broker_id} host={broker_info['host']} control={broker_info['control_port']}"
    )
    return True, resp


def connect_to_best_broker(discovery: BrokerDiscovery, strategy="lowest-latency", exclude_id=None):
    brokers = discovery.list_brokers()
    if exclude_id:
        brokers.pop(exclude_id, None)
    if not brokers:
        return False

    items = list(brokers.items())
    if strategy == "round-robin":
        items.sort(key=lambda item: item[0])
    else:
        items.sort(key=lambda item: item[1].get("latency", 999999))

    # Try every known broker before giving up.
    for broker_id, info in items:
        try:
            ok, resp = try_login(broker_id, info)
            if ok:
                return True
            print(f"[Broker] login falhou em {broker_id}: {resp.get('error')}")
        except Exception as exc:
            print(f"[Broker] erro ao conectar {broker_id}: {exc}")

    return False


def heartbeat_monitor(discovery: BrokerDiscovery, strategy):
    failures = 0
    try:
        while not stop_event.is_set():
            broker, _ = get_current_broker()
            if not broker:
                time.sleep(0.5)
                continue

            try:
                resp = control_request(
                    broker,
                    {"action": "ping", "user_id": USER_ID},
                    timeout_ms=1000,
                )
                if resp.get("ok"):
                    failures = 0
                    control_request(
                        broker,
                        {"action": "heartbeat_user", "user_id": USER_ID},
                        timeout_ms=1000,
                    )
                else:
                    failures += 1
            except Exception:
                failures += 1

            if failures >= 3:
                old_id = broker.get("broker_id")
                print(f"[Failover] broker {old_id} indisponivel. Procurando outro...")
                switched = connect_to_best_broker(discovery, strategy=strategy, exclude_id=old_id)
                if switched:
                    failures = 0
                    add_system_chat(f"Failover para broker {get_current_broker()[0].get('broker_id')}")
                else:
                    add_system_chat("Sem broker disponivel no momento")

            time.sleep(2)
    except Exception as exc:
        add_system_chat(f"Erro em heartbeat_monitor: {exc}")
    except Exception as exc:
        add_system_chat(f"Erro em heartbeat_monitor: {exc}")


def safe_queue_put(q: queue.Queue, item):
    if q.full():
        try:
            q.get_nowait()
        except queue.Empty:
            pass
    try:
        q.put_nowait(item)
    except queue.Full:
        pass


def open_camera_fallback():
    candidates = []
    if sys.platform.startswith("win"):
        candidates = [
            (0, cv2.CAP_DSHOW),
            (0, cv2.CAP_MSMF),
            (0, cv2.CAP_ANY),
            (1, cv2.CAP_DSHOW),
            (1, cv2.CAP_MSMF),
        ]
    else:
        candidates = [
            (0, cv2.CAP_V4L2),
            (0, cv2.CAP_ANY),
            (1, cv2.CAP_V4L2),
        ]

    for index, backend in candidates:
        try:
            cam = cv2.VideoCapture(index, backend)
            if cam is not None and cam.isOpened():
                return cam, index, backend
            if cam is not None:
                cam.release()
        except Exception:
            continue

    try:
        cam = cv2.VideoCapture(0)
        if cam is not None and cam.isOpened():
            return cam, 0, None
        if cam is not None:
            cam.release()
    except Exception:
        pass

    return None, None, None


def video_capture_worker():
    global VIDEO_CAPTURE_ENABLED
    try:
        cam, index, backend = open_camera_fallback()
        if cam is None:
            VIDEO_CAPTURE_ENABLED = False
            add_system_chat("Camera nao encontrada: video local desativado")
            return

        try:
            cam.set(cv2.CAP_PROP_FRAME_WIDTH, 320)
            cam.set(cv2.CAP_PROP_FRAME_HEIGHT, 240)
        except Exception:
            pass

        backend_name = "default" if backend is None else str(backend)
        add_system_chat(f"Camera ativa: index={index} backend={backend_name}")

        while not stop_event.is_set():
            if not VIDEO_CAPTURE_ENABLED:
                time.sleep(0.2)
                continue

            ok, frame = cam.read()
            if not ok:
                time.sleep(0.02)
                continue
            safe_queue_put(VIDEO_RAW_Q, frame)
    except Exception as exc:
        add_system_chat(f"Erro em video_capture_worker: {exc}")
        time.sleep(1 / 30)

    cam.release()


def video_send_worker():
    pub = None
    local_epoch = -1
    try:
        while not stop_event.is_set():
            broker, epoch = get_current_broker()
            if not broker:
                time.sleep(0.2)
                continue

            if pub is None or local_epoch != epoch:
                if pub is not None:
                    pub.close(0)
                pub = ctx.socket(zmq.PUB)
                pub.setsockopt(zmq.SNDTIMEO, 500)
                pub.connect(f"tcp://{broker['host']}:{broker['ports']['video_pub_in']}")
                local_epoch = epoch
                time.sleep(0.2)

            try:
                frame = VIDEO_RAW_Q.get(timeout=0.5)
            except queue.Empty:
                continue

            load = VIDEO_RAW_Q.qsize() / max(VIDEO_RAW_Q.maxsize, 1)
            quality = 35 if load > 0.7 else 55 if load > 0.3 else 70

            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
            if not ok:
                continue

            topic = f"video:{ROOM}:{USER_ID}".encode("utf-8")
            try:
                pub.send_multipart([topic, buf.tobytes()])
            except Exception:
                pass

            time.sleep(1 / 15)

        if pub is not None:
            pub.close(0)
    except Exception as exc:
        add_system_chat(f"Erro em video_send_worker: {exc}")


def video_recv_worker():
    sub = None
    local_epoch = -1
    try:
        while not stop_event.is_set():
            broker, epoch = get_current_broker()
            if not broker:
                time.sleep(0.2)
                continue

            if sub is None or local_epoch != epoch:
                if sub is not None:
                    sub.close(0)
                sub = ctx.socket(zmq.SUB)
                sub.setsockopt(zmq.RCVTIMEO, 1000)
                sub.connect(f"tcp://{broker['host']}:{broker['ports']['video_sub_out']}")
                sub.setsockopt(zmq.SUBSCRIBE, f"video:{ROOM}:".encode("utf-8"))
                local_epoch = epoch

            try:
                topic, data = sub.recv_multipart()
                sender = topic.decode("utf-8", errors="ignore").split(":", 2)[2]
                if sender == USER_ID:
                    continue
                arr = np.frombuffer(data, np.uint8)
                frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                if frame is None:
                    continue
                with frames_lock:
                    REMOTE_FRAMES[sender] = (frame, time.time())
            except zmq.error.Again:
                pass
            except Exception:
                pass

        if sub is not None:
            sub.close(0)
    except Exception as exc:
        add_system_chat(f"Erro em video_recv_worker: {exc}")


def detect_audio_capabilities():
    global AUDIO_CAPTURE_ENABLED, AUDIO_PLAYBACK_ENABLED

    if pyaudio is None:
        AUDIO_CAPTURE_ENABLED = False
        AUDIO_PLAYBACK_ENABLED = False
        return

    pa = None
    try:
        pa = pyaudio.PyAudio()
        try:
            pa.get_default_input_device_info()
            AUDIO_CAPTURE_ENABLED = True
        except Exception:
            AUDIO_CAPTURE_ENABLED = False

        try:
            pa.get_default_output_device_info()
            AUDIO_PLAYBACK_ENABLED = True
        except Exception:
            AUDIO_PLAYBACK_ENABLED = False
    except Exception:
        AUDIO_CAPTURE_ENABLED = False
        AUDIO_PLAYBACK_ENABLED = False
    finally:
        if pa is not None:
            try:
                pa.terminate()
            except Exception:
                pass


def audio_capture_worker():
    if pyaudio is None or not AUDIO_CAPTURE_ENABLED:
        return

    try:
        pa = None
        stream = None

        try:
            pa = pyaudio.PyAudio()
            stream = pa.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=44100,
                input=True,
                frames_per_buffer=1024,
            )
        except Exception as exc:
            add_system_chat(f"Audio captura indisponivel: {exc}")
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
            if pa is not None:
                try:
                    pa.terminate()
                except Exception:
                    pass
            return

        while not stop_event.is_set():
            try:
                data = stream.read(1024, exception_on_overflow=False)
                safe_queue_put(AUDIO_RAW_Q, data)
            except Exception:
                time.sleep(0.02)

        try:
            stream.stop_stream()
            stream.close()
        except Exception:
            pass
        try:
            pa.terminate()
        except Exception:
            pass
    except Exception as exc:
        add_system_chat(f"Erro em audio_capture_worker: {exc}")


def audio_send_worker():
    if pyaudio is None or not AUDIO_CAPTURE_ENABLED:
        return

    try:
        pub = None
        local_epoch = -1

        while not stop_event.is_set():
            broker, epoch = get_current_broker()
            if not broker:
                time.sleep(0.2)
                continue

            if pub is None or local_epoch != epoch:
                if pub is not None:
                    pub.close(0)
                pub = ctx.socket(zmq.PUB)
                pub.setsockopt(zmq.SNDTIMEO, 200)
                pub.connect(f"tcp://{broker['host']}:{broker['ports']['audio_pub_in']}")
                local_epoch = epoch
                time.sleep(0.1)

            try:
                chunk = AUDIO_RAW_Q.get(timeout=0.3)
            except queue.Empty:
                continue

            topic = f"audio:{ROOM}:{USER_ID}".encode("utf-8")
            try:
                pub.send_multipart([topic, chunk])
            except Exception:
                pass

        if pub is not None:
            pub.close(0)
    except Exception as exc:
        add_system_chat(f"Erro em audio_send_worker: {exc}")


def audio_recv_worker():
    if pyaudio is None or not AUDIO_PLAYBACK_ENABLED:
        return

    try:
        pa = None
        out = None
        try:
            pa = pyaudio.PyAudio()
            out = pa.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=44100,
                output=True,
                frames_per_buffer=1024,
            )
        except Exception as exc:
            add_system_chat(f"Audio reproducao indisponivel: {exc}")
            if out is not None:
                try:
                    out.close()
                except Exception:
                    pass
            if pa is not None:
                try:
                    pa.terminate()
                except Exception:
                    pass
            return

        sub = None
        local_epoch = -1

        while not stop_event.is_set():
            broker, epoch = get_current_broker()
            if not broker:
                time.sleep(0.2)
                continue

            if sub is None or local_epoch != epoch:
                if sub is not None:
                    sub.close(0)
                sub = ctx.socket(zmq.SUB)
                sub.setsockopt(zmq.RCVTIMEO, 500)
                sub.connect(f"tcp://{broker['host']}:{broker['ports']['audio_sub_out']}")
                sub.setsockopt(zmq.SUBSCRIBE, f"audio:{ROOM}:".encode("utf-8"))
                local_epoch = epoch

            try:
                topic, data = sub.recv_multipart()
                sender = topic.decode("utf-8", errors="ignore").split(":", 2)[2]
                if sender == USER_ID:
                    continue
                out.write(data)
            except zmq.error.Again:
                pass
            except Exception:
                pass

        if sub is not None:
            sub.close(0)
        try:
            out.stop_stream()
            out.close()
        except Exception:
            pass
        try:
            pa.terminate()
        except Exception:
            pass
    except Exception as exc:
        add_system_chat(f"Erro em audio_recv_worker: {exc}")


def text_recv_worker():
    try:
        sub = None
        local_epoch = -1

        while not stop_event.is_set():
            broker, epoch = get_current_broker()
            if not broker:
                time.sleep(0.2)
                continue

            if sub is None or local_epoch != epoch:
                if sub is not None:
                    sub.close(0)
                sub = ctx.socket(zmq.SUB)
                sub.setsockopt(zmq.RCVTIMEO, 1000)
                sub.connect(f"tcp://{broker['host']}:{broker['ports']['text_sub_out']}")
                sub.setsockopt(zmq.SUBSCRIBE, f"texto:{ROOM}:".encode("utf-8"))
                local_epoch = epoch

            try:
                _, data = sub.recv_multipart()
                msg = json.loads(data.decode("utf-8"))
                text_id = msg.get("id")
                if not text_id:
                    continue
                with chat_lock:
                    if text_id in SEEN_TEXT_IDS:
                        continue
                    SEEN_TEXT_IDS.add(text_id)
                    CHAT.append(msg)
                    if len(CHAT) > 300:
                        CHAT.pop(0)
            except zmq.error.Again:
                pass
            except Exception:
                pass

        if sub is not None:
            sub.close(0)
    except Exception as exc:
        add_system_chat(f"Erro em text_recv_worker: {exc}")


def send_text_with_retry(text):
    text_id = str(uuid.uuid4())

    for _ in range(3):
        broker, _ = get_current_broker()
        if not broker:
            time.sleep(0.2)
            continue
        try:
            resp = control_request(
                broker,
                {
                    "action": "send_text",
                    "user_id": USER_ID,
                    "room": ROOM,
                    "text": text,
                    "text_id": text_id,
                },
                timeout_ms=1000,
            )
            if resp.get("ok"):
                with chat_lock:
                    if text_id not in SEEN_TEXT_IDS:
                        SEEN_TEXT_IDS.add(text_id)
                        CHAT.append(
                            {
                                "id": text_id,
                                "de": USER_ID,
                                "msg": text,
                                "room": ROOM,
                                "ts": time.time(),
                            }
                        )
                return True
        except Exception:
            pass
        time.sleep(0.3)

    add_system_chat("Falha ao enviar texto apos 3 tentativas")
    return False


def add_system_chat(msg):
    with chat_lock:
        CHAT.append({"id": str(uuid.uuid4()), "de": "[sistema]", "msg": msg, "ts": time.time()})
        if len(CHAT) > 300:
            CHAT.pop(0)


def join_room(new_room):
    global ROOM
    new_room = (new_room or "").strip().lower()
    if not new_room:
        return

    broker, _ = get_current_broker()
    if not broker:
        add_system_chat("Sem broker ativo para trocar de sala")
        return

    try:
        resp = control_request(
            broker,
            {"action": "join_room", "user_id": USER_ID, "room": new_room},
            timeout_ms=1200,
        )
        if resp.get("ok"):
            ROOM = new_room
            set_current_broker(dict(broker))
            with frames_lock:
                REMOTE_FRAMES.clear()
            add_system_chat(f"Entrou na sala '{new_room}'")
        else:
            add_system_chat(f"Erro ao entrar na sala: {resp.get('error')}")
    except Exception as exc:
        add_system_chat(f"Erro ao trocar sala: {exc}")


def fetch_presence():
    broker, _ = get_current_broker()
    if not broker:
        add_system_chat("Sem broker ativo")
        return

    try:
        resp = control_request(broker, {"action": "presence"}, timeout_ms=1200)
        if not resp.get("ok"):
            add_system_chat(f"Falha em presence: {resp.get('error')}")
            return

        local_users = resp.get("online_local", [])
        rooms_local = resp.get("rooms_local", {})
        add_system_chat(f"Online local: {', '.join(local_users) if local_users else '(vazio)'}")
        for room_name, users in rooms_local.items():
            add_system_chat(f"Sala {room_name}: {', '.join(users) if users else '(vazio)'}")
    except Exception as exc:
        add_system_chat(f"Erro ao consultar presence: {exc}")


def make_grid_frame(local_frame, remote_frames):
    base_h, base_w = 240, 320

    local = cv2.resize(local_frame, (base_w, base_h))
    tiles = [local]

    now = time.time()
    for _, (frame, ts) in sorted(remote_frames.items())[:3]:
        if now - ts > 10:
            continue
        tiles.append(cv2.resize(frame, (base_w, base_h)))

    while len(tiles) < 4:
        tiles.append(np.zeros((base_h, base_w, 3), dtype=np.uint8))

    top = np.hstack([tiles[0], tiles[1]])
    bottom = np.hstack([tiles[2], tiles[3]])
    return np.vstack([top, bottom])


def draw_chat(chat_box):
    with chat_lock:
        history = list(CHAT)[-30:]

    chat_box.config(state="normal")
    chat_box.delete("1.0", tk.END)
    for msg in history:
        sender = msg.get("de", "?")
        body = msg.get("msg", "")
        chat_box.insert(tk.END, f"{sender}: {body}\n")
    chat_box.config(state="disabled")
    chat_box.yview(tk.END)


def build_ui():
    try:
        root = tk.Tk()
        root.title(f"VideoConf Distribuido - {USER_ID} @ {ROOM}")
        root.geometry("1080x650")
        root.configure(bg="#141922")

        left = tk.Frame(root, bg="#141922")
        left.pack(side="left", fill="both", expand=True, padx=10, pady=10)

        right = tk.Frame(root, bg="#1e2633")
        right.pack(side="right", fill="y", padx=10, pady=10)

        title = tk.Label(
            left,
            text="VideoConf Distribuido",
            font=("Segoe UI", 16, "bold"),
            fg="#dbe9ff",
            bg="#141922",
        )
        title.pack(anchor="w", pady=(0, 8))

        video_label = tk.Label(left, bg="#0f1117")
        video_label.pack(fill="both", expand=True)

        chat_box = scrolledtext.ScrolledText(
            right,
            width=42,
            height=28,
            wrap=tk.WORD,
            bg="#0f1117",
            fg="#e8edf7",
            insertbackground="#e8edf7",
        )
        chat_box.pack(padx=8, pady=8)
        chat_box.config(state="disabled")

        entry = tk.Entry(right, bg="#0f1117", fg="#e8edf7", insertbackground="#e8edf7")
        entry.pack(fill="x", padx=8, pady=(0, 8))

        help_label = tk.Label(
            right,
            text="Comandos: /join salaA, /who",
            bg="#1e2633",
            fg="#a7b6cc",
            font=("Segoe UI", 9),
        )
        help_label.pack(anchor="w", padx=8)

        def on_send():
            text = entry.get().strip()
            if not text:
                return
            entry.delete(0, tk.END)

            if text.startswith("/join "):
                join_room(text[6:].strip())
                return
            if text == "/who":
                fetch_presence()
                return

            send_text_with_retry(text)

        send_btn = tk.Button(
            right,
            text="Enviar",
            command=on_send,
            bg="#3f8cff",
            fg="white",
            relief="flat",
        )
        send_btn.pack(padx=8, pady=(0, 8), fill="x")

        def ui_loop():
            frame = None
            try:
                frame = VIDEO_RAW_Q.get_nowait()
            except queue.Empty:
                frame = np.zeros((240, 320, 3), dtype=np.uint8)

            with frames_lock:
                remotes = dict(REMOTE_FRAMES)

            tiled = make_grid_frame(frame, remotes)
            tiled = cv2.cvtColor(tiled, cv2.COLOR_BGR2RGB)

            if Image is not None and ImageTk is not None:
                image = Image.fromarray(tiled)
                photo = ImageTk.PhotoImage(image=image)
                video_label.imgtk = photo
                video_label.configure(image=photo)
            else:
                video_label.configure(text="Instale pillow para renderizar video", fg="white")

            draw_chat(chat_box)
            root.after(50, ui_loop)

        root.protocol("WM_DELETE_WINDOW", lambda: (stop_event.set(), root.destroy()))
        ui_loop()
        root.mainloop()
    except Exception as exc:
        print(f"Erro em build_ui: {exc}")
        stop_event.set()


class LoginUI:
    def run(self):
        root = tk.Tk()
        root.title("VideoConf - Login")
        root.geometry("380x280")
        root.configure(bg="#141922")

        tk.Label(
            root,
            text="VideoConf Distribuido",
            fg="#dbe9ff",
            bg="#141922",
            font=("Segoe UI", 16, "bold"),
        ).pack(pady=18)

        user_entry = tk.Entry(root, font=("Segoe UI", 12))
        user_entry.pack(pady=8, ipadx=8, ipady=6)
        user_entry.insert(0, f"user-{uuid.uuid4().hex[:4]}")

        room_entry = tk.Entry(root, font=("Segoe UI", 12))
        room_entry.pack(pady=8, ipadx=8, ipady=6)
        room_entry.insert(0, "geral")

        strategy_var = tk.StringVar(value="lowest-latency")
        tk.Radiobutton(
            root,
            text="Menor latencia",
            variable=strategy_var,
            value="lowest-latency",
            bg="#141922",
            fg="#dbe9ff",
            selectcolor="#141922",
        ).pack(anchor="w", padx=70)
        tk.Radiobutton(
            root,
            text="Round-robin",
            variable=strategy_var,
            value="round-robin",
            bg="#141922",
            fg="#dbe9ff",
            selectcolor="#141922",
        ).pack(anchor="w", padx=70)

        output = {}

        def enter():
            output["user"] = user_entry.get().strip()
            output["room"] = room_entry.get().strip().lower()
            output["strategy"] = strategy_var.get()
            root.destroy()

        tk.Button(root, text="Entrar", command=enter, bg="#3f8cff", fg="white").pack(
            pady=18
        )

        root.mainloop()
        return output.get("user", ""), output.get("room", "geral"), output.get(
            "strategy", "lowest-latency"
        )


def start_workers(discovery, strategy):
    detect_audio_capabilities()

    workers = [
        video_capture_worker,
        video_send_worker,
        video_recv_worker,
        text_recv_worker,
        lambda: heartbeat_monitor(discovery, strategy),
    ]

    if pyaudio is None:
        add_system_chat("PyAudio nao instalado: audio desativado")
    else:
        if AUDIO_CAPTURE_ENABLED:
            workers.extend([audio_capture_worker, audio_send_worker])
        else:
            add_system_chat("Sem dispositivo de entrada de audio: captura desativada")
        if AUDIO_PLAYBACK_ENABLED:
            workers.append(audio_recv_worker)
        else:
            add_system_chat("Sem dispositivo de saida de audio: reproducao desativada")

    for fn in workers:
        threading.Thread(target=fn, daemon=True).start()


def main():
    global USER_ID, ROOM

    print("=" * 70)
    print("  VIDEOCONF DISTRIBUIDO - CLIENTE")
    print("=" * 70)

    login = LoginUI()
    USER_ID, ROOM, strategy = login.run()
    if not USER_ID:
        print("Usuario invalido")
        return

    discovery = BrokerDiscovery()

    print("[Init] aguardando discovery de brokers...")
    for _ in range(50):
        if discovery.list_brokers():
            break
        time.sleep(0.1)

    if not discovery.list_brokers():
        print("Nenhum broker encontrado. Inicie ao menos um broker.")
        return

    connected = False
    deadline = time.time() + 12
    while time.time() < deadline and not connected:
        connected = connect_to_best_broker(discovery, strategy=strategy)
        if connected:
            break
        print("[Init] login em broker ainda nao concluido, tentando novamente...")
        time.sleep(1)

    if not connected:
        print("Nao foi possivel conectar/login em um broker")
        return

    start_workers(discovery, strategy)
    add_system_chat(f"Conectado como {USER_ID} na sala {ROOM}")

    build_ui()

    stop_event.set()

    broker, _ = get_current_broker()
    if broker:
        try:
            control_request(
                broker,
                {"action": "leave", "user_id": USER_ID},
                timeout_ms=800,
            )
        except Exception:
            pass


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        stop_event.set()
