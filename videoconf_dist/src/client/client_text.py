import zmq
import threading
import sys
import time
import queue
import uuid
import shared.config as cfg


HEARTBEAT_INTERVAL = 3  # Segundos entre pings
HEARTBEAT_TIMEOUT = 10  # Segundos sem receber nada antes de reconectar
RETRY_TIMEOUT = 5       # Segundos aguardando o ACK da própria mensagem


class TextClient:
    def __init__(self, user_name, room):
        self.user_name = user_name
        self.room = room
        
        # Filas para comunicação entre as threads (Concorrência)
        self.send_queue = queue.Queue()
        self.render_queue = queue.Queue()
        
        # Controle de QoS e Falhas
        self.pending_messages = {}  # Dicionário {msg_id: (mensagem_bruta, timestamp)}
        self.pending_lock = threading.Lock()
        
        self.stop_event = threading.Event()
        self.reconnect_event = threading.Event()
        
        self.last_recv_time = time.time()

    def capture_input(self):
        """Thread 1: Apenas captura o input do usuário."""
        while not self.stop_event.is_set():
            try:
                # O input fica vazio, o "> " será desenhado pelo render_output
                text = input()
                
                sys.stdout.write('\033[1A\033[2K')
                sys.stdout.flush()

                if text.strip():
                    if text.lower() in ['sair', 'exit', 'quit']:
                        self.stop_event.set()
                        break
                    
                    # Coloca a sua própria mensagem na fila para ser desenhada bonitinha
                    self.render_queue.put(f"[Você]: {text}")
                    
                    # Cria um ID único para a mensagem (QoS)
                    msg_id = str(uuid.uuid4())[:8]
                    formatted_msg = f"{self.room}:TEXTO:{self.user_name}:{msg_id}|{text}"
                    
                    self.send_queue.put(formatted_msg)
            except EOFError:
                pass

    def send_messages(self):
        """Thread 2: Gerencia o envio, QoS (Retry) e reconexão do PUB."""
        context = zmq.Context()
        socket_pub = None

        def connect_pub():
            nonlocal socket_pub
            if socket_pub:
                socket_pub.close()
            socket_pub = context.socket(zmq.PUB)
            socket_pub.connect(f"tcp://{cfg.BROKER_HOST}:{cfg.PUBLISH_PORT}")

        connect_pub()
        
        last_heartbeat = time.time()

        while not self.stop_event.is_set():
            if self.reconnect_event.is_set():
                connect_pub()
                self.reconnect_event.clear()
            
            # Envia Heartbeat (Tolerância a falhas)
            if time.time() - last_heartbeat > HEARTBEAT_INTERVAL:
                hb_msg = f"{self.room}:HEARTBEAT:{self.user_name}:0|ping"
                socket_pub.send_string(hb_msg)
                last_heartbeat = time.time()

            # Processa mensagens da fila (Envio)
            try:
                msg = self.send_queue.get(timeout=0.5)
                socket_pub.send_string(msg)
                
                # Salva para o QoS (Retry) - Apenas para mensagens de texto
                if "TEXTO" in msg:
                    msg_id = msg.split("|")[0].split(":")[3]
                    with self.pending_lock:
                        self.pending_messages[msg_id] = (msg, time.time())
                        
            except queue.Empty:
                pass

            # Verifica mensagens perdidas (QoS - Garantia de entrega)
            with self.pending_lock:
                current_time = time.time()
                for msg_id, (msg, timestamp) in list(self.pending_messages.items()):
                    if current_time - timestamp > RETRY_TIMEOUT:
                        self.render_queue.put(f"[SISTEMA]: Reenviando mensagem perdida...")
                        # Atualiza o timestamp e reenvia
                        self.pending_messages[msg_id] = (msg, current_time)
                        socket_pub.send_string(msg)

        socket_pub.close()
        context.term()

    def receive_messages(self):
        """Thread 3: Escuta o broker e detecta falhas (Heartbeat)."""
        context = zmq.Context()
        socket_sub = None

        def connect_sub():
            nonlocal socket_sub
            if socket_sub:
                socket_sub.close()
            socket_sub = context.socket(zmq.SUB)
            socket_sub.setsockopt(zmq.RCVTIMEO, 1000)   # Timeout de 1s para não travar a thread
            socket_sub.connect(f"tcp://{cfg.BROKER_HOST}:{cfg.SUBSCRIBE_PORT}")
            
            # Assina Textos e Heartbeats da sala
            socket_sub.setsockopt_string(zmq.SUBSCRIBE, f"{self.room}:TEXTO:")
            socket_sub.setsockopt_string(zmq.SUBSCRIBE, f"{self.room}:HEARTBEAT:")

        connect_sub()
        self.last_recv_time = time.time()

        while not self.stop_event.is_set():
            try:
                message = socket_sub.recv_string()
                self.last_recv_time = time.time()   # Reseta o watchdog
                
                header, content = message.split("|", 1)
                room, mtype, sender, msg_id = header.split(":")

                if mtype == "HEARTBEAT":
                    continue    # Ignora print de heartbeats

                if sender == self.user_name:
                    # QoS: Se recebi minha própria mensagem do broker, chegou com sucesso! (ACK)
                    with self.pending_lock:
                        if msg_id in self.pending_messages:
                            del self.pending_messages[msg_id]
                else:
                    # Mensagem de outra pessoa: vai para renderização
                    self.render_queue.put(f"[{sender}]: {content}")

            except zmq.error.Again:
                # Timeout do RCVTIMEO (nenhuma mensagem no último 1s)
                # Verifica Tolerância a Falhas: O broker caiu?
                if time.time() - self.last_recv_time > HEARTBEAT_TIMEOUT:
                    self.render_queue.put("[SISTEMA]: Conexão com broker perdida. Reconectando...")
                    self.reconnect_event.set()
                    connect_sub()
                    self.last_recv_time = time.time() # Dá um fôlego pós reconexão
                    
            except Exception as e:
                if not self.stop_event.is_set():
                    self.render_queue.put(f"[ERRO RECEPÇÃO]: {e}")

        socket_sub.close()
        context.term()

    def render_output(self):
        """Thread 4: Cuida de imprimir na tela mantendo o prompt sempre embaixo."""
        # Imprime o primeiro prompt quando o programa abre
        sys.stdout.write('> ')
        sys.stdout.flush()
        
        while not self.stop_event.is_set():
            try:
                # Aguarda algo para desenhar na tela
                output_str = self.render_queue.get(timeout=0.5)
                
                sys.stdout.write('\r\033[2K') 
                
                # Imprime a mensagem nova
                print(output_str)
                
                # Redesenha o prompt na linha de baixo para você continuar digitando
                sys.stdout.write('> ')
                sys.stdout.flush()
            except queue.Empty:
                pass

    def start(self):
        print(f"Bem-vindo(a) {self.user_name} à {self.room}!")
        print("Digite sua mensagem e aperte Enter (Digite 'sair' para encerrar):\n")

        threads = [
            threading.Thread(target=self.capture_input, daemon=True, name="Captura"),
            threading.Thread(target=self.send_messages, daemon=True, name="Envio"),
            threading.Thread(target=self.receive_messages, daemon=True, name="Recepcao"),
            threading.Thread(target=self.render_output, daemon=True, name="Renderizacao")
        ]

        for t in threads:
            t.start()

        try:
            while not self.stop_event.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            self.stop_event.set()

        print("\nDesconectando...")
        
    def authenticate(self):
        """Faz o handshake com o Broker via REQ/REP antes de iniciar o chat."""
        context = zmq.Context()
        socket_req = context.socket(zmq.REQ)
        
        socket_req.connect(f"tcp://{cfg.BROKER_HOST}:{cfg.AUTH_PORT}")
        
        print("Autenticando no servidor...")
        
        # Envia pedido de login
        # Formato: "LOGIN|SALA|NOME"
        socket_req.send_string(f"LOGIN|{self.room}|{self.user_name}")
        
        # Aguarda a resposta do broker
        resposta = socket_req.recv_string()
        socket_req.close()
        
        if resposta == "OK":
            return True
        else:
            print(f"Falha no login: {resposta}")
            return False