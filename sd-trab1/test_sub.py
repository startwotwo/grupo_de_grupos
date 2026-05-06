# test_sub_sdtrab1.py
import zmq, time

ctx = zmq.Context()
sub = ctx.socket(zmq.SUB)
sub.connect("tcp://localhost:7424")
sub.setsockopt(zmq.SUBSCRIBE, b"")
print("assinando porta 7424...")

while True:
    try:
        frames = sub.recv_multipart(flags=zmq.NOBLOCK)
        print(f"recebeu: {[f[:80] for f in frames]}")
    except zmq.Again:
        time.sleep(0.1)
