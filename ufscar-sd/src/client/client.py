import zmq
import threading
import time
import queue
from typing import Optional, Callable
import uuid

from common.models import Message, MessageType, ControlMessageType
from common.constants import (
    BROKER_TEXT_PUB_PORT, BROKER_AUDIO_PUB_PORT, BROKER_VIDEO_PUB_PORT,
    BROKER_CONTROL_PORT, BROKER_HEARTBEAT_PORT,
    BROKER_AUDIO_INPUT_PORT, BROKER_VIDEO_INPUT_PORT,
    HEARTBEAT_CLIENT_INTERVAL, HEARTBEAT_TIMEOUT, DISCOVERY_PORT,
    VIDEO_FRAME_WIDTH, VIDEO_FRAME_HEIGHT, VIDEO_FPS, VIDEO_JPEG_QUALITY,
    AUDIO_SAMPLE_RATE, AUDIO_CHUNK_SIZE, AUDIO_CHANNELS
)
from common.utils import serialize_message, deserialize_message, deserialize_dict
from client.connection_manager import ConnectionManager


class Client:
    def __init__(self, client_id: Optional[str] = None, broker_host: Optional[str] = None,
                 text_port: int = BROKER_TEXT_PUB_PORT,
                 audio_port: int = BROKER_AUDIO_PUB_PORT,
                 video_port: int = BROKER_VIDEO_PUB_PORT,
                 control_port: int = BROKER_CONTROL_PORT,
                 heartbeat_port: int = BROKER_HEARTBEAT_PORT,
                 audio_input_port: int = BROKER_AUDIO_INPUT_PORT,
                 video_input_port: int = BROKER_VIDEO_INPUT_PORT,
                 discovery_host: Optional[str] = None,
                 discovery_port: int = DISCOVERY_PORT,
                 enable_failover: bool = False):
        self.client_id = client_id or f"client_{uuid.uuid4().hex[:8]}"
        self.broker_host = broker_host
        self.text_port = text_port
        self.audio_port = audio_port
        self.video_port = video_port
        self.control_port = control_port
        self.heartbeat_port = heartbeat_port
        self.audio_input_port = audio_input_port
        self.video_input_port = video_input_port
        self.discovery_host = discovery_host
        self.discovery_port = discovery_port
        self.enable_failover = enable_failover

        self.context = zmq.Context()

        self.text_sub = None
        self.audio_sub = None
        self.video_sub = None
        self.control_dealer = None
        self.audio_push = None
        self.video_push = None
        self.heartbeat_sub = None

        self.connection_manager: Optional[ConnectionManager] = None
        if self.enable_failover and self.discovery_host:
            self.connection_manager = ConnectionManager(
                discovery_host=self.discovery_host,
                discovery_port=self.discovery_port
            )

        self.current_group: Optional[str] = None

        self.running = False
        self.connected = False
        self.threads = []

        self.last_broker_heartbeat = 0.0
        self.reconnecting = False

        self.video_enabled = False
        self.audio_enabled = False
        self._video_sequence = 0
        self._audio_sequence = 0

        self.on_text_received: Optional[Callable[[str, str, Optional[str]], None]] = None
        self.on_video_received: Optional[Callable[[str, bytes], None]] = None
        self.on_audio_received: Optional[Callable[[str, bytes], None]] = None
        self.on_member_joined: Optional[Callable[[str], None]] = None
        self.on_member_left: Optional[Callable[[str], None]] = None

    def connect(self) -> bool:
        if self.enable_failover and self.connection_manager:
            broker_info = self.connection_manager.select_new_broker()
            if not broker_info:
                print(f"[Client {self.client_id}] No brokers available")
                return False

            self.broker_host = broker_info.host
            self.text_port = broker_info.text_port
            self.audio_port = broker_info.audio_port
            self.video_port = broker_info.video_port
            self.control_port = broker_info.control_port
            self.heartbeat_port = self.control_port + 2

            print(f"[Client {self.client_id}] Selected broker {broker_info.broker_id}")
        elif not self.broker_host and self.discovery_host:
            tmp_manager = ConnectionManager(self.discovery_host, self.discovery_port)
            broker_info = tmp_manager.get_broker()
            tmp_manager.close()
            if not broker_info:
                print(f"[Client {self.client_id}] No brokers available via discovery")
                return False
            self.broker_host = broker_info.host
            self.text_port = broker_info.text_port
            self.audio_port = broker_info.audio_port
            self.video_port = broker_info.video_port
            self.control_port = broker_info.control_port
            self.heartbeat_port = self.control_port + 2
            print(f"[Client {self.client_id}] Selected broker {broker_info.broker_id}")

        if not self.broker_host:
            print(f"[Client {self.client_id}] No broker host specified")
            return False

        if self.text_sub is None:
            self.text_sub = self.context.socket(zmq.SUB)
            self.audio_sub = self.context.socket(zmq.SUB)
            self.video_sub = self.context.socket(zmq.SUB)
            self.control_dealer = self.context.socket(zmq.DEALER)
            self.audio_push = self.context.socket(zmq.PUSH)
            self.video_push = self.context.socket(zmq.PUSH)
            self.heartbeat_sub = self.context.socket(zmq.SUB)

        self.text_sub.connect(f"tcp://{self.broker_host}:{self.text_port}")
        self.audio_sub.connect(f"tcp://{self.broker_host}:{self.audio_port}")
        self.video_sub.connect(f"tcp://{self.broker_host}:{self.video_port}")
        self.control_dealer.connect(f"tcp://{self.broker_host}:{self.control_port}")
        self.audio_push.connect(f"tcp://{self.broker_host}:{self.audio_input_port}")
        self.video_push.connect(f"tcp://{self.broker_host}:{self.video_input_port}")
        self.heartbeat_sub.connect(f"tcp://{self.broker_host}:{self.heartbeat_port}")

        self.text_sub.setsockopt_string(zmq.SUBSCRIBE, "broadcast")
        self.heartbeat_sub.setsockopt_string(zmq.SUBSCRIBE, "")

        login_msg = Message.create_control(
            sender_id=self.client_id,
            control_type=ControlMessageType.LOGIN
        )
        self.control_dealer.send(serialize_message(login_msg))

        if self.control_dealer.poll(timeout=5000):
            response_data = self.control_dealer.recv()
            response = deserialize_message(response_data)

            if response.control_type == ControlMessageType.ACK:
                self.connected = True
                self.last_broker_heartbeat = time.time()
                print(f"[Client {self.client_id}] Connected to broker at {self.broker_host}")
                return True

        print(f"[Client {self.client_id}] Failed to connect")
        return False

    def start(self):
        if not self.connected:
            if not self.connect():
                return

        self.running = True

        receive_thread = threading.Thread(target=self._receive_loop, daemon=True)
        heartbeat_send_thread = threading.Thread(target=self._heartbeat_send_loop, daemon=True)
        heartbeat_monitor_thread = threading.Thread(target=self._heartbeat_monitor_loop, daemon=True)
        video_recv_thread = threading.Thread(target=self._receive_video_loop, daemon=True)
        audio_recv_thread = threading.Thread(target=self._receive_audio_loop, daemon=True)

        receive_thread.start()
        heartbeat_send_thread.start()
        heartbeat_monitor_thread.start()
        video_recv_thread.start()
        audio_recv_thread.start()

        self.threads.extend([receive_thread, heartbeat_send_thread, heartbeat_monitor_thread,
                              video_recv_thread, audio_recv_thread])

        print(f"[Client {self.client_id}] Started")

    def stop(self):
        if self.connected and self.control_dealer:
            logout_msg = Message.create_control(
                sender_id=self.client_id,
                control_type=ControlMessageType.LOGOUT
            )
            self.control_dealer.send(serialize_message(logout_msg))

        self.video_enabled = False
        self.audio_enabled = False
        self.running = False

        for thread in self.threads:
            thread.join(timeout=2)

        for sock in (self.text_sub, self.audio_sub, self.video_sub,
                     self.control_dealer, self.audio_push, self.video_push,
                     self.heartbeat_sub):
            if sock:
                sock.close()

        if self.connection_manager:
            self.connection_manager.close()

        self.context.term()

        print(f"[Client {self.client_id}] Stopped")

    def join_group(self, group: str) -> bool:
        msg = Message.create_control(
            sender_id=self.client_id,
            control_type=ControlMessageType.JOIN_GROUP,
            payload=group.encode('utf-8')
        )
        self.control_dealer.send(serialize_message(msg))

        if self.control_dealer.poll(timeout=5000):
            response_data = self.control_dealer.recv()
            response = deserialize_message(response_data)

            if response.control_type == ControlMessageType.ACK:
                if self.current_group:
                    self.text_sub.setsockopt_string(zmq.UNSUBSCRIBE, f"group:{self.current_group}")
                    self.audio_sub.setsockopt_string(zmq.UNSUBSCRIBE, f"group:{self.current_group}")
                    self.video_sub.setsockopt_string(zmq.UNSUBSCRIBE, f"group:{self.current_group}")

                self.current_group = group
                self.text_sub.setsockopt_string(zmq.SUBSCRIBE, f"group:{group}")
                self.text_sub.setsockopt_string(zmq.SUBSCRIBE, f"user:{self.client_id}")
                self.audio_sub.setsockopt_string(zmq.SUBSCRIBE, f"group:{group}")
                self.video_sub.setsockopt_string(zmq.SUBSCRIBE, f"group:{group}")

                try:
                    members_data = deserialize_dict(response.payload)
                    for member_id in members_data.get('members', []):
                        if member_id != self.client_id and self.on_member_joined:
                            self.on_member_joined(member_id)
                except Exception:
                    pass

                print(f"[Client {self.client_id}] Joined group {group}")
                return True
            else:
                print(f"[Client {self.client_id}] Failed to join group: {response.payload.decode('utf-8')}")
                return False

        return False

    def leave_group(self) -> bool:
        if not self.current_group:
            return True

        msg = Message.create_control(
            sender_id=self.client_id,
            control_type=ControlMessageType.LEAVE_GROUP
        )
        self.control_dealer.send(serialize_message(msg))

        if self.control_dealer.poll(timeout=5000):
            response_data = self.control_dealer.recv()
            response = deserialize_message(response_data)

            if response.control_type == ControlMessageType.ACK:
                self.text_sub.setsockopt_string(zmq.UNSUBSCRIBE, f"group:{self.current_group}")
                self.text_sub.setsockopt_string(zmq.UNSUBSCRIBE, f"user:{self.client_id}")
                self.audio_sub.setsockopt_string(zmq.UNSUBSCRIBE, f"group:{self.current_group}")
                self.video_sub.setsockopt_string(zmq.UNSUBSCRIBE, f"group:{self.current_group}")
                self.current_group = None

                print(f"[Client {self.client_id}] Left group")
                return True

        return False

    def send_text(self, text: str, recipient: Optional[str] = None):
        if not self.control_dealer or not self.connected:
            print(f"[Client {self.client_id}] Not connected, cannot send text")
            return

        msg = Message.create_text(
            sender_id=self.client_id,
            text=text,
            group=self.current_group if not recipient else None,
            recipient=recipient,
            qos_level=1
        )

        self.control_dealer.send(serialize_message(msg))

        if self.control_dealer.poll(timeout=5000):
            response_data = self.control_dealer.recv()
            response = deserialize_message(response_data)

            if response.control_type == ControlMessageType.ACK:
                print(f"[Client {self.client_id}] Message sent successfully")
            else:
                print(f"[Client {self.client_id}] Message send failed")

    def list_users(self):
        msg = Message.create_control(
            sender_id=self.client_id,
            control_type=ControlMessageType.LIST_USERS
        )
        self.control_dealer.send(serialize_message(msg))

        if self.control_dealer.poll(timeout=5000):
            response_data = self.control_dealer.recv()
            response = deserialize_message(response_data)

            if response.control_type == ControlMessageType.ACK:
                users_data = deserialize_dict(response.payload)
                print(f"\n[Online Users]")
                for user in users_data['users']:
                    group_info = f" (Group: {user['group']})" if user['group'] else ""
                    print(f"  - {user['client_id']}{group_info}")

    def list_groups(self):
        msg = Message.create_control(
            sender_id=self.client_id,
            control_type=ControlMessageType.LIST_GROUPS
        )
        self.control_dealer.send(serialize_message(msg))

        if self.control_dealer.poll(timeout=5000):
            response_data = self.control_dealer.recv()
            response = deserialize_message(response_data)

            if response.control_type == ControlMessageType.ACK:
                groups_data = deserialize_dict(response.payload)
                print(f"\n[Active Groups]")
                for group in groups_data['groups']:
                    print(f"  - {group['name']}: {len(group['members'])} members")
                    for member in group['members']:
                        print(f"    - {member}")

    def _receive_loop(self):
        while self.running:
            if self.reconnecting or not self.text_sub:
                time.sleep(0.1)
                continue
            try:
                if self.text_sub.poll(timeout=100):
                    topic, data = self.text_sub.recv_multipart()
                    message = deserialize_message(data)
                    if message.sender_id != self.client_id:
                        if message.type == MessageType.PRESENCE:
                            self._handle_presence_message(message)
                        else:
                            self._handle_received_message(message)
            except zmq.ZMQError:
                pass

    def _handle_received_message(self, message: Message):
        if message.type == MessageType.TEXT:
            text = message.payload.decode('utf-8')
            sender = message.sender_id
            group = message.group

            if self.on_text_received:
                self.on_text_received(sender, text, group)
            else:
                group_str = f" [Group {group}]" if group else " [DM]"
                print(f"\n[{sender}]{group_str}: {text}")

    def _handle_presence_message(self, message):
        is_online = message.payload == b'1'
        if is_online and self.on_member_joined:
            self.on_member_joined(message.sender_id)
        elif not is_online and self.on_member_left:
            self.on_member_left(message.sender_id)

    def enable_video(self):
        if self.video_enabled:
            return
        self.video_enabled = True
        t = threading.Thread(target=self._capture_video_loop, daemon=True)
        t.start()
        self.threads.append(t)

    def disable_video(self):
        self.video_enabled = False

    def enable_audio(self):
        if self.audio_enabled:
            return
        self.audio_enabled = True
        t = threading.Thread(target=self._capture_audio_loop, daemon=True)
        t.start()
        self.threads.append(t)

    def disable_audio(self):
        self.audio_enabled = False

    def _capture_video_loop(self):
        try:
            import cv2
        except ImportError:
            print(f"[Client {self.client_id}] opencv not available, video disabled")
            self.video_enabled = False
            return

        cap = cv2.VideoCapture(0)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, VIDEO_FRAME_WIDTH)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, VIDEO_FRAME_HEIGHT)
        frame_interval = 1.0 / VIDEO_FPS

        while self.running and self.video_enabled:
            ret, frame = cap.read()
            if ret and self.current_group and self.video_push:
                _, jpeg = cv2.imencode('.jpg', frame,
                                       [cv2.IMWRITE_JPEG_QUALITY, VIDEO_JPEG_QUALITY])
                msg = Message.create_video(
                    sender_id=self.client_id,
                    video_data=jpeg.tobytes(),
                    group=self.current_group,
                    sequence=self._video_sequence
                )
                self._video_sequence += 1
                try:
                    if not self.reconnecting:
                        self.video_push.send(serialize_message(msg), zmq.NOBLOCK)
                except zmq.ZMQError:
                    pass
            time.sleep(frame_interval)

        cap.release()

    def _capture_audio_loop(self):
        try:
            import pyaudio
        except ImportError:
            print(f"[Client {self.client_id}] pyaudio not available, audio disabled")
            self.audio_enabled = False
            return

        p = pyaudio.PyAudio()
        try:
            stream = p.open(format=pyaudio.paInt16, channels=AUDIO_CHANNELS,
                            rate=AUDIO_SAMPLE_RATE, input=True,
                            frames_per_buffer=AUDIO_CHUNK_SIZE)
        except Exception as e:
            print(f"[Client {self.client_id}] Audio input error: {e}")
            self.audio_enabled = False
            p.terminate()
            return

        while self.running and self.audio_enabled:
            try:
                data = stream.read(AUDIO_CHUNK_SIZE, exception_on_overflow=False)
                if self.current_group and self.audio_push and not self.reconnecting:
                    msg = Message.create_audio(
                        sender_id=self.client_id,
                        audio_data=data,
                        group=self.current_group,
                        sequence=self._audio_sequence
                    )
                    self._audio_sequence += 1
                    try:
                        self.audio_push.send(serialize_message(msg), zmq.NOBLOCK)
                    except zmq.ZMQError:
                        pass
            except Exception:
                pass

        stream.stop_stream()
        stream.close()
        p.terminate()

    def _receive_video_loop(self):
        try:
            import cv2
            import numpy as np
        except ImportError:
            return

        while self.running:
            if self.reconnecting or not self.video_sub:
                time.sleep(0.1)
                continue
            try:
                if self.video_sub.poll(timeout=50):
                    topic, data = self.video_sub.recv_multipart()
                    message = deserialize_message(data)
                    if message.sender_id != self.client_id and self.on_video_received:
                        nparr = np.frombuffer(message.payload, np.uint8)
                        frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
                        if frame is not None:
                            self.on_video_received(message.sender_id, frame)
            except zmq.ZMQError:
                pass

    def _receive_audio_loop(self):
        try:
            import pyaudio
        except ImportError:
            return

        p = pyaudio.PyAudio()
        stream = None
        try:
            stream = p.open(format=pyaudio.paInt16, channels=AUDIO_CHANNELS,
                            rate=AUDIO_SAMPLE_RATE, output=True,
                            frames_per_buffer=AUDIO_CHUNK_SIZE)
        except Exception as e:
            print(f"[Client {self.client_id}] Audio output error: {e}")
            p.terminate()
            return

        while self.running:
            if self.reconnecting or not self.audio_sub:
                time.sleep(0.1)
                continue
            try:
                if self.audio_sub.poll(timeout=50):
                    topic, data = self.audio_sub.recv_multipart()
                    message = deserialize_message(data)
                    if message.sender_id != self.client_id:
                        if self.on_audio_received:
                            self.on_audio_received(message.sender_id, message.payload)
                        else:
                            stream.write(message.payload)
            except zmq.ZMQError:
                pass
            except Exception:
                pass

        if stream:
            stream.stop_stream()
            stream.close()
        p.terminate()

    def _heartbeat_send_loop(self):
        while self.running:
            if not self.reconnecting and self.control_dealer:
                try:
                    heartbeat = Message.create_heartbeat(self.client_id)
                    self.control_dealer.send(serialize_message(heartbeat))
                except zmq.ZMQError:
                    pass
            time.sleep(HEARTBEAT_CLIENT_INTERVAL)

    def _heartbeat_monitor_loop(self):
        while self.running:
            if not self.reconnecting and self.heartbeat_sub:
                try:
                    if self.heartbeat_sub.poll(timeout=100):
                        data = self.heartbeat_sub.recv()
                        message = deserialize_message(data)
                        if message.type == MessageType.HEARTBEAT:
                            self.last_broker_heartbeat = time.time()
                except zmq.ZMQError:
                    pass

            if self.last_broker_heartbeat > 0 and not self.reconnecting:
                if time.time() - self.last_broker_heartbeat > HEARTBEAT_TIMEOUT:
                    print(f"[Client {self.client_id}] Broker heartbeat timeout detected")
                    self.connected = False
                    self.last_broker_heartbeat = 0
                    if self.enable_failover:
                        self._attempt_reconnect()

    def _attempt_reconnect(self):
        if self.reconnecting:
            return

        self.reconnecting = True
        print(f"[Client {self.client_id}] Attempting to reconnect...")

        saved_group = self.current_group

        for sock in (self.text_sub, self.audio_sub, self.video_sub,
                     self.control_dealer, self.audio_push, self.video_push,
                     self.heartbeat_sub):
            if sock:
                sock.close()

        self.text_sub = None
        self.audio_sub = None
        self.video_sub = None
        self.control_dealer = None
        self.audio_push = None
        self.video_push = None
        self.heartbeat_sub = None

        time.sleep(2)

        if self.connect():
            if saved_group:
                self.join_group(saved_group)

            print(f"[Client {self.client_id}] Successfully reconnected!")
        else:
            print(f"[Client {self.client_id}] Reconnection failed")

        self.reconnecting = False
