import argparse
import threading
import time
import zmq
import json

from client import (
    BrokerDiscovery, connect_to_best_broker,
    send_text_with_retry, CHAT, chat_lock,
    stop_event, heartbeat_monitor,
)
import client as _c

parser = argparse.ArgumentParser()
parser.add_argument("--user", required=True)
parser.add_argument("--room", default="geral")
args = parser.parse_args()

_c.USER_ID = args.user
_c.ROOM = args.room.lower()  # usa a sala passada pelo usuário

discovery = BrokerDiscovery()

print("[Init] aguardando broker...")
for _ in range(50):
    if discovery.list_brokers():
        break
    time.sleep(0.1)

if not discovery.list_brokers():
    print("Nenhum broker encontrado.")
    raise SystemExit(1)

connected = False
deadline = time.time() + 12
while time.time() < deadline and not connected:
    connected = connect_to_best_broker(discovery)
    time.sleep(1)

if not connected:
    print("Não foi possível conectar.")
    raise SystemExit(1)

broker, _ = _c.get_current_broker()
print(f"Conectado como {_c.USER_ID} na sala {_c.ROOM}")
# print(f"[debug] text_sub_out={broker['ports']['text_sub_out']} host={broker['host']}")


def text_recv():
    sub = _c.ctx.socket(zmq.SUB)
    sub.setsockopt(zmq.RCVTIMEO, 1000)
    host = broker['host']
    port = broker['ports']['text_sub_out']
    sub.connect(f"tcp://{host}:{port}")
    sub.setsockopt(zmq.SUBSCRIBE, f"texto:{_c.ROOM}:".encode("utf-8"))
    # print(f"[debug] assinando texto:{_c.ROOM}: em tcp://{host}:{port}")

    while not stop_event.is_set():
        try:
            frames = sub.recv_multipart()
            # print(f"[debug] recebeu: {[f[:80] for f in frames]}")
            if len(frames) < 2:
                continue
            try:
                msg = json.loads(frames[1].decode("utf-8"))
            except Exception:
                continue
            sender = msg.get("de", "?")
            body = msg.get("msg", "")
            if sender != _c.USER_ID:
                print(f"\n[{sender}]: {body}")
                print("> ", end="", flush=True)
        except zmq.error.Again:
            pass
        except Exception as e:
            print(f"[debug] erro: {e}")


threading.Thread(target=text_recv, daemon=True).start()
threading.Thread(target=lambda: heartbeat_monitor(discovery, "lowest-latency"), daemon=True).start()

try:
    while not stop_event.is_set():
        text = input("> ")
        if text.strip():
            send_text_with_retry(text.strip())
except KeyboardInterrupt:
    stop_event.set()
