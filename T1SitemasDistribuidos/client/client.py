import logging
import threading
import time
import queue
import sys
import os
import argparse

# Adiciona o diretório raiz ao path para importações
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from identity.session import Session
from capture import CaptureManager
from sender import Sender
from receiver import Receiver
from ui import UI
from media.video_codec import decode_frame
from media.audio_codec import RATE, CHANNELS, CHUNK

# Configuração de log
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

try:
    import pyaudio
    AUDIO_OK = True
except ImportError:
    AUDIO_OK = False

class VideoConferenceClient:
    def __init__(self, nome, sala, use_camera=True, registry_host="localhost"):
        self.session = Session(nome, sala, on_reconnect=self._on_broker_reconnect, registry_host=registry_host)
        self.use_camera = use_camera
        
        self.video_send_q = queue.Queue(maxsize=10)
        self.audio_send_q = queue.Queue(maxsize=50)
        self.text_send_q  = queue.Queue(maxsize=10)
        
        self.capture = CaptureManager(self.video_send_q, self.audio_send_q)
        self.ui = UI(capture_manager=self.capture)
        self.sender = None
        self.receiver = None
        
        self.running = False
        
        # Audio Playback
        if AUDIO_OK:
            self.pa = pyaudio.PyAudio()
            self.audio_stream = self.pa.open(
                format=pyaudio.paInt16,
                channels=CHANNELS,
                rate=RATE,
                output=True,
                frames_per_buffer=CHUNK
            )
        else:
            self.audio_stream = None

    def start(self):
        if not self.session.login():
            log.error("Nenhum broker encontrado.")
            return

        # self.session.broker_info contém o 'host' retornado pelo broker
        self.sender = Sender(self.session.context, self.session.broker_info)
        self.receiver = Receiver(
            self.session.context, self.session.broker_info,
            self.on_video_received, self.on_audio_received, self.on_text_received
        )

        self.running = True
        self.capture.start()
        self.receiver.start()
        threading.Thread(target=self._send_loop, daemon=True).start()
        log.info("Cliente iniciado com sucesso!")
        
        threading.Thread(target=self.terminal_chat_loop, daemon=True).start()
        self.ui.start() # Bloqueia aqui na Main Thread

    def _on_ui_close(self):
        self.stop()

    def _send_loop(self):
        while self.running:
            try:
                if not self.video_send_q.empty():
                    self.sender.send_video(self.video_send_q.get(), self.session.nome)
                if not self.audio_send_q.empty():
                    self.sender.send_audio(self.audio_send_q.get())
                if not self.text_send_q.empty():
                    self.sender.send_text(self.text_send_q.get(), self.session.nome)
            except Exception as e:
                log.error(f"[CLIENTE] Erro no send_loop: {e}")
            import time
            time.sleep(0.01)

    def on_video_received(self, user_id, data):
        try:
            from media.video_codec import decode_frame
            frame = decode_frame(data)
            self.ui.display_video(user_id, frame)
        except Exception as e:
            log.error(f"Erro decod vídeo: {e}")

    def on_audio_received(self, data):
        if self.audio_stream:
            try:
                self.audio_stream.write(data)
            except Exception as e:
                log.error(f"Erro audio playback: {e}")

    def on_text_received(self, text):
        print(f"\n[CHAT] {text}")
        print("Mensagem (ou '/sair'): ", end="", flush=True)

    def send_chat(self, text):
        if not self.text_send_q.full():
            self.text_send_q.put(text)

    def terminal_chat_loop(self):
        while self.running:
            try:
                txt = input("Mensagem (ou '/sair'): ")
                if txt == "/sair":
                    self.stop()
                    break
                if txt:
                    self.send_chat(txt)
            except EOFError:
                break
        self.stop()

    def run_interactive(self):
        try:
            self.start()
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def _on_broker_reconnect(self, new_broker_info):
        """Chamado pela Session quando o failover ocorre. Recria sockets no novo broker."""
        log.info("[CLIENTE] Recriando sockets de mídia no novo broker...")
        # Para os sockets antigos sem matar as threads de captura
        if self.sender:
            self.sender.stop()
        if self.receiver:
            self.receiver.stop()
        # Pequena pausa para os sockets ZMQ fecharem
        import time as _time
        _time.sleep(0.5)
        # Recria com o novo broker, mantendo a sala original
        self.sender = Sender(self.session.context, new_broker_info)
        self.receiver = Receiver(
            self.session.context, new_broker_info,
            self.on_video_received, self.on_audio_received, self.on_text_received
        )
        self.receiver.start()
        log.info(f"[CLIENTE] Sockets recriados. Sala '{self.session.sala}' mantida!")

    def stop(self):
        self.running = False
        if self.use_camera:
            self.capture.stop()
        if self.sender:
            self.sender.stop()
        if self.receiver:
            self.receiver.stop()
        self.session.logout()
        log.info("Cliente encerrado.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cliente de Videoconferência")
    parser.add_argument("--no-camera", action="store_true", dest="no_camera",
                        help="Inicia sem capturar vídeo da webcam")
    parser.add_argument("--nome", type=str, default=None,
                        help="Seu nome de exibição")
    parser.add_argument("--sala", type=str, default=None,
                        help="Sala (A-K)")
    parser.add_argument("--registry", type=str, default=None,
                        help="IP do Registry (ex: 192.168.1.10)")
    args = parser.parse_args()
    
    nome = args.nome or input("Seu nome: ") or "User"
    sala = (args.sala or input("Sala (A-K): ") or "A").upper()
    registry_ip = args.registry or input("IP do Servidor (Enter para localhost): ") or "localhost"
    
    client = VideoConferenceClient(nome, sala, use_camera=not args.no_camera, registry_host=registry_ip)
    client.run_interactive()

