import zmq
import threading
import json
import time

# ─────────────────────────────────────────
# CONFIGURAÇÕES — Ajuste antes de rodar
# ─────────────────────────────────────────

# Seu broker local (Texto)
MEU_IP             = "10.236.21.184" 
MEU_SUB_PORT       = 6001            # Onde seu broker aceita injeção de texto
MEU_PUB_PORT       = 6002            # Onde seu broker publica texto
MINHA_SALA         = "ROOM_A"        # Nome da sua sala no seu sistema
MEU_BROKER_ID      = "hub-bridge"    

# Seu broker local (Vídeo)
MEU_SUB_VIDEO_PORT = 6003            # Onde seu broker aceita injeção de vídeo
MEU_PUB_VIDEO_PORT = 6004            # Onde seu broker publica vídeo

# Grupo Anadão
IP_ANADAO                 = "10.236.21.84"
PORTA_ANADAO_TEXTO_PUB    = 5560     # XPUB texto deles (nós assinamos)
PORTA_ANADAO_TEXTO_ACEITA = 5559     # XSUB texto deles (nós injetamos)
PORTA_ANADAO_VIDEO_PUB    = 5556     # XPUB vídeo deles (nós assinamos)
PORTA_ANADAO_VIDEO_ACEITA = 5555     # XSUB vídeo deles (nós injetamos)
PORTA_ANADAO_CONTROLE     = 5564     # ROUTER deles (para comandos como /status)
SALA_ANADAO               = "A"      # Nome da sala lá

# ─────────────────────────────────────────

context = zmq.Context()

# ==========================================
# THREADS DE TEXTO E STATUS
# ==========================================

def anadao_para_meu_sistema_texto():
    """
    Assina o canal de texto do Anadão (multipart 3 frames) e converte para o seu JSON.
    """
    sub = context.socket(zmq.SUB)
    sub.connect(f"tcp://{IP_ANADAO}:{PORTA_ANADAO_TEXTO_PUB}")
    sub.setsockopt_string(zmq.SUBSCRIBE, SALA_ANADAO)
    
    pub = context.socket(zmq.PUB)
    pub.connect(f"tcp://{MEU_IP}:{MEU_SUB_PORT}")
    time.sleep(0.5)

    print(f"[Hub Texto] Escutando Anadão em {IP_ANADAO}:{PORTA_ANADAO_TEXTO_PUB}")

    while True:
        try:
            parts = sub.recv_multipart()
            if len(parts) < 3: 
                continue

            id_bytes    = parts[1]
            texto_bytes = parts[2]

            nome = id_bytes.decode("utf-8")
            texto = texto_bytes.decode("utf-8")

            # Monta JSON para injetar no seu broker
            msg_json = json.dumps({
                "action":  "TEXT",
                "room":    MINHA_SALA,
                "user_id": f"[Anadão]{nome}",
                "msg_id":  0,
                "text":    texto,
                "origin":  MEU_BROKER_ID
            }).encode("utf-8")

            topico = f"{MINHA_SALA} TEXT".encode("utf-8")
            pub.send_multipart([topico, msg_json])
            print(f"[Hub ← Anadão] {nome}: {texto}")
            
        except Exception as e:
            print(f"[Hub Texto] Erro RX: {e}")
            time.sleep(1)

def meu_sistema_para_anadao_texto():
    """
    Assina o seu JSON, converte para multipart 3 frames e injeta no Anadão.
    Também intercepta o comando /status.
    """
    sub = context.socket(zmq.SUB)
    sub.connect(f"tcp://{MEU_IP}:{MEU_PUB_PORT}")
    sub.setsockopt_string(zmq.SUBSCRIBE, f"{MINHA_SALA} TEXT")
    
    pub_anadao = context.socket(zmq.PUB)
    pub_anadao.connect(f"tcp://{IP_ANADAO}:{PORTA_ANADAO_TEXTO_ACEITA}")
    
    # Socket para devolver o resultado do /status para você mesmo
    pub_local = context.socket(zmq.PUB)
    pub_local.connect(f"tcp://{MEU_IP}:{MEU_SUB_PORT}")
    
    time.sleep(0.5)
    print(f"[Hub Texto] Repassando para Anadão em {IP_ANADAO}:{PORTA_ANADAO_TEXTO_ACEITA}")

    while True:
        try:
            parts = sub.recv_multipart()
            if len(parts) < 2: 
                continue

            msg = json.loads(parts[1].decode("utf-8"))

            # Evita loop de mensagens que o próprio hub injetou
            if msg.get("origin") == MEU_BROKER_ID: 
                continue
            
            user_id = msg.get("user_id", "")
            if user_id.startswith("[Anadão]"): 
                continue

            texto = msg.get("text", "").strip()
            
            # --- INTERCEPTAÇÃO DO /STATUS ---
            if texto == "/status":
                print("[Hub Status] Buscando status no ROUTER do Anadão...")
                req = context.socket(zmq.REQ)
                req.connect(f"tcp://{IP_ANADAO}:{PORTA_ANADAO_CONTROLE}")
                req.setsockopt(zmq.RCVTIMEO, 2000) # Timeout de 2s
                
                req.send_string(f"LIST_SALA {SALA_ANADAO}")
                
                try:
                    resposta_status = req.recv_string()
                except zmq.error.Again:
                    resposta_status = "ERRO: Timeout ao buscar status do Anadão."
                
                req.close()

                # Devolve a resposta na sua tela
                msg_json = json.dumps({
                    "action":  "TEXT",
                    "room":    MINHA_SALA,
                    "user_id": "[SISTEMA]",
                    "msg_id":  0,
                    "text":    f"Status Anadão: {resposta_status}",
                    "origin":  MEU_BROKER_ID
                }).encode("utf-8")
                
                pub_local.send_multipart([f"{MINHA_SALA} TEXT".encode("utf-8"), msg_json])
                continue # Interrompe aqui, não envia o "/status" pro chat deles
            # --------------------------------

            # Se for texto normal, repassa pro Anadão
            sala_bytes  = SALA_ANADAO.encode("utf-8")
            nome_bytes  = user_id.encode("utf-8")
            texto_bytes = texto.encode("utf-8")

            pub_anadao.send_multipart([sala_bytes, nome_bytes, texto_bytes])
            print(f"[Hub → Anadão] {user_id}: {texto}")
            
        except Exception as e:
            print(f"[Hub Texto] Erro TX: {e}")
            time.sleep(1)


# ==========================================
# THREADS DE VÍDEO
# ==========================================

def anadao_para_meu_sistema_video():
    """
    Assina o canal de vídeo do Anadão e repassa para o seu sistema.
    """
    sub = context.socket(zmq.SUB)
    sub.connect(f"tcp://{IP_ANADAO}:{PORTA_ANADAO_VIDEO_PUB}")
    sub.setsockopt_string(zmq.SUBSCRIBE, SALA_ANADAO)
    
    pub = context.socket(zmq.PUB)
    pub.connect(f"tcp://{MEU_IP}:{MEU_SUB_VIDEO_PORT}")
    time.sleep(0.5)
    
    print(f"[Hub Vídeo] Escutando Anadão em {IP_ANADAO}:{PORTA_ANADAO_VIDEO_PUB}")

    while True:
        try:
            parts = sub.recv_multipart()
            if len(parts) < 3: 
                continue
            
            id_bytes     = parts[1]
            buffer_bytes = parts[2] # Frame em JPEG cru
            
            nome = f"[Anadão]{id_bytes.decode('utf-8')}"
            
            # Repassa usando o tópico e formato do seu sistema de vídeo
            # Assumindo formato genérico 3-frames: [TÓPICO, NOME, BYTES_JPEG]
            meu_topico_video = f"{MINHA_SALA} VIDEO".encode("utf-8")
            pub.send_multipart([meu_topico_video, nome.encode('utf-8'), buffer_bytes])
            
        except Exception as e:
            print(f"[Hub Vídeo] Erro RX: {e}")
            time.sleep(1)

def meu_sistema_para_anadao_video():
    """
    Assina o seu canal de vídeo e repassa no formato do Anadão.
    """
    sub = context.socket(zmq.SUB)
    sub.connect(f"tcp://{MEU_IP}:{MEU_PUB_VIDEO_PORT}")
    sub.setsockopt_string(zmq.SUBSCRIBE, f"{MINHA_SALA} VIDEO") 
    
    pub_anadao = context.socket(zmq.PUB)
    pub_anadao.connect(f"tcp://{IP_ANADAO}:{PORTA_ANADAO_VIDEO_ACEITA}")
    time.sleep(0.5)

    print(f"[Hub Vídeo] Repassando para Anadão em {IP_ANADAO}:{PORTA_ANADAO_VIDEO_ACEITA}")

    while True:
        try:
            parts = sub.recv_multipart()
            if len(parts) < 3: 
                continue
            
            id_bytes     = parts[1]
            buffer_bytes = parts[2]
            
            # Repassa no formato do Anadão: [b"A", b"seu_nome", b"bytes_jpeg"]
            pub_anadao.send_multipart([SALA_ANADAO.encode("utf-8"), id_bytes, buffer_bytes])
            
        except Exception as e:
            print(f"[Hub Vídeo] Erro TX: {e}")
            time.sleep(1)


if __name__ == "__main__":
    print("=" * 65)
    print("  🚀 hub.py — Bridge Completa (Texto, Vídeo & Status)")
    print("=" * 65)
    
    threads = [
        threading.Thread(target=anadao_para_meu_sistema_texto, daemon=True, name="texto_rx"),
        threading.Thread(target=meu_sistema_para_anadao_texto, daemon=True, name="texto_tx"),
        threading.Thread(target=anadao_para_meu_sistema_video, daemon=True, name="video_rx"),
        threading.Thread(target=meu_sistema_para_anadao_video, daemon=True, name="video_tx"),
    ]

    for t in threads:
        t.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[Hub] Encerrando ponte de conexão...")
