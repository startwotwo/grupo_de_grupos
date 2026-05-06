import zmq
import threading
import time
import json
import uuid
import argparse

class Broker:
    def __init__(self, registry_endpoint, host="127.0.0.1", port_base=6000):
        self.registry_endpoint = registry_endpoint
        self.broker_id = str(uuid.uuid4())[:8]
        self.host = host
        self.port_base = port_base
        
        self.cmd_port = port_base
        self.sub_port = port_base + 1
        self.pub_port = port_base + 2
        self.cluster_pub_port = port_base + 3
        
        self.context = zmq.Context()
        self.cluster_peers = set()
        
        self.users = {} # user_id -> room
        self.lock = threading.Lock()

    def start(self):
        # Client sockets
        self.cmd_socket = self.context.socket(zmq.ROUTER)
        self.cmd_socket.bind(f"tcp://*:{self.cmd_port}")
        
        self.sub_socket = self.context.socket(zmq.SUB)
        self.sub_socket.bind(f"tcp://*:{self.sub_port}")
        self.sub_socket.setsockopt_string(zmq.SUBSCRIBE, "")
        
        self.pub_socket = self.context.socket(zmq.PUB)
        self.pub_socket.bind(f"tcp://*:{self.pub_port}")
        
        # Cluster sockets
        self.cluster_pub = self.context.socket(zmq.PUB)
        self.cluster_pub.bind(f"tcp://*:{self.cluster_pub_port}")
        
        self.cluster_sub = self.context.socket(zmq.SUB)
        self.cluster_sub.setsockopt_string(zmq.SUBSCRIBE, "")
        
        # Threads
        threading.Thread(target=self.registry_thread, daemon=True).start()
        threading.Thread(target=self.cmd_thread, daemon=True).start()
        threading.Thread(target=self.media_proxy_thread, daemon=True).start()
        threading.Thread(target=self.cluster_proxy_thread, daemon=True).start()

        print(f"[Broker {self.broker_id}] Started with base port {self.port_base}")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("[Broker] Shutting down...")

    def registry_thread(self):
        reg_socket = self.context.socket(zmq.REQ)
        reg_socket.connect(self.registry_endpoint)
        
        endpoints = {
            "cmd": f"tcp://{self.host}:{self.cmd_port}",
            "pub": f"tcp://{self.host}:{self.pub_port}",
            "sub": f"tcp://{self.host}:{self.sub_port}"
        }
        cluster_endpoints = {
            "pub": f"tcp://{self.host}:{self.cluster_pub_port}"
        }
        
        try:
            reg_socket.send_json({
                "action": "REGISTER", 
                "id": self.broker_id,
                "endpoint": endpoints,
                "cluster_endpoint": cluster_endpoints
            })
            reply = reg_socket.recv_json()
            if reply.get("status") != "ok":
                print("[Broker] Registry rejected registration.")
                return
        except zmq.ZMQError as e:
            print(f"[Broker] Initial registry connection failed: {e}")
        
        while True:
            time.sleep(3)
            with self.lock:
                load = len(self.users)
            try:
                reg_socket.send_json({
                    "action": "HEARTBEAT",
                    "id": self.broker_id,
                    "load": load
                })
                reply = reg_socket.recv_json()
                if reply.get("status") == "error" and reply.get("reason") == "not_registered":
                    print("[Broker] Registry forgot us. Re-registering...")
                    reg_socket.send_json({
                        "action": "REGISTER", 
                        "id": self.broker_id,
                        "endpoint": endpoints,
                        "cluster_endpoint": cluster_endpoints
                    })
                    reg_socket.recv_json()
                elif reply.get("status") == "ok":
                    cluster_list = reply.get("cluster", [])
                    self.update_cluster_connections(cluster_list)
                    
            except zmq.ZMQError:
                print("[Broker] Heartbeat to registry failed. Retrying later...")
                reg_socket.close()
                reg_socket = self.context.socket(zmq.REQ)
                reg_socket.connect(self.registry_endpoint)

    def update_cluster_connections(self, cluster_list):
        current_peers = {peer["id"]: peer["cluster_endpoint"]["pub"] for peer in cluster_list}
        peer_ids = set(current_peers.keys())
        
        for new_peer in peer_ids - self.cluster_peers:
            endpoint = current_peers[new_peer]
            print(f"[Broker] Connecting to cluster peer {new_peer} at {endpoint}")
            self.cluster_sub.connect(endpoint)
            self.cluster_peers.add(new_peer)

    def cmd_thread(self):
        while True:
            try:
                parts = self.cmd_socket.recv_multipart()
                if len(parts) >= 2:
                    client_id = parts[0]
                    msg_bytes = parts[-1]
                    
                    msg = json.loads(msg_bytes.decode('utf-8'))
                    action = msg.get("action")
                    
                    if action == "LOGIN":
                        user_id = msg.get("user_id")
                        room = msg.get("room")
                        with self.lock:
                            self.users[user_id] = room
                        self.cmd_socket.send_multipart([client_id, b"", json.dumps({"status": "ok"}).encode('utf-8')])
                        print(f"[Broker] User {user_id} joined room '{room}'. Load: {len(self.users)}")
                        
                    elif action == "TEXT":
                        room = msg.get("room")
                        # ACK text
                        self.cmd_socket.send_multipart([client_id, b"", json.dumps({"status": "ok", "ack": msg.get("msg_id")}).encode('utf-8')])
                        
                        # Broker publish local
                        topic = f"{room} TEXT".encode('utf-8')
                        self.pub_socket.send_multipart([topic, msg_bytes])
                        
                        # Broker publish cluster
                        msg["origin"] = self.broker_id
                        cluster_topic = f"{room} TEXT".encode('utf-8')
                        self.cluster_pub.send_multipart([cluster_topic, json.dumps(msg).encode('utf-8')])

                    elif action == "PING":
                        self.cmd_socket.send_multipart([client_id, b"", json.dumps({"status": "PONG"}).encode('utf-8')])
                        
                    elif action == "ONLINE":
                        room = msg.get("room")
                        with self.lock:
                            users_in_room = [u for u, r in self.users.items() if r == room]
                        self.cmd_socket.send_multipart([client_id, b"", json.dumps({"status": "online", "users": users_in_room}).encode('utf-8')])
            except Exception as e:
                print(f"[Broker Error] CMD Thread: {e}")

    def media_proxy_thread(self):
        while True:
            try:
                parts = self.sub_socket.recv_multipart()
                self.pub_socket.send_multipart(parts)
                self.cluster_pub.send_multipart(parts)
            except Exception as e:
                print(f"[Broker Error] Media Proxy Thread: {e}")

    def cluster_proxy_thread(self):
        while True:
            try:
                parts = self.cluster_sub.recv_multipart()
                self.pub_socket.send_multipart(parts)
            except Exception as e:
                print(f"[Broker Error] Cluster Proxy Thread: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", default="tcp://127.0.0.1:5555")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port-base", type=int, default=6000)
    args = parser.parse_args()
    
    broker = Broker(args.registry, args.host, args.port_base)
    broker.start()
