# test_capture.py
import queue, time, logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

from client.capture import CaptureManager

vq = queue.Queue(maxsize=10)
aq = queue.Queue(maxsize=20)

cap = CaptureManager(vq, aq)
cap.start()

print("Capturando por 5 segundos...")
time.sleep(5)

print(f"\nFrames de vídeo capturados: {vq.qsize()}")
print(f"Chunks de áudio capturados:  {aq.qsize()}")

cap.stop()