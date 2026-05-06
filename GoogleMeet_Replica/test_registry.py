"""
Rode este script com o registry.py já ativo para verificar a descoberta de brokers.
python test_registry.py
"""
import zmq

REGISTRY_HOST = "localhost"
REGISTRY_PORT = 5550


def send_request(payload):
    context = zmq.Context()
    req = context.socket(zmq.REQ)
    req.connect(f"tcp://{REGISTRY_HOST}:{REGISTRY_PORT}")
    req.setsockopt(zmq.RCVTIMEO, 3000)

    try:
        print(f"Enviando: {payload}")
        req.send_json(payload)
        resp = req.recv_json()
        print(f"Resposta: {resp}\n")
    except zmq.Again:
        print("TIMEOUT — registry não respondeu. Verifique se registry.py está rodando.\n")
    except Exception as e:
        print(f"Erro: {e}\n")
    finally:
        req.close()
        context.term()


if __name__ == "__main__":
    send_request({"type": "Get_broker"})
    send_request({"type": "Get_all_brokers"})
