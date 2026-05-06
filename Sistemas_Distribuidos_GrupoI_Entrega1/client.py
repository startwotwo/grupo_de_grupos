import zmq
import threading
import time
import json
import uuid
import sys
import argparse
import cv2
import numpy as np
import queue

try:
    import pyaudio
except ImportError:
    pyaudio = None

class VideoConferenceClient:
    def __init__(self, registry_endpoint, user_id, room="ROOM_A"):
        self.registry_endpoint = registry_endpoint
        self.user_id = user_id
        self.room = room
        
        self.context = zmq.Context()
        self.cmd_socket = None
        self.pub_socket = None
        self.sub_socket = None
        self.lock = threading.Lock()
        
        self.running = True
        self.msg_id_counter = 0
        self.frame_queue = queue.Queue(maxsize=10)
        
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
                time.sleep(2)
            
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
        print(f"[Client {self.user_id}] Conectando ao sistema...")
        if not self.discover_and_connect():
            return
            
        print(f"[Client {self.user_id}] Conectado com sucesso! Digite mensagens abaixo:")
        threading.Thread(target=self.sub_thread, daemon=True).start()
        threading.Thread(target=self.video_capture_thread, daemon=True).start()
        threading.Thread(target=self.audio_capture_thread, daemon=True).start()
        threading.Thread(target=self.text_input_loop, daemon=True).start()
        
        # OpenCV GUI Loop must run in the main thread!
        while self.running:
            try:
                user, frame = self.frame_queue.get(timeout=0.1)
                cv2.imshow(f"Video {user}", frame)
            except queue.Empty:
                pass
            cv2.waitKey(1)
            
        cv2.destroyAllWindows()

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
                    parts = sub_sock.recv_multipart()
                    if len(parts) >= 2:
                        topic_type = parts[0].decode('utf-8').split(' ')
                        msg_type = topic_type[1]
                        
                        if msg_type == "TEXT":
                            data = json.loads(parts[1].decode('utf-8'))
                            if data.get("user_id") != self.user_id:
                                sys.stdout.write(f"\r[{data['user_id']}] {data['text']}\n> ")
                                sys.stdout.flush()
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
                                        # Escreve os bits de áudio recebidos no alto-falante principal
                                        self.audio_out_stream.write(parts[1])
                                    except Exception:
                                        pass
            except Exception:
                time.sleep(0.1)
                
    def video_capture_thread(self):
        cap = cv2.VideoCapture(0)
        has_cam = cap.isOpened()
        
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
                    # Mock Video Frame
                    frame = np.zeros((240, 320, 3), dtype=np.uint8)
                    frame[:] = (0, 100, 0)
                    cv2.putText(frame, "USER: " + self.user_id, (10,120), cv2.FONT_HERSHEY_SIMPLEX, 1, (255,255,255), 2)

                _, buffer = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 50])
                topic = f"{self.room} VIDEO {self.user_id}".encode('utf-8')
                pub_sock.send_multipart([topic, buffer.tobytes()])
            except Exception:
                pass
            
            # QoS: Sleep to cap FPS
            time.sleep(0.1) # 10 FPS
            
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

    def text_input_loop(self):
        while self.running:
            try:
                sys.stdout.write("> ")
                sys.stdout.flush()
                msg_text = input()
                if msg_text.strip():
                    if msg_text.strip() == "/quit":
                        self.running = False
                        break
                    elif msg_text.strip() == "/online":
                        self.get_online_users()
                    else:
                        self.send_text(msg_text)
            except EOFError:
                self.running = False
                
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
                            print(f"\n[Sistema] Usuários online na sala '{self.room}': {', '.join(ans['users'])}")
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
        
        # QoS Retry loop for Text Message
        retries = 3
        while retries > 0:
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
                            ack_msg = json.loads(reply[-1].decode('utf-8'))
                            if ack_msg.get("ack") == self.msg_id_counter:
                                return # Success
            except Exception as e:
                pass
                
            retries -= 1
            if retries > 0:
                print(f"\n[Client] Text retry {3 - retries}/3...")
        
        print("\n[Client] Broker off-line! Procurando outro broker...")
        self.discover_and_connect()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", default="tcp://127.0.0.1:5555")
    parser.add_argument("--user", default="User" + str(uuid.uuid4())[:4])
    parser.add_argument("--room", default="ROOM_A")
    args = parser.parse_args()
    
    client = VideoConferenceClient(args.registry, args.user, args.room)
    try:
        client.start()
    except KeyboardInterrupt:
        print("\nExiting...")
        sys.exit(0)
