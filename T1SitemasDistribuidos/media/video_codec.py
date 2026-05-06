# media/video_codec.py
import cv2
import numpy as np

def encode_frame(frame: np.ndarray, quality=50) -> bytes:
    """
    Converte frame BGR do OpenCV para bytes JPEG com qualidade adaptativa.
    quality: 0-100 (mais alto = melhor qualidade e maior banda)
    """
    # Redimensionamento opcional para economizar ainda mais banda em conexões ruins
    if quality < 20:
        h, w = frame.shape[:2]
        frame = cv2.resize(frame, (w // 2, h // 2))
        
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError("Falha ao codificar frame")
    return buf.tobytes()

def decode_frame(data: bytes) -> np.ndarray:
    """Converte bytes JPEG de volta para frame BGR."""
    arr = np.frombuffer(data, dtype=np.uint8)
    frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if frame is None:
        raise RuntimeError("Falha ao decodificar frame")
    return frame