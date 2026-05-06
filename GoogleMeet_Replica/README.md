# GoogleMeet_Replica

Projeto de Sistemas Distribuídos 2026

Ferramenta de videoconferência com suporte a texto, áudio e vídeo em Python 3, usando ZeroMQ para comunicação assíncrona.

- Gabriel Lucchetta Garcia Sanchez - 828513
- Vinícius Yuya Massuda - 834426
- Ivan Fernando Rizzi Villalba - 821478
- Gabriel de Souza Cavalca Leite - 813615

---

## 1. Visão Geral do Projeto

O sistema é composto por três camadas:
- `registry.py`: serviço de descoberta de brokers e balanceamento round-robin.
- `broker.py`: brokers cooperativos que roteiam mensagens entre clientes e entre brokers.
- `client.py` + `gui.py`: clientes que enviam/recebem texto, vídeo e áudio.

### Implementações entregues
- Arquitetura distribuída com múltiplos brokers cooperando.
- Descoberta dinâmica de serviços via registry.
- Tolerância a falhas: heartbeat, detecção de broker offline e failover automático.
- QoS simplificado:
  - Texto: retry e fila de reenvio.
  - Áudio: baixa latência e descarte natural de pacotes antigos.
  - Vídeo: taxa adaptativa e drop de frames atrasados.
- Identidade única e controle de presença por sala.
- GUI desktop com múltiplas pré-visualizações de vídeo remotas.

---

## 2. Arquitetura Técnica

### Componentes ZeroMQ
- `REQ/REP` entre clientes/brokers e registry para descoberta e seleção de broker.
- `XPUB/XSUB` nos brokers para receber publicações de clientes e enviar assinaturas locais.
- `PUB/SUB` para heartbeat dos brokers e para cluster de brokers.

### Fluxo de mensagens
- Cliente publica texto, áudio e vídeo para seu broker local.
- Broker encaminha a mensagem para assinantes locais e publica ao cluster de brokers.
- Outros brokers recebem as mensagens do cluster e entregam aos seus assinantes locais.
- Isso permite que usuários em diferentes brokers compartilhem a mesma sala.

### Tolerância a falhas
- Brokers enviam heartbeat periódico em `HB/<BROKER_ID>|ALIVE`.
- Registry monitora os heartbeats e marca brokers offline.
- Cliente detecta perda do broker local e faz failover consultando o registry novamente.
- O estado de sessão do usuário é preservado pela reconexão automática e reenvio de texto pendente.

### Controle de presença
- A interface exibe participantes por sala.
- Entrada e saída são anunciadas com mensagens de sistema.
- Mensagens de presença são usadas para atualizar a lista de participantes e liberar telas de vídeo.

---

## 3. Como executar

### Pré-requisitos
```bash
pip install -r requirements.txt
```

> Observação: no Windows, o PyAudio pode precisar de instalação de binários compatíveis. Se necessário, use `pip install pipwin` e `pipwin install pyaudio`.

### Passos de execução

1. Inicie o registry:
```bash
python registry.py
```

2. Inicie um ou mais brokers em portas diferentes:
```bash
python broker.py --id BROKER1
```

Se você quiser definir portas explicitamente ou se a porta 5555 já estiver em uso, use:
```bash
python broker.py --id BROKER1 --pub-port 5555 --sub-port 5556 --aud-pub-port 5557 --aud-sub-port 5558 --vid-pub-port 5559 --vid-sub-port 5560 --hb-port 5561
```

Em outra janela:
```bash
python broker.py --id BROKER2
```

O `broker.py` aplica offset automático de porta por ID, então em `BROKER2` ele usará a faixa 6555/6556/6557/6558/6559/6560/6561 por padrão.

3. Inicie clientes:
```bash
python gui.py
```

Execute múltiplas instâncias do `gui.py` para simular vários usuários.

---

## 4. Demonstração obrigatória

### Cenário 1: comunicação entre usuários
1. Inicie o `registry.py`.
2. Inicie pelo menos dois brokers (`BROKER1`, `BROKER2`).
3. Abra duas instâncias de `gui.py`.
4. Entre na mesma sala em cada cliente.
5. Envie mensagens de texto, áudio e vídeo.
6. Observe que os usuários aparecem na lista de participantes e que o vídeo remoto é exibido.

### Cenário 2: falha de broker e reconexão automática
1. Com clientes conectados, pare um broker (`Ctrl+C`).
2. Observe a janela de reconexão no cliente.
3. O cliente deve reconectar automaticamente a outro broker disponível.
4. Verifique que mensagens de texto e mídia voltam a ser entregues após o failover.

### Cenário 3: múltiplos brokers cooperando
1. Inicie dois brokers diferentes e vários clientes.
2. Verifique que cada broker encaminha mensagens para o outro pelo cluster.
3. Ao enviar mensagens de um cliente conectado a `BROKER1`, clientes conectados a `BROKER2` também recebem.

---

## 5. Testes

### Verificar registry
```bash
python test_registry.py
```

### Compilar arquivos Python
```bash
python -m py_compile client.py broker.py registry.py gui.py
```

---

## 6. Estrutura do código

- `registry.py`: registro e saúde de brokers.
- `broker.py`: proxy de mensagens e cluster entre brokers.
- `client.py`: descoberta de broker, failover, envio e recepção de mídia.
- `gui.py`: interface desktop, chat, pré-visualização local e remota.
- `audio.py`: captura e reprodução de áudio.
- `camera.py`: captura de vídeo ou placeholder quando a câmera não está disponível.
