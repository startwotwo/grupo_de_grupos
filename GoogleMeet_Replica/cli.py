import threading
import time
from client import Cliente

username = input("Username: ").strip()
room = input("Sala (ex: ROOM_A): ").strip() or "ROOM_A"

def on_msg(user, msg):
    if msg != "__PRESENCE__":
        print(f"\n[{user}]: {msg}\n> ", end="", flush=True)

client = Cliente(
    username, room,
    msgCallBack=on_msg,
    registry_port=7200,
)

# aguarda conexão
for _ in range(20):
    if client.brokerVivo:
        break
    time.sleep(0.5)

client.threadEscuta()
print(f"Conectado como {username} na sala {room}. Digite mensagens:")

try:
    while True:
        msg = input("> ")
        if msg.strip():
            client.enviarMsg(msg)
except KeyboardInterrupt:
    pass

client.desconectar()
