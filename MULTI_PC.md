# Federação multi-PC na mesma LAN

Guia para rodar a federação `grupo_de_grupos` distribuída — cada grupo
num PC diferente, todos na mesma rede local.

Modo single-PC continua funcionando: defaults são `localhost`, basta
rodar `python federation/launch_all.py` como antes.

---

## Topologia

```
PC-fed   (host central)
  ├── FedRegistry  :7777
  └── SuperBroker  :8101 / :8103 / :8105

PC-A → roda grupo_i        PC-D → roda sd_trab1     PC-G → roda trabalho1
PC-B → roda googlemeet     PC-E → roda sd_meeting   PC-H → roda sd_trabalho
PC-C → roda expansion      PC-F → roda t1sistemas   PC-I → roda ufscar
                                                    PC-J → roda videoconf
```

PC-fed pode ser um dos PCs de grupo (acumula papéis) ou dedicado.
Clientes finais conectam no broker do **próprio grupo** (mesmo PC), não
no SuperBroker.

---

## Pré-requisitos

1. Todos os PCs na mesma sub-rede / VLAN, sem NAT entre eles.
2. Wi-Fi sem isolamento de cliente (AP isolation OFF).
3. Python 3.10+ e dependências instaladas em cada PC:
   ```powershell
   pip install -r requirements.txt
   pip install -r <grupo>/requirements.txt   # se aplicável
   ```
4. Firewall Windows liberado para as portas do grupo daquele PC + portas
   do SuperBroker/FedRegistry no PC central. Exemplo (admin PowerShell):
   ```powershell
   New-NetFirewallRule -DisplayName "Federacao SD" -Direction Inbound `
     -Protocol TCP -LocalPort 5555-8200 -Action Allow
   ```
5. Relógios sincronizados (NTP). Importa para grupos que usam timestamp.

---

## Passo 1 — descobrir IPs

Em cada PC:

```powershell
ipconfig | Select-String IPv4
```

Anotar IP de cada PC. Exemplo deste guia:

| PC      | Função      | IP             |
|---------|-------------|----------------|
| PC-fed  | super+reg   | 192.168.1.10   |
| PC-A    | grupo_i     | 192.168.1.20   |
| PC-B    | googlemeet  | 192.168.1.21   |
| PC-C    | expansion   | 192.168.1.22   |
| ...     | ...         | ...            |

---

## Passo 2 — editar `federation/ports.yaml`

Substituir `localhost` pelo IP de cada grupo + central. **O mesmo arquivo
deve estar idêntico em todos os PCs** (sincronizar via git ou cópia).

```yaml
fed_registry_host: 192.168.1.10
fed_registry_port: 7777

super_broker_host: 192.168.1.10
super_broker:
  txt_xsub: 8100
  txt_xpub: 8101
  # ...

groups:
  grupo_i:
    host: 192.168.1.20
    # ... resto inalterado
  googlemeet:
    host: 192.168.1.21
    # ...
  expansion:
    host: 192.168.1.22
    # ...
```

`{host}` em `launch.broker` e `env:` é substituído automaticamente pelo
`host:` do grupo (já configurado para `grupo_i` e `sd_trabalho`).

---

## Passo 3 — subir os processos

Em **cada PC**, rodar o launcher. Ele detecta o host local e sobe só os
serviços daquele PC.

### No PC central (PC-fed)

```powershell
python federation/launch_all.py
```

Sobe FedRegistry + SuperBroker. Não sobe nenhum grupo (porque nenhum
`host:` em ports.yaml aponta pra ele, salvo se acumular papéis).

### Nos PCs dos grupos

```powershell
python federation/launch_all.py
```

Sobe registry + broker do(s) grupo(s) cujo `host:` bate com o IP/hostname
deste PC. Pula os grupos remotos. Não sobe SuperBroker/FedRegistry.

### Flags úteis

| Flag                   | Quando usar                                            |
|------------------------|--------------------------------------------------------|
| `--list`               | Inspecionar mapa de hosts/portas                       |
| `--all-local`          | Forçar tudo no PC atual (modo single-PC retrocompat)   |
| `--host 192.168.1.20`  | Fingir ser outro PC (testes em VM única)               |
| `--no-super`           | Não subir SuperBroker (se outro PC já roda)            |
| `--no-fed-registry`    | Não subir FedRegistry                                  |
| `--groups grupo_i`     | Restringir a um subset                                 |

---

## Passo 4 — rodar clientes

Cliente conecta no broker do **próprio grupo**. Setar variável de
ambiente no PC do cliente apontando para o PC do broker daquele grupo
(normalmente o mesmo PC, mas pode ser remoto).

### grupo_i

```powershell
python Sistemas_Distribuidos_GrupoI_Entrega1/client.py `
  --user Ivan --room ROOM_A --registry tcp://192.168.1.20:7100
```

### googlemeet

```powershell
$env:REGISTRY_HOST = "192.168.1.21"
$env:REGISTRY_PORT = "7200"
cd GoogleMeet_Replica
python cli.py
cd ..
```

### expansion

```powershell
$env:DISCOVERY_ADDR = "tcp://192.168.1.22:7300"
python Expansion/member.py --id Ivan --room A
```

### sd_trab1

```powershell
$env:BROKER_HOST = "192.168.1.23"
$env:DISCOVERY_PORT = "6000"
python sd-trab1/client.py
```

(UDP discovery do sd_trab1 detecta host automaticamente se broker fizer
broadcast na sub-rede; `BROKER_HOST` é fallback.)

### sd_meeting

Editar `sd-meeting-app/config_fed.yaml`:
```yaml
client:
  registry_host: 192.168.1.24
```
Depois:
```powershell
cd sd-meeting-app
python client.py
cd ..
```

### t1sistemas

```powershell
python T1SitemasDistribuidos/client/client.py --registry 192.168.1.25
```

### trabalho1

```powershell
$env:BROKER_HOST = "192.168.1.26"
python Trabalho_1_Distribuidos/client.py
```

### sd_trabalho

```powershell
$env:REGISTRY_HOST = "192.168.1.27"
$env:REGISTRY_PORT = "7800"
python -m sd-trabalho.client
```

### ufscar

```powershell
$env:BROKER_HOST = "192.168.1.28"
$env:DISCOVERY_HOST = "192.168.1.28"
python ufscar-sd/examples/start_client_gui.py alice
```

### videoconf

```powershell
$env:BROKER_HOST = "192.168.1.29"
python videoconf_dist/src/client/client.py Ivan SALA_A
```

---

## Passo 5 — validar federação

Do PC central (ou qualquer PC com acesso à 192.168.1.10:8101):

```powershell
python federation/test_federation.py
```

Lê `ports.yaml` e injeta texto em cada grupo, verificando que o
SuperBroker reemite no `txt_xpub` (8101). Saída esperada:

```
=== RESULTADO ===
  grupo_i         ✓
  googlemeet      ✓
  ...
Todos os grupos conectados ao SuperBroker!
```

---

## Troubleshooting

| Sintoma                                          | Causa provável                                      |
|--------------------------------------------------|-----------------------------------------------------|
| `[launch] Nenhum serviço deste host`             | `host:` em ports.yaml não bate com IP/hostname local. Conferir `ipconfig`. Workaround: `--host <alias>`. |
| Cliente conecta mas não recebe mensagens         | Broker bindando `127.0.0.1` em vez de `0.0.0.0`. Toca-se no código do grupo. |
| `[SuperBroker] inject ...` ok mas grupo não vê   | Firewall do PC do grupo bloqueando porta inject.    |
| Registry não acha brokers remotos                | Broker registrando com `localhost` em vez de IP real. Verificar arg `--host` ou env `*_ADVERTISE_HOST`. |
| Sd_trab1 cliente não acha broker                 | UDP broadcast (porta 6000) não atravessa sub-rede. Rodar broker com `BROADCAST=255.255.255.255` ou setar `BROKER_HOST` no cliente. |
| `kill_port` não acha porta no PC remoto          | Esperado — launcher só mata portas locais. Rodar launcher no PC certo. |

---

## Arquitetura de mudança vs single-PC

Diferenças do que existia antes:

- `ports.yaml`: novos campos `fed_registry_host`, `super_broker_host`,
  `host:` por grupo. Default `localhost`.
- `super_broker.py`: SUB e inject usam `tcp://{host}:{port}`.
- `launch_all.py`: filtra grupos por host local; substitui `{host}` em
  comandos e env.
- Clientes hardcoded (`trabalho1`, `googlemeet`, `sd_trab1`, `expansion`,
  `videoconf`, `ufscar`) agora aceitam env vars (`BROKER_HOST`,
  `REGISTRY_HOST`, etc.).

Reverter para single-PC: deixar todos os `host:` em `localhost` ou usar
`--all-local`.
