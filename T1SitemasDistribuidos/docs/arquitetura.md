# Arquitetura do Sistema

## Visão Geral

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
          │ SUB/PUB (mídia local)
 ┌────────┴────────┐
 ▼                 ▼
┌─────────┐   ┌─────────┐
│Client A │   │Client B │
└─────────┘   └─────────┘

## Fluxo de uma Mensagem de Vídeo

1. `CaptureManager` captura frame via OpenCV
2. `QoSManager` avalia congestionamento e comprime o frame
3. `Sender` publica no broker local via `PUB`
4. Broker propaga para o cluster via `ROUTER/DEALER`
5. Broker remoto republica localmente via `PUB`
6. `Receiver` do cliente destino consome via `SUB`

## Failover

- Cliente envia heartbeat a cada N segundos
- Timeout → consulta Registry por broker alternativo
- Re-login transparente para o usuário


