# client/capture.py
import threading
import queue
import time
import logging
import numpy as np

log = logging.getLogger(__name__)

# Importação condicional do pyaudio (pode não ter microfone no ambiente)
try:
    import pyaudio
    AUDIO_OK = True
except ImportError:
    AUDIO_OK = False
    log.warning("[CAPTURE] pyaudio não disponível — áudio desativado")

try:
    import cv2
    VIDEO_OK = True
except ImportError:
    VIDEO_OK = False
    log.warning("[CAPTURE] opencv não disponível — vídeo desativado")

from media.audio_codec import RATE, CHANNELS, CHUNK
from media.video_codec import encode_frame

class CaptureManager:
    """
    Gerencia captura de câmera e microfone em threads separadas.
    Produz frames/audio em filas para o Sender consumir.
    """

    def __init__(self, video_queue: queue.Queue, audio_queue: queue.Queue):
        self.video_q   = video_queue
        self.audio_q   = audio_queue
        self._running  = False
        self._threads  = []
        self.video_enabled = True
        self.audio_enabled = True

    # ------------------------------------------------------------------
    # Vídeo
    # ------------------------------------------------------------------
    def _capture_video(self):
        if not VIDEO_OK:
            return
            
        import os
        os.environ["OPENCV_VIDEOIO_LOG_LEVEL"] = "0"
        
        cap = None
        
        # Frames de fallback
        placeholder = np.zeros((480, 640, 3), dtype=np.uint8)
        cv2.putText(placeholder, "Camera em uso / Bloqueada", (100, 240), 
                    cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
        
        disabled_frame = np.zeros((480, 640, 3), dtype=np.uint8)
        cv2.putText(disabled_frame, "Camera Desativada", (150, 240), 
                    cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2)

        while self._running:
            if not self.video_enabled:
                if cap:
                    cap.release()
                    cap = None
                frame = disabled_frame
                time.sleep(0.1)
            else:
                if cap is None:
                    cap = cv2.VideoCapture(0)
                    if not cap.isOpened():
                        cap = None # Falha ao abrir
                
                if cap:
                    ok, frame = cap.read()
                    if not ok:
                        cap.release()
                        cap = None
                        frame = placeholder
                    # Sucesso!
                else:
                    frame = placeholder
                    time.sleep(0.1)

            try:
                q_size = self.video_q.qsize()
                quality = 15 if q_size > 7 else (30 if q_size > 4 else 60)
                data = encode_frame(frame, quality=quality)
                if self.video_q.full():
                    try: self.video_q.get_nowait()
                    except: pass
                self.video_q.put_nowait(data)
            except Exception as e:
                log.error(f"[CAPTURE] Erro vídeo: {e}")
                
        if cap:
            cap.release()
        log.info("[CAPTURE] Thread de vídeo encerrada")

    # ------------------------------------------------------------------
    # Áudio
    # ------------------------------------------------------------------
    def _capture_audio(self):
        if not AUDIO_OK:
            return
        
        pa = pyaudio.PyAudio()
        stream = None
        
        while self._running:
            if not self.audio_enabled:
                if stream:
                    stream.stop_stream()
                    stream.close()
                    stream = None
                # Envia silêncio
                pcm = b'\x00' * (CHUNK * 2) 
                time.sleep(CHUNK / RATE)
            else:
                if stream is None:
                    try:
                        stream = pa.open(
                            format=pyaudio.paInt16,
                            channels=CHANNELS,
                            rate=RATE,
                            input=True,
                            frames_per_buffer=CHUNK,
                        )
                    except:
                        stream = None
                
                if stream:
                    try:
                        pcm = stream.read(CHUNK, exception_on_overflow=False)
                    except:
                        pcm = b'\x00' * (CHUNK * 2)
                else:
                    pcm = b'\x00' * (CHUNK * 2)
                    time.sleep(0.1)

            try:
                if self.audio_q.full():
                    try: self.audio_q.get_nowait()
                    except: pass
                self.audio_q.put_nowait(pcm)
            except Exception as e:
                log.error(f"[CAPTURE] Erro áudio: {e}")
                
        if stream:
            stream.stop_stream()
            stream.close()
        pa.terminate()
        log.info("[CAPTURE] Microfone encerrado")

    # ------------------------------------------------------------------
    # Start / stop
    # ------------------------------------------------------------------
    def start(self):
        self._running = True
        tv = threading.Thread(target=self._capture_video, daemon=True, name="capture-video")
        ta = threading.Thread(target=self._capture_audio, daemon=True, name="capture-audio")
        self._threads = [tv, ta]
        for t in self._threads:
            t.start()

    def stop(self):
        self._running = False
        log.info("[CAPTURE] Captura encerrada")
