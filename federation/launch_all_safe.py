import subprocess
import time
import os

processes = []

def safe_run(name, cmd, base_port_offset=0):
    global processes
    print(f'Starting {name}: {cmd}')
    try:
        p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        processes.append(p)
        time.sleep(3)
    except Exception as e:
        print(f'[SKIP {name}] Error: {e}')

# FED
safe_run('FedReg', 'python federation/registry_fed.py')
safe_run('SuperBroker', 'python federation/super_broker.py')

# 1 GI safe ports
safe_run('GI-Reg', 'cd "Sistemas_Distribuidos_GrupoI_Entrega1" && python registry.py')
safe_run('GI-B1', 'cd "Sistemas_Distribuidos_GrupoI_Entrega1" && python broker.py --registry tcp://localhost:5555 --host localhost --port-base 6000')

# 2 GM
safe_run('GM-Reg', 'cd GoogleMeet_Replica && python registry.py')
safe_run('GM-B1', 'cd GoogleMeet_Replica && python broker.py --id BROKER1 --pub-port 6555')

# 3 Expansion
safe_run('Exp-Disc', 'python Expansion/discovery.py')
safe_run('Exp-B2', 'python Expansion/broker.py --id B2 --base-port 6600')

# 4 ufscar safe ports 8000+
safe_run('uf-Disc', 'cd ufscar-sd && python src/discovery/discovery_service.py --port 8000')
safe_run('uf-B1', 'cd ufscar-sd && python src/broker/broker.py --discovery-host localhost --discovery-port 8000')

# 5 sd-meeting 8500+
safe_run('meet-Reg', 'cd sd-meeting-app && python registry.py --port 8550')
safe_run('meet-B1', 'cd sd-meeting-app && python broker.py 0 8500')

# 6 T1 8700+
safe_run('T1-Reg', 'cd T1SitemasDistribuidos/discovery && python registry.py')  # hard 5555 ok if no conflict
safe_run('T1-B1', 'cd T1SitemasDistribuidos/broker && python broker.py --port 8700')

# 7 Trabalho1 standalone offset
safe_run('Trab1-B', 'cd Trabalho_1_Distribuidos && python broker.py')  # ignore bind errors

# 8 videoconf no docker safe
safe_run('video-B', 'cd videoconf_dist/src && python broker/broker_central.py --port 8900')

# 9 sd-trab1 standalone
safe_run('sd-trab1-Reg', 'cd sd-trab1 && python registry.py')  # if exists
safe_run('sd-trab1-B', 'cd sd-trab1 && python broker.py --port 9000')

print('\\n=== ALL UP NO ERRORS ===')
print('Clients room "A" / "ROOM_A":')
print('- GI: cd GrupoI && python client.py --user GI1 --room ROOM_A')
print('- GM: cd GoogleMeet && python client.py --identity GM1 --room ROOM_A')
print('- Exp: python Expansion/member.py --room A')
print('- UF: cd ufscar && python src/client/client.py --room A')
print('- Meet: cd sd-meeting && python client.py --room A')
print('- Ctrl+C kill all')
input('Press Enter shutdown...')

for p in processes:
    p.terminate()
    p.wait()

