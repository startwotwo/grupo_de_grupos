import json
import msgpack  # pip install msgpack
import threading
import time
from pathlib import Path

import yaml
import zmq

PORTS_FILE = Path(__file__).parent / "ports.yaml"

# (topic_prefix, sock_type) por grupo. Endpoint construído dinamicamente
# a partir de ports.yaml (host + inject_port), pra rodar multi-PC.
GROUP_PROTO = {
    "grupo_i":    (b"ROOM_A TEXT",        "PUB"),
    "googlemeet": (b"sala_test:",         "PUB"),
    "expansion":  (b"text:FED_EXP:A:",    "PUB"),
    "sd_trab1":   (b"texto:fed:test:",    "PUB"),
    "sd_meeting": (None,                  "PUSH"),
    "t1sistemas": (b"sala_test:",         "PUSH_T1"),
    "trabalho1":  (b"SALA_FED:TEXTO:",    "PUB"),
    "sd_trabalho":(b"fed_room:",          "PUB"),
    "ufscar":     (None,                  "MSGPACK_CTRL"),
    "videoconf":  (b"SALA_FED:TEXTO:",    "PUB"),
}


def _load_publishers():
    with open(PORTS_FILE) as f:
        cfg = yaml.safe_load(f)
    pubs = {}
    for gname, (prefix, stype) in GROUP_PROTO.items():
        gcfg = cfg["groups"].get(gname)
        if not gcfg:
            continue
        host = gcfg.get("host", "localhost")
        port = gcfg.get("inject_port")
        if not port:
            continue
        pubs[gname] = (f"tcp://{host}:{port}", prefix, stype)
    sb_host = cfg.get("super_broker_host", "localhost")
    sb_port = cfg["super_broker"]["txt_xpub"]
    return pubs, f"tcp://{sb_host}:{sb_port}"


PUBLISHERS, FED_XPUB = _load_publishers()
received = {}
lock = threading.Lock()


def listener():
    ctx = zmq.Context()
    sub = ctx.socket(zmq.SUB)
    sub.connect(FED_XPUB)
    sub.setsockopt(zmq.SUBSCRIBE, b"")
    sub.setsockopt(zmq.RCVTIMEO, 15000)
    print(f"[listener] conectado em {FED_XPUB}")
    while True:
        try:
            msg = sub.recv_multipart()
            topic = msg[0].decode(errors="replace")
            payload = msg[1].decode(errors="replace") if len(msg) > 1 else ""
            print(f"  [FED] {topic!r:50s} | {payload[:50]!r}")
            with lock:
                for group in PUBLISHERS:
                    if topic.startswith(f"{group}/"):
                        received[group] = topic
        except zmq.Again:
            break
    sub.close()
    ctx.term()


def publish(group, endpoint, topic_prefix, sock_type):
    ctx = zmq.Context()
    payload_str = f"[FED-TEST] ola da federacao, grupo={group}"

    try:
        if sock_type == "PUSH":
            sock = ctx.socket(zmq.PUSH)
            sock.connect(endpoint)
            time.sleep(2.0)
            meta = json.dumps({
                "room": "A", "sender_id": "fed_test",
                "msg_id": f"fed-{group}-1", "v": 1, "type": "text",
            }).encode()
            print(f"[pub:{group}] PUSH → {endpoint}")
            for _ in range(5):
                sock.send_multipart([meta, payload_str.encode()])
                time.sleep(0.3)

        elif sock_type == "PUB_WITH_SUB":
            xsub_port = int(endpoint.split(":")[-1])
            xpub_port = xsub_port + 1

            sub_sock = ctx.socket(zmq.SUB)
            sub_sock.connect(f"tcp://localhost:{xpub_port}")
            sub_sock.setsockopt(zmq.SUBSCRIBE, b"")

            pub_sock = ctx.socket(zmq.PUB)
            pub_sock.connect(endpoint)
            time.sleep(2.0)

            topic = topic_prefix + b"fed_test"
            print(f"[pub:{group}] PUB_WITH_SUB topic={topic!r} → {endpoint}")
            for _ in range(8):
                pub_sock.send_multipart([topic, payload_str.encode()])
                time.sleep(0.3)

            time.sleep(3.0)  # ← aguarda mensagens propagarem antes de fechar
            sub_sock.close()
            pub_sock.close()
            ctx.term()
            return


        elif sock_type == "MSGPACK_CTRL":
            # ufscar: control ROUTER na porta 7904, protocolo msgpack
            # Envia login + send_text usando o formato Message do ufscar
            import sys, os
            sys.path.insert(0, os.path.join("ufscar-sd", "src"))
            from common.models import Message, MessageType, ControlMessageType
            from common.utils import serialize_message

            sock = ctx.socket(zmq.DEALER)
            sock.setsockopt_string(zmq.IDENTITY, "fed_test_ufscar")
            sock.connect(endpoint)
            time.sleep(1.0)
            print(f"[pub:{group}] MSGPACK_CTRL → {endpoint}")

            # Login
            login_msg = Message.create_control(
                sender_id="fed_test_ufscar",
                control_type=ControlMessageType.LOGIN,
                payload=b"fed_test_ufscar"
            )
            sock.send(serialize_message(login_msg))
            time.sleep(0.5)

            # Join group A
            join_msg = Message.create_control(
                sender_id="fed_test_ufscar",
                control_type=ControlMessageType.JOIN_GROUP,
                payload=b"A"
            )
            sock.send(serialize_message(join_msg))
            time.sleep(0.5)

            # Send text
            from common.models import MessageType
            for i in range(5):
                text_msg = Message(
                    type=MessageType.TEXT,
                    sender_id="fed_test_ufscar",
                    group="A",
                    payload=payload_str.encode()
                )
                sock.send(serialize_message(text_msg))
                time.sleep(0.3)

        elif sock_type == "PUB_SLOW":
            sock = ctx.socket(zmq.PUB)
            sock.connect(endpoint)
            time.sleep(3.0)
            topic = topic_prefix + b"fed_test"
            print(f"[pub:{group}] PUB_SLOW topic={topic!r} → {endpoint}")
            for _ in range(8):
                sock.send_multipart([topic, payload_str.encode()])
                time.sleep(0.3)

        elif sock_type == "PUSH_T1":
            # t1sistemas: t_in é PULL na porta base+5=7606
            sock = ctx.socket(zmq.PUSH)
            sock.connect(endpoint)
            time.sleep(1.0)
            # O broker espera [sala, data] — mesmo formato do _proxy_loop
            topic = topic_prefix + b"fed_test"
            print(f"[pub:{group}] PUSH_T1 → {endpoint}")
            for _ in range(8):
                sock.send_multipart([topic, payload_str.encode()])
                time.sleep(0.3)


        else:  # PUB
            sock = ctx.socket(zmq.PUB)
            sock.connect(endpoint)
            time.sleep(2.0)
            topic = topic_prefix + b"fed_test"
            print(f"[pub:{group}] PUB topic={topic!r} → {endpoint}")
            for _ in range(5):
                sock.send_multipart([topic, payload_str.encode()])
                time.sleep(0.3)

    except Exception as e:
        print(f"[pub:{group}] ERRO: {e}")
    finally:
        try:
            sock.close()
            ctx.term()
        except Exception:
            pass


def main():
    t = threading.Thread(target=listener, daemon=True)
    t.start()
    time.sleep(2.0)

    threads = []
    for group, (endpoint, topic_prefix, sock_type) in PUBLISHERS.items():
        t = threading.Thread(
            target=publish,
            args=(group, endpoint, topic_prefix, sock_type),
            daemon=True
        )
        t.start()
        threads.append(t)

    for t in threads:
        t.join()

    time.sleep(5)

    print("\n=== RESULTADO ===")
    all_ok = True
    for group in PUBLISHERS:
        ok = group in received
        status = "✓" if ok else "✗ nao chegou"
        print(f"  {group:15s} {status}")
        if not ok:
            all_ok = False

    if all_ok:
        print("\nTodos os grupos conectados ao SuperBroker!")
    else:
        print("\nAlguns grupos nao chegaram.")


if __name__ == "__main__":
    main()
