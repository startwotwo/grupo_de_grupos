import zmq
import threading
import time
import queue
from typing import Dict
from collections import deque

from common.models import Message, MessageType, BrokerInfo
from common.constants import (
    BROKER_INTER_BROKER_PORT, MESSAGE_CACHE_SIZE, HEARTBEAT_TIMEOUT
)
from common.utils import serialize_message, deserialize_message


class InterBrokerCommunication:
    def __init__(self, broker_id: str, host: str = "localhost",
                 inter_broker_port: int = BROKER_INTER_BROKER_PORT,
                 on_remote_message=None):
        self.broker_id = broker_id
        self.host = host
        self.inter_broker_port = inter_broker_port
        self.on_remote_message = on_remote_message

        self.context = zmq.Context()
        self.router = self.context.socket(zmq.ROUTER)
        self.router.setsockopt(zmq.LINGER, 0)

        # Owned exclusively by _send_loop — never touched from other threads
        self._dealers: Dict[str, zmq.Socket] = {}
        self._known_brokers: Dict[str, BrokerInfo] = {}

        self.message_cache = deque(maxlen=MESSAGE_CACHE_SIZE)

        # Cross-thread communication into _send_loop
        self._connect_queue: queue.Queue = queue.Queue()
        self._forward_queue: queue.Queue = queue.Queue()

        self.running = False
        self.threads = []

    # ------------------------------------------------------------------ public

    def start(self):
        self.router.bind(f"tcp://{self.host}:{self.inter_broker_port}")
        self.running = True

        recv_thread = threading.Thread(target=self._receive_loop, daemon=True)
        send_thread = threading.Thread(target=self._send_loop, daemon=True)
        recv_thread.start()
        send_thread.start()
        self.threads.extend([recv_thread, send_thread])

        print(f"[InterBroker] Started on tcp://{self.host}:{self.inter_broker_port}")

    def stop(self):
        self.running = False
        for t in self.threads:
            t.join(timeout=3)
        for dealer in self._dealers.values():
            dealer.setsockopt(zmq.LINGER, 0)
            dealer.close()
        self._dealers.clear()
        self.router.close()
        self.context.term()
        print("[InterBroker] Stopped")

    def connect_to_broker(self, broker_info: BrokerInfo):
        if broker_info.broker_id != self.broker_id:
            self._connect_queue.put(broker_info)

    def forward_message(self, message: Message, exclude_broker: str = None):
        # Dedup before enqueuing to keep the queue small
        if message.message_id in self.message_cache:
            return
        self.message_cache.append(message.message_id)
        self._forward_queue.put((message, exclude_broker))

    def get_broker_count(self) -> int:
        return len(self._dealers)

    def get_known_brokers(self) -> Dict[str, BrokerInfo]:
        return dict(self._known_brokers)

    # ----------------------------------------------- _send_loop private helpers

    def _create_dealer(self, broker_info: BrokerInfo):
        broker_id = broker_info.broker_id
        if broker_id in self._dealers:
            return
        dealer = self.context.socket(zmq.DEALER)
        dealer.setsockopt(zmq.LINGER, 0)
        dealer.setsockopt_string(zmq.IDENTITY, self.broker_id)
        dealer.connect(f"tcp://{broker_info.host}:{broker_info.control_port + 1}")
        self._dealers[broker_id] = dealer
        self._known_brokers[broker_id] = broker_info
        broker_info.last_heartbeat = time.time()
        print(f"[InterBroker] Connected to broker {broker_id}")

    def _drop_dealer(self, broker_id: str):
        dealer = self._dealers.pop(broker_id, None)
        self._known_brokers.pop(broker_id, None)
        if dealer:
            dealer.setsockopt(zmq.LINGER, 0)
            dealer.close()
            print(f"[InterBroker] Disconnected from broker {broker_id}")

    def _send_to_all(self, data: bytes, exclude: str = None):
        for broker_id, dealer in list(self._dealers.items()):
            if broker_id != exclude:
                try:
                    dealer.send(data, copy=True)
                except zmq.ZMQError:
                    pass

    # ------------------------------------------------------------- _send_loop

    def _send_loop(self):
        last_heartbeat = time.time()

        while self.running:
            # 1. process new broker connections
            while True:
                try:
                    self._create_dealer(self._connect_queue.get_nowait())
                except queue.Empty:
                    break

            # 2. forward pending messages
            while True:
                try:
                    message, exclude = self._forward_queue.get_nowait()
                    self._send_to_all(serialize_message(message), exclude)
                except queue.Empty:
                    break

            # 3. heartbeat every 1 s
            now = time.time()
            if now - last_heartbeat >= 1.0:
                hb = serialize_message(Message.create_heartbeat(self.broker_id))
                self._send_to_all(hb)

                dead = [bid for bid, info in self._known_brokers.items()
                        if not info.is_alive(HEARTBEAT_TIMEOUT)]
                for bid in dead:
                    self._drop_dealer(bid)
                    print(f"[InterBroker] Detected dead broker: {bid}")

                last_heartbeat = now

            time.sleep(0.05)

    # ----------------------------------------------------------- _receive_loop

    def _receive_loop(self):
        while self.running:
            if not self.router.poll(timeout=100):
                continue
            try:
                identity, data = self.router.recv_multipart()
            except zmq.ZMQError:
                continue
            try:
                message = deserialize_message(data)
            except Exception:
                continue

            if message.message_id in self.message_cache:
                continue
            self.message_cache.append(message.message_id)

            sender_id = identity.decode('utf-8')

            if message.type == MessageType.HEARTBEAT:
                if sender_id in self._known_brokers:
                    self._known_brokers[sender_id].last_heartbeat = time.time()
            else:
                if self.on_remote_message:
                    self.on_remote_message(message, sender_id)
