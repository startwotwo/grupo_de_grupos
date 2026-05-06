import zmq
import cv2 as cv
import numpy as np
import sounddevice as sd
import threading
import sys
import time
import uuid
import os
import json
import random
import argparse
from queue import Queue, Full, Empty
from common import ROOMS, PRESENCE_INTERVAL, DISCOVERY_PORT, CMD_LIST, channel_ports, parse_topic

parser = argparse.ArgumentParser()
parser.add_argument("--id", default=uuid.uuid4().hex[:6])
parser.add_argument("--room", default="A", choices=ROOMS)
parser.add_argument("--discovery", default=f"tcp://localhost:{DISCOVERY_PORT}", help="endereço do discovery (ex: tcp://192.168.0.155:5570)")
args = parser.parse_args()

member_id = args.id
my_room = args.room
discovery_addr = args.discovery

online_members = set()       # quem está na sala
online_lock = threading.Lock()

muted = False
stop_event = threading.Event()

# print("="*60)
# print(f"[Member {member_id}] Audio devices available:")
# print(sd.query_devices())
# print(f"[Member {member_id}] Default input/output device: {sd.default.device}")
# if output_device is not None:
#     print(f"[Member {member_id}] Forcing output device: {output_device}")
# print("="*60)

def fetch_broker(discovery_addr, ctx):
    """Asks discovery which brokers are alive and chooses one."""
    sock = ctx.socket(zmq.REQ)
    sock.setsockopt(zmq.LINGER, 0)
    sock.setsockopt(zmq.RCVTIMEO, 3000)  # 3s timeout
    sock.connect(discovery_addr)
    try:
        sock.send_string(json.dumps({"cmd": CMD_LIST}))
        resp = json.loads(sock.recv_string())
    except zmq.Again:
        raise RuntimeError(f"discovery {discovery_addr} did not respond")
    finally:
        sock.close(linger=0)

    brokers = resp.get("brokers", [])
    if not brokers:
        raise RuntimeError("no broker available in discovery")

    # estratégia simples: aleatório (~ load balance)
    chosen = random.choice(brokers)
    print(f"[client] discovery returned {len(brokers)} broker(s); "
          f"chosen: {chosen['id']} @ {chosen['host']}:{chosen['base_port']}")
    return chosen

class Conn:
    """Container thread-safe dos sockets atuais do cliente.
    
    A cada swap (failover), recria um zmq.Context inteiro novo —
    isso emula o comportamento de reiniciar o cliente do zero,
    eliminando estado interno do ZMQ que pode ficar inconsistente
    após múltiplas migrações.
    """

    def __init__(self, ctx, member_id, room):
        self.ctx = ctx                  # context inicial (do main)
        self.member_id = member_id
        self.room = room
        self.broker_id = None
        self.host = None
        self._lock = threading.Lock()
        self.video_pub = self.video_sub = None
        self.audio_pub = self.audio_sub = None
        self.text_pub  = self.text_sub  = None
        self.presence_pub = self.presence_sub = None

    def _build_with_ctx(self, ctx, broker):
        """Cria 8 sockets novos no context fornecido."""
        host = broker["host"]
        ports = channel_ports(broker["base_port"])

        v_pub = ctx.socket(zmq.PUB)
        v_pub.connect(f"tcp://{host}:{ports['video'][0]}")
        v_sub = ctx.socket(zmq.SUB)
        v_sub.connect(f"tcp://{host}:{ports['video'][1]}")
        v_sub.setsockopt_string(zmq.SUBSCRIBE, "video:")

        a_pub = ctx.socket(zmq.PUB)
        a_pub.connect(f"tcp://{host}:{ports['audio'][0]}")
        a_sub = ctx.socket(zmq.SUB)
        a_sub.connect(f"tcp://{host}:{ports['audio'][1]}")
        a_sub.setsockopt_string(zmq.SUBSCRIBE, "audio:")

        t_pub = ctx.socket(zmq.PUB)
        t_pub.connect(f"tcp://{host}:{ports['text'][0]}")
        t_sub = ctx.socket(zmq.SUB)
        t_sub.connect(f"tcp://{host}:{ports['text'][1]}")
        t_sub.setsockopt_string(zmq.SUBSCRIBE, "text:")

        p_pub = ctx.socket(zmq.PUB)
        p_pub.connect(f"tcp://{host}:{ports['presence'][0]}")
        p_sub = ctx.socket(zmq.SUB)
        p_sub.connect(f"tcp://{host}:{ports['presence'][1]}")
        p_sub.setsockopt_string(zmq.SUBSCRIBE, "presence:")

        # espera generosa para subscriptions propagarem
        time.sleep(0.5)
        return (v_pub, v_sub, a_pub, a_sub, t_pub, t_sub,
                p_pub, p_sub, host, broker["id"])

    def swap(self, broker):
        # No primeiro swap (inicialização), reusa o ctx do main.
        # Em swaps seguintes (failover real), cria um ctx novo —
        # isso simula um restart do cliente do zero.
        is_first_swap = (self.broker_id is None)
        if is_first_swap:
            new_ctx = self.ctx
            old_ctx_to_term = None
        else:
            new_ctx = zmq.Context()
            old_ctx_to_term = self.ctx

        new = self._build_with_ctx(new_ctx, broker)

        with self._lock:
            old_sockets = (self.video_pub, self.video_sub,
                           self.audio_pub, self.audio_sub,
                           self.text_pub,  self.text_sub,
                           self.presence_pub, self.presence_sub)
            self.ctx = new_ctx
            (self.video_pub, self.video_sub,
             self.audio_pub, self.audio_sub,
             self.text_pub,  self.text_sub,
             self.presence_pub, self.presence_sub,
             self.host, self.broker_id) = new

        # fecha sockets antigos
        for s in old_sockets:
            if s is not None:
                try: s.close(linger=0)
                except Exception: pass

        # destrói o context antigo — libera todo estado interno do ZMQ.
        # destroy(linger=0) é mais agressivo que term() e não bloqueia.
        if old_ctx_to_term is not None:
            try:
                old_ctx_to_term.destroy(linger=0)
            except Exception:
                pass

        msg = f"[client] conectado em {self.broker_id} @ {self.host} (sala {self.room})"
        if not is_first_swap:
            msg += " — context recriado"
        print(msg)

        # limpa estado de presença — vai ser repopulado pelos JOINs/WHOIS
        with online_lock:
            online_members.clear()

        time.sleep(0.3)

        # anuncia presença + pede que todos se anunciem (rajada)
        join_topic = f"presence:{self.broker_id}:{self.room}:{self.member_id}".encode()
        for _ in range(3):
            try:
                self.presence_pub.send_multipart([join_topic, b"JOIN"])
                self.presence_pub.send_multipart([join_topic, b"WHOIS"])
                time.sleep(0.15)
            except Exception:
                pass

discovery_addr = args.discovery
watchdog_ctx = zmq.Context()
member_ctx = zmq.Context()

conn = Conn(member_ctx, member_id, my_room)
broker = fetch_broker(discovery_addr, watchdog_ctx)
conn.swap(broker)

video_render_queue = Queue(200)
audio_render_queue = Queue(100)
text_render_queue = Queue(100)

topics = ["video", "audio", "text"]

def watchdog(conn, discovery_addr, stop_event):
    """A cada 2s, verifica se o broker atual ainda está vivo no discovery.
    Se sumiu, escolhe outro e troca."""
    sock = watchdog_ctx.socket(zmq.REQ)
    sock.setsockopt(zmq.LINGER, 0)
    sock.setsockopt(zmq.RCVTIMEO, 2000)
    sock.connect(discovery_addr)

    def list_brokers():
        nonlocal sock
        try:
            sock.send_string(json.dumps({"cmd": CMD_LIST}))
            return json.loads(sock.recv_string()).get("brokers", [])
        except zmq.Again:
            # recria o REQ após timeout
            sock.close(linger=0)
            sock = watchdog_ctx.socket(zmq.REQ)
            sock.setsockopt(zmq.LINGER, 0)
            sock.setsockopt(zmq.RCVTIMEO, 2000)
            sock.connect(discovery_addr)
            return None

    while not stop_event.wait(2.0):
        brokers = list_brokers()
        if brokers is None:
            continue  # discovery indisponível, tenta de novo
        alive_ids = {b["id"] for b in brokers}
        if conn.broker_id in alive_ids:
            continue  # tudo certo
        # broker atual morreu
        if not brokers:
            print("[watchdog] nenhum broker vivo, aguardando...")
            continue
        new_broker = random.choice(brokers)
        print(f"[watchdog] broker '{conn.broker_id}' caiu — migrando para '{new_broker['id']}'")
        try:
            conn.swap(new_broker)
            with online_lock:
                online_members.clear()
        except Exception as e:
            print(f"[watchdog] falha ao migrar: {e}")

def main_thread():
    global muted
    while True:
        try:
            frame = video_render_queue.get_nowait()
            cv.imshow(f"Member {member_id} - Video", frame)
        except Empty:
            pass
        try:
            msg = text_render_queue.get_nowait()
            sender, content = msg.split(":", 1)
            print(f"[Member {sender}] Sent: {content}")
        except Empty:
            pass
        key = cv.waitKey(1) & 0xFF
        if key == ord('b'):
            break
        elif key == ord('m'):
            muted = not muted
            print(f"[Member {member_id}] Muted: {muted}")

def receive_video():
    while True:
        try:
            msg = conn.video_sub.recv_multipart()
        except zmq.ZMQError:
            time.sleep(0.1)
            continue
        if len(msg) < 2:
            continue
        topic = msg[0].decode('utf-8', errors='ignore')
        parsed = parse_topic(topic)
        if not parsed:
            continue
        _, _, room, sender_id = parsed
        if room != conn.room:
            continue
        if sender_id == member_id:
            continue
        frame = cv.imdecode(np.frombuffer(msg[1], dtype=np.uint8), cv.IMREAD_COLOR)
        if frame is None:
            continue
        try:
            video_render_queue.put_nowait(frame)
        except Full:
            try:
                video_render_queue.get_nowait()
            except Empty:
                pass
            video_render_queue.put_nowait(frame)


def receive_audio():
    while True:
        try:
            msg = conn.audio_sub.recv_multipart()
        except zmq.ZMQError:
            time.sleep(0.1)
            continue
        if len(msg) < 2:
            continue
        topic = msg[0].decode('utf-8', errors='ignore')
        parsed = parse_topic(topic)
        if not parsed:
            continue
        _, _, room, sender_id = parsed
        if room != conn.room:
            continue
        if sender_id == member_id:
            continue
        try:
            audio_render_queue.put_nowait(msg[1])
        except Full:
            try:
                audio_render_queue.get_nowait()
            except Empty:
                pass
            audio_render_queue.put_nowait(msg[1])


def receive_text():
    while True:
        try:
            msg = conn.text_sub.recv_multipart()
        except zmq.ZMQError:
            time.sleep(0.1)
            continue
        if len(msg) < 2:
            continue
        topic = msg[0].decode('utf-8', errors='ignore')
        # print(f"[debug] recebi do socket: topic={topic}") --- debug
        parsed = parse_topic(topic)
        if not parsed:
            continue
        _, _, room, sender_id = parsed
        if room != conn.room:
            # print(f"[debug]   descartado: sala {room} != {conn.room}") --- debug
            continue
        if sender_id == member_id:
            continue
        try:
            text_render_queue.put_nowait(msg[1].decode('utf-8'))
        except Full:
            try:
                text_render_queue.get_nowait()
            except Empty:
                pass
            text_render_queue.put_nowait(msg[1].decode('utf-8'))

def send_video():
    cap = cv.VideoCapture(0)
    while True:
        ret, frame = cap.read()
        if not ret: break
        _, buffer = cv.imencode('.jpg', frame)
        conn.video_pub.send_multipart([f"video:{conn.broker_id}:{conn.room}:{member_id}".encode(), buffer.tobytes()])
        if cv.waitKey(1) & 0xFF == ord('b'): break
    cap.release()

def send_audio():
    try:
        with sd.RawInputStream(samplerate=16000, channels=1, dtype='int16', blocksize=1024) as stream:
            while True:
                data, _ = stream.read(1024)
                conn.audio_pub.send_multipart([f"audio:{conn.broker_id}:{conn.room}:{member_id}".encode(), bytes(data)])
    except sd.PortAudioError:
        print(f"[Member {member_id}] No audio device found, skipping audio capture.")

def play_audio():
    try:
        with sd.RawOutputStream(samplerate=16000, channels=1, dtype='int16', blocksize=1024) as stream:
            while True:
                try:
                    chunk = audio_render_queue.get(timeout=0.1)
                    if muted:
                        continue
                    audio_data = np.frombuffer(chunk, dtype=np.int16)
                    stream.write(audio_data)
                except Empty:
                    pass
    except sd.PortAudioError as e:
        print(f"[Member {member_id}] Audio output error: {e}")

def send_text():
    while True:
        text = input(f"[{member_id}@{conn.room}] > ")
        if text.lower() == 'exit':
            # avisa que estou saindo
            try:
                topic = f"presence:{conn.broker_id}:{conn.room}:{member_id}".encode()
                conn.presence_pub.send_multipart([topic, b"LEAVE"])
                time.sleep(0.2)  # dá tempo de propagar
            except Exception:
                pass
            os._exit(0)
        if text == '/who':
            with online_lock:
                others = sorted(online_members)
            print(f"[sala {conn.room}] online: {[member_id] + others}")
            continue
        if text.startswith('/room '):
            new_room = text.split()[1].upper()
            if new_room not in ROOMS:
                print(f"sala inválida. válidas: {ROOMS}")
                continue
            # avisa LEAVE da sala antiga
            old_topic = f"presence:{conn.broker_id}:{conn.room}:{member_id}".encode()
            try:
                conn.presence_pub.send_multipart([old_topic, b"LEAVE"])
            except Exception:
                pass
            with online_lock:
                online_members.clear()
            conn.room = new_room
            print(f"[client] mudou para sala {new_room}")
            continue
        topic = f"text:{conn.broker_id}:{conn.room}:{member_id}".encode()
        payload = f"{member_id}:{text}".encode()
        try:
            conn.text_pub.send_multipart([topic, payload], flags=zmq.NOBLOCK)
            # print(f"[debug] enviei via {conn.broker_id}, topic={topic}") --- debug
        except (zmq.Again, zmq.ZMQError) as e:
            print(f"[debug] FALHA ao enviar: {e}") 

def announce_presence():
    """Publica JOIN periodicamente. Quem entra depois descobre quem já está aqui."""
    while True:
        topic = f"presence:{conn.broker_id}:{conn.room}:{member_id}".encode()
        payload = b"JOIN"
        try:
            conn.presence_pub.send_multipart([topic, payload], flags=zmq.NOBLOCK)
        except (zmq.Again, zmq.ZMQError):
            pass
        time.sleep(PRESENCE_INTERVAL)

def receive_presence():
    last_seen = {}
    while True:
        try:
            msg = conn.presence_sub.recv_multipart()
        except zmq.ZMQError:
            time.sleep(0.1)
            continue
        topic = msg[0].decode()
        parsed = parse_topic(topic)
        if not parsed:
            continue
        _, _, room, sender_id = parsed
        if room != conn.room or sender_id == member_id:
            continue
        action = msg[1].decode()

        with online_lock:
            now = time.time()
            if action == "JOIN":
                if sender_id not in online_members:
                    online_members.add(sender_id)
                    print(f"[sala {conn.room}] {sender_id} entrou")
                last_seen[sender_id] = now
            elif action == "LEAVE":
                if sender_id in online_members:
                    online_members.discard(sender_id)
                    last_seen.pop(sender_id, None)
                    print(f"[sala {conn.room}] {sender_id} saiu")
            elif action == "WHOIS":
                # alguém pediu para todos se anunciarem — respondo
                topic = f"presence:{conn.broker_id}:{conn.room}:{member_id}".encode()
                try:
                    conn.presence_pub.send_multipart([topic, b"JOIN"])
                except Exception:
                    pass

            timeout = PRESENCE_INTERVAL * 4
            stale = [u for u, t in last_seen.items() if now - t > timeout]
            for u in stale:
                online_members.discard(u)
                last_seen.pop(u, None)
                print(f"[sala {conn.room}] {u} saiu (timeout)")

threading.Thread(target=receive_video, daemon=True).start()
threading.Thread(target=receive_audio, daemon=True).start()
threading.Thread(target=receive_text, daemon=True).start()
threading.Thread(target=send_video, daemon=True).start()
threading.Thread(target=send_audio, daemon=True).start()
threading.Thread(target=play_audio, daemon=True).start()
threading.Thread(target=send_text, daemon=True).start()
threading.Thread(target=announce_presence, daemon=True).start()
threading.Thread(target=receive_presence, daemon=True).start()
threading.Thread(target=watchdog, args=(conn, discovery_addr, stop_event), daemon=True).start()

main_thread()