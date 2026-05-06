import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from client import Client
from client.gui import VideoConferenceApp


def main():
    client_id = sys.argv[1] if len(sys.argv) > 1 else None
    broker_host = sys.argv[2] if len(sys.argv) > 2 else "localhost"

    client = Client(client_id=client_id, broker_host=broker_host)

    if not client.connect():
        print("Falha ao conectar ao broker. Certifique-se de que o broker está rodando.")
        sys.exit(1)

    client.start()

    app = VideoConferenceApp(client)
    app.run()


if __name__ == "__main__":
    main()
