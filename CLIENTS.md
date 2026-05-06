# Clientes da federação

Como rodar o cliente de cada grupo enquanto a federação está no ar
(`python federation/launch_all.py`). Sala canônica: `A`–`K`.
Cada grupo aceita variações (`ROOM_A`, `a`, etc.) — o SuperBroker normaliza.

Todos os comandos rodam a partir da raiz do repositório (PowerShell).

---

## grupo_i

```powershell
python Sistemas_Distribuidos_GrupoI_Entrega1/client.py `
  --user Ivan --room ROOM_A --registry tcp://localhost:7100
```

Sala usa prefixo `ROOM_`.

---

## googlemeet

```powershell
cd GoogleMeet_Replica
python cli.py
cd ..
```

Interativo. Digita `Username` e `Sala` (ex: `ROOM_A`). Registry 7200 já é default.

---

## expansion

```powershell
python Expansion/member.py --id Ivan --room A --discovery tcp://localhost:7300
```

Discovery padrão é 5570 (standalone) — precisa apontar pra 7300 da federação.

---

## sd_trab1

```powershell
cd sd-trab1
python cli.py --user Ivan --room a
cd ..
```

Sala em minúsculo.

---

## sd_meeting

```powershell
cd sd-meeting-app
python client.py --username Ivan --room A --no-av
cd ..
```

`--no-av` desativa áudio/vídeo (apenas texto). Lê `config.yaml`; federação usa
porta 7500 do registry.

---

## t1sistemas

```powershell
cd T1SitemasDistribuidos
python cli.py --nome Ivan --sala A
cd ..
```

Registry padrão `localhost:7650` já bate com federação.

---

## trabalho1

```powershell
cd Trabalho_1_Distribuidos
python client.py
cd ..
```

Interativo. Pede `ID` e `Sala` (`A`–`K`). Portas hardcoded 5555-5562 — coincidem
com a configuração da federação.

---

## sd_trabalho

```powershell
cd sd-trabalho
$env:REGISTRY_HOST="localhost"; $env:REGISTRY_PORT="7800"
python -m client.client --id Ivan --room A
cd ..
```

Tem que ser executado como módulo (`-m client.client`) por causa dos imports
relativos. Registry padrão é 5550 — sobrescrito por env.

---

## ufscar

```powershell
python federation/ufscar_client.py Ivan
```

Depois, no prompt: `/join A`. Wrapper em `federation/` porque o
`examples/start_client.py` usa portas hardcoded (5555+) que não batem com
a federação (7900–7907).

Comandos: `/join <A-K>`, `/leave`, `/users`, `/groups`, `/quit`,
`<mensagem>` (envia ao grupo atual).

---

## videoconf

```powershell
python federation/videoconf_client.py Ivan A
```

Wrapper em `federation/` porque `videoconf_dist/src/shared/config.py` é
hardcoded em 5555/5556/5557; o wrapper monkeypatcha o módulo para usar
8000/8001/8002 antes de subir os clientes de texto/áudio/vídeo.
