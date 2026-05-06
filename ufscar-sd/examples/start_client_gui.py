import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from client import Client
from client.gui import VideoConferenceApp


def main():
    client_id = sys.argv[1] if len(sys.argv) > 1 else None
    broker_host = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("BROKER_HOST", "localhost")
    discovery_host = os.environ.get("DISCOVERY_HOST")

    kwargs = {"client_id": client_id, "broker_host": broker_host}
    if discovery_host:
        kwargs["discovery_host"] = discovery_host

    client = Client(**kwargs)

    if not client.connect():
        print("Falha ao conectar ao broker. Certifique-se de que o broker está rodando.")
        sys.exit(1)

    client.start()

    app = VideoConferenceApp(client)
    app.run()


if __name__ == "__main__":
    main()
