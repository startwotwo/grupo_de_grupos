import zmq
import json
import time
import threading

REGISTRY_PORT = 5550
BROKER_TIMEOUT = 6  # segundos sem heartbeat → broker considerado offline

class Registry:
    def __init__(self, host="*", port=REGISTRY_PORT):
        self.host = host
        self.port = port
        self.context = zmq.Context()

        # Socket REP — responde clientes e brokers
        self.rep_socket = self.context.socket(zmq.REP)
        self.rep_socket.bind(f"tcp://{host}:{port}")

        # Banco de brokers: { broker_id: {ip, port, hb_port, last_seen, status} }
        self.brokers: dict = {}
        self._lock = threading.Lock()

        # Índice para round-robin
        self._rr_index = 0

        self.running = True

    def _heartbeat_listener(self):
        """Escuta heartbeats de todos os brokers e atualiza last_seen."""
        sub = self.context.socket(zmq.SUB)
        sub.setsockopt_string(zmq.SUBSCRIBE, "HB/")

        connected_ports = set()

        while self.running:
            # Conecta em brokers recém-registrados
            with self._lock:
                for bid, info in self.brokers.items():
                    hb_port = info.get("hb_port")
                    ip = info.get("ip", "localhost")
                    if hb_port and hb_port not in connected_ports:
                        sub.connect(f"tcp://{ip}:{hb_port}")
                        connected_ports.add(hb_port)
                        print(f"[Registry] Escutando heartbeat de {bid} em {ip}:{hb_port}")

            # Tenta receber com timeout para não bloquear forever
            if sub.poll(timeout=500):  # ms
                try:
                    msg = sub.recv_string(zmq.NOBLOCK)
                    # Formato: "HB/<BROKER_ID>|ALIVE"
                    parts = msg.split("|")
                    if len(parts) == 2 and parts[1] == "ALIVE":
                        broker_id = parts[0][3:]  # remove "HB/"
                        with self._lock:
                            if broker_id in self.brokers:
                                self.brokers[broker_id]["last_seen"] = time.time()
                                if self.brokers[broker_id]["status"] != "online":
                                    self.brokers[broker_id]["status"] = "online"
                                    print(f"[Registry] Broker {broker_id} voltou online.")
                except zmq.ZMQError:
                    pass

        sub.close()

    # ------------------------------------------------------------------
    # Watchdog — marca brokers sem heartbeat como offline
    # ------------------------------------------------------------------
    def _watchdog(self):
        while self.running:
            now = time.time()
            with self._lock:
                for bid, info in self.brokers.items():
                    if info["status"] == "online":
                        if now - info.get("last_seen", now) > BROKER_TIMEOUT:
                            info["status"] = "offline"
                            print(f"[Registry] Broker {bid} marcado como OFFLINE (timeout).")
            time.sleep(2)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _online_brokers(self) -> dict:
        return {bid: info for bid, info in self.brokers.items()
                if info["status"] == "online"}

    def _pick_broker_round_robin(self) -> dict | None:
        """Retorna um broker online via round-robin, ou None se não houver."""
        with self._lock:
            online = list(self._online_brokers().values())
        if not online:
            return None
        broker = online[self._rr_index % len(online)]
        self._rr_index += 1
        return broker

    # ------------------------------------------------------------------
    # Loop principal REP
    # ------------------------------------------------------------------
    def start(self):
        threading.Thread(target=self._heartbeat_listener, daemon=True).start()
        threading.Thread(target=self._watchdog, daemon=True).start()

        print(f"[Registry] Ativo na porta {self.port}. Aguardando brokers e clientes...")

        while self.running:
            try:
                if not self.rep_socket.poll(timeout=1000):
                    continue

                msg = self.rep_socket.recv_json()

                # --- Broker se registrando ---
                if msg.get("type") == "Register":
                    bid   = msg["id"]
                    ip    = msg["ip"]
                    port  = msg["port"]
                    hb_port = msg.get("hb_port", 5559)
                    cluster_txt_port = msg.get("cluster_txt_port")
                    cluster_aud_port = msg.get("cluster_aud_port")
                    cluster_vid_port = msg.get("cluster_vid_port")
                    sub_port = msg.get("sub_port")
                    aud_sub_port = msg.get("aud_sub_port")
                    vid_sub_port = msg.get("vid_sub_port")

                    with self._lock:
                        self.brokers[bid] = {
                            "id": bid,
                            "ip": ip,
                            "port": port,
                            "sub_port": sub_port,
                            "aud_pub_port": msg.get("aud_pub_port"),
                            "aud_sub_port": aud_sub_port,
                            "vid_pub_port": msg.get("vid_pub_port"),
                            "vid_sub_port": vid_sub_port,
                            "hb_port": hb_port,
                            "cluster_txt_port": cluster_txt_port,
                            "cluster_aud_port": cluster_aud_port,
                            "cluster_vid_port": cluster_vid_port,
                            "status": "online",
                            "last_seen": time.time(),
                        }

                    self.rep_socket.send_json({"status": "ok", "msg": "Registrado_com_sucesso"})
                    print(f"[Registry] Broker {bid} registrado em {ip}:{port} (hb:{hb_port})")

                # --- Cliente pedindo UM broker (round-robin) ---
                elif msg.get("type") == "Get_broker":
                    broker = self._pick_broker_round_robin()
                    if broker:
                        self.rep_socket.send_json({"status": "ok", "broker": broker})
                    else:
                        self.rep_socket.send_json({"status": "error", "msg": "Nenhum broker disponível"})
                    print(f"[Registry] Get_broker respondido → {broker}")

                # --- Cliente pedindo TODOS os brokers online ---
                elif msg.get("type") == "Get_all_brokers":
                    with self._lock:
                        online = self._online_brokers()
                    self.rep_socket.send_json({"status": "ok", "brokers": online})

                else:
                    self.rep_socket.send_json({"status": "error", "msg": "Tipo desconhecido"})

            except KeyboardInterrupt:
                print("[Registry] Encerrando...")
                self.running = False
            except Exception as e:
                print(f"[Registry] Erro: {e}")
                try:
                    self.rep_socket.send_json({"status": "error", "msg": str(e)})
                except Exception:
                    pass

        self.rep_socket.close()
        self.context.term()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=REGISTRY_PORT)
    args = parser.parse_args()

    r = Registry(port=args.port)
    r.start()

