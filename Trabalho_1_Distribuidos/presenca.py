"""
presenca.py — Serviço de identidade, presença e salas (RF01–RF03).

Cobertura de requisitos:
  [DONE] RF01: login por ID único (validação de unicidade no broker).
  [DONE] RF02: controle de presença — broker mantém tabela de online e
         publica eventos ONLINE/OFFLINE no canal PUB de presença.
  [DONE] RF03: entrada/saída em salas (Grupos A–K) + broadcast JOIN/LEAVE.

Arquitetura:
  - `EstadoPresenca`: tabela em memória (thread-safe) com a lógica pura.
  - `handle_cmd`: parser de comandos texto → resposta + eventos a publicar.
  - `ClientePresenca`: wrapper ZMQ do lado cliente (REQ + SUB).
  - O broker importa `EstadoPresenca`/`handle_cmd` e acopla aos sockets ROUTER/PUB.

Protocolo (linha de texto simples, separado por espaço):
  Requisições (REQ → ROUTER):
    LOGIN <id>
    LOGOUT <id>
    JOIN <id> <sala>
    LEAVE <id> <sala>
    LIST
    LIST_SALA <sala>
  Respostas: começam com "OK ..." em sucesso ou "ERR ..." em falha.
  Eventos (PUB → SUB):
    PRESENCE ONLINE <id>
    PRESENCE OFFLINE <id>
    SALA <sala> JOIN <id>
    SALA <sala> LEAVE <id>
"""

from __future__ import annotations

import threading
import time
from typing import Dict, List, Set, Tuple

import zmq


CTRL_PORT = 5561     # ROUTER do broker (requisições de controle)
PRESENCE_PORT = 5562  # PUB do broker (eventos de presença/sala)

# ARQ04: heartbeat para detectar clientes que cairam sem LOGOUT (ex.: terminal
# fechado à força). Cliente envia HEARTBEAT a cada HEARTBEAT_INTERVAL; broker
# expira usuários cujo último heartbeat passou de HEARTBEAT_TIMEOUT.
HEARTBEAT_INTERVAL = 2.0
HEARTBEAT_TIMEOUT = 8.0


class EstadoPresenca:
    """Tabela em memória de usuários online e suas salas."""

    def __init__(self) -> None:
        self._usuarios: Dict[str, Set[str]] = {}
        self._last_seen: Dict[str, float] = {}
        self._lock = threading.Lock()

    def login(self, uid: str) -> Tuple[bool, str]:
        uid = uid.strip()
        if not uid:
            return False, "ERR id vazio"
        with self._lock:
            if uid in self._usuarios:
                return False, f"ERR ID '{uid}' ja em uso"
            self._usuarios[uid] = set()
            self._last_seen[uid] = time.time()
            return True, f"OK LOGIN {uid}"

    def logout(self, uid: str) -> Tuple[bool, str, List[str]]:
        with self._lock:
            if uid not in self._usuarios:
                return False, f"ERR ID '{uid}' nao logado", []
            salas = sorted(self._usuarios.pop(uid))
            self._last_seen.pop(uid, None)
            return True, f"OK LOGOUT {uid}", salas

    def heartbeat(self, uid: str) -> Tuple[bool, str]:
        with self._lock:
            if uid not in self._usuarios:
                return False, f"ERR ID '{uid}' nao logado"
            self._last_seen[uid] = time.time()
            return True, "OK PONG"

    def expire_stale(self, timeout: float) -> List[Tuple[str, List[str]]]:
        """Remove usuários sem heartbeat recente. Retorna [(uid, [salas]), ...]."""
        agora = time.time()
        expirados: List[Tuple[str, List[str]]] = []
        with self._lock:
            for uid, ts in list(self._last_seen.items()):
                if agora - ts > timeout:
                    salas = sorted(self._usuarios.pop(uid, set()))
                    self._last_seen.pop(uid, None)
                    expirados.append((uid, salas))
        return expirados

    def join(self, uid: str, sala: str) -> Tuple[bool, str]:
        with self._lock:
            if uid not in self._usuarios:
                return False, f"ERR ID '{uid}' nao logado"
            if sala in self._usuarios[uid]:
                return False, f"ERR ja esta na sala '{sala}'"
            self._usuarios[uid].add(sala)
            return True, f"OK JOIN {uid} {sala}"

    def leave(self, uid: str, sala: str) -> Tuple[bool, str]:
        with self._lock:
            if uid not in self._usuarios:
                return False, f"ERR ID '{uid}' nao logado"
            if sala not in self._usuarios[uid]:
                return False, f"ERR nao esta na sala '{sala}'"
            self._usuarios[uid].discard(sala)
            return True, f"OK LEAVE {uid} {sala}"

    def list_all(self) -> Dict[str, List[str]]:
        with self._lock:
            return {uid: sorted(salas) for uid, salas in self._usuarios.items()}

    def list_sala(self, sala: str) -> List[str]:
        with self._lock:
            return sorted(
                uid for uid, salas in self._usuarios.items() if sala in salas
            )


def handle_cmd(estado: EstadoPresenca, msg: str) -> Tuple[str, List[str]]:
    """Processa uma linha de comando. Retorna (resposta, eventos_para_publicar)."""
    partes = msg.strip().split()
    if not partes:
        return "ERR comando vazio", []
    cmd = partes[0].upper()

    if cmd == "LOGIN" and len(partes) == 2:
        ok, resp = estado.login(partes[1])
        return resp, [f"PRESENCE ONLINE {partes[1]}"] if ok else []

    if cmd == "LOGOUT" and len(partes) == 2:
        ok, resp, salas = estado.logout(partes[1])
        if not ok:
            return resp, []
        eventos = [f"SALA {s} LEAVE {partes[1]}" for s in salas]
        eventos.append(f"PRESENCE OFFLINE {partes[1]}")
        return resp, eventos

    if cmd == "JOIN" and len(partes) == 3:
        ok, resp = estado.join(partes[1], partes[2])
        return resp, [f"SALA {partes[2]} JOIN {partes[1]}"] if ok else []

    if cmd == "LEAVE" and len(partes) == 3:
        ok, resp = estado.leave(partes[1], partes[2])
        return resp, [f"SALA {partes[2]} LEAVE {partes[1]}"] if ok else []

    if cmd == "LIST" and len(partes) == 1:
        d = estado.list_all()
        itens = ";".join(
            f"{uid}:{','.join(salas) if salas else '-'}" for uid, salas in d.items()
        )
        return f"OK LIST {itens}", []

    if cmd == "HEARTBEAT" and len(partes) == 2:
        _, resp = estado.heartbeat(partes[1])
        return resp, []

    if cmd == "LIST_SALA" and len(partes) == 2:
        membros = estado.list_sala(partes[1])
        return f"OK LIST_SALA {partes[1]} {','.join(membros)}", []

    return f"ERR comando invalido: {msg.strip()}", []


def parse_list(resposta: str) -> Dict[str, List[str]]:
    """Parseia resposta de LIST → {id: [salas]}. Retorna {} se vazio/ERR."""
    if not resposta.startswith("OK LIST"):
        return {}
    corpo = resposta[len("OK LIST"):].strip()
    if not corpo:
        return {}
    out: Dict[str, List[str]] = {}
    for item in corpo.split(";"):
        if ":" not in item:
            continue
        uid, salas = item.split(":", 1)
        out[uid] = [] if salas == "-" else salas.split(",")
    return out


def parse_list_sala(resposta: str) -> List[str]:
    """Parseia resposta de LIST_SALA → [ids]. Retorna [] se vazio/ERR."""
    if not resposta.startswith("OK LIST_SALA"):
        return []
    corpo = resposta[len("OK LIST_SALA"):].strip()
    partes = corpo.split(" ", 1)
    if len(partes) < 2 or not partes[1]:
        return []
    return [x for x in partes[1].split(",") if x]


# -------------------------- lado cliente -------------------------------------


class ClientePresenca:
    """Cliente de presença: REQ para comandos + SUB para eventos assíncronos.

    Uso típico:
        cp = ClientePresenca(ctx, "tcp://127.0.0.1:5561", "tcp://127.0.0.1:5562")
        resp = cp.login("alice")
        cp.join("A")
        cp.list_online()
        cp.close()
    """

    def __init__(
        self,
        contexto: zmq.Context,
        ctrl_addr: str,
        sub_addr: str,
        timeout_ms: int = 2000,
    ) -> None:
        self._ctx = contexto
        self._ctrl_addr = ctrl_addr
        self._sub_addr = sub_addr
        self._timeout = timeout_ms
        self._req_lock = threading.Lock()
        self._req = self._novo_req()

        self.ID: str | None = None
        self.salas: Set[str] = set()
        self.online: Set[str] = set()   # atualizado pelos eventos PRESENCE
        self.salas_membros: Dict[str, Set[str]] = {}  # sala -> ids

        self._parar = threading.Event()
        self._sub_thread: threading.Thread | None = None
        self._hb_thread: threading.Thread | None = None
        self._estado_lock = threading.Lock()

    # --- internals ----------------------------------------------------------
    def _novo_req(self) -> zmq.Socket:
        s = self._ctx.socket(zmq.REQ)
        s.setsockopt(zmq.RCVTIMEO, self._timeout)
        s.setsockopt(zmq.SNDTIMEO, self._timeout)
        s.setsockopt(zmq.LINGER, 0)
        s.connect(self._ctrl_addr)
        return s

    def _cmd(self, msg: str) -> str:
        """Envia comando e recebe resposta. Reseta o REQ em timeout (REQ fica
        travado em estado inválido após um timeout). Thread-safe."""
        with self._req_lock:
            try:
                self._req.send_string(msg)
                return self._req.recv_string()
            except zmq.Again:
                self._req.close(linger=0)
                self._req = self._novo_req()
                return "ERR timeout"
            except zmq.ZMQError as e:
                self._req.close(linger=0)
                self._req = self._novo_req()
                return f"ERR zmq: {e}"

    # --- API pública --------------------------------------------------------
    def login(self, ID: str) -> str:
        resp = self._cmd(f"LOGIN {ID}")
        if resp.startswith("OK"):
            self.ID = ID
            self._iniciar_sub()
        return resp

    def logout(self) -> str:
        if not self.ID:
            return "ERR nao logado"
        resp = self._cmd(f"LOGOUT {self.ID}")
        self._parar.set()
        if self._sub_thread is not None:
            self._sub_thread.join(timeout=1)
        if self._hb_thread is not None:
            self._hb_thread.join(timeout=1)
        self.ID = None
        self.salas.clear()
        return resp

    def join(self, sala: str) -> str:
        if not self.ID:
            return "ERR nao logado"
        resp = self._cmd(f"JOIN {self.ID} {sala}")
        if resp.startswith("OK"):
            with self._estado_lock:
                self.salas.add(sala)
        return resp

    def leave(self, sala: str) -> str:
        if not self.ID:
            return "ERR nao logado"
        resp = self._cmd(f"LEAVE {self.ID} {sala}")
        if resp.startswith("OK"):
            with self._estado_lock:
                self.salas.discard(sala)
        return resp

    def list_online(self) -> Dict[str, List[str]]:
        return parse_list(self._cmd("LIST"))

    def list_sala(self, sala: str) -> List[str]:
        return parse_list_sala(self._cmd(f"LIST_SALA {sala}"))

    def close(self) -> None:
        self._parar.set()
        if self._sub_thread is not None:
            self._sub_thread.join(timeout=1)
        if self._hb_thread is not None:
            self._hb_thread.join(timeout=1)
        with self._req_lock:
            self._req.close(linger=0)

    # --- SUB de eventos -----------------------------------------------------
    def _iniciar_sub(self) -> None:
        if self._sub_thread is not None and self._sub_thread.is_alive():
            return
        self._parar.clear()
        self._sub_thread = threading.Thread(target=self._loop_sub, daemon=True)
        self._sub_thread.start()
        self._hb_thread = threading.Thread(target=self._loop_heartbeat, daemon=True)
        self._hb_thread.start()

    def _loop_heartbeat(self) -> None:
        # ARQ04: mantém o broker ciente de que estamos vivos. Sem isso, um
        # encerramento abrupto (terminal fechado) deixaria o usuário "online"
        # para sempre na tabela do broker.
        while not self._parar.is_set():
            if self.ID is not None:
                self._cmd(f"HEARTBEAT {self.ID}")
            # sleep em pedaços para responder rápido ao parar
            t = 0.0
            while t < HEARTBEAT_INTERVAL and not self._parar.is_set():
                time.sleep(0.1)
                t += 0.1

    def _loop_sub(self) -> None:
        sub = self._ctx.socket(zmq.SUB)
        sub.setsockopt(zmq.LINGER, 0)
        sub.connect(self._sub_addr)
        sub.setsockopt_string(zmq.SUBSCRIBE, "")
        poller = zmq.Poller()
        poller.register(sub, zmq.POLLIN)
        try:
            # ao conectar, semeia o estado atual via LIST (evita race com
            # eventos que já passaram antes do SUB se conectar).
            for uid, salas in self.list_online().items():
                with self._estado_lock:
                    self.online.add(uid)
                    for s in salas:
                        self.salas_membros.setdefault(s, set()).add(uid)

            while not self._parar.is_set():
                socks = dict(poller.poll(200))
                if sub in socks:
                    try:
                        msg = sub.recv_string(zmq.NOBLOCK)
                    except zmq.Again:
                        continue
                    self._aplicar_evento(msg)
        finally:
            sub.close(linger=0)

    def _aplicar_evento(self, msg: str) -> None:
        partes = msg.split()
        with self._estado_lock:
            if len(partes) >= 3 and partes[0] == "PRESENCE":
                if partes[1] == "ONLINE":
                    self.online.add(partes[2])
                elif partes[1] == "OFFLINE":
                    self.online.discard(partes[2])
                    for membros in self.salas_membros.values():
                        membros.discard(partes[2])
            elif len(partes) >= 4 and partes[0] == "SALA":
                sala, acao, uid = partes[1], partes[2], partes[3]
                membros = self.salas_membros.setdefault(sala, set())
                if acao == "JOIN":
                    membros.add(uid)
                elif acao == "LEAVE":
                    membros.discard(uid)
