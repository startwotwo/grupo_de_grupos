import subprocess
import time
import os

def run(name, cmd):
    print(f'Starting {name}: {cmd}')
    p = subprocess.Popen(cmd, shell=True)
    processes.append(p)
    time.sleep(2)
    return p

processes = []

# FED Core
run('FedReg', 'python federation/registry_fed.py')
run('SuperBroker', 'python federation/super_broker.py')

# 1. GrupoI (ports 5555 reg, 6000+ broker)
run('GI-Reg', 'cd "Sistemas_Distribuidos_GrupoI_Entrega1" && python registry.py')
run('GI-B1', 'cd "Sistemas_Distribuidos_GrupoI_Entrega1" && python broker.py --registry tcp://localhost:5555 --host localhost --port-base 6000')

# 2. GoogleMeet (5550 reg, 6555+ broker)
run('GM-Reg', 'cd GoogleMeet_Replica && python registry.py')
run('GM-B1', 'cd GoogleMeet_Replica && python broker.py --id BROKER1 --pub-port 6555')

# 3. Expansion (5570 disc, 6600+ B2)
run('Exp-Disc', 'python Expansion/discovery.py')
run('Exp-B2', 'python Expansion/broker.py --id B2 --base-port 6600')

# 4. ufscar-sd (7000+)
run('uf-Disc', 'cd ufscar-sd && python -m examples.start_discovery --port 7000')
run('uf-B', 'cd ufscar-sd && python -m examples.start_broker 7001')

# 5. sd-meeting-app (7500+)
run('meet-Reg', 'cd sd-meeting-app && python registry.py --port 7550')
run('meet-B', 'cd sd-meeting-app && python broker.py --port 7555')

# 6. T1SitemasDistribuidos (7700+)
run('T1-Reg', 'cd T1SitemasDistribuidos/discovery && python registry.py --port 7755')
run('T1-B', 'cd T1SitemasDistribuidos/broker && python broker.py --port 7700')

# 7. Trabalho_1 (7800+)
run('Trab1-Reg', 'echo no reg - broker standalone')
run('Trab1-B', 'cd Trabalho_1_Distribuidos && python broker.py --port 7800')

# 8. videoconf_dist (7900+ no docker)
run('video-B', 'cd videoconf_dist/src && python -m broker.broker_central --port 7900')

print('\n=== ALL 10+ GRUPOS UP ===')
print('Test ROOM_A:')
print('- GI: cd GrupoI && python client.py --user GI1 --room ROOM_A')
print('- GM: cd GoogleMeet && python client.py --identity GM1 --room ROOM_A')
print('- Exp: python Expansion/member.py --room A --id E1')
print('- UF: cd ufscar && python -m examples.demo_basic --room A')
print('- Meet: cd sd-meeting && python client.py --room A')
print('- Ctrl+C stop')
print('Cross msg all!')
input('Press Enter to shutdown...')
for p in processes:
    p.terminate()
    p.wait()

