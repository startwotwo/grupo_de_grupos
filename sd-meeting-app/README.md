# SD Meeting App

Sistema de videoconferência distribuído, resiliente e escalável, construído com Python 3 e ZeroMQ.

## Integrantes

| Nome                         | RA     |
| ---------------------------- | ------ |
| Rodrigo Coffani              | 800345 |
| Pedro Yuji Teixeira Harada   | 800636 |
| Murilo de Miranda Silva      | 812069 |
| Guilherme Barbosa            | 811692 |
| Sérgio Felipe Bezerra Rabelo | 812205 |

## Pré-requisitos

- Python 3.11+
- Sistema Linux (ou WSL/PowerShell no Windows)
- Microfone e câmera (opcionais)

## Instalação

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

## Como rodar

**Demo automatizado** (tudo em um comando):

```bash
source venv/bin/activate && python3 run_demo.py
```

**Manual com GUI** (abra 4 terminais):

```bash
# Terminal 1: Registry
python3 registry.py
# Terminal 2: Broker-0
python3 broker.py 0
# Terminal 3: Broker-1
python3 broker.py 1
# Terminal 4+: Clientes
python3 client_gui.py
```

**Manual com CLI** (abra 4 terminais como acima, terminal 4+):

```bash
python3 client.py --username alice --room A
python3 client.py --username bob --room B --no-av
```

**Scripts shell** (alternativamente):

```bash
chmod +x run_*.sh
./run_registry.sh &
./run_broker.sh 0 &
./run_broker.sh 1 &
./run_client.sh --username alice --room A
```

Veja [PLAN.md](PLAN.md) para arquitetura técnica completa, QoS, padrões ZMQ e detalhes de implementação.

## LAN Setup — Conectar clientes em máquinas diferentes

Para permitir que clientes em diferentes máquinas da LAN participem das mesmas conferências:

### 1. **Encontre o IP da máquina com o Registry**

```bash
# No Linux/macOS:
hostname -I          # retorna algo como: 192.168.1.100 10.0.0.5

# No Windows (PowerShell):
ipconfig | findstr "IPv4"
```

Escolha o IP que está na mesma rede que os outros participantes (ex: 192.168.1.x).

### 2. **Configure o IP no config.yaml** (máquina com Registry)

Edite `config.yaml` e altere:
```yaml
registry:
  listen_host: 0.0.0.0         # escuta em todas as interfaces
  host: 192.168.1.100           # ← MUDE para o IP da sua máquina
  port: 5500
```

### 3. **Inicie Registry, Brokers e Clientes**

**Terminal 1 (máquina com Registry):**
```bash
source venv/bin/activate
python3 registry.py
# Output: [Registry] Escutando em 0.0.0.0:5500
```

**Terminal 2 (máquina com Registry, ou outra máquina qualquer):**
```bash
source venv/bin/activate
python3 broker.py 0 auto    # ← "auto" detecta IP automaticamente
# Output: [Broker-0] ID=abc123... host=192.168.x.y rooms=...
```

**Terminal 3+ (qualquer máquina na LAN):**
```bash
source venv/bin/activate
# Opção A: Editar config.yaml (mesmo que acima)
python3 client.py --username alice --room A

# Opção B: Passar IP via CLI (sem editar config):
python3 client.py --username alice --room A --registry-host 192.168.1.100

# Com GUI:
python3 client_gui.py
# → Quando pedido, use --registry-host se necessário
```

### 4. **Verificar conectividade**

Teste a conexão antes de lançar o app completo:
```bash
# Ping do registry
ping 192.168.1.100

# Teste de porta ZMQ (porta 5500 do registry)
telnet 192.168.1.100 5500
```

### 5. **Firewall — Liberar portas necessárias**

Se os clientes não conseguem se conectar, verifique o firewall:

**Linux (UFW):**
```bash
sudo ufw allow 5500      # Registry
sudo ufw allow 5551:5660 # Broker text/audio/video/control
sudo ufw allow 5700      # Broker heartbeat
sudo ufw allow 5600      # Broker inter-broker relay
```

**Windows (PowerShell como Admin):**
```powershell
New-NetFirewallRule -DisplayName "SD-Meeting Registry" `
  -Direction Inbound -LocalPort 5500 -Protocol TCP -Action Allow

New-NetFirewallRule -DisplayName "SD-Meeting Brokers" `
  -Direction Inbound -LocalPort 5551-5760 -Protocol TCP -Action Allow
```

**macOS:**
```bash
# macOS bloqueia por padrão. Permita o Python:
# Abra System Preferences > Security & Privacy > Firewall > Firewall Options
# → Adicione Python à lista de aplicativos permitidos
```

### 6. **Troubleshooting**

| Problema | Solução |
|----------|---------|
| `Connection refused` | Verifique se registry está rodando; revise firewall |
| `Timeout` | Ping a máquina para confirmar conectividade; mude `registry.host` se necessário |
| `Nome não resolvido (machine1.local)` | Use IP direto; ou verifique DNS/mDNS na rede |
| Broker não se conecta ao registry | Verifique `registry.host` em `config.yaml`; confirme que não está usando `127.0.0.1` |
| Cliente conecta mas não vê broker | Brokers podem estar registrados em outro registry; confirme todos apontam para o mesmo |

---
