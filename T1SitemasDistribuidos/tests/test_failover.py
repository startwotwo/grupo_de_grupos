# tests/test_failover.py
import sys
import os
import time
import subprocess
import zmq

# IMPORTANTE: sys.path.append ANTES dos imports do projeto
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from identity.session import Session

def test_failover_logic():
    print("\n--- Teste 3: Tolerância a Falhas (Failover) ---")
    
    # 1. Iniciar Registry e Broker A (Portas seguras)
    print("[TEST] Iniciando ambiente inicial (Porta 7000)...")
    reg_proc = subprocess.Popen([sys.executable, "discovery/registry.py"])
    time.sleep(1)
    b1_proc = subprocess.Popen([sys.executable, "broker/broker.py", "--port", "7000"])
    time.sleep(2)
    
    # 2. Cliente faz Login
    print("[TEST] Cliente conectando ao Broker A...")
    sess = Session("TestUser", "A")
    if not sess.login():
        print("[ERRO] Falha no login inicial")
        reg_proc.kill()
        b1_proc.kill()
        return False
        
    print(f"[OK] Cliente logado no broker: {sess.broker_info['broker_id']}")
    
    # 3. Derrubar Broker A
    print("[TEST] Derrubando Broker A...")
    b1_proc.kill()
    time.sleep(1)
    
    # 4. Iniciar Broker B para o failover (Porta 8000)
    print("[TEST] Iniciando Broker B para recuperação (Porta 8000)...")
    b2_proc = subprocess.Popen([sys.executable, "broker/broker.py", "--port", "8000"])
    
    # O loop de heartbeat do cliente deve detectar a falha e reconectar
    print("[TEST] Aguardando reconexão automática (timeout de HB)...")
    
    success = False
    for i in range(20): # Dá mais tempo para o failover
        if sess.online and sess.broker_info['ports']['control'] == 8007:
            success = True
            break
        time.sleep(1)
        
    try:
        if success:
            print(f"[OK] Cliente reconectado com sucesso ao Broker B!")
            return True
        else:
            print("[FALHA] Cliente não reconectou ao Broker B dentro do tempo esperado.")
            return False
    finally:
        reg_proc.kill()
        b2_proc.kill()
        sess.logout()

if __name__ == "__main__":
    try:
        if test_failover_logic():
            print("\n--- TESTE DE FAILOVER PASSOU! ---")
            sys.exit(0)
        else:
            sys.exit(1)
    except Exception as e:
        print(f"\n[ERRO] Teste falhou: {e}")
        sys.exit(1)
