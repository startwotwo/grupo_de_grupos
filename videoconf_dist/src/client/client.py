import sys
import threading
from shared import config as cfg
from client.client_text import TextClient
from client.client_audio import AudioClient
from client.client_video import VideoClient, ClientConfig


def main():
    if len(sys.argv) < 3:
        print("Uso: python client_main.py <SEU_NOME> <SALA>")
        return

    user_name = sys.argv[1]
    room = sys.argv[2]
    
    broker_host = getattr(cfg, 'BROKER_HOST', 'localhost')
    pub_port = getattr(cfg, 'PUBLISH_PORT', 5555)
    sub_port = getattr(cfg, 'SUBSCRIBE_PORT', 5556)
    camera_index = getattr(cfg, 'VIDEO_CAMERA_INDEX', 0)

    print(f"[{user_name}] Iniciando videoconferência na sala {room}...")

    # Instancia os dois clientes
    text_client = TextClient(user_name, room)
    audio_client = AudioClient(user_name, room)
    video_config = ClientConfig(
        user_id=user_name,
        room=room,
        broker_host=broker_host,
        video_pub_port=pub_port,
        video_sub_port=sub_port,
        camera_index=camera_index
    )
    video_client = VideoClient(video_config)

    # Autenticação
    if text_client.authenticate():
        print("Autenticação concluída!")
        
        # Áudio e Vídeo em segundo plano
        audio_thread = threading.Thread(target=audio_client.start, daemon=True)
        audio_thread.start()
        video_client.start()
        
        # Texto em primeiro plano
        text_client.start()
        
        video_client.stop()
        
    else:
        print("Encerrando cliente devido a falha no login.")

if __name__ == "__main__":
    main()