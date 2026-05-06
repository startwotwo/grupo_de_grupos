import subprocess
import time
import os
import signal
import sys

class FederationLauncher:
    def __init__(self):
        self.processes = []

    def run_cmd(self, cmd, name):
        print(f'Starting {name}: {cmd}')
        p = subprocess.Popen(cmd, shell=True)
        self.processes.append((p, name))
        return p

    def shutdown(self):
        print('Shutting down all processes...')
        for p, name in self.processes:
            print(f'Terminating {name}')
            p.terminate()
        for p, name in self.processes:
            p.wait()

    def demo(self):
        # Fed core
        self.run_cmd('python federation/registry_fed.py', 'FedRegistry')
        time.sleep(2)
        self.run_cmd('python federation/super_broker.py', 'SuperBroker')
        time.sleep(3)

        # GroupI
        self.run_cmd('cd Sistemas_Distribuidos_GrupoI_Entrega1 && python registry.py', 'GrupoI-Registry')
        time.sleep(1)
        self.run_cmd('cd Sistemas_Distribuidos_GrupoI_Entrega1 && python broker.py --registry tcp://localhost:5555 --host localhost --port-base 6000', 'GrupoI-Broker1')
        time.sleep(2)

        # GoogleMeet (adjust if ports conflict)
        self.run_cmd('cd GoogleMeet_Replica && python registry.py', 'GoogleMeet-Registry')
        time.sleep(1)
        self.run_cmd('cd GoogleMeet_Replica && python broker.py --id BROKER1 --pub-port 6555', 'GoogleMeet-Broker1')
        time.sleep(2)

        # Expansion
        self.run_cmd('python Expansion/discovery.py', 'Expansion-Discovery')
        time.sleep(1)
        self.run_cmd('python Expansion/broker.py --id B2 --base-port 6600', 'Expansion-B2')

        # sd-meeting-app
        self.run_cmd('cd sd-meeting-app && python registry.py', 'sd-meet-Reg')
        self.run_cmd('cd sd-meeting-app && python broker.py', 'sd-meet-B')

        # sd-trab1
        self.run_cmd('cd sd-trab1 && python registry.py', 'sd-trab1-Reg')
        self.run_cmd('cd sd-trab1 && python broker.py', 'sd-trab1-B')


        # sd-trabalho
        self.run_cmd('cd sd-trabalho/registry && python registry.py', 'sd-trab-Reg')
        self.run_cmd('cd sd-trabalho/broker && python broker_cluster.py', 'sd-trab-Clust')


        # Sistemas_Distribuidos_GrupoI_Entrega1

        # T1SitemasDistribuidos

        # Trabalho_1_Distribuidos

        # ufscar-sd
        self.run_cmd('cd ufscar-sd && python -m examples.start_discovery', 'ufscar-Disc')
        self.run_cmd('cd ufscar-sd && python -m examples.start_broker_cluster', 'ufscar-Clust')

        # videoconf_dist

        print('\\n=== FEDERATION READY ===')
        print('Run clients:')
        print('cd Sistemas_Distribuidos_GrupoI_Entrega1 && python client.py --user FedUser1 --room FED_ROOM')
        print('cd GoogleMeet_Replica && python gui.py')
        print('\\nCtrl+C to stop all')
        print('Test: Messages/video cross groups in FED_ROOM')

        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            self.shutdown()

if __name__ == '__main__':
    FederationLauncher().demo()

