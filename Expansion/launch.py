# launcher.py
import subprocess
import time
import sys
import socket
from common import DISCOVERY_PORT

processes = []

HOST_IP = socket.gethostbyname(socket.gethostname())

def start(name, cmd):
    print(f"[launcher] iniciando {name}...")
    p = subprocess.Popen(
        ["python"] + cmd,
        creationflags=subprocess.CREATE_NEW_CONSOLE  # janela separada (Windows)
    )
    processes.append((name, p))
    return p

try:
    discovery = start("discovery", ["discovery.py"])
    time.sleep(2)
    b1 = start("B1", ["broker.py", "--id", "B1", "--base-port", "5555"])
    time.sleep(2)
    b2 = start("B2", ["broker.py", "--id", "B2", "--base-port", "5575"])
    time.sleep(2)
    start("Ivan", ["member.py", "--id", "Ivan", "--room", "A", "--discovery", f"tcp://{HOST_IP}:{DISCOVERY_PORT}"])
    start("Samuel", ["member.py", "--id", "Samuel", "--room", "A", "--discovery", f"tcp://{HOST_IP}:{DISCOVERY_PORT}"])
    start("Fuji", ["member.py", "--id", "Fuji", "--room", "A", "--discovery", f"tcp://{HOST_IP}:{DISCOVERY_PORT}"])

    input("\nPressione ENTER para matar B1 (failover demo)...")
    b1.terminate()
    print("[launcher] B1 morto. Observe os clientes migrando.")

    input("\nPressione ENTER para encerrar tudo...")
finally:
    for name, p in processes:
        try: p.terminate()
        except: pass
    print("[launcher] tudo encerrado.")