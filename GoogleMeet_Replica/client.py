import zmq
import threading
import time

REGISTRY_HOST = "localhost"
REGISTRY_PORT = 5550
BROKER_TIMEOUT = 5   # segundos sem heartbeat → broker considerado morto

class Cliente:
    def __init__(self, identity: str, room: str,
                 msgCallBack,
                 audCallBack=None,
                 vidCallBack=None,
                 brokerStatusCallBack=None,
                 registry_host: str = REGISTRY_HOST,
                 registry_port: int = REGISTRY_PORT):

        self.identity = identity
        self.room = room
        self.msgCallBack = msgCallBack
        self.audCallBack = audCallBack
        self.vidCallBack = vidCallBack
        self.brokerStatusCallBack = brokerStatusCallBack
        self.registry_host = registry_host
        self.registry_port = registry_port

        self.context = zmq.Context()
        self.running = True

        # Estado do broker atual
        self.broker_ip   = None
        self.broker_port = None   # porta de publicação TXT (XSUB do broker)
        self.broker_sub_port = None  # porta de assinatura TXT (XPUB do broker)
        self.broker_aud_pub_port = None  # porta de publicação AUD
        self.broker_aud_sub_port = None  # porta de assinatura AUD
        self.broker_vid_pub_port = None  # porta de publicação VID
        self.broker_vid_sub_port = None  # porta de assinatura VID
        self.broker_hb_port  = None

        self.ultimoHeartbeat = time.time()
        self.brokerVivo = False
        self.broker_version = 0

        # Socket de publicação — será (re)criado em _conectar_broker
        self.pub = None
        self.aud_pub = None
        self.vid_pub = None
        self._pub_lock = threading.Lock()
        self._aud_lock = threading.Lock()
        self._vid_lock = threading.Lock()
        self._pending_text = []
        self._pending_lock = threading.Lock()
        self._pending_max = 100
        self._presence_interval = 3
        self._presence_thread = None

        # Conecta em background — não bloqueia a GUI
        threading.Thread(target=self._descobrir_e_conectar, daemon=True).start()

    # ------------------------------------------------------------------
    # Service discovery — consulta o Registry
    # ------------------------------------------------------------------
    def _consultar_registry(self) -> dict | None:
        req = self.context.socket(zmq.REQ)
        req.setsockopt(zmq.RCVTIMEO, 3000)  # 3s timeout na resposta
        req.setsockopt(zmq.LINGER, 0)
        req.connect(f"tcp://{self.registry_host}:{self.registry_port}")
        try:
            req.send_json({"type": "Get_broker"})
            resp = req.recv_json()
            if resp.get("status") == "ok":
                return resp["broker"]
            else:
                print(f"[Cliente] Registry sem brokers: {resp.get('msg')}")
                return None
        except zmq.ZMQError as e:
            # EAGAIN = timeout expirou (registry offline ou sem resposta)
            if e.errno == zmq.EAGAIN:
                print(f"[Cliente] Registry não respondeu (timeout). Tentando novamente...")
            else:
                print(f"[Cliente] Erro consultando registry: {e}")
            return None
        finally:
            # Sempre fecha e descarta o socket REQ — não reutilizar após erro
            req.close()

    # ------------------------------------------------------------------
    # Conexão / reconexão a um broker
    # ------------------------------------------------------------------
    def _conectar_broker(self, ip: str, pub_port: int, sub_port: int, aud_pub_port: int, aud_sub_port: int, vid_pub_port: int, vid_sub_port: int, hb_port: int):
        """(Re)cria o socket PUB apontando para o novo broker."""
        with self._pub_lock:
            if self.pub:
                self.pub.close()
            self.pub = self.context.socket(zmq.PUB)
            self.pub.setsockopt(zmq.SNDHWM, 1000)
            self.pub.setsockopt(zmq.LINGER, 0)
            self.pub.connect(f"tcp://{ip}:{pub_port}")

        with self._aud_lock:
            if self.aud_pub:
                self.aud_pub.close()
            self.aud_pub = self.context.socket(zmq.PUB)
            self.aud_pub.setsockopt(zmq.SNDHWM, 50)
            self.aud_pub.setsockopt(zmq.LINGER, 0)
            self.aud_pub.connect(f"tcp://{ip}:{aud_pub_port}")

        with self._vid_lock:
            if self.vid_pub:
                self.vid_pub.close()
            self.vid_pub = self.context.socket(zmq.PUB)
            self.vid_pub.setsockopt(zmq.SNDHWM, 20)
            self.vid_pub.setsockopt(zmq.LINGER, 0)
            self.vid_pub.connect(f"tcp://{ip}:{vid_pub_port}")

        # Aguarda handshake de sockets PUB/SUB antes de marcar o broker como ativo.
        time.sleep(0.1)

        self.broker_ip       = ip
        self.broker_port     = pub_port
        self.broker_sub_port = sub_port
        self.broker_aud_pub_port = aud_pub_port
        self.broker_aud_sub_port = aud_sub_port
        self.broker_vid_pub_port = vid_pub_port
        self.broker_vid_sub_port = vid_sub_port
        self.broker_hb_port  = hb_port
        self.ultimoHeartbeat = time.time()
        self.brokerVivo      = True
        self.broker_version += 1
        print(f"[Cliente] Conectado ao broker {ip}:{pub_port}")

        self._flush_pending_text()
        self.enviarMsg("__PRESENCE__")

    def _flush_pending_text(self):
        with self._pending_lock:
            if not self._pending_text:
                return
            pending = list(self._pending_text)
            self._pending_text.clear()

        for text in pending:
            self._send_text_now(text)

    def _send_text_now(self, msg: str) -> bool:
        if not self.pub:
            return False

        payload = f"TXT/{self.room}|{self.identity}|{msg}"
        try:
            self.pub.send_string(payload, zmq.DONTWAIT)
            return True
        except zmq.ZMQError as e:
            if self.running:
                print(f"[Cliente] Erro ao enviar mensagem: {e}")
            return False

    def _descobrir_e_conectar(self):
        for attempt in range(10):
            broker = self._consultar_registry()
            if broker:
                self._conectar_broker(
                    ip=broker["ip"],
                    pub_port=int(broker["port"]),
                    sub_port=int(broker.get("sub_port", int(broker["port"]) + 1)),
                    aud_pub_port=int(broker.get("aud_pub_port", 5557)),
                    aud_sub_port=int(broker.get("aud_sub_port", 5558)),
                    vid_pub_port=int(broker.get("vid_pub_port", 5559)),
                    vid_sub_port=int(broker.get("vid_sub_port", 5560)),
                    hb_port=int(broker.get("hb_port", 5561)),
                )
                return
            print(f"[Cliente] Nenhum broker disponível (tentativa {attempt+1}). Aguardando...")
            time.sleep(2)
        print("[Cliente] Não foi possível conectar a nenhum broker.")

    # ------------------------------------------------------------------
    # Threads de escuta
    # ------------------------------------------------------------------
    def threadEscuta(self):
        threading.Thread(target=self._escutar_txt,       daemon=True).start()
        threading.Thread(target=self._escutar_aud,       daemon=True).start()
        threading.Thread(target=self._escutar_vid,       daemon=True).start()
        threading.Thread(target=self._escutar_heartbeat, daemon=True).start()
        threading.Thread(target=self._monitor_broker,    daemon=True).start()
        if not self._presence_thread:
            self._presence_thread = threading.Thread(target=self._presence_loop, daemon=True)
            self._presence_thread.start()

    def _escutar_txt(self):
        subscriber = None
        current_version = -1

        while self.running:
            if self.broker_ip is None:
                time.sleep(0.2)
                continue

            if subscriber is None or current_version != self.broker_version:
                if subscriber:
                    subscriber.close()
                subscriber = self.context.socket(zmq.SUB)
                subscriber.setsockopt(zmq.LINGER, 0)
                subscriber.setsockopt_string(zmq.SUBSCRIBE, f"TXT/{self.room}")
                subscriber.connect(f"tcp://{self.broker_ip}:{self.broker_sub_port}")
                current_version = self.broker_version

            try:
                if subscriber.poll(timeout=500):
                    while True:
                        try:
                            message = subscriber.recv_string(zmq.DONTWAIT)
                        except zmq.Again:
                            break
                        partes = message.split("|", 2)
                        if len(partes) == 3:
                            _, user, msg = partes
                            if user != self.identity:
                                self.msgCallBack(user, msg)
            except zmq.ZMQError as e:
                if self.running:
                    print(f"[Cliente] Erro TXT: {e}")

        if subscriber:
            subscriber.close()

    def _escutar_aud(self):
        subscriber = None
        current_version = -1

        while self.running:
            if self.broker_ip is None:
                time.sleep(0.2)
                continue

            if subscriber is None or current_version != self.broker_version:
                if subscriber:
                    subscriber.close()
                subscriber = self.context.socket(zmq.SUB)
                subscriber.setsockopt(zmq.LINGER, 0)
                subscriber.setsockopt_string(zmq.SUBSCRIBE, f"AUD/{self.room}")
                subscriber.connect(f"tcp://{self.broker_ip}:{self.broker_aud_sub_port}")
                current_version = self.broker_version

            try:
                if subscriber.poll(timeout=500):
                    latest = {}
                    while True:
                        try:
                            message = subscriber.recv(zmq.DONTWAIT)
                        except zmq.Again:
                            break
                        partes = message.split(b"|", 2)
                        if len(partes) == 3:
                            _, user, data = partes
                            decoded_user = user.decode()
                            if decoded_user != self.identity:
                                latest[decoded_user] = data

                    for user, data in latest.items():
                        if self.audCallBack:
                            self.audCallBack(user, data)
            except zmq.ZMQError as e:
                if self.running:
                    print(f"[Cliente] Erro AUD: {e}")

        if subscriber:
            subscriber.close()

    def _escutar_vid(self):
        subscriber = None
        current_version = -1

        while self.running:
            if self.broker_ip is None:
                time.sleep(0.2)
                continue

            if subscriber is None or current_version != self.broker_version:
                if subscriber:
                    subscriber.close()
                subscriber = self.context.socket(zmq.SUB)
                subscriber.setsockopt(zmq.LINGER, 0)
                subscriber.setsockopt_string(zmq.SUBSCRIBE, f"VID/{self.room}")
                subscriber.connect(f"tcp://{self.broker_ip}:{self.broker_vid_sub_port}")
                current_version = self.broker_version

            try:
                if subscriber.poll(timeout=500):
                    last_frame = {}
                    while True:
                        try:
                            message = subscriber.recv(zmq.DONTWAIT)
                        except zmq.Again:
                            break
                        partes = message.split(b"|", 2)
                        if len(partes) == 3:
                            _, user, data = partes
                            decoded_user = user.decode()
                            if decoded_user != self.identity:
                                last_frame[decoded_user] = data

                    for user, data in last_frame.items():
                        if self.vidCallBack:
                            self.vidCallBack(user, data)
            except zmq.ZMQError as e:
                if self.running:
                    print(f"[Cliente] Erro VID: {e}")

        if subscriber:
            subscriber.close()

    def _escutar_heartbeat(self):
        subscriber = None
        current_version = -1

        while self.running:
            if self.broker_ip is None:
                time.sleep(0.2)
                continue

            if subscriber is None or current_version != self.broker_version:
                if subscriber:
                    subscriber.close()
                subscriber = self.context.socket(zmq.SUB)
                subscriber.setsockopt(zmq.LINGER, 0)
                subscriber.setsockopt_string(zmq.SUBSCRIBE, "HB/")
                subscriber.connect(f"tcp://{self.broker_ip}:{self.broker_hb_port}")
                current_version = self.broker_version

            try:
                if subscriber.poll(timeout=500):
                    subscriber.recv_string(zmq.NOBLOCK)
                    self.ultimoHeartbeat = time.time()
            except zmq.ZMQError as e:
                if self.running:
                    print(f"[Cliente] Erro HB: {e}")

        if subscriber:
            subscriber.close()

    # ------------------------------------------------------------------
    # Monitor de broker — detecta falha e faz failover
    # ------------------------------------------------------------------
    def _monitor_broker(self):
        while self.running:
            morto = (time.time() - self.ultimoHeartbeat) > BROKER_TIMEOUT

            if morto and self.brokerVivo:
                self.brokerVivo = False
                print(f"[Cliente] Broker perdido. Tentando failover...")
                if self.brokerStatusCallBack:
                    self.brokerStatusCallBack(False)
                self._failover()

            elif not morto and not self.brokerVivo:
                self.brokerVivo = True
                print(f"[Cliente] Broker reconectado.")
                if self.brokerStatusCallBack:
                    self.brokerStatusCallBack(True)

            time.sleep(1)

    def _failover(self):
        """Consulta o registry e troca para outro broker disponível."""
        for attempt in range(20):
            if not self.running:
                return
            broker = self._consultar_registry()
            if broker:
                new_ip   = broker["ip"]
                new_port = int(broker["port"])
                # Evita reconectar no mesmo broker que acabou de cair
                if new_ip == self.broker_ip and new_port == self.broker_port:
                    print(f"[Cliente] Registry retornou o mesmo broker offline. Aguardando...")
                else:
                    self._conectar_broker(
                        ip=new_ip,
                        pub_port=new_port,
                        sub_port=int(broker.get("sub_port", new_port + 1)),
                        aud_pub_port=int(broker.get("aud_pub_port", 5557)),
                        aud_sub_port=int(broker.get("aud_sub_port", 5558)),
                        vid_pub_port=int(broker.get("vid_pub_port", 5559)),
                        vid_sub_port=int(broker.get("vid_sub_port", 5560)),
                        hb_port=int(broker.get("hb_port", 5561)),
                    )
                    # Notifica GUI que voltou
                    if self.brokerStatusCallBack:
                        self.brokerStatusCallBack(True)
                    return
            time.sleep(3)
        print("[Cliente] Failover esgotado. Sem brokers disponíveis.")

    # ------------------------------------------------------------------
    # Envio de mensagem
    # ------------------------------------------------------------------
    def enviarMsg(self, msg: str):
        if not msg:
            return

        if self._send_text_now(msg):
            return

        with self._pending_lock:
            if len(self._pending_text) >= self._pending_max:
                self._pending_text.pop(0)
            self._pending_text.append(msg)
            print(f"[Cliente] Mensagem armazenada para reenvio ({len(self._pending_text)} pendentes)")

    def _presence_loop(self):
        while self.running:
            if self.brokerVivo and self.broker_ip is not None:
                self.enviarMsg("__PRESENCE__")
            time.sleep(self._presence_interval)

    def enviarAudio(self, data: bytes):
        if not data:
            return
        payload = f"AUD/{self.room}|{self.identity}|".encode() + data
        with self._aud_lock:
            if self.aud_pub:
                try:
                    self.aud_pub.send(payload)
                except zmq.ZMQError as e:
                    print(f"[Cliente] Erro ao enviar áudio: {e}")

    def enviarVideo(self, data: bytes):
        if not data:
            return
        payload = f"VID/{self.room}|{self.identity}|".encode() + data
        with self._vid_lock:
            if self.vid_pub:
                try:
                    self.vid_pub.send(payload)
                except zmq.ZMQError as e:
                    print(f"[Cliente] Erro ao enviar vídeo: {e}")

    # ------------------------------------------------------------------
    # Desconexão
    # ------------------------------------------------------------------
    def desconectar(self):
        self.running = False
        self.enviarMsg("saiu da ligação")
        time.sleep(0.5)

        with self._pub_lock:
            if self.pub:
                self.pub.close()
                self.pub = None
        with self._aud_lock:
            if self.aud_pub:
                self.aud_pub.close()
                self.aud_pub = None
        with self._vid_lock:
            if self.vid_pub:
                self.vid_pub.close()
                self.vid_pub = None

        # Aguarda as threads de recebimento saírem do loop antes de encerrar o contexto.
        time.sleep(0.2)
        try:
            self.context.term()
        except Exception:
            pass

# end