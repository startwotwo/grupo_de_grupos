import zmq
import threading
import time
import argparse
import json
import socket
import time

from common import (
    DISCOVERY_PORT, HEARTBEAT_INTERVAL,
    CMD_REGISTER, CMD_HEARTBEAT, CMD_UNREGISTER, CMD_LIST,
    channel_ports, parse_topic,
)

def start_channel(ctx, pub_port, sub_port, name):
    # create a XPUB/XSUB proxy to forward messages from publishers to subscribers
    frontend = ctx.socket(zmq.XSUB)   # receive from the PUBs
    frontend.bind(f"tcp://*:{pub_port}")

    backend = ctx.socket(zmq.XPUB)    # send to the SUBs
    backend.bind(f"tcp://*:{sub_port}")

    # HWM for non-accumulating messages when subscribers are slow or disconnected
    frontend.setsockopt(zmq.RCVHWM, 10)
    backend.setsockopt(zmq.SNDHWM, 10)

    t = threading.Thread(
        target=zmq.proxy, args=(frontend, backend), daemon=True, name=f"proxy-{name}"
    )
    t.start()
    return t

def discovery_client(broker_id, host, base_port, discovery_addr, stop_event):
    """Registers the broker in discovery and maintains heartbeat."""
    ctx = zmq.Context.instance()
    sock = ctx.socket(zmq.REQ)
    sock.setsockopt(zmq.LINGER, 0)
    sock.setsockopt(zmq.RCVTIMEO, 2000)  # 2s timeout
    sock.connect(discovery_addr)

    def send(req):
        sock.send_string(json.dumps(req))
        return json.loads(sock.recv_string())

    # initial registration
    try:
        send({"cmd": CMD_REGISTER, "id": broker_id,
              "host": host, "base_port": base_port})
        print(f"[broker] registered in {discovery_addr} as '{broker_id}'")
    except zmq.Again:
        print(f"[broker] WARNING: discovery {discovery_addr} did not respond")

    # heartbeats
    while not stop_event.wait(HEARTBEAT_INTERVAL):
        try:
            resp = send({"cmd": CMD_HEARTBEAT, "id": broker_id})
            if resp.get("need_register"):
                # discovery restarted and lost the registration -> redo
                send({"cmd": CMD_REGISTER, "id": broker_id,
                      "host": host, "base_port": base_port})
                print("[broker] re-registered (discovery had lost the state)")
        except zmq.Again:
            print("[broker] heartbeat failed (timeout) — trying again")
            # recreate REQ socket because it gets in bad state after timeout
            sock.close(linger=0)
            sock = ctx.socket(zmq.REQ)
            sock.setsockopt(zmq.LINGER, 0)
            sock.setsockopt(zmq.RCVTIMEO, 2000)
            sock.connect(discovery_addr)

    # when leaving, notify
    try:
        send({"cmd": CMD_UNREGISTER, "id": broker_id})
    except Exception:
        pass
    sock.close(linger=0)

def mesh_bridge(my_broker_id, my_base_port, discovery_addr, stop_event):
    """
    Mantém conexões SUB com os XPUBs dos outros brokers e reinjeta
    mensagens no XSUB local. Loop-safe via filtro por broker_id.
    """
    ctx = zmq.Context.instance()
    my_ports = channel_ports(my_base_port)

    # PUBs internos que reinjetam no nosso próprio XSUB local (frontends)
    inject = {}
    for ch in ("video", "audio", "text", "presence"):
        s = ctx.socket(zmq.PUB)
        # conecta no XSUB local (porta de entrada do nosso proxy)
        s.connect(f"tcp://localhost:{my_ports[ch][0]}")
        inject[ch] = s

    # Sockets SUB que escutam os outros brokers (um set por canal)
    sub = {}
    for ch in ("video", "audio", "text", "presence"):
        s = ctx.socket(zmq.SUB)
        # se inscreve em TODAS as mensagens do canal, mas vamos
        # filtrar manualmente as que vieram de nós mesmos
        s.setsockopt_string(zmq.SUBSCRIBE, f"{ch}:")
        # HWMs alinhados com o canal
        if ch == "video":
            s.setsockopt(zmq.RCVHWM, 5)
        elif ch == "audio":
            s.setsockopt(zmq.RCVHWM, 20)
        else:
            s.setsockopt(zmq.RCVHWM, 1000)
        sub[ch] = s

    # rastreia quais brokers já estão conectados
    connected_peers = set()

    # discovery REQ socket (para perguntar a lista periodicamente)
    disc = ctx.socket(zmq.REQ)
    disc.setsockopt(zmq.LINGER, 0)
    disc.setsockopt(zmq.RCVTIMEO, 2000)
    disc.connect(discovery_addr)

    def refresh_peers():
        nonlocal disc
        try:
            disc.send_string(json.dumps({"cmd": CMD_LIST}))
            resp = json.loads(disc.recv_string())
        except zmq.Again:
            disc.close(linger=0)
            disc = ctx.socket(zmq.REQ)
            disc.setsockopt(zmq.LINGER, 0)
            disc.setsockopt(zmq.RCVTIMEO, 2000)
            disc.connect(discovery_addr)
            return

        current_peers = {b["id"]: b for b in resp.get("brokers", []) if b["id"] != my_broker_id}

        # Só CONECTA em peers novos — nunca recria sockets nem desconecta.
        # Peers mortos: o ZMQ tenta reconectar internamente, sem custo.
        # Se um peer ressuscitar, a conexão é restaurada automaticamente.
        for peer_id, b in current_peers.items():
            if peer_id in connected_peers:
                continue
            peer_ports = channel_ports(b["base_port"])
            for ch in ("video", "audio", "text", "presence"):
                sub[ch].connect(f"tcp://{b['host']}:{peer_ports[ch][1]}")
            connected_peers.add(peer_id)
            print(f"[mesh] conectado ao peer {peer_id} @ {b['host']}:{b['base_port']}")

    # poller para receber dos 3 SUBs sem bloquear
    poller = zmq.Poller()
    for s in sub.values():
        poller.register(s, zmq.POLLIN)

    last_refresh = 0
    REFRESH_INTERVAL = 3.0  # segundos

    while not stop_event.is_set():
        # periodicamente atualiza lista de peers
        now = time.time()
        if now - last_refresh > REFRESH_INTERVAL:
            refresh_peers()
            last_refresh = now

        events = dict(poller.poll(timeout=500))
        for ch, s in sub.items():
            if s in events:
                try:
                    msg = s.recv_multipart(flags=zmq.NOBLOCK)
                except zmq.Again:
                    continue
                if not msg:
                    continue
                topic = msg[0].decode("utf-8", errors="ignore")
                parsed = parse_topic(topic)
                if not parsed:
                    continue
                _, origin_broker, _, _ = parsed
                # filtro anti-loop: se a mensagem nasceu aqui, ignora
                if origin_broker == my_broker_id:
                    continue
                # reinjeta no nosso XSUB local
                try:
                    inject[ch].send_multipart(msg, flags=zmq.NOBLOCK)
                    # print(f"[mesh-debug] {my_broker_id} reinjetando {ch}: topic={topic}") --- debug
                except zmq.Again:
                    pass  # QoS por canal já lida com drops

    # cleanup
    for s in sub.values():
        s.close(linger=0)
    for s in inject.values():
        s.close(linger=0)
    disc.close(linger=0)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--id", required=True, help="Unique broker ID (e.g., B1)")
    parser.add_argument("--base-port", type=int, default=5555,
                        help="base port; occupies base..base+5")
    parser.add_argument("--discovery", default=f"tcp://localhost:{DISCOVERY_PORT}",
                        help="address of the discovery service")
    parser.add_argument("--host", default=socket.gethostbyname(socket.gethostname()),
                        help="IP that clients will use to connect to this broker")
    args = parser.parse_args()

    ctx = zmq.Context.instance()
    ports = channel_ports(args.base_port)

    # starts the 3 channels using their calculated ports
    start_channel(ctx, ports["video"][0], ports["video"][1], "video")
    start_channel(ctx, ports["audio"][0], ports["audio"][1], "audio")
    start_channel(ctx, ports["text"][0],  ports["text"][1],  "text")
    start_channel(ctx, ports["presence"][0], ports["presence"][1], "presence")

    # registers in discovery
    stop_event = threading.Event()
    threading.Thread(
        target=discovery_client,
        args=(args.id, args.host, args.base_port, args.discovery, stop_event),
        daemon=True,
    ).start()
    # starts the mesh bridge to connect with other brokers
    threading.Thread(
        target=mesh_bridge,
        args=(args.id, args.base_port, args.discovery, stop_event),
        daemon=True,
    ).start()

    print(f"[broker] '{args.id}' running on {args.host}, base_port={args.base_port}")
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[broker] shutting down...")
        stop_event.set()
        time.sleep(0.3)
        ctx.term()

if __name__ == "__main__":
    main()