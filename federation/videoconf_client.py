"""Wrapper para rodar videoconf_dist client conectado ao broker federado.

videoconf_dist/src/shared/config.py tem portas hardcoded (5555/5556/5557).
A federação inicia o broker_central em 8000/8001/8002. Este wrapper
monkeypatcha o módulo config antes de importar os clients, sem modificar
o código do grupo.

Uso:
    python federation/videoconf_client.py Ivan A
"""
import os
import sys

VIDEOCONF_SRC = os.path.join(os.path.dirname(__file__), "..", "videoconf_dist", "src")
sys.path.insert(0, os.path.abspath(VIDEOCONF_SRC))

from shared import config as cfg
cfg.BROKER_HOST = "localhost"
cfg.PUBLISH_PORT = 8000
cfg.SUBSCRIBE_PORT = 8001
cfg.AUTH_PORT = 8002

import threading
from client.client_text import TextClient
from client.client_audio import AudioClient
from client.client_video import VideoClient, ClientConfig


def main():
    if len(sys.argv) < 3:
        print("Uso: python federation/videoconf_client.py <NOME> <SALA>")
        return

    user_name = sys.argv[1]
    room = sys.argv[2]

    print(f"[{user_name}] Iniciando videoconferência na sala {room}...")

    text_client = TextClient(user_name, room)
    audio_client = AudioClient(user_name, room)
    video_config = ClientConfig(
        user_id=user_name,
        room=room,
        broker_host=cfg.BROKER_HOST,
        video_pub_port=cfg.PUBLISH_PORT,
        video_sub_port=cfg.SUBSCRIBE_PORT,
        camera_index=getattr(cfg, "VIDEO_CAMERA_INDEX", 0),
    )
    video_client = VideoClient(video_config)

    if text_client.authenticate():
        print("Autenticação concluída!")
        threading.Thread(target=audio_client.start, daemon=True).start()
        video_client.start()
        text_client.start()
        video_client.stop()
    else:
        print("Encerrando cliente devido a falha no login.")


if __name__ == "__main__":
    main()
