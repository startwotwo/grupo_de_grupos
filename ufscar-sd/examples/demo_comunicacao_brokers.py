import sys
import os
import time
import threading
import signal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from discovery import DiscoveryService
from broker import Broker
from client import Client

SEP = "=" * 60


def start_broker(broker_id, text_port, stop_event):
    def run():
        b = Broker(
            broker_id=broker_id,
            text_port=text_port,
            audio_pub_port=text_port + 1,
            video_pub_port=text_port + 2,
            control_port=text_port + 3,
            heartbeat_port=text_port + 5,
            audio_input_port=text_port + 6,
            video_input_port=text_port + 7,
            discovery_host='localhost',
            enable_clustering=True
        )
        b.start()
        while not stop_event.is_set():
            time.sleep(0.1)
        b.stop()
    t = threading.Thread(target=run, daemon=True)
    t.start()


def main():
    print(SEP)
    print("  Demo: Comunicação Entre Brokers")
    print(SEP)

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda s, f: stop.set())

    print("\n[1] Iniciando Discovery Service...")
    discovery = DiscoveryService()
    discovery.start()
    time.sleep(1)

    print("\n[2] Iniciando cluster com 3 brokers...")
    start_broker("broker1", 5556, stop)
    time.sleep(0.8)
    start_broker("broker2", 5656, stop)
    time.sleep(0.8)
    start_broker("broker3", 5756, stop)
    time.sleep(2)
    print("     brokers 1, 2 e 3 no ar e sincronizados.")

    print("\n[3] Conectando clientes (alice e bob)...")
    received_by_bob = []
    received_by_alice = []

    alice = Client(client_id="alice", discovery_host='localhost', enable_failover=True)
    bob = Client(client_id="bob", discovery_host='localhost', enable_failover=True)

    alice.on_text_received = lambda sid, txt, grp: received_by_alice.append((sid, txt))
    bob.on_text_received = lambda sid, txt, grp: received_by_bob.append((sid, txt))

    alice.start()
    time.sleep(0.5)
    bob.start()
    time.sleep(1.5)

    alice.join_group("A")
    time.sleep(0.3)
    bob.join_group("A")
    time.sleep(1)

    print("\n[4] Testando comunicação entre brokers...")
    print()

    msgs_alice = [
        "Olá Bob! Mensagem cruzando o cluster.",
        "Broker 1, 2 ou 3 — tanto faz, chega aqui.",
    ]
    msgs_bob = [
        "Recebi, Alice! Roteamento funcionando.",
        "Cluster distribuído em ação.",
    ]

    for msg in msgs_alice:
        print(f"     alice -> grupo A: \"{msg}\"")
        alice.send_text(msg)
        time.sleep(1.2)

    for msg in msgs_bob:
        print(f"     bob   -> grupo A: \"{msg}\"")
        bob.send_text(msg)
        time.sleep(1.2)

    time.sleep(1)

    print("\n[5] Resultado:")
    print(f"     Bob recebeu {len(received_by_bob)} mensagem(s) de alice:")
    for sid, txt in received_by_bob:
        print(f"       [{sid}] {txt}")

    print(f"\n     Alice recebeu {len(received_by_alice)} mensagem(s) de bob:")
    for sid, txt in received_by_alice:
        print(f"       [{sid}] {txt}")

    print("\n[6] Encerrando...")
    alice.stop()
    bob.stop()
    stop.set()
    time.sleep(0.5)
    discovery.stop()

    print()
    print(SEP)
    print("  Mensagens roteadas entre brokers com sucesso!")
    print(SEP)


if __name__ == "__main__":
    main()
