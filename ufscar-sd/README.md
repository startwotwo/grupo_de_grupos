# Sistema de Videoconferência Distribuído

Sistema de comunicação distribuído em Python com ZeroMQ, suportando texto, áudio e vídeo em tempo real com múltiplos brokers, tolerância a falhas e service discovery.

## Arquitetura

- **Broker**: roteador central de mensagens (PUB/SUB para mídia, ROUTER/DEALER para controle)
- **Cliente**: envia e recebe texto, áudio e vídeo; mantém sessão em grupos
- **Inter-broker**: brokers se comunicam via ROUTER/DEALER para replicar mensagens entre clusters
- **Discovery**: registry centralizado para seleção e failover automático de brokers

## Instalação

```bash
pip3 install -r requirements.txt
```

Para suporte a câmera e microfone:

```bash
pip3 install -r requirements-full.txt
```

## Demo com interface gráfica (dois usuários)

Sobe um broker e abre duas janelas GUI com os usuários `alice` e `bob`:

```bash
make demo-gui
```

Na interface de cada usuário:
1. Selecione um grupo (ex: `A`) e clique **Entrar**
2. A câmera e o microfone são ativados automaticamente
3. O tile do outro usuário aparece assim que ele entra no grupo
4. Use o botão **Câmera** para ligar/desligar — o tile do outro lado reflete o estado em ~2s
5. Use o chat lateral para enviar mensagens de texto

Para encerrar todos os processos:

```bash
make kill-gui
```

## Demo automática (cluster com failover)

```bash
make demo
```

Simula um cluster de 3 brokers, múltiplos clientes e failover automático. Duração ~30s.

## Demo manual (cluster completo)

```bash
make discovery        # terminal 1
make broker1          # terminal 2
make broker2          # terminal 3
make broker3          # terminal 4
USER_ID=alice make client   # terminal 5
USER_ID=bob   make client   # terminal 6
```

No cliente, use `/join A` para entrar no grupo e digitar para enviar mensagens.

## Testes

```bash
make test
```

## Estrutura

```
src/
  broker/      broker principal e comunicação inter-broker
  client/      cliente, gerenciador de conexão e GUI
  common/      modelos, constantes e utilitários
  discovery/   registry de brokers
examples/      scripts de inicialização e demos
tests/
```

## Grupo

1. Daniel de Souza Sobrinho Macedo - RA 813524
2. Giovanna Rabello Luciano - RA 824749
3. Gustavo Gonçalves de Souza Geraldelli - RA 800523
4. Maria Luiza Fernandes Prestes Cesar - RA 832374
5. Pedro Cappelini Miguel - RA 832795
6. Vitor Yuki Inumaru Ferreira - RA 794041
