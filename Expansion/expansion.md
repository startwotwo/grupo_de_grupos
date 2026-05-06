# Sistema de Videoconferência Distribuído

Sistema de videoconferência peer-to-peer com áudio, vídeo e texto, construído sobre **ZeroMQ**, com arquitetura distribuída tolerante a falhas: múltiplos brokers cooperando em malha, descoberta dinâmica de serviços e migração automática de clientes em caso de falha.

Este documento descreve a evolução do MVP inicial (1 broker centralizado) para um sistema distribuído com múltiplos brokers, service discovery, failover automático e suporte a salas.

---

## Sumário

- [Arquitetura](#arquitetura)
- [Componentes](#componentes)
- [Padrões ZeroMQ utilizados](#padrões-zeromq-utilizados)
- [Formato de mensagens](#formato-de-mensagens)
- [Tolerância a falhas](#tolerância-a-falhas)
- [Controle de Qualidade (QoS)](#controle-de-qualidade-qos)
- [Identidade, salas e presença](#identidade-salas-e-presença)
- [Como executar](#como-executar)
- [Comandos do cliente](#comandos-do-cliente)
- [Demonstração de failover](#demonstração-de-failover)
- [Decisões de design relevantes](#decisões-de-design-relevantes)

---

## Arquitetura

```
                       ┌────────────────────┐
                       │  Discovery Service │  REP socket (porta 5570)
                       │   (registry.py)    │  registry de brokers vivos
                       └─────────┬──────────┘
                                 │ REGISTER, HEARTBEAT, LIST
            ┌────────────────────┼────────────────────┐
            │                    │                    │
            ▼                    ▼                    ▼
   ┌──────────────┐      ┌──────────────┐      ┌──────────────┐
   │  Broker B1   │◄────►│  Broker B2   │◄────►│  Broker BN   │
   │  XSUB/XPUB   │ mesh │  XSUB/XPUB   │ mesh │  XSUB/XPUB   │
   │  4 canais    │      │  4 canais    │      │  4 canais    │
   └──────┬───────┘      └──────┬───────┘      └──────┬───────┘
          │                     │                     │
       clientes              clientes              clientes
       locais                locais                locais
```

Cada **broker** é um proxy XSUB/XPUB com 4 canais independentes (vídeo, áudio, texto, presença). Os brokers se conhecem via **discovery** e formam um **mesh**: cada broker assina os canais de todos os outros e reinjeta as mensagens recebidas no seu próprio fluxo local. Isso permite que clientes conectados em brokers diferentes troquem mensagens transparentemente.

Os **clientes** consultam o discovery na inicialização, escolhem um broker aleatoriamente (load balancing simples) e mantêm um **watchdog** que monitora a saúde do broker atual. Se o broker cair, o cliente migra automaticamente para outro broker disponível, recriando o `zmq.Context` para garantir um estado limpo.

---

## Componentes

| Arquivo | Responsabilidade |
|---------|------------------|
| `common.py` | Constantes compartilhadas (portas, comandos do discovery, formato de tópicos, salas válidas) |
| `discovery.py` | Service Discovery via REP socket. Mantém o registry de brokers vivos com timeout por heartbeat |
| `broker.py` | Broker de mensagens. Implementa proxy XSUB/XPUB local, registro no discovery e mesh entre brokers |
| `member.py` | Cliente. Captura/renderização de mídia, conexão a broker via discovery, watchdog para failover, salas e presença |
| `launch.py` | Orquestrador em Python. Sobe discovery + brokers + clientes em janelas separadas e oferece modo "demo failover" |

---

## Padrões ZeroMQ utilizados

| Padrão | Onde é usado | Por quê |
|--------|--------------|---------|
| **PUB/SUB** | Cliente → broker (envio) e broker → cliente (recepção) nos 4 canais | Disseminação 1:N. Filtro por tópico no socket SUB. Broker não precisa rastrear destinatários individualmente |
| **XSUB/XPUB** | Frontend/backend do broker | Permite que o broker faça `zmq.proxy` entre publishers e subscribers, propagando subscriptions automaticamente |
| **REQ/REP** | Discovery service e watchdog | Comunicação síncrona com timeout. Ideal para registro, heartbeat e listagem onde resposta é esperada |
| **PUB/SUB (entre brokers)** | Mesh: cada broker tem um SUB conectado aos XPUBs dos outros | Forma a malha de roteamento sem precisar de topologia mais complexa (ROUTER/DEALER) |

A escolha de PUB/SUB para o mesh entre brokers (em vez de ROUTER/DEALER) foi proposital: PUB/SUB é mais simples e a malha completa (cada broker conectado a todos) garante propagação em **1 hop**, eliminando a necessidade de roteamento mais sofisticado.

---

## Formato de mensagens

Toda mensagem trafega como `multipart` com 2 frames:

```
Frame 1 (tópico):   "{canal}:{broker_origem}:{sala}:{user_id}"
Frame 2 (payload):  bytes específicos do canal
```

Exemplos:
```
Tópico: "text:B1:A:Ivan"        Payload: "Ivan|olá pessoal"
Tópico: "video:B2:B:Anna"       Payload: <bytes JPEG>
Tópico: "audio:B1:A:Fuji"       Payload: <PCM int16 16kHz>
Tópico: "presence:B2:A:Bob"     Payload: "JOIN" | "LEAVE" | "WHOIS"
```

**Por que tudo no tópico?** O ZMQ filtra mensagens por **prefixo do primeiro frame**. Colocar canal, broker e sala no tópico permite:

- **Filtro nativo por canal** via `setsockopt(SUBSCRIBE, "video:")`
- **Filtro de loop no mesh** comparando o `broker_origem` com o id local
- **Filtro de sala** comparando com a sala do cliente (feito em código, mas barato)

---

## Tolerância a falhas

### Detecção de broker morto

O discovery mantém um campo `last_seen` para cada broker registrado. Brokers enviam heartbeats a cada `1.5s`. Se um broker passa `5s` sem heartbeat, ele é considerado morto e removido da listagem.

### Migração automática do cliente (failover)

Cada cliente roda uma thread `watchdog` que, a cada `2s`:

1. Pergunta ao discovery a lista de brokers vivos
2. Verifica se o broker atual ainda está na lista
3. Se não está, escolhe outro broker aleatoriamente e dispara `Conn.swap()`

O `Conn.swap()` é a operação central do failover. Ele:

1. Cria um **novo `zmq.Context`** (não reutiliza o anterior)
2. Cria os 8 sockets novos (4 PUB + 4 SUB) nesse novo context
3. Conecta todos no broker novo
4. Aguarda `0.5s` para subscriptions propagarem
5. Atomicamente substitui as referências dentro de um lock
6. Fecha os sockets antigos
7. Destroi o context antigo via `destroy(linger=0)`
8. Limpa a lista local de membros online
9. Publica um burst de `JOIN` + `WHOIS` para se reanunciar e provocar respostas

**Por que recriar o context inteiro?** Em testes, descobrimos que reusar o context entre swaps produz estado inconsistente nas subscriptions ZMQ — especialmente quando múltiplos clientes migram simultaneamente do mesmo broker morto. Recriar o context emula um restart limpo e elimina esse problema. O custo é desprezível (poucos milissegundos).

### Detecção de peer morto entre brokers

O `mesh_bridge` no broker consulta o discovery a cada `3s` para descobrir peers novos. Quando um peer aparece, abre conexões SUB para ele. Se um peer desaparece, **não fazemos nada** — o ZMQ trata reconexão internamente, e tentativas para um peer morto não causam problema funcional.

---

## Controle de Qualidade (QoS)

Cada canal tem características diferentes, refletidas em `HWM` (High Water Mark) e estratégia de envio:

| Canal | RCVHWM | SNDHWM | Estratégia | Justificativa |
|-------|--------|--------|------------|---------------|
| **Vídeo** | 5 | 5 | `send_multipart(NOBLOCK)` + drop em `zmq.Again`. Fila local de tamanho 2 com sobrescrita | Frame antigo é inútil; preferimos perder frames a acumular latência |
| **Áudio** | 20 | 20 | `send_multipart(NOBLOCK)` + drop em `zmq.Again`. Fila local de tamanho 50 com drop-old | Latência baixa é crítica; pequena perda é tolerável (gera estalo, mas não trava) |
| **Texto** | 1000 | 1000 | Buffer alto, sem drop | Garantia de entrega — nenhuma mensagem deve ser perdida |
| **Presença** | 1000 | 1000 | Sem drop | Estado de quem-está-online não pode ser perdido |

Adicionalmente, o vídeo é throttled no envio para `15 FPS` com qualidade JPEG 60% e resolução `640×480`, evitando saturação de rede.

---

## Identidade, salas e presença

### Identidade

Cada cliente tem um `member_id` único, fornecido via `--id` ou gerado automaticamente como UUID curto. O id aparece em todas as mensagens publicadas (no tópico) e identifica o remetente.

### Salas

São suportadas 11 salas: `A` a `K`. Clientes em salas diferentes não veem nada uns dos outros — o filtro é feito no cliente, comparando o campo `sala` do tópico com a sala atual (`conn.room`). Não filtramos via `SUBSCRIBE` porque a quantidade de prefixos possíveis (`text:B1:A:`, `text:B2:A:`, …) cresceria com o número de brokers.

Comandos:
- `--room A` na inicialização
- `/room B` em runtime para mudar de sala (republica `LEAVE` na antiga e `JOIN` na nova)

### Presença

Implementada como um quarto canal PUB/SUB. Cada cliente:

- Publica `JOIN` ao entrar e a cada `1.5s` (re-anúncio periódico)
- Publica `LEAVE` ao sair (graceful)
- Publica `WHOIS` após failover, pedindo que outros respondam com `JOIN` imediato
- Mantém localmente um `set` de membros vistos recentemente, com timeout de `6s` (4× o intervalo de re-anúncio)

O comando `/who` exibe a lista atual.

---

## Como executar

### Pré-requisitos

```
python -m pip install pyzmq opencv-python numpy sounddevice
```

(no Windows: pode ser necessário Visual C++ Redistributable para o `sounddevice`)

### Rodar tudo de uma vez

```
python launch.py
```

O `launch.py`:

1. Sobe o `discovery.py` em uma janela
2. Sobe `broker.py --id B1 --base-port 5555` em outra
3. Sobe `broker.py --id B2 --base-port 5575` em outra
4. Sobe três clientes (`Ivan`, `Anna`, `Fuji`) na sala A
5. Aguarda a tecla ENTER para opcionalmente matar B1 (demo de failover)
6. Após nova ENTER, encerra todos os processos

### Rodar componentes manualmente

Em terminais separados:

```
python discovery.py
python broker.py --id B1 --base-port 5555
python broker.py --id B2 --base-port 5575
python member.py --id Ivan --room A
python member.py --id Anna --room A
```

### Argumentos relevantes

**broker.py**
- `--id` — identificador único do broker (obrigatório)
- `--base-port` — porta inicial; ocupa `base..base+7` (4 canais × 2 portas)
- `--discovery` — endereço do discovery (default: `tcp://localhost:5570`)
- `--host` — IP que clientes usarão para conectar (default: IP local detectado)

**member.py**
- `--id` — identificador do membro (default: UUID curto)
- `--room` — sala inicial, A–K (default: A)

---

## Comandos do cliente

Dentro de uma sessão do `member.py`:

| Comando | Efeito |
|---------|--------|
| `<texto>` | Envia mensagem de texto para a sala |
| `/who` | Lista membros online na sala atual |
| `/room X` | Muda para a sala X (A–K) |
| `exit` | Encerra o cliente (publica `LEAVE` antes de sair) |

Na janela de vídeo:
- `b` — sair pela janela de vídeo

---

## Demonstração de failover

A demo segue este roteiro:

1. **Setup**: discovery + 2 brokers + 3 clientes na sala A, todos se vendo
2. **Conversa estável**: clientes trocam texto, áudio, vídeo. Comando `/who` mostra os 3
3. **Falha do broker**: `Ctrl+C` (ou `taskkill`) em um dos brokers
4. **Detecção**: em até `5s`, discovery imprime `[discovery] TIMEOUT B1`
5. **Migração**: em até `2s` adicionais, watchdogs dos clientes detectam ausência e disparam `swap()`. Cada cliente imprime `[client] conectado em B2 ... — context recriado`
6. **Recuperação completa**: clientes republicam `WHOIS` e se redescobrem. `/who` volta a listar os 3
7. **Conversa retomada**: mensagens, áudio e vídeo voltam a fluir entre todos via B2

Tempo total típico de recuperação: `7-10 segundos`.

---

## Decisões de design relevantes

### Por que recriar o `zmq.Context` no failover?

Em testes com múltiplos clientes migrando simultaneamente do mesmo broker, observamos que reaproveitar o context produzia subscriptions inconsistentes — clientes deixavam de receber mensagens de outros clientes do mesmo broker novo, mesmo após `setsockopt(SUBSCRIBE)`. Recriar o context resolve definitivamente. O custo é mínimo (poucos ms) e o comportamento fica idêntico ao de um restart manual do cliente.

### Por que o broker não usa ROUTER/DEALER no mesh?

PUB/SUB é mais simples e suficiente para a topologia em malha completa. Como cada broker se conecta a todos os outros, mensagens atravessam **no máximo 1 hop**, eliminando necessidade de roteamento. ROUTER/DEALER seria adequado se quiséssemos topologia em árvore ou cadeia, mas isso adicionaria complexidade sem ganho prático.

### Como evitamos loops no mesh?

Cada mensagem carrega o `broker_origem` no tópico. Quando o `mesh_bridge` recebe uma mensagem via SUB de outro broker, verifica se o `broker_origem` é o próprio id — se for, descarta. Isso evita que mensagens reinjetadas ricocheteiem entre brokers infinitamente.

### Por que PUB/SUB em vez de manter sessões com cada cliente?

Numa videoconferência típica, todos enviam para todos. Manter sessões individuais por cliente (como em ROUTER/DEALER) exigiria que o broker rastreasse cada destinatário e reenviasse N vezes por mensagem. Com PUB/SUB, a entrega 1:N é feita pelo ZMQ no kernel, sem custo extra para o broker.

### Por que o filtro de sala é em código e não via SUBSCRIBE?

O ZMQ filtra por prefixo do primeiro frame. Para filtrar via SUBSCRIBE pela sala, precisaríamos assinar `text:B1:A:`, `text:B2:A:`, …, um prefixo por broker existente. Como a lista de brokers é dinâmica, manter as subscriptions atualizadas seria custoso. A alternativa adotada (assinar `text:` e filtrar a sala em código) tem custo desprezível porque salas são poucas e as mensagens descartadas chegam só do canal já filtrado.

### Por que recriamos sockets no `_build_with_ctx` mas não no broker mesh?

No cliente, recriação acontece raramente (só em failover). Já no broker, mudanças de peers acontecem com frequência (heartbeats, novos brokers entrando) e recriar tudo a cada mudança causa **flapping**: as conexões TCP entre brokers oscilavam, derrubando o forwarding momentaneamente e fazendo mensagens se perderem. Por isso, o broker apenas **adiciona** novas conexões SUB conforme novos peers aparecem, e deixa o ZMQ lidar com peers que somem.