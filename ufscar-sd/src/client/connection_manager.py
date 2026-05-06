import zmq
import time
from typing import Optional, List
import threading

from common.models import BrokerInfo
from common.constants import DISCOVERY_PORT, HEARTBEAT_TIMEOUT
from common.utils import serialize_dict, deserialize_dict


class ConnectionManager:
    def __init__(self, discovery_host: str = "localhost",
                 discovery_port: int = DISCOVERY_PORT):
        self.discovery_host = discovery_host
        self.discovery_port = discovery_port

        self.context = zmq.Context()
        self.current_broker: Optional[BrokerInfo] = None
        self.available_brokers: List[BrokerInfo] = []

        self.lock = threading.Lock()

    def get_broker(self, strategy: str = 'least_loaded') -> Optional[BrokerInfo]:
        brokers = self._fetch_brokers()

        if not brokers:
            return None

        if strategy == 'least_loaded':
            return min(brokers, key=lambda b: b.load)
        elif strategy == 'round_robin':
            return brokers[0]
        elif strategy == 'random':
            import random
            return random.choice(brokers)
        else:
            return brokers[0]

    def _fetch_brokers(self) -> List[BrokerInfo]:
        req_socket = self.context.socket(zmq.REQ)
        req_socket.setsockopt(zmq.RCVTIMEO, 5000)
        req_socket.setsockopt(zmq.SNDTIMEO, 5000)

        req_socket.connect(f"tcp://{self.discovery_host}:{self.discovery_port}")

        request = {
            'action': 'GET_BROKERS'
        }

        req_socket.send(serialize_dict(request))

        response_data = req_socket.recv()
        response = deserialize_dict(response_data)

        req_socket.close()

        if response['status'] == 'OK':
            brokers = [BrokerInfo.from_dict(b) for b in response['brokers']]
            with self.lock:
                self.available_brokers = brokers
            return brokers
        else:
            return []

    def select_new_broker(self, exclude_broker_id: Optional[str] = None) -> Optional[BrokerInfo]:
        brokers = self._fetch_brokers()

        if exclude_broker_id:
            brokers = [b for b in brokers if b.broker_id != exclude_broker_id]

        if not brokers:
            return None

        broker = min(brokers, key=lambda b: b.load)

        with self.lock:
            self.current_broker = broker

        return broker

    def get_current_broker(self) -> Optional[BrokerInfo]:
        with self.lock:
            return self.current_broker

    def close(self):
        self.context.term()
