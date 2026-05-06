import cv2
import zmq
import numpy as np
import time
import queue
import threading
import uuid
from dataclasses import dataclass
import sys
import math
import platform

VIDEO_FPS = 15
MAX_FRAME_QUEUE = 10
REMOTE_FRAME_TIMEOUT = 5.0
WINDOW_NAME = "Video-Conferencia"


@dataclass
class ClientConfig:
    user_id: str
    room: str
    broker_host: str = "localhost"
    video_pub_port: int = 5555
    video_sub_port: int = 5556
    camera_index: int = 0


class VideoClient:
    def __init__(self, config: ClientConfig):
        self.config = config
        self.context = zmq.Context()
        self.running = False

        self.video_pub = self.context.socket(zmq.PUB)
        self.video_pub.connect(
            f"tcp://{config.broker_host}:{config.video_pub_port}"
        )

        self.video_sub = self.context.socket(zmq.SUB)
        self.video_sub.connect(
            f"tcp://{config.broker_host}:{config.video_sub_port}"
        )
        self.video_sub.setsockopt_string(zmq.SUBSCRIBE, config.room)

        self.frame_queue = queue.Queue(maxsize=MAX_FRAME_QUEUE)
        self.remote_frames = {}
        self.remote_last_seen = {}
        self.local_frame = None
        self.lock = threading.Lock()
        self.threads = []

    def login(self):
        print(f"[LOGIN] Usuário {self.config.user_id} conectado")
        print(f"[ROOM] Entrando na sala {self.config.room}")
        print(f"[BROKER] {self.config.broker_host}:{self.config.video_pub_port}/{self.config.video_sub_port}")
        print(f"[CAMERA] índice {self.config.camera_index}")
        print("[INFO] Pressione 'q' para sair")

    def start(self):
        self.running = True
        # self.login()

        # Dá tempo para o SUB assinar antes do fluxo de vídeo começar.
        time.sleep(1.5)

        self.threads = [
            threading.Thread(target=self.capture_loop, name="capture", daemon=True),
            threading.Thread(target=self.send_loop, name="send", daemon=True),
            threading.Thread(target=self.receive_loop, name="recv", daemon=True),
            threading.Thread(target=self.render_loop, name="render", daemon=True),
            threading.Thread(target=self.cleanup_loop, name="cleanup", daemon=True),
        ]

        for thread in self.threads:
            thread.start()

    def stop(self):
        if not self.running:
            return

        self.running = False
        print("[STOP] Encerrando cliente de vídeo")

        try:
            cv2.destroyAllWindows()
        except Exception:
            pass

        try:
            self.video_pub.close(0)
        except Exception:
            pass

        try:
            self.video_sub.close(0)
        except Exception:
            pass

        try:
            self.context.term()
        except Exception:
            pass

    def _open_camera(self):
        system = platform.system()
        if system == "Linux":
            cap = cv2.VideoCapture(self.config.camera_index, cv2.CAP_V4L2)
            if cap.isOpened():
                return cap

        return cv2.VideoCapture(self.config.camera_index)

    def capture_loop(self):
        cap = self._open_camera()
        if not cap.isOpened():
            print(f"[ERRO] Não foi possível abrir a câmera no índice {self.config.camera_index}")
            self.stop()
            return

        cap.set(cv2.CAP_PROP_FPS, VIDEO_FPS)

        while self.running:
            ok, frame = cap.read()
            if not ok or frame is None:
                time.sleep(0.02)
                continue

            frame = cv2.resize(frame, (320, 240))

            with self.lock:
                self.local_frame = frame.copy()

            if self.frame_queue.full():
                try:
                    self.frame_queue.get_nowait()
                except queue.Empty:
                    pass

            self.frame_queue.put(frame)
            time.sleep(1 / VIDEO_FPS)

        cap.release()

    def send_loop(self):
        encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), 60]
        time.sleep(1)

        while self.running:
            try:
                frame = self.frame_queue.get(timeout=0.1)
            except queue.Empty:
                continue

            success, buffer = cv2.imencode(".jpg", frame, encode_param)
            if not success:
                continue

            payload = buffer.tobytes()
            msg_id = str(uuid.uuid4()).encode()
            timestamp = str(time.time()).encode()

            try:
                self.video_pub.send_multipart([
                    self.config.room.encode(),
                    self.config.user_id.encode(),
                    msg_id,
                    timestamp,
                    payload,
                ])

            except zmq.ZMQError:
                if self.running:
                    print("[ERRO] Falha ao enviar frame ao broker")
                break

    def receive_loop(self):
        while self.running:
            try:
                parts = self.video_sub.recv_multipart(flags=zmq.NOBLOCK)
                # Informação de vídeo
                if len(parts) != 5:
                        continue
                topic, sender, msg_id, timestamp, payload = parts
            except zmq.Again:
                time.sleep(0.01)
                continue
            except zmq.ZMQError:
                if self.running:
                    print("[ERRO] Falha ao receber frame do broker")
                break

            sender_name = sender.decode(errors="ignore")
            if sender_name == self.config.user_id:
                continue
            np_buffer = np.frombuffer(payload, dtype=np.uint8)
            frame = cv2.imdecode(np_buffer, cv2.IMREAD_COLOR)

            if frame is None:
                continue

            with self.lock:
                self.remote_frames[sender_name] = frame
                self.remote_last_seen[sender_name] = time.time()

    def cleanup_loop(self):
        while self.running:
            now = time.time()
            with self.lock:
                expired = [
                    sender for sender, last_seen in self.remote_last_seen.items()
                    if now - last_seen > REMOTE_FRAME_TIMEOUT
                ]
                for sender in expired:
                    self.remote_last_seen.pop(sender, None)
                    self.remote_frames.pop(sender, None)
            time.sleep(1.0)

    def _build_tile(self, frame, label):
        tile = cv2.resize(frame, (320, 240))
        cv2.rectangle(tile, (0, 0), (319, 35), (0, 0, 0), -1)
        cv2.putText(
            tile,
            label,
            (10, 24),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (0, 255, 0),
            2,
            cv2.LINE_AA,
        )
        return tile

    def _placeholder_tile(self, text):
        tile = np.zeros((240, 320, 3), dtype=np.uint8)
        cv2.putText(
            tile,
            text,
            (20, 120),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.8,
            (255, 255, 255),
            2,
            cv2.LINE_AA,
        )
        return tile

    def _compose_grid(self, tiles):
        import math

        count = len(tiles)

        if count == 0:
            return np.zeros((240, 320, 3), dtype=np.uint8)

        cols = math.ceil(math.sqrt(count))
        rows = math.ceil(count / cols)

        rows_imgs = []
        idx = 0

        for r in range(rows):
            row = []
            for c in range(cols):
                if idx < count:
                    row.append(tiles[idx])
                else:
                    row.append(np.zeros_like(tiles[0]))
                idx += 1
            rows_imgs.append(np.hstack(row))

        return np.vstack(rows_imgs)

    def render_loop(self):
        while self.running:
            tiles = []

            with self.lock:
                local_frame = None if self.local_frame is None else self.local_frame.copy()
                remote_items = [
                    (sender, frame.copy())
                    for sender, frame in self.remote_frames.items()
                ]

            if local_frame is not None:
                tiles.append(self._build_tile(local_frame, f"{self.config.user_id}"))
            else:
                tiles.append(self._placeholder_tile("Aguardando camera..."))

            for sender, frame in sorted(remote_items, key=lambda item: item[0]):
                tiles.append(self._build_tile(frame, sender))

            canvas = self._compose_grid(tiles)
            cv2.imshow(WINDOW_NAME, canvas)

            key = cv2.waitKey(1) & 0xFF
            if key == ord("q"):
                self.stop()
                break

        cv2.destroyAllWindows()


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("Uso: python client_video.py <user_id> <room> <broker_host> [camera_index]")
        sys.exit(1)

    user_id = sys.argv[1]
    room = sys.argv[2]
    broker_host = sys.argv[3]
    camera_index = int(sys.argv[4]) if len(sys.argv) > 4 else 0

    config = ClientConfig(
        user_id=user_id,
        room=room,
        broker_host=broker_host,
        camera_index=camera_index,
    )

    client = VideoClient(config)

    try:
        client.start()
        while client.running:
            time.sleep(0.2)
    except KeyboardInterrupt:
        client.stop()
