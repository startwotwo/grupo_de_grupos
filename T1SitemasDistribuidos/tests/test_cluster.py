# tests/test_cluster.py
import sys
import os
import time
import subprocess
import zmq
import threading

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def test_inter_broker_routing():
    print("\n--- Teste 2: Roteamento Inter-Broker (Cluster) ---")
    
    # 1. Iniciar Infraestrutura com portas seguras (7000 e 8000)
    print("[TEST] Iniciando Registry e 2 Brokers (7000 e 8000)...")
    reg_proc = subprocess.Popen([sys.executable, "discovery/registry.py"])
    time.sleep(1)
    b1_proc = subprocess.Popen([sys.executable, "broker/broker.py", "--port", "7000"])
    b2_proc = subprocess.Popen([sys.executable, "broker/broker.py", "--port", "8000"])
    
    # Esperar os brokers se registrarem e SE DESCOBRIREM
    # O loop de registro é de 2s, 10s deve ser mais que suficiente
    print("[TEST] Aguardando formação do cluster (10s)...")
    time.sleep(10) 
    
    context = zmq.Context()
    
    # 2. Configurar Subscriber no Broker B (Base 8000 -> Texto Out = 8006)
    sub = context.socket(zmq.SUB)
    sub.setsockopt(zmq.LINGER, 0)
    sub.connect("tcp://localhost:8006")
    sub.setsockopt(zmq.SUBSCRIBE, b"SALA_TESTE")
    
    # 3. Configurar PUSH no Broker A (Base 7000 -> Texto In = 7005)
    push = context.socket(zmq.PUSH)
    push.setsockopt(zmq.LINGER, 0)
    push.connect("tcp://localhost:7005")
    
    # Tempo para o handshake PULL/PUSH e ROUTER/DEALER estabilizar
    time.sleep(3)
    
    received_msgs = []
    def sub_loop():
        print("[SUB] Escutando...")
        start_time = time.time()
        # Polling por até 20 segundos
        while time.time() - start_time < 20:
            if sub.poll(1000): 
                msg = sub.recv_multipart()
                received_msgs.append(msg)
                print(f"[SUB] MENSAGEM RECEBIDA: {msg[1].decode()}")
                return
    
    t = threading.Thread(target=sub_loop, daemon=True)
    t.start()
    
    # 4. Enviar mensagem para Broker A repetidamente para vencer o handshake assíncrono
    print("[TEST] Enviando burst de mensagens para Broker A...")
    for i in range(15):
        msg_text = f"Ola Cluster Burst {i}!"
        pub.send_multipart([b"SALA_TESTE", msg_text.encode()])
        time.sleep(0.5)
    
    t.join(timeout=20)
    
    # 5. Verificar resultado
    success = len(received_msgs) > 0
    
    print("[TEST] Limpando processos...")
    reg_proc.kill()
    b1_proc.kill()
    b2_proc.kill()
    sub.close()
    pub.close()
    context.term()
    
    if success:
        print("[OK] Comunicação inter-broker validada!")
        return True
    else:
        print("[FALHA] Nenhuma mensagem recebida via cluster após 15 disparos.")
        return False

if __name__ == "__main__":
    try:
        if test_inter_broker_routing():
            print("\n--- TESTE DE CLUSTER PASSOU! ---")
            sys.exit(0)
        else:
            sys.exit(1)
    except Exception as e:
        print(f"\n[ERRO] Teste falhou: {e}")
        sys.exit(1)
