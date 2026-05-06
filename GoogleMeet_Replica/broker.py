import zmq
import time
import threading
import argparse
import re

REGISTRY_HOST = "localhost"
REGISTRY_PORT = 5550
DISCOVERY_INTERVAL = 5

class Broker:
    def __init__(
        self,
        broker_id: str,
        host: str = "localhost",
        pub_port: int = 5555,
        sub_port: int = 5556,
        aud_pub_port: int = 5557,
        aud_sub_port: int = 5558,
        vid_pub_port: int = 5559,
        vid_sub_port: int = 5560,
        hb_port: int = 5561,
        registry_host: str = REGISTRY_HOST,
        registry_port: int = REGISTRY_PORT,
        cluster_txt_port: int | None = None,
        cluster_aud_port: int | None = None,
        cluster_vid_port: int | None = None,
    ):
        self.id = broker_id
        self.host = host
        self.pub_port = pub_port   # XSUB — clientes publicam TXT aqui
        self.sub_port = sub_port   # XPUB — clientes assinam TXT aqui
        self.aud_pub_port = aud_pub_port  # XSUB — clientes publicam AUD aqui
        self.aud_sub_port = aud_sub_port  # XPUB — clientes assinam AUD aqui
        self.vid_pub_port = vid_pub_port  # XSUB — clientes publicam VID aqui
        self.vid_sub_port = vid_sub_port  # XPUB — clientes assinam VID aqui
        self.hb_port = hb_port    # PUB heartbeat

        self.cluster_txt_port = cluster_txt_port or (self.pub_port + 100)
        self.cluster_aud_port = cluster_aud_port or (self.aud_pub_port + 100)
        self.cluster_vid_port = cluster_vid_port or (self.vid_pub_port + 100)

        self.registry_host = registry_host
        self.registry_port = registry_port

        self.context = zmq.Context()

        def bind_socket(sock, endpoint, label):
            try:
                sock.bind(endpoint)
            except zmq.ZMQError as e:
                raise RuntimeError(
                    f"[{self.id}] Falha ao bindar {label} em {endpoint}: {e}. "
                    "Verifique se outro processo já está usando essa porta e se não existe uma instância antiga de broker ligada."
                ) from e

        # TXT: Clientes postam aqui (XSUB)
        self.frontend = self.context.socket(zmq.XSUB)
        self.frontend.setsockopt(zmq.RCVHWM, 1000)
        bind_socket(self.frontend, f"tcp://*:{pub_port}", "TXT frontend")

        # TXT: Clientes assinam aqui (XPUB)
        self.backend = self.context.socket(zmq.XPUB)
        self.backend.setsockopt(zmq.SNDHWM, 1000)
        bind_socket(self.backend, f"tcp://*:{sub_port}", "TXT backend")

        # AUD: Clientes postam aqui (XSUB)
        self.aud_frontend = self.context.socket(zmq.XSUB)
        self.aud_frontend.setsockopt(zmq.RCVHWM, 200)
        bind_socket(self.aud_frontend, f"tcp://*:{aud_pub_port}", "AUD frontend")

        # AUD: Clientes assinam aqui (XPUB)
        self.aud_backend = self.context.socket(zmq.XPUB)
        self.aud_backend.setsockopt(zmq.SNDHWM, 200)
        bind_socket(self.aud_backend, f"tcp://*:{aud_sub_port}", "AUD backend")

        # VID: Clientes postam aqui (XSUB)
        self.vid_frontend = self.context.socket(zmq.XSUB)
        self.vid_frontend.setsockopt(zmq.RCVHWM, 100)
        bind_socket(self.vid_frontend, f"tcp://*:{vid_pub_port}", "VID frontend")

        # VID: Clientes assinam aqui (XPUB)
        self.vid_backend = self.context.socket(zmq.XPUB)
        self.vid_backend.setsockopt(zmq.SNDHWM, 100)
        bind_socket(self.vid_backend, f"tcp://*:{vid_sub_port}", "VID backend")

        # Heartbeat
        self.hb_socket = self.context.socket(zmq.PUB)
        self.hb_socket.setsockopt(zmq.SNDHWM, 10)
        bind_socket(self.hb_socket, f"tcp://*:{hb_port}", "heartbeat")

        # Cluster entre brokers
        self.cluster_pub_txt = self.context.socket(zmq.PUB)
        self.cluster_pub_txt.setsockopt(zmq.SNDHWM, 100)
        bind_socket(self.cluster_pub_txt, f"tcp://*:{self.cluster_txt_port}", "cluster TXT")

        self.cluster_sub_txt = self.context.socket(zmq.SUB)
        self.cluster_sub_txt.setsockopt_string(zmq.SUBSCRIBE, "")
        self.cluster_sub_txt.setsockopt(zmq.RCVHWM, 500)

        self.cluster_pub_aud = self.context.socket(zmq.PUB)
        self.cluster_pub_aud.setsockopt(zmq.SNDHWM, 100)
        self.cluster_pub_aud.bind(f"tcp://*:{self.cluster_aud_port}")

        self.cluster_sub_aud = self.context.socket(zmq.SUB)
        self.cluster_sub_aud.setsockopt_string(zmq.SUBSCRIBE, "")
        self.cluster_sub_aud.setsockopt(zmq.RCVHWM, 500)

        self.cluster_pub_vid = self.context.socket(zmq.PUB)
        self.cluster_pub_vid.setsockopt(zmq.SNDHWM, 100)
        self.cluster_pub_vid.bind(f"tcp://*:{self.cluster_vid_port}")

        self.cluster_sub_vid = self.context.socket(zmq.SUB)
        self.cluster_sub_vid.setsockopt_string(zmq.SUBSCRIBE, "")
        self.cluster_sub_vid.setsockopt(zmq.RCVHWM, 500)

        self.running = True
        self.connected_peers = set()

        print(
            f"[{self.id}] Broker ativo — TXT:{pub_port}/{sub_port} AUD:{aud_pub_port}/{aud_sub_port} "
            f"VID:{vid_pub_port}/{vid_sub_port} HB:{hb_port} CLUSTER:{self.cluster_txt_port}/{self.cluster_aud_port}/{self.cluster_vid_port}"
        )

    # ------------------------------------------------------------------
    # Registro no registry
    # ------------------------------------------------------------------
    def _register(self):
        req = self.context.socket(zmq.REQ)
        req.setsockopt(zmq.RCVTIMEO, 3000)
        req.setsockopt(zmq.LINGER, 0)
        req.connect(f"tcp://{self.registry_host}:{self.registry_port}")

        payload = {
            "type": "Register",
            "id": self.id,
            "ip": self.host,
            "port": self.pub_port,
            "sub_port": self.sub_port,
            "aud_pub_port": self.aud_pub_port,
            "aud_sub_port": self.aud_sub_port,
            "vid_pub_port": self.vid_pub_port,
            "vid_sub_port": self.vid_sub_port,
            "hb_port": self.hb_port,
            "cluster_txt_port": self.cluster_txt_port,
            "cluster_aud_port": self.cluster_aud_port,
            "cluster_vid_port": self.cluster_vid_port,
        }

        for attempt in range(5):
            try:
                req.send_json(payload)
                resp = req.recv_json()
                if resp.get("status") == "ok":
                    print(f"[{self.id}] Registrado no Registry com sucesso.")
                    req.close()
                    return
                print(f"[{self.id}] Registry respondeu erro: {resp}")
            except zmq.ZMQError as e:
                print(f"[{self.id}] Tentativa {attempt+1} de registro falhou: {e}")
                time.sleep(2)

        print(f"[{self.id}] Não foi possível registrar no Registry. Continuando mesmo assim.")
        req.close()

    def _query_all_brokers(self) -> dict | None:
        req = self.context.socket(zmq.REQ)
        req.setsockopt(zmq.RCVTIMEO, 3000)
        req.setsockopt(zmq.LINGER, 0)
        req.connect(f"tcp://{self.registry_host}:{self.registry_port}")
        try:
            req.send_json({"type": "Get_all_brokers"})
            resp = req.recv_json()
            if resp.get("status") == "ok":
                return resp["brokers"]
            print(f"[{self.id}] Falha ao consultar brokers do Registry: {resp}")
        except zmq.ZMQError as e:
            print(f"[{self.id}] Erro consultando brokers do Registry: {e}")
        finally:
            req.close()
        return None

    def _connect_cluster_peer(self, broker_id: str, broker_info: dict):
        if broker_id == self.id:
            return

        ip = broker_info.get("ip")
        txt_port = int(broker_info.get("cluster_txt_port", 0))
        aud_port = int(broker_info.get("cluster_aud_port", 0))
        vid_port = int(broker_info.get("cluster_vid_port", 0))

        if txt_port:
            endpoint = f"tcp://{ip}:{txt_port}"
            if endpoint not in self.connected_peers:
                self.cluster_sub_txt.connect(endpoint)
                self.connected_peers.add(endpoint)
                print(f"[{self.id}] Conectado ao cluster TXT de {broker_id} em {endpoint}")

        if aud_port:
            endpoint = f"tcp://{ip}:{aud_port}"
            if endpoint not in self.connected_peers:
                self.cluster_sub_aud.connect(endpoint)
                self.connected_peers.add(endpoint)
                print(f"[{self.id}] Conectado ao cluster AUD de {broker_id} em {endpoint}")

        if vid_port:
            endpoint = f"tcp://{ip}:{vid_port}"
            if endpoint not in self.connected_peers:
                self.cluster_sub_vid.connect(endpoint)
                self.connected_peers.add(endpoint)
                print(f"[{self.id}] Conectado ao cluster VID de {broker_id} em {endpoint}")

    def _refresh_cluster_peers(self):
        while self.running:
            brokers = self._query_all_brokers()
            if brokers:
                for broker_id, broker_info in brokers.items():
                    self._connect_cluster_peer(broker_id, broker_info)
            time.sleep(DISCOVERY_INTERVAL)

    # ------------------------------------------------------------------
    # Heartbeat loop
    # ------------------------------------------------------------------
    def _heartbeat_loop(self):
        print(f"[{self.id}] Heartbeat iniciado")
        while self.running:
            self.hb_socket.send_string(f"HB/{self.id}|ALIVE")
            time.sleep(2)

    # ------------------------------------------------------------------
    # Start
    # ------------------------------------------------------------------
    def start(self):
        threading.Thread(target=self._register, daemon=True).start()
        threading.Thread(target=self._refresh_cluster_peers, daemon=True).start()
        threading.Thread(target=self._heartbeat_loop, daemon=True).start()
        threading.Thread(target=self._proxy_txt, daemon=True).start()
        threading.Thread(target=self._proxy_aud, daemon=True).start()
        threading.Thread(target=self._proxy_vid, daemon=True).start()

        print(f"[{self.id}] Proxies iniciados.")

        try:
            while self.running:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            self._shutdown()

    def _proxy_txt(self):
        print(f"[{self.id}] Proxy TXT iniciado.")
        poller = zmq.Poller()
        poller.register(self.frontend, zmq.POLLIN)
        poller.register(self.backend, zmq.POLLIN)
        poller.register(self.cluster_sub_txt, zmq.POLLIN)

        while self.running:
            events = dict(poller.poll(1000))
            if self.frontend in events:
                msg = self.frontend.recv_multipart()
                self.backend.send_multipart(msg)
                self.cluster_pub_txt.send_multipart(msg)
            if self.backend in events:
                msg = self.backend.recv_multipart()
                self.frontend.send_multipart(msg)
            if self.cluster_sub_txt in events:
                msg = self.cluster_sub_txt.recv_multipart()
                self.backend.send_multipart(msg)

    def _proxy_aud(self):
        print(f"[{self.id}] Proxy AUD iniciado.")
        poller = zmq.Poller()
        poller.register(self.aud_frontend, zmq.POLLIN)
        poller.register(self.aud_backend, zmq.POLLIN)
        poller.register(self.cluster_sub_aud, zmq.POLLIN)

        while self.running:
            events = dict(poller.poll(1000))
            if self.aud_frontend in events:
                msg = self.aud_frontend.recv_multipart()
                self.aud_backend.send_multipart(msg)
                self.cluster_pub_aud.send_multipart(msg)
            if self.aud_backend in events:
                msg = self.aud_backend.recv_multipart()
                self.aud_frontend.send_multipart(msg)
            if self.cluster_sub_aud in events:
                msg = self.cluster_sub_aud.recv_multipart()
                self.aud_backend.send_multipart(msg)

    def _proxy_vid(self):
        print(f"[{self.id}] Proxy VID iniciado.")
        poller = zmq.Poller()
        poller.register(self.vid_frontend, zmq.POLLIN)
        poller.register(self.vid_backend, zmq.POLLIN)
        poller.register(self.cluster_sub_vid, zmq.POLLIN)

        while self.running:
            events = dict(poller.poll(1000))
            if self.vid_frontend in events:
                msg = self.vid_frontend.recv_multipart()
                self.vid_backend.send_multipart(msg)
                self.cluster_pub_vid.send_multipart(msg)
            if self.vid_backend in events:
                msg = self.vid_backend.recv_multipart()
                self.vid_frontend.send_multipart(msg)
            if self.cluster_sub_vid in events:
                msg = self.cluster_sub_vid.recv_multipart()
                self.vid_backend.send_multipart(msg)

    def _shutdown(self):
        self.running = False
        self.frontend.close()
        self.backend.close()
        self.aud_frontend.close()
        self.aud_backend.close()
        self.vid_frontend.close()
        self.vid_backend.close()
        self.cluster_pub_txt.close()
        self.cluster_sub_txt.close()
        self.cluster_pub_aud.close()
        self.cluster_sub_aud.close()
        self.cluster_pub_vid.close()
        self.cluster_sub_vid.close()
        self.hb_socket.close()
        self.context.term()
        print(f"[{self.id}] Encerrado.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Inicia um broker ZMQ.")
    parser.add_argument("--id",       type=str, default="BROKER1")
    parser.add_argument("--host",     type=str, default="localhost")
    parser.add_argument("--pub-port", type=int, default=5555)
    parser.add_argument("--sub-port", type=int, default=5556)
    parser.add_argument("--aud-pub-port", type=int, default=5557)
    parser.add_argument("--aud-sub-port", type=int, default=5558)
    parser.add_argument("--vid-pub-port", type=int, default=5559)
    parser.add_argument("--vid-sub-port", type=int, default=5560)
    parser.add_argument("--hb-port",  type=int, default=5561)
    parser.add_argument("--cluster-txt-port", type=int, default=None)
    parser.add_argument("--cluster-aud-port", type=int, default=None)
    parser.add_argument("--cluster-vid-port", type=int, default=None)
    parser.add_argument("--registry-host", type=str, default=REGISTRY_HOST)
    parser.add_argument("--registry-port", type=int, default=REGISTRY_PORT)
    args = parser.parse_args()

    try:
        broker_num = int(re.search(r'\d+', args.id).group())
    except:
        broker_num = 1 # Padrão se não houver número

    # Se for o broker 1, o offset é 0. Se for o 2, o offset é 1000, etc.
    # Isso evita que as faixas de portas se atropelem
    offset = (broker_num - 1) * 1000
    
    # Aplica o offset apenas se as portas forem as padrão
    if args.pub_port == 5555: args.pub_port += offset
    if args.sub_port == 5556: args.sub_port += offset
    if args.aud_pub_port == 5557: args.aud_pub_port += offset
    if args.aud_sub_port == 5558: args.aud_sub_port += offset
    if args.vid_pub_port == 5559: args.vid_pub_port += offset
    if args.vid_sub_port == 5560: args.vid_sub_port += offset
    if args.hb_port == 5561: args.hb_port += offset

    broker = Broker(
        broker_id=args.id,
        host=args.host,
        pub_port=args.pub_port,
        sub_port=args.sub_port,
        aud_pub_port=args.aud_pub_port,
        aud_sub_port=args.aud_sub_port,
        vid_pub_port=args.vid_pub_port,
        vid_sub_port=args.vid_sub_port,
        hb_port=args.hb_port,
        cluster_txt_port=args.cluster_txt_port,
        cluster_aud_port=args.cluster_aud_port,
        cluster_vid_port=args.cluster_vid_port,
        registry_host=args.registry_host,
        registry_port=args.registry_port,
    )
    broker.start()