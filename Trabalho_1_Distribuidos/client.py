"""
client.py — Cliente de videoconferência (Fase 1).

Status dos requisitos cobertos neste arquivo:
  [DONE]    RF01: login via ID único — validação de unicidade no broker
            (ClientePresenca.login).
  [DONE]    RF02: presença — lista LIST + eventos ONLINE/OFFLINE via SUB,
            mantidos em ClientePresenca.online.
  [DONE]    RF03: entrada/saída de salas (JOIN/LEAVE) + lista de membros por
            sala via eventos. Falta ainda prefixar SALA nos sends de mídia
            (item separado de RF04/RF05).
  [DONE]    RF04: captura + envio + recepção de vídeo/áudio/texto.
  [DONE]    RF05: canais separados (sockets distintos por mídia).
  [TODO]    RF06: IPs 127.0.0.1 hardcoded em pubPacotes/subPacotes.
  [TODO]    RF07: integração com service discovery (ainda não existe).
  [TODO]    RF08: seleção de broker (round-robin / menor latência).

  [TODO]    RNF01: retry de texto não implementado.
  [DONE]    RNF02: áudio em PUB/SUB (baixa latência).
  [PARCIAL] RNF03: drop de frames via HWM no PUB/SUB; falta taxa adaptativa.
  [DONE]    RNF04: uso de threads para async.
  [DONE]    RNF05: Captura / Envio / Recepção / Renderização em threads
            separadas.
  [DONE]    RNF06/RNF07: Python 3 + ZeroMQ.

  [TODO]    ARQ04/ARQ05/ARQ06: heartbeat, timeouts e failover.
"""

import zmq
import threading
import queue
import time
import cv2
import numpy as np

try:
    import pyaudio  # type: ignore
except Exception as _e:
    pyaudio = None
    print(f"[init] PyAudio indisponível ({_e}); áudio será desabilitado.")

from presenca import ClientePresenca, CTRL_PORT, PRESENCE_PORT

global ID

BROKER_HOST = "127.0.0.1"   # [TODO] RF06: substituir por service discovery.
SALAS_VALIDAS = [chr(c) for c in range(ord("A"), ord("K") + 1)]  # Grupos A–K

# Parâmetros de captura (defaults)
VIDEO_WIDTH = 640
VIDEO_HEIGHT = 480
VIDEO_FPS = 30
VIDEO_JPEG_QUALITY = 70

AUDIO_RATE = 16000
AUDIO_CHANNELS = 1
AUDIO_FORMAT = pyaudio.paInt16 if pyaudio is not None else 0
AUDIO_CHUNK = 1024

# PortAudio não é totalmente thread-safe na inicialização/terminação;
# serializar PyAudio() / .terminate() entre threads evita segfault no shutdown.
_PA_LOCK = threading.Lock()


def _captura_video(fila_video, parar_evento):
    import sys
    cap = None
    backends = (cv2.CAP_MSMF, cv2.CAP_DSHOW, cv2.CAP_ANY) if sys.platform == "win32" else (cv2.CAP_ANY,)
    for idx in range(3):
        for backend in backends:
            c = cv2.VideoCapture(idx, backend)
            if c.isOpened():
                cap = c
                break
            c.release()
        if cap is not None:
            break
    if cap is None or not cap.isOpened():
        print("[captura_video] Webcam indisponível. Verifique: câmera em uso por outro app? Privacidade > Câmera bloqueada no Windows?")
        return

    cap.set(cv2.CAP_PROP_FRAME_WIDTH, VIDEO_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, VIDEO_HEIGHT)
    cap.set(cv2.CAP_PROP_FPS, VIDEO_FPS)

    intervalo = 1.0 / VIDEO_FPS
    encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), VIDEO_JPEG_QUALITY]

    try:
        while not parar_evento.is_set():
            inicio = time.time()
            ok, frame = cap.read()
            if not ok:
                time.sleep(intervalo)
                continue

            ok, buffer = cv2.imencode(".jpg", frame, encode_params)
            if ok:
                fila_video.put(buffer.tobytes())

            dt = time.time() - inicio
            if dt < intervalo:
                time.sleep(intervalo - dt)
    finally:
        cap.release()


def _captura_audio(fila_audio, parar_evento):
    if pyaudio is None:
        print("[captura_audio] PyAudio indisponível; pulando captura.")
        return
    try:
        with _PA_LOCK:
            pa = pyaudio.PyAudio()
    except Exception as e:
        print(f"[captura_audio] Falha ao iniciar PyAudio: {e}")
        return
    try:
        stream = pa.open(
            format=AUDIO_FORMAT,
            channels=AUDIO_CHANNELS,
            rate=AUDIO_RATE,
            input=True,
            frames_per_buffer=AUDIO_CHUNK,
        )
    except Exception as e:
        print(f"[captura_audio] Microfone indisponível: {e}")
        with _PA_LOCK:
            pa.terminate()
        return

    try:
        while not parar_evento.is_set():
            try:
                dados = stream.read(AUDIO_CHUNK, exception_on_overflow=False)
                fila_audio.put(dados)
            except Exception as e:
                print(f"[captura_audio] Erro na leitura: {e}")
                break
    finally:
        try:
            stream.stop_stream()
            stream.close()
        except Exception:
            pass
        with _PA_LOCK:
            pa.terminate()


# [DONE] RNF05.1 (Captura) — webcam + microfone em sub-threads dedicadas.
def capturaImagemeAudio(contexto, fila_video, fila_audio, parar_evento=None):
    if parar_evento is None:
        parar_evento = threading.Event()

    t_video = threading.Thread(
        target=_captura_video, args=(fila_video, parar_evento), daemon=True
    )
    t_audio = threading.Thread(
        target=_captura_audio, args=(fila_audio, parar_evento), daemon=True
    )

    t_video.start()
    t_audio.start()

    print("IMAGEM E AUDIO CAPTURADOS")

    try:
        while not parar_evento.is_set():
            if not t_video.is_alive() and not t_audio.is_alive():
                break
            time.sleep(0.1)
    except KeyboardInterrupt:
        parar_evento.set()

    t_video.join(timeout=2)
    t_audio.join(timeout=2)

# [DONE] RNF05.4 (Renderização) — consome filas _sub:
#   - vídeo: decodifica JPEG e abre uma janela cv2 por remetente.
#   - áudio: stream de saída do PyAudio (escreve chunks conforme chegam).
#   - texto: imprime no terminal (ignora mensagens do próprio usuário).
def _render_audio(fila_audio, parar_evento, meu_id):
    if pyaudio is None:
        print("[render_audio] PyAudio indisponível; áudio remoto não tocará.")
        # drena a fila para não acumular memória.
        while not parar_evento.is_set():
            try:
                fila_audio.get(timeout=0.2)
            except queue.Empty:
                pass
        return
    try:
        with _PA_LOCK:
            pa = pyaudio.PyAudio()
    except Exception as e:
        print(f"[render_audio] Falha ao iniciar PyAudio: {e}")
        return
    try:
        stream = pa.open(
            format=AUDIO_FORMAT,
            channels=AUDIO_CHANNELS,
            rate=AUDIO_RATE,
            output=True,
            frames_per_buffer=AUDIO_CHUNK,
        )
    except Exception as e:
        print(f"[render_audio] Saída de áudio indisponível: {e}")
        with _PA_LOCK:
            pa.terminate()
        return

    try:
        while not parar_evento.is_set():
            try:
                _sala, sender, dados = fila_audio.get(timeout=0.05)
            except queue.Empty:
                continue
            # Bug: o cliente recebia o próprio áudio, dobrando o fluxo de
            # entrada e gerando latência crescente.
            if sender == meu_id:
                continue
            # Se a fila acumulou (PUB chega mais rápido que a saída), pula
            # adiante para evitar atraso percebido — RNF03 (drop adaptativo).
            while fila_audio.qsize() > 5:
                try:
                    _sala, sender, dados = fila_audio.get_nowait()
                except queue.Empty:
                    break
                if sender == meu_id:
                    dados = None
            if dados is None:
                continue
            try:
                stream.write(dados)
            except Exception as e:
                print(f"[render_audio] Erro: {e}")
                break
    finally:
        try:
            stream.stop_stream()
            stream.close()
        except Exception:
            pass
        with _PA_LOCK:
            pa.terminate()


def _render_texto(fila_texto, parar_evento, meu_id):
    while not parar_evento.is_set():
        try:
            sala, sender, texto = fila_texto.get(timeout=0.2)
        except queue.Empty:
            continue
        if sender == meu_id:
            continue  # não exibe eco da própria mensagem
        print(f"\n[chat {sala}] {sender}: {texto}\n> ", end="", flush=True)


def _render_video(fila_video, parar_evento, meu_id):
    frames_por_remetente = {}  # sender_id -> frame decodificado mais recente

    try:
        while not parar_evento.is_set():
            # drena a fila pegando o frame mais novo de cada remetente
            drenou = False
            while True:
                try:
                    _sala, sender, payload = fila_video.get_nowait()
                except queue.Empty:
                    break
                if sender == meu_id:
                    continue  # não mostra a própria webcam como remoto
                arr = np.frombuffer(payload, dtype=np.uint8)
                frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                if frame is not None:
                    frames_por_remetente[sender] = frame
                    drenou = True

            for sid, frame in frames_por_remetente.items():
                cv2.imshow(f"video - {sid}", frame)

            # waitKey também bombeia eventos da janela — necessário sempre
            k = cv2.waitKey(1) & 0xFF
            if k == ord("q"):
                parar_evento.set()
                break

            if not drenou:
                time.sleep(0.01)
    finally:
        try:
            cv2.destroyAllWindows()
            cv2.waitKey(1)
        except Exception:
            pass


def renderizacaoInterface(contexto, fila_video, fila_audio, fila_texto, meu_id,
                          parar_evento=None):
    if parar_evento is None:
        parar_evento = threading.Event()

    t_audio = threading.Thread(
        target=_render_audio, args=(fila_audio, parar_evento, meu_id), daemon=True
    )
    t_texto = threading.Thread(
        target=_render_texto, args=(fila_texto, parar_evento, meu_id), daemon=True
    )

    t_audio.start()
    t_texto.start()

    print("INTERFACE RENDERIZADA (pressione 'q' na janela de vídeo para sair)")
    _render_video(fila_video, parar_evento, meu_id)

    t_audio.join(timeout=2)
    t_texto.join(timeout=2)

# [DONE] RNF05.2 (Envio) — vídeo, áudio e texto publicados como multipart
# [SALA, ID, payload]. SUBs filtram pelo primeiro frame (tópico = SALA).
def pubPacotes(contexto, fila_video, fila_audio, fila_texto, ID, sala_ref,
               parar_evento=None):
    # [TODO] RF06/RF08: service discovery e seleção de broker.
    if parar_evento is None:
        parar_evento = threading.Event()

    video_pub = contexto.socket(zmq.PUB)
    video_pub.setsockopt(zmq.SNDHWM, 10)  # RNF03: drop de frames em caso de gargalo
    video_pub.connect("tcp://127.0.0.1:5555")

    audio_pub = contexto.socket(zmq.PUB)
    audio_pub.setsockopt(zmq.SNDHWM, 10)
    audio_pub.connect("tcp://127.0.0.1:5557")

    texto_pub = contexto.socket(zmq.PUB)
    texto_pub.connect("tcp://127.0.0.1:5559")

    time.sleep(0.5)  # slow-joiner: aguarda SUBs se conectarem antes de publicar

    id_b = ID.encode()

    print("Cliente PUB iniciado")

    try:
        while not parar_evento.is_set():
            sala_b = sala_ref[0].encode()
            try:
                frame = fila_video.get(timeout=0.01)
                video_pub.send_multipart([sala_b, id_b, frame])
            except queue.Empty:
                pass

            try:
                audio = fila_audio.get(timeout=0.01)
                audio_pub.send_multipart([sala_b, id_b, audio])
            except queue.Empty:
                pass

            # [TODO] RNF01: adicionar política de retry (ACK + reenvio) para chat.
            try:
                texto = fila_texto.get(timeout=0.01)
                texto_pub.send_multipart([sala_b, id_b, texto.encode("utf-8")])
            except queue.Empty:
                pass
    finally:
        video_pub.close(linger=0)
        audio_pub.close(linger=0)
        texto_pub.close(linger=0)

# [DONE] RNF05.3 (Recepção) — SUB nos 3 canais do broker, filtrado por SALA.
# Cada pacote é [SALA, ID_remetente, payload]. Empilha em filas para a thread
# de renderização consumir.
def subPacotes(contexto, fila_video, fila_audio, fila_texto, sala_ref,
               parar_evento=None):
    if parar_evento is None:
        parar_evento = threading.Event()

    sala_atual = sala_ref[0]
    sala_b = sala_atual.encode()

    video_sub = contexto.socket(zmq.SUB)
    video_sub.setsockopt(zmq.RCVHWM, 10)
    video_sub.connect("tcp://127.0.0.1:5556")
    video_sub.setsockopt(zmq.SUBSCRIBE, sala_b)

    audio_sub = contexto.socket(zmq.SUB)
    audio_sub.setsockopt(zmq.RCVHWM, 10)
    audio_sub.connect("tcp://127.0.0.1:5558")
    audio_sub.setsockopt(zmq.SUBSCRIBE, sala_b)

    texto_sub = contexto.socket(zmq.SUB)
    texto_sub.connect("tcp://127.0.0.1:5560")
    texto_sub.setsockopt(zmq.SUBSCRIBE, sala_b)

    poller = zmq.Poller()
    poller.register(video_sub, zmq.POLLIN)
    poller.register(audio_sub, zmq.POLLIN)
    poller.register(texto_sub, zmq.POLLIN)

    print("Cliente SUB iniciado")

    try:
        while not parar_evento.is_set():
            # resubscreve se a sala mudou
            nova = sala_ref[0]
            if nova != sala_atual:
                nova_b = nova.encode()
                for s in (video_sub, audio_sub, texto_sub):
                    s.setsockopt(zmq.UNSUBSCRIBE, sala_b)
                    s.setsockopt(zmq.SUBSCRIBE, nova_b)
                sala_atual = nova
                sala_b = nova_b

            try:
                socks = dict(poller.poll(200))
            except zmq.ContextTerminated:
                break

            if video_sub in socks:
                try:
                    sala, sender, payload = video_sub.recv_multipart(zmq.NOBLOCK)
                    fila_video.put((sala.decode(), sender.decode(), payload))
                except (zmq.Again, ValueError):
                    pass

            if audio_sub in socks:
                try:
                    sala, sender, payload = audio_sub.recv_multipart(zmq.NOBLOCK)
                    fila_audio.put((sala.decode(), sender.decode(), payload))
                except (zmq.Again, ValueError):
                    pass

            if texto_sub in socks:
                try:
                    sala, sender, payload = texto_sub.recv_multipart(zmq.NOBLOCK)
                    fila_texto.put(
                        (sala.decode(), sender.decode(),
                         payload.decode("utf-8", errors="replace"))
                    )
                except (zmq.Again, ValueError):
                    pass
    finally:
        video_sub.close(linger=0)
        audio_sub.close(linger=0)
        texto_sub.close(linger=0)


def fazer_login(cp: ClientePresenca) -> str:
    """[DONE] RF01 — pede ID e retenta até o broker aceitar (unicidade)."""
    while True:
        ID = input("Digite seu ID: ").strip()
        if not ID:
            print("  ID vazio, tente novamente.")
            continue
        resp = cp.login(ID)
        print(f"  broker: {resp}")
        if resp.startswith("OK"):
            return ID


def escolher_sala(cp: ClientePresenca) -> str:
    """[DONE] RF03 — escolhe uma das salas A–K e faz JOIN."""
    print(f"Salas disponíveis: {', '.join(SALAS_VALIDAS)}")
    while True:
        sala = input("Entre em uma sala: ").strip().upper()
        if sala not in SALAS_VALIDAS:
            print(f"  Sala inválida. Escolha entre {SALAS_VALIDAS}.")
            continue
        resp = cp.join(sala)
        print(f"  broker: {resp}")
        if resp.startswith("OK"):
            return sala


def menu_controle(cp: ClientePresenca, parar_evento: threading.Event,
                  fila_texto_pub: queue.Queue, sala_ref: list) -> None:
    """[DONE] RF02/RF03/RF04(texto) — loop interativo do terminal."""
    ajuda = (
        "\nComandos: [l] listar online  [s] listar sala  [j <S>] join  "
        "[x <S>] leave  [m <texto>] enviar msg  [q] sair\n"
    )
    print(ajuda)
    try:
        while not parar_evento.is_set():
            linha = input("> ").strip()
            if not linha:
                continue
            partes = linha.split()
            cmd = partes[0].lower()
            if cmd == "q":
                break
            elif cmd == "l":
                online = cp.list_online()
                if not online:
                    print("  (ninguém online)")
                else:
                    for uid, salas in sorted(online.items()):
                        marca = " (você)" if uid == cp.ID else ""
                        salas_txt = ",".join(salas) if salas else "-"
                        print(f"  - {uid}{marca}  salas: {salas_txt}")
            elif cmd == "s":
                sala = partes[1].upper() if len(partes) > 1 else (
                    next(iter(cp.salas), "")
                )
                if not sala:
                    print("  uso: s <SALA>")
                    continue
                membros = cp.list_sala(sala)
                print(f"  sala {sala}: {membros or '(vazia)'}")
            elif cmd == "j" and len(partes) == 2:
                nova_sala = partes[1].upper()
                sala_antiga = sala_ref[0]
                if nova_sala == sala_antiga:
                    print(f"  já está na sala {nova_sala}")
                    continue
                cp.leave(sala_antiga)
                resp = cp.join(nova_sala)
                print(f"  broker: {resp}")
                if resp.startswith("OK"):
                    sala_ref[0] = nova_sala
                    print(f"  [sala atual: {nova_sala}]")
                else:
                    cp.join(sala_antiga)  # volta para a sala anterior se falhar
            elif cmd == "x" and len(partes) == 2:
                print(f"  broker: {cp.leave(partes[1].upper())}")
            elif cmd == "m" and len(partes) >= 2:
                texto = linha[1:].lstrip()  # tudo após o "m"
                fila_texto_pub.put(texto)
                print(f"  [você → {sala_ref[0]}] {texto}")
            else:
                print(ajuda)
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        parar_evento.set()


def main():
    contexto = zmq.Context()

    # [DONE] RF01/RF02/RF03 — serviço de identidade e presença.
    cp = ClientePresenca(
        contexto,
        f"tcp://{BROKER_HOST}:{CTRL_PORT}",
        f"tcp://{BROKER_HOST}:{PRESENCE_PORT}",
    )
    ID = fazer_login(cp)
    SALA = escolher_sala(cp)
    sala_ref = [SALA]  # referência mutável compartilhada entre threads

    # Filas de Saída (Upload)
    fila_video_pub = queue.Queue()
    fila_audio_pub = queue.Queue()
    fila_texto_pub = queue.Queue()

    # Filas de Entrada (Download)
    fila_video_sub = queue.Queue()
    fila_audio_sub = queue.Queue()
    fila_texto_sub = queue.Queue()

    parar_evento = threading.Event()

    t_captura = threading.Thread(
        target=capturaImagemeAudio,
        args=(contexto, fila_video_pub, fila_audio_pub, parar_evento),
        daemon=True,
    )
    t_envio = threading.Thread(
        target=pubPacotes,
        args=(contexto, fila_video_pub, fila_audio_pub, fila_texto_pub, ID, sala_ref,
              parar_evento),
        daemon=True,
    )
    t_recep = threading.Thread(
        target=subPacotes,
        args=(contexto, fila_video_sub, fila_audio_sub, fila_texto_sub, sala_ref,
              parar_evento),
        daemon=True,
    )
    t_render = threading.Thread(
        target=renderizacaoInterface,
        args=(contexto, fila_video_sub, fila_audio_sub, fila_texto_sub, ID,
              parar_evento),
        daemon=True,
    )

    t_captura.start()
    t_envio.start()
    t_recep.start()
    t_render.start()

    try:
        menu_controle(cp, parar_evento, fila_texto_pub, sala_ref)
    finally:
        print("\nEncerrando o cliente...")
        parar_evento.set()
        # dá tempo para as threads saírem dos pollers e fecharem sockets
        for t in (t_envio, t_recep, t_render, t_captura):
            t.join(timeout=1)
        try:
            print(f"  broker: {cp.logout()}")
        except Exception as e:
            print(f"  logout falhou: {e}")
        cp.close()
        contexto.term()

if __name__ == "__main__":
    main()
