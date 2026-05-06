import zmq
import time
import threading
import json

class Registry:
    def __init__(self, port=5555):
        self.port = port
        self.brokers = {} # format: {broker_id: {"endpoint": {"cmd": str, "pub": str, "sub": str}, "cluster_endpoint": str, "last_seen": float, "load": int}}
        self.lock = threading.Lock()
        
    def start(self):
        context = zmq.Context()
        socket = context.socket(zmq.REP)
        socket.bind(f"tcp://*:{self.port}")
        
        # Start cleanup thread
        threading.Thread(target=self.cleanup_brokers, daemon=True).start()
        
        print(f"[Registry] Started on port {self.port}")
        while True:
            try:
                msg = socket.recv_json()
                action = msg.get("action")
                
                if action == "REGISTER":
                    broker_id = msg.get("id")
                    endpoint = msg.get("endpoint") # dict with cmd, pub, sub ports
                    cluster_endpoint = msg.get("cluster_endpoint") # dict with pub, sub ports
                    
                    with self.lock:
                        self.brokers[broker_id] = {
                            "endpoint": endpoint,
                            "cluster_endpoint": cluster_endpoint,
                            "last_seen": time.time(),
                            "load": 0
                        }
                    socket.send_json({"status": "ok"})
                    print(f"[Registry] Broker {broker_id} registered with endpoint {endpoint}")
                    
                elif action == "HEARTBEAT":
                    broker_id = msg.get("id")
                    load = msg.get("load", 0)
                    with self.lock:
                        if broker_id in self.brokers:
                            self.brokers[broker_id]["last_seen"] = time.time()
                            self.brokers[broker_id]["load"] = load
                            # Reply with current cluster list so brokers can connect to each other
                            cluster_list = [{"id": bid, "cluster_endpoint": b["cluster_endpoint"]} 
                                            for bid, b in self.brokers.items() if bid != broker_id]
                            socket.send_json({"status": "ok", "cluster": cluster_list})
                        else:
                            socket.send_json({"status": "error", "reason": "not_registered"})
                            print(f"[Registry] Unknown broker {broker_id} heartbeat, forcing re-registration.")
                            
                elif action == "DISCOVER":
                    with self.lock:
                        if not self.brokers:
                            socket.send_json({"status": "error", "reason": "no_brokers"})
                        else:
                            # Pick broker with lowest load
                            best_broker_item = min(self.brokers.items(), key=lambda x: x[1]['load'])
                            best_broker_id = best_broker_item[0]
                            best_broker_data = best_broker_item[1]
                            socket.send_json({
                                "status": "ok", 
                                "broker_id": best_broker_id, 
                                "endpoint": best_broker_data["endpoint"]
                            })
                            
                else:
                    socket.send_json({"status": "error", "reason": "unknown_action"})
            except zmq.ZMQError as e:
                print(f"[Registry] ZMQ Error: {e}")
            except Exception as e:
                print(f"[Registry] Error: {e}")
                
    def cleanup_brokers(self):
        while True:
            time.sleep(5)
            with self.lock:
                now = time.time()
                dead_brokers = [b for b, data in self.brokers.items() if now - data["last_seen"] > 10]
                for b in dead_brokers:
                    print(f"[Registry] Removing dead broker {b}")
                    del self.brokers[b]

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5555)
    args = parser.parse_args()
    registry = Registry(port=args.port)
    registry.start()