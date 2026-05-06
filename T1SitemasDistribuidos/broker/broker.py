# broker/broker.py
import zmq
import threading
import logging
import sys
import time
import uuid

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [BROKER-%(broker_id)s] %(message)s",
    datefmt="%H:%M:%S"
)

class Broker:
    def __init__(self, host="localhost", base_port=5000, registry_addr="tcp://localhost:5555"):
        self.host = host
        self.base_port = base_port
        self.registry_addr = registry_addr
        self.broker_id = str(uuid.uuid4())[:4]
        self.log = logging.LoggerAdapter(logging.getLogger(__name__), {"broker_id": self.broker_id})
        
        self.context = zmq.Context()
        self.running = False
        self.clients = {}
        self._lock = threading.Lock()
        
        # Sockets Ports
        self.p_video_in  = base_port + 1
        self.p_video_out = base_port + 2
        self.p_audio_in  = base_port + 3
        self.p_audio_out = base_port + 4
        self.p_text_in   = base_port + 5
        self.p_text_out  = base_port + 6
        self.p_control   = base_port + 7
        self.p_inter     = base_port + 8

        self.peers = {} # addr -> socket
        self.stats = {"video": 0, "audio": 0, "text": 0}

    def _setup_sockets(self):
        def create_socket(typ):
            s = self.context.socket(typ)
            s.setsockopt(zmq.LINGER, 0)
            return s

        # Vídeo/áudio: SUB (não-bloqueante, descarta se sobrecarregado)
        self.v_in = create_socket(zmq.SUB)
        self.v_in.setsockopt(zmq.RCVHWM, 4)  # Descarta frames antigos se acumular
        self.v_in.bind(f"tcp://*:{self.p_video_in}")
        self.v_in.setsockopt(zmq.SUBSCRIBE, b"")
        self.v_out = create_socket(zmq.PUB)
        self.v_out.setsockopt(zmq.SNDHWM, 4)
        self.v_out.bind(f"tcp://*:{self.p_video_out}")
        
        self.a_in = create_socket(zmq.SUB)
        self.a_in.setsockopt(zmq.RCVHWM, 8)
        self.a_in.bind(f"tcp://*:{self.p_audio_in}")
        self.a_in.setsockopt(zmq.SUBSCRIBE, b"")
        self.a_out = create_socket(zmq.PUB)
        self.a_out.setsockopt(zmq.SNDHWM, 8)
        self.a_out.bind(f"tcp://*:{self.p_audio_out}")
        
        # Texto: PULL (confiável, entrega garantida)
        self.t_in = create_socket(zmq.PULL)
        self.t_in.bind(f"tcp://*:{self.p_text_in}")
        self.t_out = create_socket(zmq.PUB)
        self.t_out.bind(f"tcp://*:{self.p_text_out}")

        self.control = create_socket(zmq.REP); self.control.bind(f"tcp://*:{self.p_control}")
        self.inter = create_socket(zmq.ROUTER); self.inter.bind(f"tcp://*:{self.p_inter}")

    def _proxy_loop(self, socket_in, socket_out, name, type_code):
        poller = zmq.Poller()
        poller.register(socket_in, zmq.POLLIN)
        
        while self.running:
            try:
                socks = dict(poller.poll(500))
                if not self.running: break
                
                if socket_in in socks:
                    msg = socket_in.recv_multipart()
                    # msg[0] = sala, msg[1] = data
                    socket_out.send_multipart(msg)
                    self._broadcast_to_cluster(type_code, msg)
                    self.stats[name] += 1
            except zmq.ZMQError: break

    def _broadcast_to_cluster(self, type_code, msg):
        # [type_code, source_id, sala, data]
        inter_msg = [type_code.to_bytes(1, 'big'), self.broker_id.encode()] + msg
        with self._lock:
            for addr, sock in self.peers.items():
                try:
                    sock.send_multipart(inter_msg)
                except: pass

    def _inter_broker_receiver(self):
        poller = zmq.Poller()
        poller.register(self.inter, zmq.POLLIN)
        while self.running:
            try:
                socks = dict(poller.poll(500))
                if self.inter in socks:
                    parts = self.inter.recv_multipart()
                    if len(parts) < 5: continue
                    
                    type_code = int.from_bytes(parts[1], 'big')
                    source_id = parts[2].decode()
                    sala = parts[3]
                    data = parts[4]
                    
                    if source_id == self.broker_id: continue
                    
                    if type_code == 0: self.v_out.send_multipart([sala, data])
                    elif type_code == 1: self.a_out.send_multipart([sala, data])
                    elif type_code == 2: self.t_out.send_multipart([sala, data])
            except zmq.ZMQError: break

    def _handle_control(self):
        while self.running:
            try:
                if self.control.poll(500):
                    msg = self.control.recv_json()
                    action = msg.get("action")
                    if action == "login":
                        self.control.send_json({"status": "ok", "broker_id": self.broker_id, 
                                              "ports": {"video_in": self.p_video_in, "video_out": self.p_video_out,
                                                        "audio_in": self.p_audio_in, "audio_out": self.p_audio_out,
                                                        "text_in": self.p_text_in, "text_out": self.p_text_out,
                                                        "control": self.p_control}})
                    elif action == "stats":
                        self.control.send_json({"status": "ok", "stats": self.stats, "clients": len(self.clients)})
                    elif action == "heartbeat":
                        self.control.send_json({"status": "ok"})
                    else:
                        self.control.send_json({"status": "error"})
            except zmq.ZMQError:
                continue  # Não deixa um erro isolado matar a thread de controle

    def _registry_sync_loop(self):
        my_addr = f"{self.host}:{self.base_port}"
        
        while self.running:
            # Recria o socket REQ a cada ciclo para evitar trava de estado (send/recv dessincronizados)
            reg_sock = self.context.socket(zmq.REQ)
            reg_sock.setsockopt(zmq.LINGER, 0)
            reg_sock.setsockopt(zmq.RCVTIMEO, 2000)
            reg_sock.setsockopt(zmq.SNDTIMEO, 2000)
            reg_sock.connect(self.registry_addr)
            try:
                reg_sock.send_json({"action": "register", "address": my_addr})
                reg_sock.recv_json()
                reg_sock.send_json({"action": "list_brokers"})
                resp = reg_sock.recv_json()
                if resp.get("status") == "ok":
                    active_brokers = resp.get("brokers", [])
                    with self._lock:
                        for b_addr in active_brokers:
                            if b_addr != my_addr and b_addr not in self.peers:
                                self.log.info(f"Conectando ao peer: {b_addr}")
                                p_host, p_base = b_addr.split(":")
                                p_inter_port = int(p_base) + 8
                                dealer = self.context.socket(zmq.DEALER)
                                dealer.setsockopt(zmq.LINGER, 0)
                                dealer.setsockopt(zmq.SNDTIMEO, 100)  # 100ms: nunca bloqueia em peer morto
                                dealer.setsockopt(zmq.SNDHWM, 10)     # Descarta rápido se fila encher
                                dealer.connect(f"tcp://{p_host}:{p_inter_port}")
                                self.peers[b_addr] = dealer
                    # Remove peers que não estão mais no registry (broker morto)
                    with self._lock:
                        dead = [addr for addr in self.peers if addr not in active_brokers and addr != my_addr]
                        for addr in dead:
                            self.log.info(f"Removendo peer inativo: {addr}")
                            self.peers[addr].close()
                            del self.peers[addr]
            except Exception:
                pass  # Erro isolado nao para o loop de registro
            finally:
                reg_sock.close()  # Sempre fecha para liberar recursos
            time.sleep(2)

    def start(self):
        self.running = True
        self._setup_sockets()
        threading.Thread(target=self._registry_sync_loop, daemon=True).start()
        threading.Thread(target=self._handle_control, daemon=True).start()
        threading.Thread(target=self._inter_broker_receiver, daemon=True).start()
        threading.Thread(target=self._proxy_loop, args=(self.v_in, self.v_out, "video", 0), daemon=True).start()
        threading.Thread(target=self._proxy_loop, args=(self.a_in, self.a_out, "audio", 1), daemon=True).start()
        threading.Thread(target=self._proxy_loop, args=(self.t_in, self.t_out, "text", 2), daemon=True).start()
        self.log.info(f"Broker ativo na base {self.base_port}")

    def stop(self):
        self.running = False
        self.context.term()

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", type=str, default="localhost", help="IP do broker (ex: 192.168.1.10)")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--registry", type=str, default="tcp://localhost:5555", help="Endereço do registry")
    args = parser.parse_args()
    b = Broker(host=args.host, base_port=args.port, registry_addr=args.registry)
    try:
        b.start()
        while True: time.sleep(1)
    except KeyboardInterrupt:
        b.stop()