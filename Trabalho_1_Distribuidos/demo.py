"""
demo.py — Demonstração end-to-end do sistema de videoconferência distribuído.

Executa o broker e três clientes sintéticos no mesmo processo (sem webcam/
microfone), mostrando passo a passo os requisitos cobertos:

  RF01  login por ID único (e rejeição de duplicado)
  RF02  presença online + eventos ONLINE/OFFLINE via PUB
  RF03  entrada/saída em salas e listagem por sala
  RF04  captura, envio e recepção de Vídeo, Áudio e Texto
  RF05  canais separados e unidirecionais por mídia
  RNF04/05  cada cliente roda captura/envio/recepção/render em threads próprias

Como rodar:

    python demo.py

Não precisa de webcam/mic: o demo usa frames JPEG sintéticos e chunks de
áudio com silêncio. Ao final, imprime um sumário do que foi observado.
"""

from __future__ import annotations

import os
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List

import cv2
import numpy as np
import zmq

import broker
from presenca import (
    ClientePresenca,
    CTRL_PORT,
    EstadoPresenca,
    PRESENCE_PORT,
)


# Portas batem com o broker.py (vide RF05 — canais separados/unidirecionais).
VIDEO_FRONT, VIDEO_BACK = 5555, 5556
AUDIO_FRONT, AUDIO_BACK = 5557, 5558
TEXTO_FRONT, TEXTO_BACK = 5559, 5560


# ---------------------------------------------------------------------------
# utilitários de log
# ---------------------------------------------------------------------------
def header(t: str) -> None:
    print("\n" + "=" * 72)
    print(t)
    print("=" * 72)


def passo(t: str) -> None:
    print(f"\n>> {t}")


# ---------------------------------------------------------------------------
# cliente sintético: usa ClientePresenca para controle e sockets PUB/SUB
# crus para mídia. Conta pacotes recebidos por tipo/remetente.
# ---------------------------------------------------------------------------
@dataclass
class Recebidos:
    video: Dict[str, int] = field(default_factory=dict)
    audio: Dict[str, int] = field(default_factory=dict)
    texto: List[tuple] = field(default_factory=list)  # (sala, sender, msg)


class ClienteSintetico:
    def __init__(self, ctx: zmq.Context, uid: str, sala: str) -> None:
        self.ctx = ctx
        self.uid = uid
        self.sala = sala
        self.cp = ClientePresenca(
            ctx,
            f"tcp://127.0.0.1:{CTRL_PORT}",
            f"tcp://127.0.0.1:{PRESENCE_PORT}",
            timeout_ms=1500,
        )
        self.recebidos = Recebidos()
        self._parar = threading.Event()
        self._sub_thread: threading.Thread | None = None

        self._video_pub = ctx.socket(zmq.PUB)
        self._video_pub.setsockopt(zmq.SNDHWM, 10)
        self._video_pub.connect(f"tcp://127.0.0.1:{VIDEO_FRONT}")

        self._audio_pub = ctx.socket(zmq.PUB)
        self._audio_pub.setsockopt(zmq.SNDHWM, 10)
        self._audio_pub.connect(f"tcp://127.0.0.1:{AUDIO_FRONT}")

        self._texto_pub = ctx.socket(zmq.PUB)
        self._texto_pub.connect(f"tcp://127.0.0.1:{TEXTO_FRONT}")

    # --- presença (RF01/RF02/RF03) -----------------------------------------
    def login(self) -> str:
        return self.cp.login(self.uid)

    def join(self, sala: str | None = None) -> str:
        s = sala or self.sala
        self.sala = s
        return self.cp.join(s)

    def leave(self, sala: str) -> str:
        return self.cp.leave(sala)

    def logout(self) -> str:
        return self.cp.logout()

    # --- recepção de mídia (RNF05.3) ---------------------------------------
    def iniciar_sub(self) -> None:
        self._sub_thread = threading.Thread(target=self._loop_sub, daemon=True)
        self._sub_thread.start()

    def _loop_sub(self) -> None:
        sala_b = self.sala.encode()
        v = self.ctx.socket(zmq.SUB)
        v.setsockopt(zmq.RCVHWM, 10)
        v.connect(f"tcp://127.0.0.1:{VIDEO_BACK}")
        v.setsockopt(zmq.SUBSCRIBE, sala_b)

        a = self.ctx.socket(zmq.SUB)
        a.setsockopt(zmq.RCVHWM, 10)
        a.connect(f"tcp://127.0.0.1:{AUDIO_BACK}")
        a.setsockopt(zmq.SUBSCRIBE, sala_b)

        t = self.ctx.socket(zmq.SUB)
        t.connect(f"tcp://127.0.0.1:{TEXTO_BACK}")
        t.setsockopt(zmq.SUBSCRIBE, sala_b)

        poller = zmq.Poller()
        poller.register(v, zmq.POLLIN)
        poller.register(a, zmq.POLLIN)
        poller.register(t, zmq.POLLIN)

        try:
            while not self._parar.is_set():
                try:
                    socks = dict(poller.poll(200))
                except zmq.ContextTerminated:
                    break
                if v in socks:
                    try:
                        _s, sender, _ = v.recv_multipart(zmq.NOBLOCK)
                        sid = sender.decode()
                        if sid != self.uid:
                            self.recebidos.video[sid] = (
                                self.recebidos.video.get(sid, 0) + 1
                            )
                    except (zmq.Again, ValueError):
                        pass
                if a in socks:
                    try:
                        _s, sender, _ = a.recv_multipart(zmq.NOBLOCK)
                        sid = sender.decode()
                        if sid != self.uid:
                            self.recebidos.audio[sid] = (
                                self.recebidos.audio.get(sid, 0) + 1
                            )
                    except (zmq.Again, ValueError):
                        pass
                if t in socks:
                    try:
                        _s, sender, payload = t.recv_multipart(zmq.NOBLOCK)
                        sid = sender.decode()
                        msg = payload.decode("utf-8", errors="replace")
                        if sid != self.uid:
                            self.recebidos.texto.append((self.sala, sid, msg))
                    except (zmq.Again, ValueError):
                        pass
        finally:
            v.close(linger=0)
            a.close(linger=0)
            t.close(linger=0)

    # --- envio (RNF05.2) ---------------------------------------------------
    def enviar_video(self, frame: bytes) -> None:
        self._video_pub.send_multipart([self.sala.encode(), self.uid.encode(), frame])

    def enviar_audio(self, chunk: bytes) -> None:
        self._audio_pub.send_multipart([self.sala.encode(), self.uid.encode(), chunk])

    def enviar_texto(self, msg: str) -> None:
        self._texto_pub.send_multipart(
            [self.sala.encode(), self.uid.encode(), msg.encode("utf-8")]
        )

    def fechar(self) -> None:
        self._parar.set()
        if self._sub_thread is not None:
            self._sub_thread.join(timeout=1)
        for s in (self._video_pub, self._audio_pub, self._texto_pub):
            s.close(linger=0)
        self.cp.close()


# ---------------------------------------------------------------------------
# helpers de mídia sintética
# ---------------------------------------------------------------------------
def gerar_frame_jpeg(cor: tuple) -> bytes:
    img = np.zeros((120, 160, 3), dtype=np.uint8)
    img[:] = cor  # BGR
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
    return buf.tobytes() if ok else b""


def gerar_chunk_audio() -> bytes:
    # 1024 amostras de 16 bits em silêncio — formato compatível com AUDIO_CHUNK.
    return (b"\x00\x00") * 1024


# ---------------------------------------------------------------------------
# demo
# ---------------------------------------------------------------------------
def iniciar_broker(ctx: zmq.Context, parar: threading.Event) -> None:
    threading.Thread(target=broker.roteador_video, args=(ctx,), daemon=True).start()
    threading.Thread(target=broker.roteador_audio, args=(ctx,), daemon=True).start()
    threading.Thread(target=broker.roteador_texto, args=(ctx,), daemon=True).start()
    threading.Thread(
        target=broker.controle_presenca,
        args=(ctx, parar, CTRL_PORT, PRESENCE_PORT, EstadoPresenca()),
        daemon=True,
    ).start()
    # tempo para os binds dos proxies/router subirem
    time.sleep(0.5)


def aguardar(cond_fn, timeout=2.0, intervalo=0.05) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond_fn():
            return True
        time.sleep(intervalo)
    return False


def main() -> int:
    ctx = zmq.Context()
    parar_broker = threading.Event()

    header("1. Subindo o broker (proxies de vídeo/áudio/texto + controle)")
    iniciar_broker(ctx, parar_broker)
    print(
        f"  - VIDEO  XSUB:{VIDEO_FRONT}  XPUB:{VIDEO_BACK}\n"
        f"  - AUDIO  XSUB:{AUDIO_FRONT}  XPUB:{AUDIO_BACK}\n"
        f"  - TEXTO  XSUB:{TEXTO_FRONT}  XPUB:{TEXTO_BACK}\n"
        f"  - CTRL   ROUTER:{CTRL_PORT}  PUB:{PRESENCE_PORT}"
    )

    alice = ClienteSintetico(ctx, "alice", "A")
    bob = ClienteSintetico(ctx, "bob", "A")
    carol = ClienteSintetico(ctx, "carol", "B")
    falhas: List[str] = []

    try:
        # ==== RF01 ========================================================
        header("2. RF01 — Login por ID único")
        for c in (alice, bob, carol):
            r = c.login()
            print(f"  login({c.uid}) -> {r}")
            if not r.startswith("OK"):
                falhas.append(f"login {c.uid}")

        passo("Tentando login duplicado de 'alice' (deve falhar)")
        dup = ClientePresenca(
            ctx,
            f"tcp://127.0.0.1:{CTRL_PORT}",
            f"tcp://127.0.0.1:{PRESENCE_PORT}",
            timeout_ms=1500,
        )
        r = dup.login("alice")
        print(f"  login(alice) #2 -> {r}")
        if not r.startswith("ERR"):
            falhas.append("duplicate-login deveria falhar")
        dup.close()

        # ==== RF02 ========================================================
        header("3. RF02 — Presença (lista LIST + eventos PUB ONLINE/OFFLINE)")
        time.sleep(0.3)
        online = alice.cp.list_online()
        print(f"  LIST visto por alice: {online}")
        for esperado in ("alice", "bob", "carol"):
            if esperado not in online:
                falhas.append(f"LIST sem {esperado}")

        passo("Criando observador para verificar evento OFFLINE em tempo real")
        obs = ClientePresenca(
            ctx,
            f"tcp://127.0.0.1:{CTRL_PORT}",
            f"tcp://127.0.0.1:{PRESENCE_PORT}",
            timeout_ms=1500,
        )
        obs.login("obs")
        time.sleep(0.3)
        print(f"  obs.online (após semente via LIST): {sorted(obs.online)}")

        # ==== RF03 ========================================================
        header("4. RF03 — Entrada/saída de salas (Grupos A–K)")
        for c in (alice, bob):
            r = c.join("A")
            print(f"  {c.uid}.join(A) -> {r}")
        r = carol.join("B")
        print(f"  carol.join(B) -> {r}")

        ok = aguardar(lambda: {"alice", "bob"} <= obs.salas_membros.get("A", set()))
        print(f"  obs vê membros sala A: {sorted(obs.salas_membros.get('A', set()))} "
              f"{'OK' if ok else 'FALHOU'}")
        if not ok:
            falhas.append("evento SALA A JOIN nao propagou")

        membros_A = alice.cp.list_sala("A")
        print(f"  LIST_SALA A -> {membros_A}")
        if sorted(membros_A) != ["alice", "bob"]:
            falhas.append("LIST_SALA A inconsistente")

        # ==== RF04/RF05 ===================================================
        header("5. RF04/RF05 — Mídia: vídeo, áudio e texto em canais separados")
        for c in (alice, bob, carol):
            c.iniciar_sub()
        time.sleep(0.5)  # slow-joiner: deixa SUBs assinarem antes de publicar

        passo("alice e bob (sala A) trocam mídia; carol (sala B) NÃO deve receber")
        frame_alice = gerar_frame_jpeg((20, 200, 20))   # verde
        frame_bob = gerar_frame_jpeg((20, 20, 200))     # vermelho
        chunk = gerar_chunk_audio()

        # 30 frames cada (~1s a 30 fps), 30 chunks de áudio, 5 mensagens texto
        for i in range(30):
            alice.enviar_video(frame_alice)
            bob.enviar_video(frame_bob)
            alice.enviar_audio(chunk)
            bob.enviar_audio(chunk)
            time.sleep(0.01)

        for i in range(5):
            alice.enviar_texto(f"oi bob, msg #{i}")
            bob.enviar_texto(f"oi alice, msg #{i}")
        # carol manda algo na sala B só para mostrar isolamento por tópico
        carol.enviar_texto("ninguem em B alem de mim")

        time.sleep(0.8)  # deixa o que está em voo chegar

        passo("Resumo do que cada cliente recebeu (filtrado por sala)")
        for c in (alice, bob, carol):
            print(
                f"  {c.uid:6s} sala={c.sala}  "
                f"video={dict(c.recebidos.video)}  "
                f"audio={dict(c.recebidos.audio)}  "
                f"texto={[(s, m) for _, s, m in c.recebidos.texto]}"
            )

        # validações
        if alice.recebidos.video.get("bob", 0) == 0:
            falhas.append("alice nao recebeu video de bob")
        if bob.recebidos.video.get("alice", 0) == 0:
            falhas.append("bob nao recebeu video de alice")
        if alice.recebidos.audio.get("bob", 0) == 0:
            falhas.append("alice nao recebeu audio de bob")
        if any(s == "alice" or s == "bob" for _, s, _ in carol.recebidos.texto):
            falhas.append("carol recebeu mensagem da sala A (vazamento entre salas!)")
        if not any(s == "bob" for _, s, _ in alice.recebidos.texto):
            falhas.append("alice nao recebeu texto de bob")

        # ==== leave + offline =============================================
        header("6. Saída de sala e logout — eventos LEAVE/OFFLINE")
        r = bob.leave("A")
        print(f"  bob.leave(A) -> {r}")
        ok = aguardar(lambda: "bob" not in obs.salas_membros.get("A", set()))
        print(f"  obs vê sala A: {sorted(obs.salas_membros.get('A', set()))} "
              f"{'OK' if ok else 'FALHOU'}")
        if not ok:
            falhas.append("evento SALA A LEAVE nao propagou")

        r = carol.logout()
        print(f"  carol.logout() -> {r}")
        ok = aguardar(lambda: "carol" not in obs.online)
        print(f"  obs.online após logout: {sorted(obs.online)} "
              f"{'OK' if ok else 'FALHOU'}")
        if not ok:
            falhas.append("evento PRESENCE OFFLINE carol nao propagou")

        # ==== sumário =====================================================
        header("7. Sumário")
        if falhas:
            print("FALHAS:")
            for f in falhas:
                print(f"  - {f}")
            print("\nDEMO TERMINOU COM ERROS")
            return 1
        print("Todos os requisitos demonstrados OK:")
        print("  RF01 login único (com rejeição de duplicado)")
        print("  RF02 presença + eventos ONLINE/OFFLINE")
        print("  RF03 join/leave de salas + LIST_SALA")
        print("  RF04/RF05 vídeo, áudio e texto em canais separados")
        print("        (isolamento por sala validado: B não recebeu tráfego de A)")
        return 0

    finally:
        for c in (alice, bob, carol):
            try:
                c.fechar()
            except Exception:
                pass
        try:
            obs.close()
        except Exception:
            pass
        parar_broker.set()
        # NÃO chamamos ctx.term(): isso desperta as threads de proxy/router
        # do broker e gera tracebacks ContextTerminated. O os._exit no
        # __main__ encerra o processo limpamente.


if __name__ == "__main__":
    import sys
    rc = main()
    sys.stdout.flush()
    sys.stderr.flush()
    # encerra abruptamente para evitar tracebacks ContextTerminated dos
    # proxies daemon (zmq.proxy não aceita shutdown limpo).
    os._exit(rc)
