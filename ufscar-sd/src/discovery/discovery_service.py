import zmq
import threading
import time
from typing import Dict, List, Optional
import uuid

from common.models import BrokerInfo
from common.constants import DISCOVERY_PORT, DISCOVERY_PUB_PORT, HEARTBEAT_TIMEOUT
from common.utils import serialize_dict, deserialize_dict


class DiscoveryService:
    def __init__(self, host: str = "localhost", port: int = DISCOVERY_PORT,
                 pub_port: int = DISCOVERY_PUB_PORT):
        self.service_id = str(uuid.uuid4())
        self.host = host
        self.port = port
        self.pub_port = pub_port

        self.context = zmq.Context()
        self.rep_socket = self.context.socket(zmq.REP)
        self.pub_socket = self.context.socket(zmq.PUB)

        self.brokers: Dict[str, BrokerInfo] = {}
        self.lock = threading.Lock()

        self.running = False
        self.threads = []

    def start(self):
        self.rep_socket.bind(f"tcp://{self.host}:{self.port}")
        self.pub_socket.bind(f"tcp://{self.host}:{self.pub_port}")

        self.running = True

        request_thread = threading.Thread(target=self._request_loop, daemon=True)
        cleanup_thread = threading.Thread(target=self._cleanup_loop, daemon=True)
        broadcast_thread = threading.Thread(target=self._broadcast_loop, daemon=True)

        request_thread.start()
        cleanup_thread.start()
        broadcast_thread.start()

        self.threads.extend([request_thread, cleanup_thread, broadcast_thread])

        print(f"[Discovery] Started on tcp://{self.host}:{self.port}")

    def stop(self):
        self.running = False

        for thread in self.threads:
            thread.join(timeout=2)

        for sock in (self.rep_socket, self.pub_socket):
            sock.setsockopt(zmq.LINGER, 0)
            sock.close()
        self.context.term()

        print("[Discovery] Stopped")

    def _request_loop(self):
        while self.running:
            if self.rep_socket.poll(timeout=100):
                request_data = self.rep_socket.recv()
                request = deserialize_dict(request_data)

                response = self._handle_request(request)
                self.rep_socket.send(serialize_dict(response))

    def _handle_request(self, request: dict) -> dict:
        action = request.get('action')

        if action == 'REGISTER_BROKER':
            return self._register_broker(request)
        elif action == 'UNREGISTER_BROKER':
            return self._unregister_broker(request)
        elif action == 'GET_BROKERS':
            return self._get_brokers(request)
        elif action == 'HEARTBEAT':
            return self._update_heartbeat(request)
        else:
            return {'status': 'ERROR', 'message': f'Unknown action: {action}'}

    def _register_broker(self, request: dict) -> dict:
        broker_info = BrokerInfo.from_dict(request['broker_info'])
        broker_info.last_heartbeat = time.time()

        with self.lock:
            self.brokers[broker_info.broker_id] = broker_info

        print(f"[Discovery] Registered broker: {broker_info.broker_id}")

        return {
            'status': 'OK',
            'message': 'Broker registered',
            'broker_id': broker_info.broker_id
        }

    def _unregister_broker(self, request: dict) -> dict:
        broker_id = request.get('broker_id')

        with self.lock:
            if broker_id in self.brokers:
                del self.brokers[broker_id]

        print(f"[Discovery] Unregistered broker: {broker_id}")

        return {
            'status': 'OK',
            'message': 'Broker unregistered'
        }

    def _get_brokers(self, request: dict) -> dict:
        with self.lock:
            alive_brokers = [
                broker.to_dict()
                for broker in self.brokers.values()
                if broker.is_alive(HEARTBEAT_TIMEOUT)
            ]

        alive_brokers.sort(key=lambda b: b['load'])

        return {
            'status': 'OK',
            'brokers': alive_brokers
        }

    def _update_heartbeat(self, request: dict) -> dict:
        broker_id = request.get('broker_id')
        load = request.get('load', 0)

        with self.lock:
            if broker_id in self.brokers:
                self.brokers[broker_id].last_heartbeat = time.time()
                self.brokers[broker_id].load = load

        return {
            'status': 'OK',
            'message': 'Heartbeat updated'
        }

    def _cleanup_loop(self):
        while self.running:
            time.sleep(10)

            dead_brokers = []

            with self.lock:
                for broker_id, broker_info in self.brokers.items():
                    if not broker_info.is_alive(HEARTBEAT_TIMEOUT):
                        dead_brokers.append(broker_id)

                for broker_id in dead_brokers:
                    del self.brokers[broker_id]
                    print(f"[Discovery] Removed dead broker: {broker_id}")

    def _broadcast_loop(self):
        while self.running:
            time.sleep(5)

            with self.lock:
                alive_brokers = [
                    broker.to_dict()
                    for broker in self.brokers.values()
                    if broker.is_alive(HEARTBEAT_TIMEOUT)
                ]

            broadcast_data = {
                'type': 'BROKER_UPDATE',
                'brokers': alive_brokers,
                'timestamp': time.time()
            }

            self.pub_socket.send(serialize_dict(broadcast_data))
