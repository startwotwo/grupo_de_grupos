# Videoconferência Distribuída com ZeroMQ

Este projeto é uma ferramenta de videoconferência descentralizada construída em Python3. Ela utiliza **ZeroMQ** para comunicação assíncrona, capturando áudio via PyAudio e vídeo via OpenCV. O sistema é tolerante a falhas e opera com uma arquitetura de múltiplos brokers e descoberta de serviços (Service Discovery).

## 🛠️ Tecnologias e Bibliotecas
- `python 3.9+`
- `pyzmq` (ZeroMQ)
- `opencv-python`
- `pyaudio`
- `numpy`

---

## ✨ Funcionalidades Principais
O projeto atende a rigorosos requisitos de sistemas distribuídos:
- **Identificação de Sessão**: O controle de acesso e identificação intrínseca aos pacotes (tanto em texto quanto em vídeo/áudio) é feito informando `--user` na hora de rodar o cliente.
- **Salas Isoladas (Grupos)**: Os usuários conversam apenas com quem está na mesma sala. O tráfego de uma sala não gera overhead na outra. Esse agrupamento é gerenciado através da flag `--room`.
- **Controle de Presença**: O servidor de roteamento mapeia conexões ativas na memória em tempo real. Qualquer cliente pode digitar o comando `/online` no seu console de execução a qualquer momento para fazer um Request Síncrono e visualizar rapidamente todas as pessoas logadas em sua respectiva sala.
- **Tolerância a Falhas Dinâmica (Failover)**: Se o broker cair repentinamente, o Cliente fará a detecção da ausência de ACK para pacotes de texto e fará a transição automática para outro broker sem desligar o app.

---

## 🏗️ Arquitetura do Sistema

O sistema é dividido em três componentes principais:

1. **Registry (`registry.py`)**: Atua como o "Service Discovery" (Descoberta de Serviço). Ele mantém uma lista dos brokers que estão ativos, recebe *heartbeats* (pulsos de vida) e informa aos clientes para qual broker eles devem se conectar inicialmente.
2. **Broker (`broker.py`)**: O servidor central de roteamento da sala. Ele implementa padrões `PUB/SUB` para áudio e vídeo (focando em baixa latência) e `ROUTER/DEALER` para mensagens de texto e comandos de rede (garantia de entrega via ACKs na aplicação). Múltiplos brokers conversam entre si para escalar tráfego da mesma sala pela rede.
3. **Client (`client.py`)**: A aplicação final que o usuário roda. Captura mídia localmente usando *threads* concorrentes (uma para microfone, outra para câmera) e possui lógica nativa de **tolerância a falhas** na Thread Principal (que roda via DEALER).

---

## 🚀 Como Executar Localmente (Teste em um único PC)

Para rodar tudo no seu próprio computador e testar a comunicação local, instale as dependências com `pip install -r requirements.txt` e abra diferentes abas do terminal.

**1. Inicie o Registry (O Catálogo)**
```bash
python3 registry.py
```

**2. Inicie os Brokers**
```bash
python3 broker.py --port-base 6000
python3 broker.py --port-base 6100  # (Opcional: Suba um segundo broker para permitir failover)
```

**3. Inicie os Clientes (Teste Grupos)**
Abra novos terminais para simular pessoas diferentes conectando no mesmo PC e agrupadas na mesma sala "TrabalhoSD":
```bash
python3 client.py --room TrabalhoSD --user Joao
python3 client.py --room TrabalhoSD --user Maria
```
*(Você pode usar o prompt de texto no terminal para bater-papo como chat ao vivo e usar o comando `/online`!)*

---

## 🌍 Como Executar em Múltiplos Computadores (Rede Real)

Se você quiser testar com 2 ou mais PCs na sua casa (conectados no mesmo roteador), assumiremos que o IP do Servidor é **`192.168.1.50`**.

### No Computador Servidor (PC 1)
```bash
# 1. Suba o Registry normal
python3 registry.py

# 2. Suba o Broker, mas amarre explicitamente ao seu IP de rede
python3 broker.py --host 192.168.1.50 --port-base 6000

# 3. Suba o seu Cliente local (perceba a flag "--registry" com o IP)
python3 client.py --registry tcp://192.168.1.50:5555 --user Servidor
```

### Nos Outros Computadores Visitantes (PC 2, PC 3...)
Apenas baixe os scripts Python. Não é necessário rodar Brokers nem Registry lá!
```bash
python3 client.py --registry tcp://192.168.1.50:5555 --user Visitante
```
---

## 🌩️ Testando Tolerância a Falhas 

A arquitetura exige failover se a rede apresentar problemas ou se acontecer um "Crash" crítico:
1. Rode o Registry.
2. Rode 2 Brokers simulaneamente.
3. Conecte clientes via terminal e mande textos esporadicamente.
4. Vá no terminal do **Broker 1** e mate o processo bruscamente (`Ctrl+C` ou enviando `SIGKILL`).
5. Tente mandar um texto do Cliente novamente.
6. Você observará que o cliente travará o chat por cerca de dois segundos avisando o erro, vai solicitar uma rota de resgate diretamente ao Registry, restaurará os sockets na porta 6100 e seguirá a vida da troca de mensagens/vídeo sem precisar reiniciar!
