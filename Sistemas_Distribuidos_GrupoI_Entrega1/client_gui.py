import zmq
import threading
import time
import json
import uuid
import argparse
import cv2
import numpy as np
import queue
import tkinter as tk
from tkinter import scrolledtext
from PIL import Image, ImageTk 

try:
    import pyaudio
except ImportError:
    pyaudio = None

class VideoConferenceClientGUI:
    def __init__(self, registry_endpoint, user_id, room="ROOM_A", dummy=False):
        self.registry_endpoint = registry_endpoint
        self.user_id = user_id
        self.room = room
        self.dummy = dummy # Flag para pular o hardware se for um clone local
        
        # Se for o modo dummy, desligamos o áudio para não travar o Linux
        global pyaudio
        if self.dummy:
            pyaudio = None
        
        self.context = zmq.Context()
        self.cmd_socket = None
        self.pub_socket = None
        self.sub_socket = None
        self.lock = threading.Lock()
        
        self.running = True
        self.msg_id_counter = 0
        
        # Filas nativas para comunicação segura entre as Threads e a GUI
        self.frame_queue = queue.Queue(maxsize=10)
        self.text_queue = queue.Queue() 
        
        self.CHUNK = 1024
        self.FORMAT = pyaudio.paInt16 if pyaudio else 8
        self.CHANNELS = 1
        self.RATE = 44100
        
        self.audio_out_stream = None
        if pyaudio:
            try:
                self.p_out = pyaudio.PyAudio()
                self.audio_out_stream = self.p_out.open(format=self.FORMAT, channels=self.CHANNELS, rate=self.RATE, output=True)
            except Exception:
                pass

        # --- SETUP DA INTERFACE GRÁFICA (TKINTER) ---
        self.root = tk.Tk()
        self.root.title(f"VideoConf - {self.user_id} ({self.room}) {'[DUMMY MODE]' if self.dummy else ''}")
        self.root.geometry("700x650") 
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

        # 1. Vídeos fixos no TOPO
        self.videos_frame = tk.Frame(self.root, bg="#202020", height=260)
        self.videos_frame.pack(side=tk.TOP, fill=tk.X, padx=5, pady=5)
        self.video_labels = {} 

        # 2. Barra de Input e Botões ancorados no FUNDO (BOTTOM)
        self.input_frame = tk.Frame(self.root)
        self.input_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=(0, 10))

        self.msg_entry = tk.Entry(self.input_frame, font=("Arial", 11))
        self.msg_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))
        self.msg_entry.bind("<Return>", lambda event: self.gui_send_msg())

        self.send_btn = tk.Button(self.input_frame, text="Enviar", command=self.gui_send_msg, bg="#0078D7", fg="white", font=("Arial", 10, "bold"))
        self.send_btn.pack(side=tk.LEFT)

        self.online_btn = tk.Button(self.input_frame, text="👥 Online", command=self.get_online_users)
        self.online_btn.pack(side=tk.LEFT, padx=(5, 0))

        # 3. Chat no MEIO
        self.chat_area = scrolledtext.ScrolledText(self.root, wrap=tk.WORD, state='disabled', font=("Arial", 10))
        self.chat_area.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=10, pady=5)

    def print_to_chat(self, msg):
        self.chat_area.config(state='normal')
        self.chat_area.insert(tk.END, msg + "\n")
        self.chat_area.see(tk.END)
        self.chat_area.config(state='disabled')

    def gui_send_msg(self):
        text = self.msg_entry.get()
        if text.strip():
            self.msg_entry.delete(0, tk.END)
            self.print_to_chat(f"[{self.user_id}]: {text}")
            threading.Thread(target=self.send_text, args=(text,), daemon=True).start()

    def on_closing(self):
        self.running = False
        self.root.destroy()
        
    def discover_and_connect(self):
        with self.lock:
            reg_socket = self.context.socket(zmq.REQ)
            reg_socket.connect(self.registry_endpoint)
            reg_socket.setsockopt(zmq.RCVTIMEO, 2000)
            
            while self.running:
                try:
                    reg_socket.send_json({"action": "DISCOVER"})
                    reply = reg_socket.recv_json()
                    if reply.get("status") == "ok":
                        broker_id = reply["broker_id"]
                        endpoints = reply["endpoint"]
                        
                        self._setup_broker_sockets(endpoints)
                        if self._login():
                            reg_socket.close()
                            return True
                except zmq.ZMQError:
                    pass
                
                for _ in range(20):
                    if not self.running: break
                    time.sleep(0.1)
        return False
            
    def _setup_broker_sockets(self, endpoints):
        if self.cmd_socket: self.cmd_socket.close(linger=0)
        if self.pub_socket: self.pub_socket.close(linger=0)
        if self.sub_socket: self.sub_socket.close(linger=0)
        
        self.cmd_socket = self.context.socket(zmq.DEALER)
        self.cmd_socket.setsockopt_string(zmq.IDENTITY, self.user_id)
        self.cmd_socket.connect(endpoints["cmd"])
        self.cmd_socket.setsockopt(zmq.SNDTIMEO, 2000)
        
        self.pub_socket = self.context.socket(zmq.PUB)
        self.pub_socket.connect(endpoints["sub"])
        
        self.sub_socket = self.context.socket(zmq.SUB)
        self.sub_socket.setsockopt_string(zmq.SUBSCRIBE, f"{self.room} ")
        self.sub_socket.connect(endpoints["pub"])

    def _login(self):
        try:
            poller = zmq.Poller()
            poller.register(self.cmd_socket, zmq.POLLIN)
            self.cmd_socket.send_multipart([b"", json.dumps({"action": "LOGIN", "user_id": self.user_id, "room": self.room}).encode('utf-8')])
            socks = dict(poller.poll(2000))
            if self.cmd_socket in socks:
                reply = self.cmd_socket.recv_multipart()
                if len(reply) >= 2:
                    msg = json.loads(reply[-1].decode('utf-8'))
                    if msg.get("status") == "ok":
                        return True
            return False
        except Exception as e:
            return False

    def start(self):
        self.text_queue.put("[*] Conectando ao sistema distribuído...")
        threading.Thread(target=self._startup_sequence, daemon=True).start()
        self.root.after(100, self.process_queues)
        self.root.mainloop() 

    def _startup_sequence(self):
        if not self.discover_and_connect():
            self.text_queue.put("[!] Falha Crítica de conexão.")
            return
            
        self.text_queue.put(f"[*] Conectado com sucesso à sala '{self.room}'!\n-------------------")
        
        threading.Thread(target=self.sub_thread, daemon=True).start()
        threading.Thread(target=self.video_capture_thread, daemon=True).start()
        threading.Thread(target=self.audio_capture_thread, daemon=True).start()

    def process_queues(self):
        if not self.running:
            return
            
        try:
            while not self.frame_queue.empty():
                user, frame = self.frame_queue.get_nowait()
                self.update_video_panel(user, frame)
                
            while not self.text_queue.empty():
                chat_msg = self.text_queue.get_nowait()
                self.print_to_chat(chat_msg)
                
        except queue.Empty:
            pass
        except Exception as e:
            pass
            
        self.root.after(10, self.process_queues)

    def update_video_panel(self, user, frame):
        try:
            cv_img = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            img = Image.fromarray(cv_img)
            imgtk = ImageTk.PhotoImage(image=img)
            
            if user not in self.video_labels:
                lbl = tk.Label(self.videos_frame, text=f"{user}", compound=tk.TOP, bg="#202020", fg="white", font=("Arial", 10, "bold"))
                lbl.pack(side=tk.LEFT, padx=5, pady=5)
                self.video_labels[user] = lbl
            
            self.video_labels[user].imgtk = imgtk 
            self.video_labels[user].configure(image=imgtk)
        except Exception:
            pass

    def sub_thread(self):
        while self.running:
            try:
                with self.lock:
                    sub_sock = self.sub_socket
                if not sub_sock:
                    time.sleep(0.1)
                    continue

                poller = zmq.Poller()
                poller.register(sub_sock, zmq.POLLIN)
                socks = dict(poller.poll(1000))
                
                if sub_sock in socks:
                    while True:
                        try:
                            parts = sub_sock.recv_multipart(flags=zmq.NOBLOCK)
                            if len(parts) >= 2:
                                topic_type = parts[0].decode('utf-8').split(' ')
                                msg_type = topic_type[1]
                                
                                if msg_type == "TEXT":
                                    data = json.loads(parts[1].decode('utf-8'))
                                    if data.get("user_id") != self.user_id:
                                        msg = f"[{data['user_id']}]: {data['text']}"
                                        self.text_queue.put(msg) 
                                        
                                elif msg_type == "VIDEO":
                                    np_arr = np.frombuffer(parts[1], np.uint8)
                                    frame = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
                                    if frame is not None:
                                        user = topic_type[2] if len(topic_type) > 2 else "Unknown"
                                        if user != self.user_id:
                                            if self.frame_queue.full():
                                                try: self.frame_queue.get_nowait()
                                                except: pass
                                            self.frame_queue.put((user, frame))
                                            
                                elif msg_type == "AUDIO":
                                    if self.audio_out_stream:
                                        user = topic_type[2] if len(topic_type) > 2 else "Unknown"
                                        if user != self.user_id:
                                            try:
                                                self.audio_out_stream.write(parts[1])
                                            except Exception:
                                                pass
                        except zmq.Again:
                            break 
            except Exception:
                time.sleep(0.1)
                
    def video_capture_thread(self):
        # Se for o modo dummy, não tenta ligar a câmera física
        if not self.dummy:
            cap = cv2.VideoCapture(0)
            has_cam = cap.isOpened()
        else:
            has_cam = False
            
        while self.running:
            try:
                with self.lock:
                    pub_sock = self.pub_socket
                if not pub_sock:
                    time.sleep(0.1)
                    continue

                frame = None
                if has_cam:
                    ret, cframe = cap.read()
                    if ret:
                        cframe = cv2.resize(cframe, (320, 240))
                        frame = cframe
                        
                if frame is None:
                    frame = np.zeros((240, 320, 3), dtype=np.uint8)
                    frame[:] = (0, 100, 0)
                    cv2.putText(frame, "USER: " + self.user_id, (50,130), cv2.FONT_HERSHEY_SIMPLEX, 1, (255,255,255), 2)

                if self.frame_queue.full():
                    try: self.frame_queue.get_nowait()
                    except: pass
                self.frame_queue.put(("Você", frame.copy()))

                _, buffer = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 50])
                topic = f"{self.room} VIDEO {self.user_id}".encode('utf-8')
                pub_sock.send_multipart([topic, buffer.tobytes()])
            except Exception:
                pass
            
            time.sleep(0.1) 
            
    def audio_capture_thread(self):
        if not pyaudio: return
        p = pyaudio.PyAudio()
        try:
            stream = p.open(format=self.FORMAT, channels=self.CHANNELS, rate=self.RATE, input=True, frames_per_buffer=self.CHUNK)
        except Exception:
            return
            
        while self.running:
            try:
                data = stream.read(self.CHUNK, exception_on_overflow=False)
                with self.lock:
                    pub_sock = self.pub_socket
                if pub_sock:
                    topic = f"{self.room} AUDIO {self.user_id}".encode('utf-8')
                    pub_sock.send_multipart([topic, data])
            except Exception:
                pass

    def get_online_users(self):
        msg = {"action": "ONLINE", "room": self.room}
        try:
            with self.lock:
                cmd_sock = self.cmd_socket
            if cmd_sock:
                cmd_sock.send_multipart([b"", json.dumps(msg).encode('utf-8')])
                poller = zmq.Poller()
                poller.register(cmd_sock, zmq.POLLIN)
                socks = dict(poller.poll(2000))
                if cmd_sock in socks:
                    reply = cmd_sock.recv_multipart()
                    if len(reply) >= 2:
                        ans = json.loads(reply[-1].decode('utf-8'))
                        if ans.get("status") == "online":
                            online_list = ', '.join(ans['users'])
                            self.text_queue.put(f"👥 Usuários na sala: {online_list}")
        except Exception:
            pass
            
    def send_text(self, text):
        self.msg_id_counter += 1
        msg = {
            "action": "TEXT",
            "room": self.room,
            "user_id": self.user_id,
            "msg_id": self.msg_id_counter,
            "text": text
        }
        
        # QoS: Loop de tentativas (Retries)
        retries = 3
        while retries > 0 and self.running:
            try:
                with self.lock:
                    cmd_sock = self.cmd_socket
                
                if cmd_sock:
                    cmd_sock.send_multipart([b"", json.dumps(msg).encode('utf-8')])
                    poller = zmq.Poller()
                    poller.register(cmd_sock, zmq.POLLIN)
                    # Aguarda 2 segundos pela confirmação (ACK)
                    socks = dict(poller.poll(2000)) 
                    
                    if cmd_sock in socks:
                        reply = cmd_sock.recv_multipart()
                        if len(reply) >= 2:
                            ack_msg = json.loads(reply[-1].decode('utf-8'))
                            if ack_msg.get("ack") == self.msg_id_counter:
                                return # Sucesso! Mensagem entregue.
            except Exception:
                pass
                
            retries -= 1
            if retries > 0:
                self.text_queue.put(f"[!] Aviso: Demora na rede. Tentativa {3 - retries}/3...")
        
        # Se chegou aqui, acabaram os retries
        if self.running:
            self.text_queue.put("⚠️ Broker off-line ou indisponível! Iniciando migração de rota (Failover)...")
            
            if self.discover_and_connect():
                self.text_queue.put("✅ Reconectado a um novo Broker! Reenviando última mensagem...")
                try:
                    with self.lock:
                        if self.cmd_socket:
                            self.cmd_socket.send_multipart([b"", json.dumps(msg).encode('utf-8')])
                except Exception:
                    pass
            else:
                 self.text_queue.put("❌ Falha crítica: Nenhum Broker disponível no sistema.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", default="tcp://127.0.0.1:5555")
    parser.add_argument("--user", default="User" + str(uuid.uuid4())[:4])
    parser.add_argument("--room", default="ROOM_A")
    parser.add_argument("--dummy", action="store_true", help="Usa câmera e microfone falsos para testes locais")
    args = parser.parse_args()
    
    client = VideoConferenceClientGUI(args.registry, args.user, args.room, dummy=args.dummy)
    client.start()