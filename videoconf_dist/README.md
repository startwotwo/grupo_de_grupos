# Ferramenta de Videoconferência

Sistema de videoconferência assíncrona.

### Build da Imagem

```bash
docker build -t videoconf .
```

### Iniciar o Broker

```bash
# Terminal 1: Broker
docker-compose up broker
```

### Iniciar Clientes

Em terminais diferentes:

```bash
# Terminal 2: Usuário 1
cd src
python -m client.client user1 SALA_A

# Terminal 3: Usuário 2
cd src
python -m client.client user2 SALA_A
```

### Fechar o Broker

```bash
# Terminal 1: Broker
docker-compose down
```