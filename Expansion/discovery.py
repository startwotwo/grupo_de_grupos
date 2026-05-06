"""
Discovery Service — simple broker registry.

Protocol (REQ/REP, JSON):

  REGISTER  -> {"cmd":"REGISTER","id":"B1","host":"192.168.0.10","base_port":5555}
               <- {"ok":true}

  HEARTBEAT -> {"cmd":"HEARTBEAT","id":"B1"}
               <- {"ok":true}

  UNREGISTER-> {"cmd":"UNREGISTER","id":"B1"}
               <- {"ok":true}

  LIST      -> {"cmd":"LIST"}
               <- {"ok":true,"brokers":[{"id":"B1","host":"...","base_port":5555}, ...]}

Usage:
  python discovery.py
  python discovery.py --port 5570
"""

import argparse
import json
import threading
import time

import zmq

from common import (
    DISCOVERY_PORT, BROKER_TIMEOUT,
    CMD_REGISTER, CMD_HEARTBEAT, CMD_LIST, CMD_UNREGISTER,
)


class Registry:
    """Stores alive brokers. Thread-safe."""

    def __init__(self):
        self._brokers = {}  # id -> {"host":..., "base_port":..., "last_seen":t}
        self._lock = threading.Lock()

    def register(self, broker_id, host, base_port):
        with self._lock:
            self._brokers[broker_id] = {
                "host": host,
                "base_port": base_port,
                "last_seen": time.time(),
            }
        print(f"[discovery] REGISTER {broker_id} @ {host}:{base_port}")

    def heartbeat(self, broker_id):
        with self._lock:
            if broker_id in self._brokers:
                self._brokers[broker_id]["last_seen"] = time.time()
                return True
            return False  # unknown broker, force re-registration

    def unregister(self, broker_id):
        with self._lock:
            if self._brokers.pop(broker_id, None):
                print(f"[discovery] UNREGISTER {broker_id}")

    def list_alive(self):
        now = time.time()
        with self._lock:
            return [
                {"id": bid, "host": b["host"], "base_port": b["base_port"]}
                for bid, b in self._brokers.items()
                if now - b["last_seen"] < BROKER_TIMEOUT
            ]

    def cleanup_dead(self):
        """Remove brokers without heartbeat. Runs in separate thread."""
        now = time.time()
        with self._lock:
            dead = [
                bid for bid, b in self._brokers.items()
                if now - b["last_seen"] >= BROKER_TIMEOUT
            ]
            for bid in dead:
                del self._brokers[bid]
        for bid in dead:
            print(f"[discovery] TIMEOUT {bid} (no heartbeat)")


def cleanup_loop(registry, stop):
    while not stop.is_set():
        time.sleep(1.0)
        registry.cleanup_dead()


def handle(req, registry):
    """Receives a dict, returns a dict."""
    cmd = req.get("cmd")

    if cmd == CMD_REGISTER:
        registry.register(req["id"], req["host"], int(req["base_port"]))
        return {"ok": True}

    if cmd == CMD_HEARTBEAT:
        ok = registry.heartbeat(req["id"])
        # If unknown, signal to the broker to re-register
        return {"ok": ok, "need_register": not ok}

    if cmd == CMD_UNREGISTER:
        registry.unregister(req["id"])
        return {"ok": True}

    if cmd == CMD_LIST:
        return {"ok": True, "brokers": registry.list_alive()}

    return {"ok": False, "error": f"unknown cmd: {cmd}"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=DISCOVERY_PORT)
    args = parser.parse_args()

    ctx = zmq.Context.instance()
    rep = ctx.socket(zmq.REP)
    rep.bind(f"tcp://*:{args.port}")
    print(f"[discovery] listening on tcp://*:{args.port}")

    registry = Registry()
    stop = threading.Event()
    threading.Thread(target=cleanup_loop, args=(registry, stop), daemon=True).start()

    try:
        while True:
            raw = rep.recv_string()
            try:
                req = json.loads(raw)
                resp = handle(req, registry)
            except json.JSONDecodeError:
                resp = {"ok": False, "error": "invalid json"}
            rep.send_string(json.dumps(resp))
    except KeyboardInterrupt:
        print("\n[discovery] shutting down...")
    finally:
        stop.set()
        rep.close()
        ctx.term()


if __name__ == "__main__":
    main()