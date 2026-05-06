import sys
import os
import threading
import argparse

sys.path.append(os.path.join(os.path.dirname(__file__), "client"))
sys.path.append(os.path.dirname(__file__))

from identity.session import Session
from client.sender import Sender
from client.receiver import Receiver

parser = argparse.ArgumentParser()
parser.add_argument("--nome", required=True)
parser.add_argument("--sala", default="A")
parser.add_argument("--registry", default="localhost:7650")
args = parser.parse_args()

session = Session(args.nome, args.sala.upper(), registry_host=args.registry)

if not session.login():
    print("Não foi possível conectar ao broker.")
    sys.exit(1)

print(f"Conectado como {session.nome} na sala {session.sala}")

def on_text(text):
    print(f"\n[CHAT] {text}")
    print("> ", end="", flush=True)

receiver = Receiver(
    session.context, session.broker_info,
    on_video=lambda u, d: None,
    on_audio=lambda d: None,
    on_text=on_text,
)
receiver.start()

sender = Sender(session.context, session.broker_info)

try:
    while True:
        text = input("> ")
        if text.strip():
            sender.send_text(text.strip(), session.nome)
except KeyboardInterrupt:
    pass

session.logout()
