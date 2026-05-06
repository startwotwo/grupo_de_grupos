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
    print("  Demo: Falha de Broker")
    print(SEP)

    broker1_stop = threading.Event()
    broker2_stop = threading.Event()

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

    signal.signal(signal.SIGINT, lambda s, f: (broker1_stop.set(), broker2_stop.set()))

    print("\n[1] Iniciando Discovery Service...")
    discovery = DiscoveryService()
    discovery.start()
    time.sleep(1)

    print("\n[2] Iniciando apenas broker1...")
    t1 = threading.Thread(target=run_broker, args=("broker1", 5556, broker1_stop), daemon=True)
    t1.start()
    time.sleep(2)
    print("     broker1 no ar.")

    print("\n[3] Conectando alice e bob ao broker1 (único disponível)...")
    received_bob = []
    received_alice = []

    alice = Client(client_id="alice", discovery_host='localhost', enable_failover=False)
    bob = Client(client_id="bob", discovery_host='localhost', enable_failover=False)

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

    print("\n[4] Sistema funcionando — trocando mensagens...")
    alice.send_text("Tudo funcionando com broker1.")
    time.sleep(1.2)
    bob.send_text("Confirmado, alice!")
    time.sleep(1.5)
    print(f"     bob recebeu: {received_bob}")
    print(f"     alice recebeu: {received_alice}")

    print("\n[5] Simulando falha: derrubando broker1 abruptamente...")
    time.sleep(1)
    broker1_stop.set()
    time.sleep(0.5)
    print("     !! broker1 está FORA do ar !!")

    print("\n[6] Tentando enviar mensagens (sem failover)...")
    received_bob.clear()
    received_alice.clear()

    alice.send_text("Alguém aí? broker1 caiu.")
    time.sleep(2)
    bob.send_text("Bob tentando alcançar alice...")
    time.sleep(2)

    if not received_bob and not received_alice:
        print("     Nenhuma mensagem entregue — broker1 fora do ar, sem failover.")
    else:
        print(f"     Mensagens entregues (inesperado): bob={received_bob} alice={received_alice}")

    print("\n[7] Subindo broker2 e demonstrando que o serviço continua para novos clientes...")
    t2 = threading.Thread(target=run_broker, args=("broker2", 5656, broker2_stop), daemon=True)
    t2.start()
    time.sleep(2)

    carol = Client(client_id="carol", discovery_host='localhost', enable_failover=False)
    carol_received = []
    carol.on_text_received = lambda sid, txt, grp: carol_received.append(txt)
    carol.start()
    time.sleep(1.5)
    carol.join_group("A")
    time.sleep(0.8)
    carol.send_text("Carol aqui, conectada ao broker2.")
    time.sleep(2)

    print(f"     carol (broker2) enviou mensagem. Sistema parcialmente operacional.")

    print("\n[8] Encerrando...")
    alice.stop()
    bob.stop()
    carol.stop()
    broker2_stop.set()
    time.sleep(0.5)
    discovery.stop()

    print()
    print(SEP)
    print("  Falha de broker simulada com sucesso.")
    print("  Clientes sem failover perdem conexão quando o broker cai.")
    print(SEP)


if __name__ == "__main__":
    main()
