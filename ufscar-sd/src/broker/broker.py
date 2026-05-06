import zmq
import threading
import time
from collections import deque
from typing import Dict, Set, Optional
import uuid

from common.models import Message, MessageType, ControlMessageType, ClientInfo, BrokerInfo
from common.constants import (
    BROKER_TEXT_PUB_PORT, BROKER_AUDIO_PUB_PORT, BROKER_VIDEO_PUB_PORT,
    BROKER_CONTROL_PORT, BROKER_HEARTBEAT_PORT, BROKER_AUDIO_INPUT_PORT,
    BROKER_VIDEO_INPUT_PORT, HEARTBEAT_BROKER_INTERVAL, HEARTBEAT_TIMEOUT,
    MESSAGE_CACHE_SIZE, GROUPS, DISCOVERY_PORT
)
from common.utils import serialize_message, deserialize_message, serialize_dict, deserialize_dict
from broker.inter_broker import InterBrokerCommunication


class Broker:
    def __init__(self, broker_id: Optional[str] = None, host: str = "localhost",
                 text_port: int = BROKER_TEXT_PUB_PORT,
                 audio_pub_port: int = BROKER_AUDIO_PUB_PORT,
                 video_pub_port: int = BROKER_VIDEO_PUB_PORT,
                 control_port: int = BROKER_CONTROL_PORT,
                 heartbeat_port: int = BROKER_HEARTBEAT_PORT,
                 audio_input_port: int = BROKER_AUDIO_INPUT_PORT,
                 video_input_port: int = BROKER_VIDEO_INPUT_PORT,
                 discovery_host: Optional[str] = None,
                 discovery_port: int = DISCOVERY_PORT,
                 enable_clustering: bool = False):
        self.broker_id = broker_id or str(uuid.uuid4())
        self.host = host
        self.text_port = text_port
        self.audio_pub_port = audio_pub_port
        self.video_pub_port = video_pub_port
        self.control_port = control_port
        self.heartbeat_port = heartbeat_port
        self.audio_input_port = audio_input_port
        self.video_input_port = video_input_port
        self.discovery_host = discovery_host
        self.discovery_port = discovery_port
        self.enable_clustering = enable_clustering

        self.context = zmq.Context()

        self.text_pub = self.context.socket(zmq.PUB)
        self.audio_pub = self.context.socket(zmq.PUB)
        self.video_pub = self.context.socket(zmq.PUB)
        self.control_router = self.context.socket(zmq.ROUTER)
        self.heartbeat_pub = self.context.socket(zmq.PUB)
        self.audio_pull = self.context.socket(zmq.PULL)
        self.video_pull = self.context.socket(zmq.PULL)

        self.clients: Dict[str, ClientInfo] = {}
        self.groups: Dict[str, Set[str]] = {group: set() for group in GROUPS}

        self.message_cache = deque(maxlen=MESSAGE_CACHE_SIZE)

        self.inter_broker: Optional[InterBrokerCommunication] = None
        if self.enable_clustering:
            self.inter_broker = InterBrokerCommunication(
                broker_id=self.broker_id,
                host=self.host,
                inter_broker_port=self.control_port + 1,
                on_remote_message=self._handle_remote_message
            )

        self.running = False
        self.threads = []

        self.lock = threading.Lock()

    def start(self):
        self.text_pub.bind(f"tcp://{self.host}:{self.text_port}")
        self.audio_pub.bind(f"tcp://{self.host}:{self.audio_pub_port}")
        self.video_pub.bind(f"tcp://{self.host}:{self.video_pub_port}")
        self.control_router.bind(f"tcp://{self.host}:{self.control_port}")
        self.heartbeat_pub.bind(f"tcp://{self.host}:{self.heartbeat_port}")
        self.audio_pull.bind(f"tcp://{self.host}:{self.audio_input_port}")
        self.video_pull.bind(f"tcp://{self.host}:{self.video_input_port}")

        self.running = True

        control_thread = threading.Thread(target=self._control_loop, daemon=True)
        heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        cleanup_thread = threading.Thread(target=self._cleanup_loop, daemon=True)
        audio_thread = threading.Thread(target=self._audio_loop, daemon=True)
        video_thread = threading.Thread(target=self._video_loop, daemon=True)

        control_thread.start()
        heartbeat_thread.start()
        cleanup_thread.start()
        audio_thread.start()
        video_thread.start()

        self.threads.extend([control_thread, heartbeat_thread, cleanup_thread,
                              audio_thread, video_thread])

        if self.enable_clustering and self.inter_broker:
            self.inter_broker.start()

        if self.discovery_host:
            discovery_thread = threading.Thread(target=self._discovery_loop, daemon=True)
            discovery_thread.start()
            self.threads.append(discovery_thread)

        print(f"[Broker {self.broker_id}] Started")
        print(f"  Text PUB:     tcp://{self.host}:{self.text_port}")
        print(f"  Audio PUB:    tcp://{self.host}:{self.audio_pub_port}")
        print(f"  Video PUB:    tcp://{self.host}:{self.video_pub_port}")
        print(f"  Audio INPUT:  tcp://{self.host}:{self.audio_input_port}")
        print(f"  Video INPUT:  tcp://{self.host}:{self.video_input_port}")
        print(f"  Control:      tcp://{self.host}:{self.control_port}")
        print(f"  Heartbeat:    tcp://{self.host}:{self.heartbeat_port}")
        if self.enable_clustering:
            print(f"  Inter-Broker: tcp://{self.host}:{self.control_port + 1}")
        if self.discovery_host:
            print(f"  Discovery: tcp://{self.discovery_host}:{self.discovery_port}")

    def stop(self):
        if self.discovery_host:
            self._unregister_from_discovery()

        self.running = False

        if self.inter_broker:
            self.inter_broker.stop()

        for thread in self.threads:
            thread.join(timeout=2)

        for sock in (self.text_pub, self.audio_pub, self.video_pub,
                     self.audio_pull, self.video_pull,
                     self.control_router, self.heartbeat_pub):
            sock.setsockopt(zmq.LINGER, 0)
            sock.close()
        self.context.term()

        print(f"[Broker {self.broker_id}] Stopped")

    def _control_loop(self):
        while self.running:
            if self.control_router.poll(timeout=100):
                identity, data = self.control_router.recv_multipart()

                message = deserialize_message(data)
                self._handle_control_message(identity, message)

    def _handle_control_message(self, identity: bytes, message: Message):
        if message.type == MessageType.CONTROL:
            if message.control_type == ControlMessageType.LOGIN:
                self._handle_login(identity, message)
            elif message.control_type == ControlMessageType.LOGOUT:
                self._handle_logout(identity, message)
            elif message.control_type == ControlMessageType.JOIN_GROUP:
                self._handle_join_group(identity, message)
            elif message.control_type == ControlMessageType.LEAVE_GROUP:
                self._handle_leave_group(identity, message)
            elif message.control_type == ControlMessageType.LIST_USERS:
                self._handle_list_users(identity, message)
            elif message.control_type == ControlMessageType.LIST_GROUPS:
                self._handle_list_groups(identity, message)

        elif message.type == MessageType.TEXT:
            self._handle_text_message(identity, message)

        elif message.type == MessageType.HEARTBEAT:
            self._handle_heartbeat(identity, message)

    def _handle_login(self, identity: bytes, message: Message):
        client_id = message.sender_id

        with self.lock:
            self.clients[client_id] = ClientInfo(
                client_id=client_id,
                last_heartbeat=time.time()
            )

        response = Message.create_control(
            sender_id=self.broker_id,
            control_type=ControlMessageType.ACK,
            payload=f"Logged in as {client_id}".encode('utf-8')
        )
        self.control_router.send_multipart([identity, serialize_message(response)])

        print(f"[Broker] Client {client_id} logged in")

    def _handle_logout(self, identity: bytes, message: Message):
        client_id = message.sender_id
        left_group = None

        with self.lock:
            if client_id in self.clients:
                client_info = self.clients[client_id]
                left_group = client_info.group
                if left_group:
                    self.groups[left_group].discard(client_id)
                del self.clients[client_id]

        if left_group:
            self._broadcast_presence(client_id, left_group, online=False)

        response = Message.create_control(
            sender_id=self.broker_id,
            control_type=ControlMessageType.ACK,
            payload=b"Logged out"
        )
        self.control_router.send_multipart([identity, serialize_message(response)])

        print(f"[Broker] Client {client_id} logged out")

    def _handle_join_group(self, identity: bytes, message: Message):
        client_id = message.sender_id
        group = message.payload.decode('utf-8')

        if group not in GROUPS:
            response = Message.create_control(
                sender_id=self.broker_id,
                control_type=ControlMessageType.NACK,
                payload=f"Invalid group: {group}".encode('utf-8')
            )
            self.control_router.send_multipart([identity, serialize_message(response)])
            return

        old_group = None
        with self.lock:
            if client_id in self.clients:
                old_group = self.clients[client_id].group
                if old_group:
                    self.groups[old_group].discard(client_id)

                self.clients[client_id].group = group
                self.groups[group].add(client_id)
                members = list(self.groups[group])

        if old_group:
            self._broadcast_presence(client_id, old_group, online=False)

        response = Message.create_control(
            sender_id=self.broker_id,
            control_type=ControlMessageType.ACK,
            payload=serialize_dict({'members': members})
        )
        self.control_router.send_multipart([identity, serialize_message(response)])

        self._broadcast_presence(client_id, group, online=True)

        print(f"[Broker] Client {client_id} joined group {group}")

    def _handle_leave_group(self, identity: bytes, message: Message):
        client_id = message.sender_id
        left_group = None

        with self.lock:
            if client_id in self.clients:
                left_group = self.clients[client_id].group
                if left_group:
                    self.groups[left_group].discard(client_id)
                    self.clients[client_id].group = None

        if left_group:
            self._broadcast_presence(client_id, left_group, online=False)

        response = Message.create_control(
            sender_id=self.broker_id,
            control_type=ControlMessageType.ACK,
            payload=b"Left group"
        )
        self.control_router.send_multipart([identity, serialize_message(response)])

        print(f"[Broker] Client {client_id} left group")

    def _handle_list_users(self, identity: bytes, message: Message):
        with self.lock:
            users_data = {
                'users': [
                    {'client_id': cid, 'group': info.group}
                    for cid, info in self.clients.items()
                ]
            }

        response = Message.create_control(
            sender_id=self.broker_id,
            control_type=ControlMessageType.ACK,
            payload=serialize_dict(users_data)
        )
        self.control_router.send_multipart([identity, serialize_message(response)])

    def _handle_list_groups(self, identity: bytes, message: Message):
        with self.lock:
            groups_data = {
                'groups': [
                    {'name': group, 'members': list(members)}
                    for group, members in self.groups.items()
                    if members
                ]
            }

        response = Message.create_control(
            sender_id=self.broker_id,
            control_type=ControlMessageType.ACK,
            payload=serialize_dict(groups_data)
        )
        self.control_router.send_multipart([identity, serialize_message(response)])

    def _broadcast_presence(self, client_id: str, group: str, online: bool):
        presence_msg = Message.create_presence(
            sender_id=client_id,
            is_online=online,
            group=group
        )
        topic = f"group:{group}".encode('utf-8')
        self.text_pub.send_multipart([topic, serialize_message(presence_msg)])

    def _handle_text_message(self, identity: bytes, message: Message):
        if message.message_id in self.message_cache:
            return

        self.message_cache.append(message.message_id)

        if message.group:
            topic = f"group:{message.group}".encode('utf-8')
        elif message.recipient:
            topic = f"user:{message.recipient}".encode('utf-8')
        else:
            topic = b"broadcast"

        self.text_pub.send_multipart([topic, serialize_message(message)])

        if self.inter_broker:
            self.inter_broker.forward_message(message)

        if message.qos_level > 0:
            ack = Message.create_control(
                sender_id=self.broker_id,
                control_type=ControlMessageType.ACK,
                payload=message.message_id.encode('utf-8')
            )
            self.control_router.send_multipart([identity, serialize_message(ack)])

        print(f"[Broker] Forwarded text message from {message.sender_id} to {topic.decode('utf-8')}")

    def _handle_heartbeat(self, identity: bytes, message: Message):
        client_id = message.sender_id

        with self.lock:
            if client_id in self.clients:
                self.clients[client_id].last_heartbeat = time.time()

    def _heartbeat_loop(self):
        while self.running:
            heartbeat = Message.create_heartbeat(self.broker_id)
            self.heartbeat_pub.send(serialize_message(heartbeat))

            time.sleep(HEARTBEAT_BROKER_INTERVAL)

    def _cleanup_loop(self):
        while self.running:
            time.sleep(5)

            current_time = time.time()
            dead_clients = []

            with self.lock:
                for client_id, client_info in self.clients.items():
                    if current_time - client_info.last_heartbeat > HEARTBEAT_TIMEOUT:
                        dead_clients.append(client_id)

                for client_id in dead_clients:
                    client_info = self.clients[client_id]
                    if client_info.group:
                        self.groups[client_info.group].discard(client_id)
                    del self.clients[client_id]
                    print(f"[Broker] Cleaned up dead client: {client_id}")

    def _audio_loop(self):
        while self.running:
            if self.audio_pull.poll(timeout=100):
                try:
                    data = self.audio_pull.recv()
                    message = deserialize_message(data)
                    self._forward_media(self.audio_pub, message)
                except zmq.ZMQError:
                    pass

    def _video_loop(self):
        while self.running:
            if self.video_pull.poll(timeout=100):
                try:
                    data = self.video_pull.recv()
                    message = deserialize_message(data)
                    self._forward_media(self.video_pub, message)
                except zmq.ZMQError:
                    pass

    def _forward_media(self, pub_socket, message: Message):
        if message.group:
            topic = f"group:{message.group}".encode('utf-8')
        elif message.recipient:
            topic = f"user:{message.recipient}".encode('utf-8')
        else:
            topic = b"broadcast"
        pub_socket.send_multipart([topic, serialize_message(message)])

    def _handle_remote_message(self, message: Message, sender_broker_id: str):
        if message.message_id in self.message_cache:
            return

        self.message_cache.append(message.message_id)

        if message.type == MessageType.TEXT:
            if message.group:
                topic = f"group:{message.group}".encode('utf-8')
            elif message.recipient:
                topic = f"user:{message.recipient}".encode('utf-8')
            else:
                topic = b"broadcast"

            self.text_pub.send_multipart([topic, serialize_message(message)])

            print(f"[Broker] Forwarded remote message from broker {sender_broker_id}")

        elif message.type == MessageType.PRESENCE:
            pass

    def _discovery_loop(self):
        time.sleep(1)

        self._register_with_discovery()

        while self.running:
            self._send_heartbeat_to_discovery()
            self._sync_with_other_brokers()

            time.sleep(5)

    def _register_with_discovery(self):
        req_socket = self.context.socket(zmq.REQ)
        req_socket.setsockopt(zmq.RCVTIMEO, 5000)
        req_socket.connect(f"tcp://{self.discovery_host}:{self.discovery_port}")

        broker_info = BrokerInfo(
            broker_id=self.broker_id,
            host=self.host,
            text_port=self.text_port,
            audio_port=0,
            video_port=0,
            control_port=self.control_port,
            load=len(self.clients)
        )

        request = {
            'action': 'REGISTER_BROKER',
            'broker_info': broker_info.to_dict()
        }

        req_socket.send(serialize_dict(request))

        response_data = req_socket.recv()
        response = deserialize_dict(response_data)

        req_socket.close()

        if response['status'] == 'OK':
            print(f"[Broker] Registered with discovery service")
        else:
            print(f"[Broker] Failed to register with discovery: {response.get('message')}")

    def _unregister_from_discovery(self):
        req_socket = self.context.socket(zmq.REQ)
        req_socket.setsockopt(zmq.RCVTIMEO, 5000)
        req_socket.connect(f"tcp://{self.discovery_host}:{self.discovery_port}")

        request = {
            'action': 'UNREGISTER_BROKER',
            'broker_id': self.broker_id
        }

        req_socket.send(serialize_dict(request))

        response_data = req_socket.recv()

        req_socket.close()

    def _send_heartbeat_to_discovery(self):
        req_socket = self.context.socket(zmq.REQ)
        req_socket.setsockopt(zmq.RCVTIMEO, 5000)
        req_socket.connect(f"tcp://{self.discovery_host}:{self.discovery_port}")

        request = {
            'action': 'HEARTBEAT',
            'broker_id': self.broker_id,
            'load': len(self.clients)
        }

        req_socket.send(serialize_dict(request))

        req_socket.recv()
        req_socket.close()

    def _sync_with_other_brokers(self):
        if not self.inter_broker:
            return

        req_socket = self.context.socket(zmq.REQ)
        req_socket.setsockopt(zmq.RCVTIMEO, 5000)
        req_socket.connect(f"tcp://{self.discovery_host}:{self.discovery_port}")

        request = {
            'action': 'GET_BROKERS'
        }

        req_socket.send(serialize_dict(request))

        response_data = req_socket.recv()
        response = deserialize_dict(response_data)

        req_socket.close()

        if response['status'] == 'OK':
            for broker_dict in response['brokers']:
                broker_info = BrokerInfo.from_dict(broker_dict)
                if broker_info.broker_id != self.broker_id:
                    self.inter_broker.connect_to_broker(broker_info)

    def get_load(self) -> int:
        with self.lock:
            return len(self.clients)
