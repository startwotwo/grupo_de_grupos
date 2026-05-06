import sys
import os
import time
import threading
import signal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from discovery import DiscoveryService
from broker import Broker
from client import Client


def run_broker(broker_id, text_port, control_port, heartbeat_port, stop_event):
    broker = Broker(
        broker_id=broker_id,
        text_port=text_port,
        control_port=control_port,
        heartbeat_port=heartbeat_port,
        discovery_host='localhost',
        enable_clustering=True
    )
    broker.start()

    while not stop_event.is_set():
        time.sleep(0.1)

    broker.stop()


def run_client(client_id, group, messages, stop_event):
    client = Client(
        client_id=client_id,
        discovery_host='localhost',
        enable_failover=True
    )
    client.start()

    time.sleep(2)

    if not stop_event.is_set():
        client.join_group(group)
        time.sleep(1)

    for msg in messages:
        if stop_event.is_set():
            break
        client.send_text(msg)
        time.sleep(1)

    while not stop_event.is_set():
        time.sleep(0.1)

    client.stop()


def main():
    print("=" * 70)
    print("Demo: Cluster de Brokers com Tolerância a Falhas")
    print("=" * 70)
    print()

    stop_event = threading.Event()

    def signal_handler(sig, frame):
        print("\n\nParando demo...")
        stop_event.set()

    signal.signal(signal.SIGINT, signal_handler)

    print("[1] Iniciando Discovery Service...")
    discovery = DiscoveryService()
    discovery.start()
    time.sleep(1)

    print("\n[2] Iniciando 3 brokers...")

    broker1_thread = threading.Thread(
        target=run_broker,
        args=("broker1", 5556, 5559, 5561, stop_event),
        daemon=True
    )

    broker2_thread = threading.Thread(
        target=run_broker,
        args=("broker2", 5656, 5659, 5661, stop_event),
        daemon=True
    )

    broker3_thread = threading.Thread(
        target=run_broker,
        args=("broker3", 5756, 5759, 5761, stop_event),
        daemon=True
    )

    broker1_thread.start()
    time.sleep(1)
    broker2_thread.start()
    time.sleep(1)
    broker3_thread.start()
    time.sleep(2)

    print("\n[3] Iniciando clientes...")

    alice_thread = threading.Thread(
        target=run_client,
        args=("alice", "A", ["Olá pessoal, tudo bem?", "Testando o cluster distribuído..."], stop_event),
        daemon=True
    )

    bob_thread = threading.Thread(
        target=run_client,
        args=("bob", "A", ["Oi Alice! Funcionando perfeitamente!", "O cluster está ótimo!"], stop_event),
        daemon=True
    )

    carol_thread = threading.Thread(
        target=run_client,
        args=("carol", "B", ["Alguém no grupo B?"], stop_event),
        daemon=True
    )

    alice_thread.start()
    time.sleep(1)
    bob_thread.start()
    time.sleep(1)
    carol_thread.start()

    print("\n[4] Sistema rodando...")
    print("    - 3 brokers cooperando")
    print("    - 3 clientes conectados")
    print("    - Mensagens sendo roteadas pelo cluster")
    print("\nPressione Ctrl+C para parar a demo (ou aguarde 15s)\n")

    stop_event.wait(timeout=15)
    stop_event.set()

    print("\n[5] Parando discovery...")
    discovery.stop()

    print("\n" + "=" * 70)
    print("Demo concluída!")
    print("=" * 70)


if __name__ == "__main__":
    main()
