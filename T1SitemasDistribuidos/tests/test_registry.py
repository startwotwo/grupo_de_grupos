# tests/test_registry.py
import sys
import os
import time
import subprocess
import zmq

# Adiciona o diretório raiz ao path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def test_registry_flow():
    print("\n--- Teste 1: Registro e Descoberta ---")
    
    # 1. Iniciar Registry
    print("[TEST] Iniciando Registry...")
    registry_proc = subprocess.Popen([sys.executable, "discovery/registry.py"])
    time.sleep(1)
    
    context = zmq.Context()
    reg_sock = context.socket(zmq.REQ)
    reg_sock.connect("tcp://localhost:5555")
    
    try:
        # 2. Testar registro de broker
        print("[TEST] Registrando broker fictício...")
        reg_sock.send_json({"action": "register", "address": "localhost:9999"})
        if reg_sock.poll(2000):
            resp = reg_sock.recv_json()
            assert resp["status"] == "ok"
            print("[OK] Broker registrado com sucesso.")
        else:
            raise Exception("Timeout esperando resposta do Registry")
        
        # 3. Testar listagem de brokers
        print("[TEST] Solicitando lista de brokers...")
        reg_sock.send_json({"action": "list_brokers"})
        resp = reg_sock.recv_json()
        assert "localhost:9999" in resp["brokers"]
        print(f"[OK] Lista de brokers correta: {resp['brokers']}")
        
        # 4. Testar obtenção de broker para cliente
        print("[TEST] Solicitando broker para cliente...")
        reg_sock.send_json({"action": "get_broker"})
        resp = reg_sock.recv_json()
        assert resp["address"] == "localhost:9999"
        print(f"[OK] Cliente recebeu broker correto: {resp['address']}")
        return True
        
    finally:
        print("[TEST] Finalizando processos do teste 1...")
        reg_sock.close()
        registry_proc.kill()
        context.term()

if __name__ == "__main__":
    try:
        if test_registry_flow():
            print("\n--- TESTE DE REGISTRO PASSOU! ---")
            sys.exit(0)
        else:
            sys.exit(1)
    except Exception as e:
        print(f"\n[ERRO] Teste falhou: {e}")
        sys.exit(1)
