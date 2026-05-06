# Documentação Técnica: Videoconferência Distribuída com ZeroMQ

## 1. Arquitetura do Sistema

O sistema foi evoluído de um único broker central para uma arquitetura distribuída, resiliente e escalável, composta por três camadas principais:

                    ┌─────────────┐
                    │   Registry  │  :5555 (REQ/REP)
                    │  (Discovery)│
                    └──────┬──────┘
                           │ Service Discovery
           ┌───────────────┼───────────────┐
           ▼               ▼               ▼
    ┌────────────┐  ┌────────────┐  ┌────────────┐
    │  Broker A  │◄─►  Broker B  │◄─►  Broker C  │
    │  :7000     │  │  :8000     │  │  :9000     │
    └─────┬──────┘  └────────────┘  └────────────┘
          │ PUSH→PULL (cliente→broker)
          │ PUB→SUB  (broker→cliente, filtrado por sala)
 ┌────────┴────────┐
 ▼                 ▼
┌─────────┐   ┌─────────┐
│Client A │   │Client B │
└─────────┘   └─────────┘

### 1.1 Registry (Discovery Service)
O Registry atua como o "cérebro" da topologia, permitindo que o sistema seja dinâmico.
- **Registro Dinâmico**: Brokers se registram ao iniciar, informando seu endereço e porta base.
- **Monitoramento**: Implementa um sistema de *timeouts* para remover brokers inativos que param de enviar heartbeats.
- **Service Discovery**: Clientes consultam o Registry para obter o endereço de um broker disponível, permitindo balanceamento de carga e tolerância a falhas.
- **Padrão ZMQ**: `REQ/REP` (Porta 5555).

### 1.2 Cluster de Brokers (Malha Distribuída)
Múltiplos brokers cooperam para distribuir mídia em escala global.
- **Inter-Broker Mesh**: Utiliza o padrão `ROUTER/DEALER` para criar uma malha de comunicação. Cada broker é conectado aos seus pares.
- **Roteamento Inteligente**: Mensagens recebidas de clientes locais são marcadas com o `broker_id` de origem e propagadas para o cluster. Brokers receptores validam o ID para evitar loops infinitos de mensagens.
- **Canal de Entrada (Cliente → Broker)**: Utiliza `PUSH/PULL`. O broker expõe um socket `PULL` que aceita conexões de múltiplos clientes `PUSH`. Esse modelo garante entrega imediata, inclusive após reconexões por failover.
- **Canal de Saída (Broker → Clientes)**: Utiliza `PUB/SUB`. O broker publica com o prefixo da sala (ex: `b"A"`), e apenas clientes assinantes daquela sala recebem os dados. Isso garante isolamento entre salas.
- **Portas Dinâmicas**: Cada broker utiliza uma faixa de portas baseada em um deslocamento (`base_port + N`), evitando conflitos em execuções locais.

### 1.3 Cliente (Multithreaded)
Aplicação modular que separa as preocupações de captura, rede e interface.
- **CaptureManager**: Threads dedicadas para OpenCV (Vídeo) e PyAudio (Áudio).
- **Sender/Receiver**: Gerenciam a comunicação assíncrona com o broker atribuído.
- **Session Manager**: Responsável pelo ciclo de vida da conexão, incluindo o *failover* automático.

---

## 2. Padrões ZeroMQ Utilizados

| Canal | Padrão ZMQ | Direção | Justificativa |
| :--- | :--- | :--- | :--- |
| **Descoberta** | `REQ/REP` | Cliente ↔ Registry | Síncrono e confiável para registro de serviços. |
| **Controle/HB** | `REQ/REP` | Cliente ↔ Broker | Utilizado para login, estatísticas e heartbeats. |
| **Vídeo (Envio)** | `PUB/SUB` | Cliente → Broker | Não-bloqueante. Perder frames de vídeo é aceitável (`SNDHWM=2`). |
| **Áudio (Envio)** | `PUB/SUB` | Cliente → Broker | Não-bloqueante. Perder chunks de áudio é imperceptivel (`SNDHWM=4`). |
| **Texto (Envio)** | `PUSH/PULL` | Cliente → Broker | Confiável. Nenhuma mensagem de chat pode ser perdida (`SNDTIMEO=500ms`). |
| **Mídia/Texto (Recebimento)** | `PUB/SUB` | Broker → Cliente | Filtro por sala. Apenas assinantes da sala correta recebem os dados. |
| **Malha Cluster** | `ROUTER/DEALER` | Broker ↔ Broker | Comunicação assíncrona bidirecional entre brokers. |

> **Evolução de Design — Canal Cliente → Broker**
>
> **Versão 1 — PUB/SUB puro:** A primeira versão usava `PUB` nos clientes para todos os canais.
> Após um *failover*, o socket `PUB` recriado sofria com o **"slow-joiner syndrome"**: o handshake
> de inscrição não concluía a tempo e mensagens eram **descartadas silenciosamente**, sem qualquer erro.
>
> **Versão 2 — PUSH/PULL puro:** Migrar todos os canais para `PUSH/PULL` resolveu o problema de texto,
> mas o `PUSH` é **bloqueante**. Com dois clientes enviando vídeo/áudio em alta frequência, o buffer
> do broker enchia e os sockets `PUSH` travavam. Isso congelava o `send_loop` do cliente, o broker
> perdia CPU para enviar heartbeats ao Registry e era removido como inativo.
>
> **Versão 3 (atual) — Híbrida por tipo:** A solução definitiva separa a estratégia por canal:
> - **Vídeo/Áudio** usam `PUB/SUB` com `SNDHWM` baixo, descartando dados antigos sob carga.
> - **Texto** usa `PUSH/PULL` com `SNDTIMEO=500ms`, garantindo entrega sem risco de travar o sistema.

---

## 3. Qualidade de Serviço (QoS) e Resiliência

### 3.1 Vídeo Adaptativo (Bitrate Variável)
Uma das funcionalidades mais avançadas do projeto. O sistema monitora a saturação das filas de envio (`video_q`):
- **Cenário Ideal**: Qualidade JPEG 60 e resolução nativa.
- **Congestionamento Médio**: Redução da qualidade JPEG para 30.
- **Congestionamento Crítico**: Qualidade JPEG 15 e **redimensionamento do frame** (Resize 50%) para reduzir drasticamente o uso de banda.

### 3.2 Tolerância a Falhas (Failover)
O cliente monitora a saúde do broker através de respostas de controle (heartbeat a cada 5s). Se um broker falha:
1. O cliente detecta o timeout no heartbeat.
2. Consulta o **Registry** por um novo broker ativo.
3. Realiza o "re-login" no novo broker.
4. **Recria os sockets** `PUSH` (texto) e `SUB` (mídia) apontando para o novo broker — mantendo a sala original.
5. A comunicação é restabelecida de forma transparente para o usuário.

### 3.3 Estabilidade do Broker — Problema de Deadlock em Peers Mortos

Durante os testes, identificou-se um bug crítico de estabilidade: quando um broker do cluster morria, os demais brokers mantinham os sockets `DEALER` apontados para o broker morto. O ZMQ tenta reconectar indefinidamente e acumula mensagens na fila interna. Com o `SNDTIMEO` padrão (`-1` = bloqueante), ao atingir o HWM, o próximo `send` **bloqueava para sempre**, travando o `_lock` interno do broker e paralisando todas as threads de proxy. O broker sobrevivente parava de enviar heartbeats ao Registry e era removido como inativo.

**Solução implementada em três partes:**
- **`SNDTIMEO=100ms`** nos sockets DEALER: descarta a mensagem se o peer não responder em 100ms, jamais bloqueando.
- **`SNDHWM=10`**: fila interna pequena — o buffer enche rápido e descarta, sem acumular.
- **Limpeza de peers mortos**: a cada ciclo, o `_registry_sync_loop` compara os peers ativos com a lista do Registry e fecha os sockets DEALER de brokers removidos.
- **`_handle_control` resiliente**: erro de socket no handler de controle usa `continue` em vez de `break`, evitando que a thread morra permanentemente por um erro isolado.
- **REQ socket recriado a cada ciclo**: o loop de registro com o Registry recria o socket `REQ` a cada iteração, evitando a trava da máquina de estados (`send→recv→send→recv`) após um timeout.

---

## 4. Suíte de Testes Automatizada

Para validar a complexidade do sistema distribuído, foi desenvolvida uma suíte de testes:

- **`test_registry.py`**: Valida se o Registry gerencia corretamente a entrada e saída de brokers.
- **`test_cluster.py`**: Simula dois brokers em portas distintas (`7000` e `8000`) e valida se uma mensagem enviada ao primeiro chega ao assinante do segundo através da malha inter-broker.
- **`test_failover.py`**: Valida a capacidade do cliente de sobreviver à queda de um servidor.
- **`run_all.py`**: Script de execução em massa com monitoramento em tempo real.

---

## 5. Instalação e Execução

### 5.1 Instalação de Dependências
O projeto requer Python 3.8+ e algumas bibliotecas específicas. 
Para instalar todas as dependências, abra um terminal na raiz do projeto e execute:

```bash
pip install -r requirements.txt
```

> **Nota para Windows**: Se o `PyAudio` der erro de compilação durante a instalação, você pode baixar o arquivo `.whl` pré-compilado adequado para sua versão do Python em sites como o de Christoph Gohlke ou instalar via `conda install pyaudio`.

### 5.2 Execução Local (Mesma Máquina)
1. **Registry**: 
   ```bash
   python discovery/registry.py
   ```
2. **Brokers** (Abra quantos desejar em terminais diferentes):
   ```bash
   python broker/broker.py --port 7000
   python broker/broker.py --port 8000
   ```
3. **Clientes**: 
   ```bash
   python client/client.py
   ```
   - Informe seu nome e a sala (ex: `A`) para entrar no chat.
   - Pressione **Enter** quando pedir o IP do Servidor para conectar no `localhost`.
   - Uma janela de vídeo abrirá. Use o terminal para enviar textos. Digite `/sair` para encerrar.

### 5.3 Execução em Rede Local (LAN entre PCs diferentes)
Para comunicação entre computadores diferentes na mesma rede Wi-Fi/cabo:

**No Computador Servidor (Ex: IP `192.168.1.50`):**
1. **Registry**: `python discovery/registry.py`
2. **Brokers** (Anuncie o IP do servidor para a rede):
   ```bash
   python broker/broker.py --port 7000 --host 192.168.1.50
   python broker/broker.py --port 8000 --host 192.168.1.50
   ```

**Nos Computadores Clientes:**
1. Inicie o cliente:
   ```bash
   python client/client.py
   ```
2. Quando perguntado, insira o **IP do Servidor** (`192.168.1.50`). Os clientes buscarão o Registry na rede e farão toda a negociação de comunicação e vídeo remotamente!

---
**Equipe**: Enzo Dezem Alves RA: 801743 , Joao Vitor T. Arroyo 814135 ,João Lucas Gomes Pelegrino  RA: 822033,Felipe  Penteado - 831439, Levir de Oliveira - 790285
**Data**: 30/04/2026
