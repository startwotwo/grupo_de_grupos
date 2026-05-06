import zmq
import json
import time
import threading
from typing import Dict, Optional

REGISTRY_FED_PORT = 7777
GROUP_TIMEOUT = 10  # seg

class RegistryFed:
    def __init__(self, host='*', port=REGISTRY_FED_PORT):
        self.context = zmq.Context()
        self.rep = self.context.socket(zmq.REP)
        self.rep.bind(f'tcp://{host}:{port}')
        self.groups: Dict[str, Dict] = {}  # {group_name: {'registry_host': str, 'last_seen': float}}
        self.lock = threading.Lock()
        self.running = True
        print(f'[FedRegistry] Online tcp://{host}:{port}')

    def _watchdog(self):
        while self.running:
            now = time.time()
            with self.lock:
                dead = [g for g, info in self.groups.items() if now - info['last_seen'] > GROUP_TIMEOUT]
                for g in dead:
                    del self.groups[g]
                    print(f'[FedRegistry] Grupo {g} offline')
            time.sleep(5)

    def start(self):
        threading.Thread(target=self._watchdog, daemon=True).start()
        while self.running:
            msg = self.rep.recv_json()
            if msg['type'] == 'RegisterGroup':
                group = msg['group']
                host = msg['registry_host']
                with self.lock:
                    self.groups[group] = {'registry_host': host, 'last_seen': time.time()}
                self.rep.send_json({'status': 'ok'})
                print(f'[FedRegistry] Grupo {group} em {host} registrado')
            elif msg['type'] == 'GetGroups':
                with self.lock:
                    active = {g: info['registry_host'] for g, info in self.groups.items()}
                self.rep.send_json({'status': 'ok', 'groups': active})
            else:
                self.rep.send_json({'status': 'error'})
        self.rep.close()
        self.context.term()

if __name__ == '__main__':
    RegistryFed().start()

