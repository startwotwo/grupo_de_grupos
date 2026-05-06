import sys
import threading
from shared import config as cfg
from .client_text import TextClient
from .client_audio import AudioClient
from .client_video import VideoClient, ClientConfig


def main():
    if len(sys.argv) < 3:
        print("Uso: python client_main.py <SEU_NOME> <SALA> [--no-video] [--no-audio]")
        return

    user_name = sys.argv[1]
    room = sys.argv[2]
    
    no_video = "--no-video" in sys.argv
    no_audio = "--no-audio" in sys.argv
    
    broker_host = getattr(cfg, 'BROKER_HOST', 'localhost')
    pub_port = getattr(cfg, 'PUBLISH_PORT', 5555)
    sub_port = getattr(cfg, 'SUBSCRIBE_PORT', 5556)
    camera_index = getattr(cfg, 'VIDEO_CAMERA_INDEX', 0)

    print(f"[{user_name}] Iniciando videoconferência na sala {room}...")

    text_client = TextClient(user_name, room)
    
    if text_client.authenticate():
        print("Autenticação concluída!")
        
        threads = []
        
        if not no_audio:
            audio_client = AudioClient(user_name, room)
            audio_thread = threading.Thread(target=audio_client.start, daemon=True)
            audio_thread.start()
            threads.append(audio_thread)
        
        if not no_video:
            video_config = ClientConfig(
                user_id=user_name,
                room=room,
                broker_host=broker_host,
                video_pub_port=pub_port,
                video_sub_port=sub_port,
                camera_index=camera_index
            )
            video_client = VideoClient(video_config)
            video_client.start()
        
        text_client.start()
        
        if not no_video:
            video_client.stop()
        
    else:
        print("Encerrando cliente devido a falha no login.")

if __name__ == "__main__":
    main()
