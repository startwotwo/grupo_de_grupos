import argparse
import threading
import time
import zmq
import json

from client import (
    BrokerDiscovery, connect_to_best_broker,
    send_text_with_retry, CHAT, chat_lock,
    stop_event, heartbeat_monitor, get_current_broker,
)
import client as _c

parser = argparse.ArgumentParser()
parser.add_argument("--user", required=True)
parser.add_argument("--room", default="geral")
args = parser.parse_args()

_c.USER_ID = args.user
_c.ROOM = "geral"  # sd_trab1 só tem sala geral

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

print(f"Conectado como {_c.USER_ID} na sala geral")


def text_recv_debug():
    sub = None
    local_epoch = -1
    while not stop_event.is_set():
        broker, epoch = get_current_broker()
        if not broker:
            time.sleep(0.2)
            continue
        if sub is None or local_epoch != epoch:
            if sub:
                sub.close(0)
            sub = _c.ctx.socket(zmq.SUB)
            sub.setsockopt(zmq.RCVTIMEO, 1000)
            sub.connect(f"tcp://{broker['host']}:{broker['ports']['text_sub_out']}")
            sub.setsockopt(zmq.SUBSCRIBE, f"texto:{_c.ROOM}:".encode("utf-8"))
            print(f"[debug] assinando texto:{_c.ROOM}: em {broker['host']}:{broker['ports']['text_sub_out']}")
            local_epoch = epoch
        try:
            frames = sub.recv_multipart()
            print(f"[debug] recebeu {len(frames)} frames: {[f[:80] for f in frames]}")
        except zmq.error.Again:
            pass
        except Exception as e:
            print(f"[debug] erro: {e}")


threading.Thread(target=text_recv_debug, daemon=True).start()
threading.Thread(target=lambda: heartbeat_monitor(discovery, "lowest-latency"), daemon=True).start()

last_seen = 0


def print_new_messages():
    global last_seen
    while not stop_event.is_set():
        with chat_lock:
            msgs = list(CHAT)
        for msg in msgs[last_seen:]:
            sender = msg.get("de", "?")
            body = msg.get("msg", "")
            if sender != _c.USER_ID:
                print(f"\n[{sender}]: {body}")
                print("> ", end="", flush=True)
        last_seen = len(msgs)
        time.sleep(0.3)


threading.Thread(target=print_new_messages, daemon=True).start()

try:
    while not stop_event.is_set():
        text = input("> ")
        if text.strip():
            send_text_with_retry(text.strip())
except KeyboardInterrupt:
    stop_event.set()
