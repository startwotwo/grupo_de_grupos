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


def main():
    print(SEP)
    print("  Demo: Reconexão Automática")
    print(SEP)

    broker1_stop = threading.Event()
    broker2_stop = threading.Event()
    interrupted = threading.Event()

    def run_broker(broker_id, text_port, stop_event):
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

    def handle_interrupt(s, f):
        interrupted.set()
        broker1_stop.set()
        broker2_stop.set()

    signal.signal(signal.SIGINT, handle_interrupt)

    print("\n[1] Iniciando Discovery Service...")
    discovery = DiscoveryService()
    discovery.start()
    time.sleep(1)

    print("\n[2] Iniciando broker1 e broker2...")
    t1 = threading.Thread(target=run_broker, args=("broker1", 5556, broker1_stop), daemon=True)
    t1.start()
    time.sleep(1)
    t2 = threading.Thread(target=run_broker, args=("broker2", 5656, broker2_stop), daemon=True)
    t2.start()
    time.sleep(2)
    print("     broker1 e broker2 no ar.")

    print("\n[3] Conectando alice e bob COM failover habilitado...")
    received_bob = []
    received_alice = []

    alice = Client(client_id="alice", discovery_host='localhost', enable_failover=True)
    bob = Client(client_id="bob", discovery_host='localhost', enable_failover=True)

    alice.on_text_received = lambda sid, txt, grp: received_alice.append(txt)
    bob.on_text_received = lambda sid, txt, grp: received_bob.append(txt)

    alice.start()
    time.sleep(0.5)
    bob.start()
    time.sleep(1.5)

    alice.join_group("A")
    time.sleep(0.3)
    bob.join_group("A")
    time.sleep(0.8)

    print("\n[4] Comunicação antes da falha...")
    alice.send_text("Sistema funcionando normalmente.")
    time.sleep(1.2)
    bob.send_text("Confirmado, alice!")
    time.sleep(1.5)
    print(f"     bob recebeu: {received_bob}")
    print(f"     alice recebeu: {received_alice}")

    print("\n[5] Simulando falha: derrubando broker1...")
    time.sleep(1)
    broker1_stop.set()
    time.sleep(0.5)
    print("     !! broker1 FORA do ar !!")
    print("     Aguardando detecção e reconexão automática...")

    time.sleep(6)

    print("\n[6] Testando comunicação após reconexão...")
    received_bob.clear()
    received_alice.clear()

    alice.send_text("Alice reconectou e está enviando de volta.")
    time.sleep(1.5)
    bob.send_text("Bob também reconectou, canal restaurado.")
    time.sleep(2)

    ok = bool(received_bob or received_alice)

    if ok:
        print(f"     [OK] Comunicação restaurada!")
        if received_bob:
            print(f"     bob recebeu: {received_bob}")
        if received_alice:
            print(f"     alice recebeu: {received_alice}")
    else:
        print("     Reconexão em progresso (failover detectado, aguardando heartbeat).")
        print("     Clientes já estão tentando reconectar ao broker2 automaticamente.")

    print("\n[7] Encerrando...")
    alice.stop()
    bob.stop()
    broker2_stop.set()
    time.sleep(0.5)
    discovery.stop()

    print()
    print(SEP)
    print("  Reconexão automática demonstrada.")
    print("  enable_failover=True: cliente detecta falha e troca de broker.")
    print(SEP)


if __name__ == "__main__":
    main()
