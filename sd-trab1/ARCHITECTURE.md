# Arquitetura Técnica - VideoConf Distribuído com ZeroMQ

## Visão Geral

**VideoConf** é uma ferramenta de videoconferência distribuída que suporta:
- **Vídeo**: transmissão em tempo real com qualidade adaptativa
- **Áudio**: stream contínuo com baixa latência
- **Texto**: mensagens confiáveis com garantia de entrega
- **Salas**: grupos isolados de comunicação (geral, sala1, sala2, etc)
- **Usuários simultâneos**: suporte a múltiplos clientes em múltiplos brokers
- **Tolerância a falhas**: failover automático entre brokers

### Componentes Principais

```
┌──────────────┐         ┌──────────────┐         ┌──────────────┐
│  Broker 1    │ ◄───►  │  Broker 2    │ ◄───►  │  Broker 3    │
│  :5500       │         │  :5510       │         │  :5520       │
│   mesh       │         │   mesh       │         │   mesh       │
└──────────────┘         └──────────────┘         └──────────────┘
  ▲      ▲                 ▲      ▲                 ▲      ▲
  │      │                 │      │                 │      │
  │      └─ Video/Audio/  │      │                 │      │
  │         Texto PUB/SUB └─ Discovery UDP ────────┘      │
  │                          heartbeat (6000)             │
  │
┌─┴──────────────────┬──────────────────┬──────────────────┴─┐
│  Client 1          │   Client 2       │   Client 3         │
│  user1 @ geral     │   user2 @ geral  │   user3 @ sala1    │
│  Failover auto     │   Failover auto  │   Failover auto    │
└────────────────────┴──────────────────┴────────────────────┘
```

---

## Arquitetura Distribuída

### 1. Cluster de Brokers Cooperando

Cada broker é uma instância independente que:
- **Publica heartbeat** via UDP multicast/broadcast
- **Se conecta** dinamicamente a outros brokers descobertos
- **Forma uma malha** (mesh) de comunicação inter-broker
- **Gerencia um subconjunto** de usuários/salas (load balancing por escolha do cliente)

#### Portas e Endpoints

Cada broker com porta primária `P` expõe:

| Função | Tipo Socket | Porta | Propósito |
|--------|------------|-------|-----------|
| Descoberta | UDP | 6000 | Heartbeat e anúncio de brokers |
| Vídeo entrada | XSUB | P+3 | Clientes publicam vídeo |
| Vídeo saída | XPUB | P+4 | Clientes recebem vídeo |
| Áudio entrada | XSUB | P+13 | Clientes publicam áudio |
| Áudio saída | XPUB | P+14 | Clientes recebem áudio |
| Texto entrada | XSUB | P+23 | Clientes publicam texto |
| Texto saída | XPUB | P+24 | Clientes recebem texto |
| Controle | REP | P+200 | REQ/REP para operações de controle |
| Malha inter-broker | PUB | P+300 | Relay entre brokers (mesh) |

**Exemplos:**
- Broker1 na porta 5500:
  - Video: 5503 (pub_in) → 5504 (sub_out)
  - Audio: 5513 → 5514
  - Texto: 5523 → 5524
  - Control: 5700
  - Mesh: porta aleatória (descoberta automática)

### 2. Descoberta Dinâmica de Brokers

**Mecanismo UDP Heartbeat:**

1. **Sender (Broker)**: A cada 2 segundos, envia UDP broadcast para porta 6000:
```json
{
  "broker_id": "broker1",
  "primary_port": 5500,
  "mesh_port": 12345,
  "control_port": 5700,
  "timestamp": "2026-04-30T12:34:56.789Z"
}
```

2. **Receiver (Broker + Cliente)**:
   - Escuta na porta 6000
   - Cria/atualiza entrada para broker descoberto
   - Timeout após 8 segundos sem heartbeat

3. **Limpeza**: Background thread remove brokers mortos


## Padrões ZeroMQ Utilizados

### 1. **PUB/SUB para Mídia (Video, Áudio)**

**Padrão**: Clientes como PUB, Broker como intermediário

```
Client1 (PUB) ──publish──► XSUB ─► Broker Process ─► XPUB ─subscribe── Client2 (SUB)
Client2 (PUB) ──publish──► XSUB ─┘                 └─► XPUB ─subscribe── Client1 (SUB)
```

**Topics**: `<stream>:<room>:<user_id>`
- Exemplo: `video:geral:user1`, `audio:sala2:user3`
- Filtro no cliente: `video:geral:*` recebe todos do vídeo da sala geral

### 2. **REQ/REP para Controle (Login, Join, Leave, Presença)**

**Padrão**: Síncrono request-reply

```
Client ──REQ──► Broker Control Server ──REP──► Client
```

**Operações:**
- `login`: Registrar usuário em uma sala
- `heartbeat_user`: Manter sessão viva
- `join_room`: Trocar de sala
- `leave`: Desconectar
- `presence`: Consultar usuários online
- `send_text`: Enviar mensagem de texto com QoS

### 3. **Malha Inter-Broker com PUB/SUB**

**Padrão**: Mesh de comunicação distribuída

```
Broker1 ◄──mesh_sub──┐         ┌──mesh_sub──► Broker2
         └──mesh_pub─┼─relay─┬─┼──mesh_pub──┘
                      └───────┘
```

**Fluxo:**
1. Broker1 recebe mensagem de vídeo de Client1
2. Broker1 encaminha para seus subscribers locais (via XPUB)
3. Broker1 **também** publica na malha com metadados especiais: `relay|video|topic|payload|meta`
4. Broker2 (mesh_sub) recebe a mensagem retransmitida
5. Broker2 entrega aos seus subscribers locais (Client2 que está em Broker2)

**Metadados de Relay:**
```json
{
  "msg_id": "uuid",           // Deduplicação
  "origin_broker": "broker1", // Previne loops
  "ttl": 3,                   // TTL decremental
  "room": "geral",            // Roteamento
  "user": "user1",            // Origem
  "stream": "video",          // Tipo de mídia
  "ts": 1234567890.123        // Timestamp
}
```

**Loop Prevention:**
- Cada broker marca mensagens vistas com `msg_id` em cache TTL de 60s
- Se vir a mesma `msg_id` de novo = ignora (já processou)
- `ttl` é decrementado a cada relay
- Quando TTL ≤ 0, para de retransmitir (mesh ativa por 4 hops)

---

## Fluxo de Mensagens

### Cenário: Dois Clientes em Salas Diferentes

```
┌─────────────────────────────────────────┐
│ Broker1 (controla: user1, user2)        │
│ Broker2 (controla: user3)               │
└─────────────────────────────────────────┘

user1 (sala geral) ──PUBLISH──► XSUB(Broker1:5503)
                                   │
                                   ├─► XPUB(Broker1:5504) ──SUB── user2 (sala geral)
                                   │
                                   └─► PUB(mesh:Broker1) ──SUB── Broker2
                                                                      │
                                                                      ├─► XPUB(Broker2:5504) ──SUB── user3 (sala geral)
                                                                      │
                                                                      └─► PUB(mesh:Broker2) ──SUB── Broker1 (TTL=2, visto, ignora)
```

### Exemplo de Mensagem de Vídeo

**Publicado pelo cliente:**
```
[Topic]   video:geral:user1
[Payload] <JPEG binary data>
```

**Retransmitido pelo broker na malha:**
```
[Topic1]    relay|video|
[Topic2]    video:geral:user1
[Payload]   <JPEG binary data>
[Metadata]  {msg_id, origin_broker, ttl, ...}
```

---

## Tolerância a Falhas

### 1. Detecção de Falha de Broker

**Heartbeat Monitor (Cliente):**

```python
def heartbeat_monitor():
    failures = 0
    while True:
        try:
            resp = control_request(broker, {"action": "ping"})
            if resp.ok:
                failures = 0  # Reset counter
            else:
                failures += 1
        except:
            failures += 1
        
        if failures >= 3:  # 3 tentativas × 2s = ~6s para detectar falha
            trigger_failover()
        
        time.sleep(2)
```

**Timeline:**
- t=0s: Broker morre
- t=2s: 1ª falha
- t=4s: 2ª falha
- t=6s: 3ª falha → **Failover disparado**
- t=7s: Cliente reconectado a Broker2

### 2. Failover Automático

**Estratégias de Seleção:**

1. **Menor Latência** (default):
   - Calcula latência de cada broker via heartbeat timestamp
   - Escolhe o broker com menor latência
   - Melhor para minimizar delay de comunicação

2. **Round-Robin**:
   - Distribui clientes uniformemente entre brokers
   - Melhor para balanceamento de carga

**Código:**
```python
def connect_to_best_broker(strategy="lowest-latency"):
    brokers = discovery.list_brokers()
    
    if strategy == "lowest-latency":
        brokers.sort(key=lambda b: b["latency"])
    else:  # round-robin
        brokers = apply_round_robin(brokers)
    
    for broker_id, info in brokers:
        try:
            resp = control_request(broker, {"action": "login", "user_id": user1, ...})
            if resp.ok:
                set_current_broker(broker)
                return True
        except:
            continue
    
    return False
```

### 3. Preservação de Sessão

Quando failover ocorre:

1. **Login novamente** no novo broker com mesmo `user_id` e `room`
2. **Mudar de sala (join_room)** se estava em sala diferente de "geral"
3. **Reconectar a todos os workers**:
   - video_send: novo PUB para novo broker
   - video_recv: novo SUB para novo broker
   - audio_send/recv: novo PUB/SUB
   - text_recv: novo SUB

**Impacto:**
- Vídeo/Áudio: reconecta em ~1 segundo, pequena interrupção
- Texto: sem perda de mensagens (garantido por retry no cliente)
- Presença: atualizada no novo broker
- Vídeo em trânsito: pode haver perda de alguns frames durante reconexão

---

## Qualidade de Serviço (QoS)

### Requisitos por Tipo de Mídia

| Mídia | Requisito | Implementação |
|-------|-----------|-----------------|
| **Texto** | Garantia de entrega | 3 retries + cache de vistos |
| **Áudio** | Baixa latência | Buffer pequeno (12 chunks) + drop do mais antigo |
| **Vídeo** | Taxa adaptativa | JPEG qualidade 35/55/70 conforme carga |

### 1. Texto - Garantia de Entrega

**Mecanismo:**
1. Cliente gera `text_id` único (UUID)
2. Envia via `send_text` control request (REQ/REP)
3. Broker confirma recebimento e publica na malha
4. Broker marca `text_id` como visto para evitar duplicação
5. Se falhar, cliente **retry até 3 vezes** com mesmo `text_id`
6. Cliente marca localmente como visto após confirmação

**Código (Cliente):**
```python
def send_text_with_retry(text):
    text_id = str(uuid.uuid4())
    for attempt in range(3):
        try:
            resp = control_request(
                broker,
                {
                    "action": "send_text",
                    "user_id": user_id,
                    "room": room,
                    "text": text,
                    "text_id": text_id
                }
            )
            if resp.ok:
                mark_text_as_seen(text_id)
                return True
        except:
            pass
        time.sleep(0.3)
    
    return False
```

**Buffer de Histórico:**
- Broker mantém `text_history[room]` com últimos 500 mensagens
- Reutilizável para sincronização futura (não implementado)

### 2. Áudio - Baixa Latência com Queda

**Buffer:**
- Tamanho fixo: 12 frames (chunks de 1024 amostras @ 44.1 kHz)
- Taxa de envio: 1 chunk a cada ~23ms
- Latência de buffer: ~276ms (12 × 23ms)

**Drop Automático:**
```python
def safe_queue_put(q, item):
    if q.full():
        try:
            q.get_nowait()  # Remove frame mais antigo
        except queue.Empty:
            pass
    q.put_nowait(item)
```

**Comportamento:**
- Se captura for mais rápida que envio → drop de frames antigos
- Se rede estiver congestionada → áudio é degradado mas não congela
- Mantém latência baixa
- Pode haver perda de audio (~50ms periodicamente em congestionamento)

### 3. Vídeo - Qualidade Adaptativa

**Monitoramento de Carga:**
```python
def video_send_worker():
    while True:
        frame = VIDEO_RAW_Q.get(timeout=0.5)
        
        load = VIDEO_RAW_Q.qsize() / VIDEO_RAW_Q.maxsize
        
        if load > 0.7:
            quality = 35  # Altamente comprimido
        elif load > 0.3:
            quality = 55  # Médio
        else:
            quality = 70  # Alta qualidade
        
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
        pub.send_multipart([topic, buf.tobytes()])
```

**Taxa de Frames:**
- Target: 15 FPS (timeout de 1/15 ≈ 67ms entre envios)
- Ajusta automaticamente se fila estiver cheia

---

## Descoberta de Serviços

### Cliente-centric Service Discovery

**Diferente de registries centralizados**, usamos **descoberta descentralizada**:

1. **Sem SPOF**: Não há servidor de registro central
2. **Auto-healing**: Quando novo broker sobe, é descoberto em <2s
3. **Transparente**: Clientes não precisam conhecer brokers previamente

**Fluxo:**

```
┌────────────────────────────────────────┐
│ Início do Cliente                      │
└────────────────────────────────────────┘
            │
            ├─► BrokerDiscovery.start()
            │   ├─► Thread: listen_heartbeats() na UDP 6000
            │   └─► Thread: cleanup() remove brokers mortos
            │
            ├─► Aguarda brokers em descoberta (até 5s)
            │
            └─► connect_to_best_broker(strategy)
                ├─► Pega lista de brokers descobertos
                ├─► Ordena por latência ou round-robin
                └─► Tenta login até conseguir
```

### Latência de Descoberta

- **Novo broker aparece em**: ~2 segundos (próximo heartbeat)
- **Broker desaparece em**: ~8 segundos (3 × heartbeat_interval)
- **Cliente descobre em**: <1s após novo heartbeat


## Concorrência

### Workers em Threads Separadas

Cada cliente executa 8-10 threads simultâneos:

| Thread | Função | Bloqueante? |
|--------|--------|-------------|
| `video_capture_worker` | Lê frames da câmera | Sim (cv2.read) |
| `video_send_worker` | Publica frames via ZMQ | Não (timeout 500ms) |
| `video_recv_worker` | Recebe frames de outros | Não (timeout 1000ms) |
| `audio_capture_worker` | Lê chunks de microfone | Sim (pyaudio) |
| `audio_send_worker` | Publica áudio via ZMQ | Não (timeout 200ms) |
| `audio_recv_worker` | Reproduz áudio recebido | Sim (pyaudio) |
| `text_recv_worker` | Recebe texto | Não (timeout 1000ms) |
| `heartbeat_monitor` | Monitora saúde do broker | Não (timeout 1000ms) |
| `build_ui` (main loop) | Renderiza UI a cada 50ms | Não (UI event loop) |

### Sincronização

**Locks usados:**
```python
state_lock       # Estado global (CURRENT_BROKER, BROKER_EPOCH)
chat_lock        # Histórico de chat (CHAT, SEEN_TEXT_IDS)
frames_lock      # Frames remotos (REMOTE_FRAMES)
```

**Pattern: Epoch-based Reconnection**

Quando broker muda:
```python
epoch = set_current_broker(new_broker)  # Incrementa contador

# Em cada worker:
while True:
    broker, local_epoch = get_current_broker()
    if pub is None or local_epoch != epoch:
        # Reconecta com novo broker
        pub.close()
        pub = create_new_pub_socket(broker)
        epoch = local_epoch
```

**Benefício**: Workers se reconectam automaticamente sem threads adicionais

---

## Sessão e Presença

### 1. Identidade de Usuário

**Atribuição:**
- Usuário escolhe `user_id` no login (ou gerado aleatoriamente)
- Único globalmente (verificação distribuída)
- Persiste enquanto conectado ao broker

**Verificação de Unicidade:**
```python
def _is_user_taken(self, user_id):
    # Locais
    if user_id in self.local_users:
        return True
    
    # Remotos (em outros brokers)
    for remote in self.remote_presence.values():
        if user_id in remote.get("users", []):
            return True
    
    return False
```

### 2. Salas (Grupos)

**Estrutura:**
```python
local_rooms: dict[room_name, set[user_id]]
# Ex:
# "geral": {"user1", "user2", "user3"}
# "sala1": {"user4"}
# "sala2": {"user5", "user6"}
```

**Operações:**
- **Login**: Usuário entra em sala inicial (default "geral")
- **Join**: `/join sala1` → muda para nova sala
- **Leave**: Disconnect → sai de todas as salas
- **Topic Filtering**: Cada cliente se inscreve apenas em `<stream>:<room>:*`

### 3. Presença Distribuída

**Broadcast de Snapshot a cada 3s:**

```python
def _presence_sender():
    while not stop_event:
        snapshot = {
            "users": sorted(self.local_users.keys()),
            "rooms": {r: len(u) for r, u in self.local_rooms.items()}
        }
        
        self.mesh_pub.send_multipart([
            b"presence|snapshot",
            json.dumps({
                "broker_id": self.config.broker_id,
                "snapshot": snapshot,
                "ts": time.time()
            }).encode()
        ])
        
        time.sleep(3)
```

**Recepção em Outro Broker:**

```python
def _presence_receiver():
    sub = self.ctx.socket(zmq.SUB)
    sub.setsockopt(zmq.SUBSCRIBE, b"presence|")
    
    for endpoint in mesh_endpoints:
        sub.connect(endpoint)
    
    while True:
        topic, payload = sub.recv_multipart()
        msg = json.loads(payload)
        
        # Atualiza visão de remote_presence
        self.remote_presence[msg["broker_id"]] = {
            "users": msg["snapshot"]["users"],
            "rooms": msg["snapshot"]["rooms"],
            "ts": time.time()
        }
```

**Comando `/who`:**
```python
def fetch_presence():
    resp = control_request(broker, {"action": "presence"})
    
    # Locais
    print(f"Online local: {resp['online_local']}")
    for room, users in resp['rooms_local'].items():
        print(f"  Sala {room}: {users}")
    
    # Remotos (agregado de outros brokers)
    for broker_id, presence in resp['remote_presence'].items():
        print(f"Broker {broker_id}: {presence['users']}")
```

---

## Instruções de Execução

### Pré-requisitos

```bash
python --version  # Python 3.8+
pip install -r requirements.txt
```

**requirements.txt:**
```
numpy==2.4.4
opencv-python==4.13.0.92
pillow==12.2.0
pyzmq==27.1.0
pyaudio  # opcional, para áudio
```

### Instalar Dependências

```bash
# Linux/Mac
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Executar Múltiplos Brokers

**Terminal 1 (Broker 1):**
```bash
python broker.py broker1 5500
```

**Terminal 2 (Broker 2):**
```bash
python broker.py broker2 5510
```

**Terminal 3 (Broker 3 - opcional):**
```bash
python broker.py broker3 5520
```

**Saída esperada:**
```
[Broker broker1] init primary=5500
[Broker broker1] discovery UDP=6000
[Broker broker1] control REP=5700
[Broker broker1] mesh PUB=<porta_aleatória>
[Broker broker1] channels video(5503->5504) audio(5513->5514) texto(5523->5524)
```

### Executar Clientes

**Terminal 4+ (Cliente 1, 2, 3...):**
```bash
python client.py
```

**Janela de Login:**
1. Digite `user_id`: Ex: `user1` (ou deixar pré-preenchido)
2. Digite `room`: Ex: `geral` (ou `sala1`)
3. Escolha estratégia: "Menor latência" (default) ou "Round-robin"
4. Clique **Entrar**


### Interações na UI

**Video:**
- Grade 2x2 com seu próprio vídeo (canto superior esquerdo)
- Até 3 vídeos remotos (outros cantos)
- Atualiza a cada 50ms

**Chat:**
- Pane direita mostra últimas 30 mensagens
- Digite mensagem + clique **Enviar** ou Enter

**Comandos:**
- `/join salaA`: Mudar para sala A
- `/who`: Listar usuários online (local + remoto)

### Teste de Failover

**Cenário:**
1. Cliente conectado ao Broker1
2. Cliente conectado ao Broker1 → Falha de Broker1 → Conecta a Broker2

**Executar:**

**Terminal 1:**
```bash
python broker.py broker1 5500
# Deixar rodar, depois Ctrl+C em ~30s
```

**Terminal 2:**
```bash
python broker.py broker2 5510
```

**Terminal 3:**
```bash
python client.py
# Login como user1, geral
# Enviar mensagens
# Após ~6s de falha de broker1: [Failover] broker1 indisponivel...
# Cliente reconecta a broker2 automaticamente
```


## Padrões de Design

### 1. Deduplicação em Malha

**Problema**: Mensagem pode chegar múltiplas vezes (loops de relay)

**Solução**:
```python
seen_messages = {}  # msg_id -> timestamp

def _mark_seen(self, msg_id):
    with seen_lock:
        self.seen_messages[msg_id] = time.time()

def _is_seen(self, msg_id):
    with seen_lock:
        return msg_id in self.seen_messages

def _cleanup_seen_cache(self):
    while not stop_event:
        now = time.time()
        with seen_lock:
            expired = [mid for mid, ts in self.seen_messages.items()
                       if now - ts > 60]
            for mid in expired:
                del self.seen_messages[mid]
        time.sleep(5)
```

### 2. Epoch-based Connection Switching

**Problema**: Workers precisam se reconectar quando broker muda

**Solução**:
```python
CURRENT_BROKER = None
BROKER_EPOCH = 0

def set_current_broker(broker_info):
    global BROKER_EPOCH
    with state_lock:
        CURRENT_BROKER = broker_info
        BROKER_EPOCH += 1

def video_send_worker():
    pub = None
    local_epoch = -1
    
    while True:
        broker, epoch = get_current_broker()
        
        if pub is None or local_epoch != epoch:
            if pub:
                pub.close()
            pub = create_pub_socket(broker)
            local_epoch = epoch
        
        # ... send frames ...
```

### 3. Graceful Shutdown

**Padrão:**
```python
stop_event = threading.Event()

def main():
    # Iniciar threads daemon
    for fn in workers:
        threading.Thread(target=fn, daemon=True).start()
    
    # Main loop
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        stop_event.set()
        # Threads daemon são encerradas automaticamente

def worker():
    while not stop_event.is_set():
        try:
            # ... trabalho ...
        except Exception:
            pass
```

