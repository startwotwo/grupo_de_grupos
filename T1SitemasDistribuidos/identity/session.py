# identity/session.py
import uuid
import zmq
import logging
import time
import threading

log = logging.getLogger(__name__)

class Session:
    def __init__(self, nome: str, sala: str = "A", on_reconnect=None, registry_host: str = "localhost"):
        if ":" in registry_host:
            self.registry_addr = f"tcp://{registry_host}"
        else:
            self.registry_addr = f"tcp://{registry_host}:5555"

        self.nome = nome
        self.sala = sala
        self.user_id = f"{nome}_{uuid.uuid4().hex[:4]}"
        self.online = False
        self.on_reconnect = on_reconnect  # Callback chamado quando reconecta
        
        self.context = zmq.Context()
        self.ctrl_sock = None
        self.broker_info = None
        self.broker_addr = None
        
        self._running = False
        self._hb_thread = None

    def _get_broker_from_registry(self):
        """Consulta o registry para obter um broker disponível."""
        reg = self.context.socket(zmq.REQ)
        reg.setsockopt(zmq.RCVTIMEO, 2000)
        reg.connect(self.registry_addr)
        try:
            reg.send_json({"action": "get_broker"})
            resp = reg.recv_json()
            if resp.get("status") == "ok":
                return resp.get("address")
        except zmq.ZMQError:
            log.error("Registry indisponível")
        finally:
            reg.close()
        return None

    def login(self):
        addr = self._get_broker_from_registry()
        if not addr:
            log.error("Nenhum broker encontrado no Registry")
            return False
        
        self.broker_addr = addr
        host, base_port = addr.split(":")
        ctrl_port = int(base_port) + 7
        
        self.ctrl_sock = self.context.socket(zmq.REQ)
        self.ctrl_sock.setsockopt(zmq.RCVTIMEO, 3000)
        self.ctrl_sock.connect(f"tcp://{host}:{ctrl_port}")
        
        try:
            self.ctrl_sock.send_json({
                "action": "login",
                "user_id": self.user_id,
                "sala": self.sala
            })
            resp = self.ctrl_sock.recv_json()
            if resp.get("status") == "ok":
                resp['sala'] = self.sala
                resp['host'] = host  # Passa o IP real do broker para o Sender/Receiver
                self.broker_info = resp
                self.online = True
                self._start_heartbeat()
                log.info(f"Conectado ao broker {resp.get('broker_id')} em {addr}")
                return True
        except zmq.ZMQError:
            log.error(f"Falha ao conectar no broker {addr}")
        
        return False

    def _start_heartbeat(self):
        self._running = True
        self._hb_thread = threading.Thread(target=self._hb_loop, daemon=True)
        self._hb_thread.start()

    def _hb_loop(self):
        while self._running:
            try:
                self.ctrl_sock.send_json({"action": "heartbeat", "user_id": self.user_id})
                if self.ctrl_sock.poll(2000):
                    self.ctrl_sock.recv_json()
                else:
                    log.warning("Broker não respondeu heartbeat! Tentando reconectar...")
                    self.reconnect()
                    break
            except zmq.ZMQError:
                self.reconnect()
                break
            time.sleep(5)

    def reconnect(self):
        self.online = False
        log.info("Tentando reconexão automática...")
        while not self.login():
            log.info("Aguardando broker disponível...")
            time.sleep(2)
        log.info(f"Reconectado com sucesso! em {self.broker_addr}")
        # Avisa o cliente para recriar os sockets de mídia no novo broker
        if self.on_reconnect:
            self.on_reconnect(self.broker_info)

    def logout(self):
        self._running = False
        if self.ctrl_sock:
            try:
                self.ctrl_sock.send_json({"action": "logout", "user_id": self.user_id})
                self.ctrl_sock.recv_json()
            except: pass
            self.ctrl_sock.close()
        self.online = False