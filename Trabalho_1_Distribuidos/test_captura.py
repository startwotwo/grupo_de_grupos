"""
Testes para capturaImagemeAudio.

Rodar unitários (com mocks, sem webcam/mic):
    python -m unittest test_captura.py -v

Rodar teste manual (precisa de webcam e microfone):
    python test_captura.py --manual
"""
import sys
import threading
import queue
import time
import unittest
from unittest.mock import patch, MagicMock

import numpy as np

import client


class TestCapturaVideo(unittest.TestCase):
    def test_video_coloca_bytes_na_fila(self):
        fila = queue.Queue()
        parar = threading.Event()

        frame_falso = np.zeros((480, 640, 3), dtype=np.uint8)
        cap_mock = MagicMock()
        cap_mock.isOpened.return_value = True
        cap_mock.read.return_value = (True, frame_falso)

        with patch("client.cv2.VideoCapture", return_value=cap_mock):
            t = threading.Thread(
                target=client._captura_video, args=(fila, parar), daemon=True
            )
            t.start()
            time.sleep(0.3)
            parar.set()
            t.join(timeout=2)

        self.assertFalse(fila.empty(), "Fila de vídeo deveria ter pelo menos um frame")
        item = fila.get_nowait()
        self.assertIsInstance(item, bytes, "Frame na fila deve ser bytes (JPEG)")
        self.assertTrue(item.startswith(b"\xff\xd8"), "Deve ter header JPEG (FFD8)")
        cap_mock.release.assert_called_once()

    def test_video_webcam_indisponivel(self):
        fila = queue.Queue()
        parar = threading.Event()

        cap_mock = MagicMock()
        cap_mock.isOpened.return_value = False

        with patch("client.cv2.VideoCapture", return_value=cap_mock):
            client._captura_video(fila, parar)

        self.assertTrue(fila.empty(), "Fila deve ficar vazia se webcam falhar")

    def test_video_respeita_parar_evento(self):
        fila = queue.Queue()
        parar = threading.Event()

        frame_falso = np.zeros((480, 640, 3), dtype=np.uint8)
        cap_mock = MagicMock()
        cap_mock.isOpened.return_value = True
        cap_mock.read.return_value = (True, frame_falso)

        with patch("client.cv2.VideoCapture", return_value=cap_mock):
            t = threading.Thread(
                target=client._captura_video, args=(fila, parar), daemon=True
            )
            t.start()
            time.sleep(0.1)
            parar.set()
            t.join(timeout=2)

        self.assertFalse(t.is_alive(), "Thread deveria ter encerrado após parar_evento")


class TestCapturaAudio(unittest.TestCase):
    def test_audio_coloca_bytes_na_fila(self):
        fila = queue.Queue()
        parar = threading.Event()

        stream_mock = MagicMock()
        stream_mock.read.return_value = b"\x00\x01" * client.AUDIO_CHUNK

        pa_mock = MagicMock()
        pa_mock.open.return_value = stream_mock

        with patch("client.pyaudio.PyAudio", return_value=pa_mock):
            t = threading.Thread(
                target=client._captura_audio, args=(fila, parar), daemon=True
            )
            t.start()
            time.sleep(0.2)
            parar.set()
            t.join(timeout=2)

        self.assertFalse(fila.empty(), "Fila de áudio deveria ter pelo menos um chunk")
        item = fila.get_nowait()
        self.assertIsInstance(item, bytes, "Chunk de áudio deve ser bytes")
        stream_mock.close.assert_called_once()
        pa_mock.terminate.assert_called_once()

    def test_audio_microfone_indisponivel(self):
        fila = queue.Queue()
        parar = threading.Event()

        pa_mock = MagicMock()
        pa_mock.open.side_effect = OSError("Sem mic")

        with patch("client.pyaudio.PyAudio", return_value=pa_mock):
            client._captura_audio(fila, parar)

        self.assertTrue(fila.empty(), "Fila deve ficar vazia se mic falhar")
        pa_mock.terminate.assert_called_once()


class TestCapturaImagemeAudio(unittest.TestCase):
    def test_inicia_duas_subthreads(self):
        fila_v = queue.Queue()
        fila_a = queue.Queue()
        parar = threading.Event()

        chamadas = {"video": 0, "audio": 0}

        def fake_video(fila, evento):
            chamadas["video"] += 1
            evento.wait(timeout=1)

        def fake_audio(fila, evento):
            chamadas["audio"] += 1
            evento.wait(timeout=1)

        with patch("client._captura_video", side_effect=fake_video), \
             patch("client._captura_audio", side_effect=fake_audio):
            t = threading.Thread(
                target=client.capturaImagemeAudio,
                args=(None, fila_v, fila_a, parar),
                daemon=True,
            )
            t.start()
            time.sleep(0.2)
            parar.set()
            t.join(timeout=3)

        self.assertEqual(chamadas["video"], 1, "Deve chamar _captura_video 1x")
        self.assertEqual(chamadas["audio"], 1, "Deve chamar _captura_audio 1x")


def teste_manual():
    """Captura 3 segundos de vídeo/áudio real e mostra estatísticas."""
    print("=== TESTE MANUAL: abrindo webcam e microfone por 3 segundos ===")
    fila_v = queue.Queue()
    fila_a = queue.Queue()
    parar = threading.Event()

    t = threading.Thread(
        target=client.capturaImagemeAudio,
        args=(None, fila_v, fila_a, parar),
        daemon=True,
    )
    t.start()

    time.sleep(3)
    parar.set()
    t.join(timeout=3)

    n_video = fila_v.qsize()
    n_audio = fila_a.qsize()
    print(f"Frames de vídeo capturados: {n_video}")
    print(f"Chunks de áudio capturados: {n_audio}")

    if n_video > 0:
        frame = fila_v.get_nowait()
        print(f"  1º frame: {len(frame)} bytes, header={frame[:4].hex()}")
    if n_audio > 0:
        chunk = fila_a.get_nowait()
        print(f"  1º chunk áudio: {len(chunk)} bytes")

    ok = n_video > 0 and n_audio > 0
    print("RESULTADO:", "OK" if ok else "FALHOU (verifique webcam/microfone)")
    return ok


if __name__ == "__main__":
    if "--manual" in sys.argv:
        sys.exit(0 if teste_manual() else 1)
    unittest.main()
