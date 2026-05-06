# client/receiver.py
import zmq
import logging
import threading
import time

log = logging.getLogger(__name__)

class Receiver:
    def __init__(self, context, broker_info, on_video, on_audio, on_text):
        self.context = context
        self.broker_info = broker_info
        self.host = broker_info.get("host", "localhost")
        self.sala = broker_info.get("sala", "A")
        
        self.on_video = on_video
        self.on_audio = on_audio
        self.on_text  = on_text
        
        self.v_sock = self.context.socket(zmq.SUB)
        self.v_sock.connect(f"tcp://{self.host}:{broker_info['ports']['video_out']}")
        self.v_sock.setsockopt(zmq.SUBSCRIBE, self.sala.encode())
        
        self.a_sock = self.context.socket(zmq.SUB)
        self.a_sock.connect(f"tcp://{self.host}:{broker_info['ports']['audio_out']}")
        self.a_sock.setsockopt(zmq.SUBSCRIBE, self.sala.encode())
        
        self.t_sock = self.context.socket(zmq.SUB)
        self.t_sock.connect(f"tcp://{self.host}:{broker_info['ports']['text_out']}")
        self.t_sock.setsockopt(zmq.SUBSCRIBE, self.sala.encode())
        
        self.running = False

    def _loop_video(self):
        while self.running:
            try:
                if self.v_sock.poll(500):
                    msg = self.v_sock.recv_multipart()
                    if len(msg) == 3:
                        topic, user_id, data = msg
                        self.on_video(user_id.decode(), data)
                    elif len(msg) == 2:
                        topic, data = msg
                        self.on_video("Desconhecido", data)
            except: break

    def _loop_audio(self):
        while self.running:
            try:
                if self.a_sock.poll(500):
                    topic, data = self.a_sock.recv_multipart()
                    self.on_audio(data)
            except: break

    def _loop_text(self):
        while self.running:
            try:
                if self.t_sock.poll(500):
                    topic, data = self.t_sock.recv_multipart()
                    self.on_text(data.decode())
            except: break

    def start(self):
        self.running = True
        threading.Thread(target=self._loop_video, daemon=True).start()
        threading.Thread(target=self._loop_audio, daemon=True).start()
        threading.Thread(target=self._loop_text, daemon=True).start()

    def stop(self):
        self.running = False
        self.v_sock.close()
        self.a_sock.close()
        self.t_sock.close()
