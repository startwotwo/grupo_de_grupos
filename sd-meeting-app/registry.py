#!/usr/bin/env python3
"""
Service Discovery Registry — sd-meeting-app

Padrão ZeroMQ: REQ/REP
  Clientes e brokers enviam REQ; registry responde com REP.

Operações suportadas:
  register    — broker anuncia presença e salas gerenciadas
  heartbeat   — broker renova registro (evita expiração)
  query_room  — cliente/broker pergunta qual broker possui uma sala
  list_brokers — retorna todos os brokers ativos

Tolerância a falhas:
  Thread de expiração remove brokers que param de enviar heartbeat
  dentro de registry.heartbeat_timeout segundos (config.yaml).
"""

import json
import threading
import time

import yaml
import zmq


def _load_cfg():
    import os
    cfg_path = os.environ.get("BROKER_CONFIG", "config.yaml")
    with open(cfg_path) as f:
        return yaml.safe_load(f)


class Registry:
    def __init__(self):
        self.cfg = _load_cfg()
        self._brokers = {}   # broker_id -> {broker_id, host, ports, rooms, last_hb}
        self._room_map = {}  # room -> broker_id
        self._lock = threading.Lock()

    def _pick_broker_for_room(self) -> dict | None:
        if not self._brokers:
            return None
        return min(self._brokers.values(), key=lambda b: len(b.get("rooms", [])))

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    def _register(self, req):
        bid = req["broker_id"]
        with self._lock:
            self._brokers[bid] = {
                "broker_id": bid,
                "host":      req["host"],
                "ports":     req["ports"],
                "rooms":     req["rooms"],
                "last_hb":   time.time(),
            }
            for room in req["rooms"]:
                self._room_map[room] = bid
        print(f"[Registry] Broker {bid[:8]}... registrado  rooms={req['rooms']}")
        return {"status": "ok"}

    def _heartbeat(self, req):
        bid = req["broker_id"]
        with self._lock:
            if bid in self._brokers:
                self._brokers[bid]["last_hb"] = time.time()
        return {"status": "ok"}

    def _query_room(self, req):
        room = req["room"]
        with self._lock:
            bid = self._room_map.get(room)
            if not bid or bid not in self._brokers:
                broker = self._pick_broker_for_room()
                if not broker:
                    return {"status": "not_found"}
                self._room_map[room] = broker["broker_id"]
                bid = broker["broker_id"]
            b = self._brokers[bid]
            return {"status": "ok", "broker": {
                "broker_id": b["broker_id"],
                "host":      b["host"],
                "ports":     b["ports"],
                "rooms":     b["rooms"],
            }}

    def _list_brokers(self):
        with self._lock:
            brokers = [
                {"broker_id": b["broker_id"], "host": b["host"],
                 "ports": b["ports"], "rooms": b["rooms"]}
                for b in self._brokers.values()
            ]
        return {"status": "ok", "brokers": brokers}

    def _dispatch(self, req):
        t = req.get("type")
        if t == "register":
            return self._register(req)
        if t == "heartbeat":
            return self._heartbeat(req)
        if t == "query_room":
            return self._query_room(req)
        if t == "list_brokers":
            return self._list_brokers()
        return {"status": "error", "msg": f"unknown type: {t}"}

    # ------------------------------------------------------------------
    # Expiração de brokers mortos
    # ------------------------------------------------------------------

    def _expiry_watcher(self):
        timeout = self.cfg["registry"]["heartbeat_timeout"]
        while True:
            time.sleep(2)
            now = time.time()
            with self._lock:
                dead = [bid for bid, b in self._brokers.items()
                        if now - b["last_hb"] > timeout]
                for bid in dead:
                    b = self._brokers.pop(bid)
                    for room in b["rooms"]:
                        if self._room_map.get(room) == bid:
                            del self._room_map[room]
                    print(f"[Registry] Broker {bid[:8]}... expirado (sem heartbeat)")

    # ------------------------------------------------------------------
    # Loop principal
    # ------------------------------------------------------------------

    def run(self):
        ctx = zmq.Context()
        rep = ctx.socket(zmq.REP)
        listen_host = self.cfg["registry"].get("listen_host", "0.0.0.0")
        port = self.cfg["registry"]["port"]
        rep.bind(f"tcp://{listen_host}:{port}")
        print(f"[Registry] Escutando em {listen_host}:{port}",flush=True)

        threading.Thread(target=self._expiry_watcher, daemon=True).start()

        while True:
            raw = rep.recv_string()
            try:
                req = json.loads(raw)
                resp = self._dispatch(req)
            except Exception as e:
                resp = {"status": "error", "msg": str(e)}
            rep.send_string(json.dumps(resp))


if __name__ == "__main__":
    Registry().run()
