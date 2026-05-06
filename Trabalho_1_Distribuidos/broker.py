"""
broker.py — Roteador central (Fase 1) do sistema de videoconferência.

Status dos requisitos cobertos neste arquivo:
  [DONE]    RF01: login por ID único (unicidade validada em controle_presenca).
  [DONE]    RF02: estado de presença + eventos ONLINE/OFFLINE via PUB 5562.
  [DONE]    RF03: join/leave de salas + broadcast SALA ... JOIN/LEAVE.
  [DONE]    RF04 (parte servidor): recebe/repassa Vídeo, Áudio e Texto.
  [DONE]    RF05: canais separados e unidirecionais (3 portas por mídia).
  [DONE]    RNF02: áudio em PUB/SUB (baixa latência, tolera perda).
  [PARCIAL] RNF03: drop de frames via HWM no vídeo; falta taxa adaptativa.
  [DONE]    RNF04: threads para processamento assíncrono (4 threads).
  [DONE]    RNF06/RNF07: Python 3 + ZeroMQ.

  [TODO]    RNF01: entrega garantida de texto — canal agora é XPUB/XSUB
            (fan-out por sala). Falta retry com ACK no cliente.
  [TODO]    ARQ01/ARQ02/ARQ03: cluster de N brokers + roteamento inter-brokers
            (XPUB/XSUB cross-broker) sem loops. Hoje há só 1 broker.
  [TODO]    ARQ04: heartbeat (PING/PONG com clientes).
  [TODO]    ARQ05: timeouts para detectar queda de broker vizinho.
  [TODO]    RF07: endpoint de registro dinâmico (ou arquivo discovery separado).
"""

import argparse
import time
import zmq
import threading

from presenca import (
    EstadoPresenca,
    handle_cmd,
    CTRL_PORT,
    PRESENCE_PORT,
    HEARTBEAT_TIMEOUT,
)

def roteador_video(context, xsub_port=5555, xpub_port=5556):    # [DONE] RF05/RNF03
    frontend = context.socket(zmq.XSUB)
    frontend.setsockopt(zmq.RCVHWM, 10)
    
    frontend.bind(f"tcp://*:{xsub_port}")

    backend = context.socket(zmq.XPUB)
    backend.setsockopt(zmq.SNDHWM, 10)
    backend.bind(f"tcp://*:{xpub_port}")

    print(f"Canal de VÍDEO (XPUB/XSUB) nas portas {xsub_port} e {xpub_port}")
    zmq.proxy(frontend, backend)

def roteador_audio(context, xsub_port=5557, xpub_port=5558):    # [DONE] RF05/RNF02
    frontend = context.socket(zmq.XSUB)
    frontend.bind(f"tcp://*:{xsub_port}")

    backend = context.socket(zmq.XPUB)
    backend.bind(f"tcp://*:{xpub_port}")

    print(f"Canal de ÁUDIO (XPUB/XSUB) nas portas {xsub_port} e {xpub_port}")
    zmq.proxy(frontend, backend)

def roteador_texto(context, xsub_port=5559, xpub_port=5560):    # [DONE] RF05; [TODO] RNF01 retry
    frontend = context.socket(zmq.XSUB)
    frontend.bind(f"tcp://*:{xsub_port}")

    backend = context.socket(zmq.XPUB)
    backend.bind(f"tcp://*:{xpub_port}")

    print(f"Canal de TEXTO (XPUB/XSUB) nas portas {xsub_port} e {xpub_port}")
    zmq.proxy(frontend, backend)

def controle_presenca(context, parar_evento=None, ctrl_port=CTRL_PORT,
                      presence_port=PRESENCE_PORT, estado=None):
    # [DONE] RF01/RF02/RF03
    if estado is None:
        estado = EstadoPresenca()

    router = context.socket(zmq.ROUTER)
    router.setsockopt(zmq.LINGER, 0)
    router.bind(f"tcp://*:{ctrl_port}")

    pub = context.socket(zmq.PUB)
    pub.setsockopt(zmq.LINGER, 0)
    pub.bind(f"tcp://*:{presence_port}")

    poller = zmq.Poller()
    poller.register(router, zmq.POLLIN)

    print(f"Canal de CONTROLE (ROUTER) na porta {ctrl_port} | PRESENÇA (PUB) na porta {presence_port}")

    ultimo_check = time.time()

    try:
        while parar_evento is None or not parar_evento.is_set():
            socks = dict(poller.poll(200))

            agora = time.time()
            if agora - ultimo_check > 1.0:
                ultimo_check = agora
                for uid, salas in estado.expire_stale(HEARTBEAT_TIMEOUT):
                    print(f"[presenca] expirando '{uid}' por inatividade")
                    for s in salas:
                        pub.send_string(f"SALA {s} LEAVE {uid}")
                    pub.send_string(f"PRESENCE OFFLINE {uid}")

            if router not in socks:
                continue
            frames = router.recv_multipart()
            if len(frames) < 3:
                continue
            identity, _empty, payload = frames[0], frames[1], frames[2]
            try:
                msg = payload.decode("utf-8", errors="replace")
            except Exception:
                msg = ""
            resposta, eventos = handle_cmd(estado, msg)
            router.send_multipart([identity, b"", resposta.encode("utf-8")])
            for ev in eventos:
                pub.send_string(ev)
    finally:
        router.close(linger=0)
        pub.close(linger=0)


def main():
    parser = argparse.ArgumentParser(description="Broker central de videoconferência")
    parser.add_argument("--vid-xsub", type=int, default=5555)
    parser.add_argument("--vid-xpub", type=int, default=5556)
    parser.add_argument("--aud-xsub", type=int, default=5557)
    parser.add_argument("--aud-xpub", type=int, default=5558)
    parser.add_argument("--txt-xsub", type=int, default=5559)
    parser.add_argument("--txt-xpub", type=int, default=5560)
    parser.add_argument("--ctrl-port", type=int, default=CTRL_PORT)
    parser.add_argument("--presence-port", type=int, default=PRESENCE_PORT)
    args = parser.parse_args()

    context = zmq.Context()

    t_video = threading.Thread(target=roteador_video, args=(context, args.vid_xsub, args.vid_xpub), daemon=True)
    t_audio = threading.Thread(target=roteador_audio, args=(context, args.aud_xsub, args.aud_xpub), daemon=True)
    t_texto = threading.Thread(target=roteador_texto, args=(context, args.txt_xsub, args.txt_xpub), daemon=True)
    t_ctrl  = threading.Thread(target=controle_presenca, args=(context,), kwargs={"ctrl_port": args.ctrl_port, "presence_port": args.presence_port}, daemon=True)

    t_video.start()
    t_audio.start()
    t_texto.start()
    t_ctrl.start()

    try:
        t_video.join()
        t_audio.join()
        t_texto.join()
        t_ctrl.join()
    except KeyboardInterrupt:
        print("\nDesligando broker central...")
    finally:
        context.term()

if __name__ == "__main__":
    main()
