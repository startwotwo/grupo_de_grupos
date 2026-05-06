import sys
import os
import time
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from broker import Broker
from client import Client


def run_client(client_id, group, messages):
    client = Client(client_id=client_id)
    client.start()

    time.sleep(1)

    client.join_group(group)
    time.sleep(0.5)

    for msg in messages:
        client.send_text(msg)
        time.sleep(0.5)

    time.sleep(2)
    client.stop()


def main():
    print("=" * 60)
    print("Demo: Sistema de Videoconferência Distribuído - Fase 1")
    print("=" * 60)
    print()

    print("[1] Starting broker...")
    broker = Broker(broker_id="demo_broker")
    broker.start()
    time.sleep(1)

    print("\n[2] Creating clients...")

    alice_thread = threading.Thread(
        target=run_client,
        args=("alice", "A", ["Hello from Alice!", "How is everyone?"])
    )

    bob_thread = threading.Thread(
        target=run_client,
        args=("bob", "A", ["Hi Alice!", "I'm good, thanks!"])
    )

    carol_thread = threading.Thread(
        target=run_client,
        args=("carol", "B", ["Anyone in group B?", "Hello?"])
    )

    print("\n[3] Starting clients...")
    alice_thread.start()
    time.sleep(0.5)
    bob_thread.start()
    time.sleep(0.5)
    carol_thread.start()

    print("\n[4] Waiting for clients to finish...")
    alice_thread.join()
    bob_thread.join()
    carol_thread.join()

    time.sleep(1)

    print("\n[5] Stopping broker...")
    broker.stop()

    print("\n" + "=" * 60)
    print("Demo completed!")
    print("=" * 60)


if __name__ == "__main__":
    main()
