"""Wrapper para rodar ufscar-sd client conectado ao broker federado.

ufscar-sd/src/common/constants.py tem portas hardcoded (5555+). O launcher
da federação inicia o broker em 7900-7907, mas o examples/start_client.py
cria Client() com defaults — não funciona contra a federação. Este wrapper
instancia Client com as portas corretas, sem modificar o código do grupo.

Uso:
    python federation/ufscar_client.py Ivan
"""
import os
import sys

UFSCAR_SRC = os.path.join(os.path.dirname(__file__), "..", "ufscar-sd", "src")
sys.path.insert(0, os.path.abspath(UFSCAR_SRC))

from client import Client


def main():
    client_id = sys.argv[1] if len(sys.argv) > 1 else None
    client = Client(
        client_id=client_id,
        broker_host="localhost",
        text_port=7901,
        audio_port=7902,
        video_port=7903,
        control_port=7904,
        heartbeat_port=7905,
        audio_input_port=7906,
        video_input_port=7907,
        discovery_host="localhost",
        discovery_port=7900,
    )

    try:
        client.start()
        print(f"\nClient {client.client_id} connected!")
        print("Commands: /join <A-K> | /leave | /users | /groups | /quit | <msg>")

        while client.running:
            try:
                s = input("> ")
            except EOFError:
                break
            if not s:
                continue
            if s.startswith("/join "):
                client.join_group(s.split()[1].upper())
            elif s == "/leave":
                client.leave_group()
            elif s == "/users":
                client.list_users()
            elif s == "/groups":
                client.list_groups()
            elif s == "/quit":
                break
            else:
                if client.current_group:
                    client.send_text(s)
                else:
                    print("join a group first: /join <A-K>")
    except KeyboardInterrupt:
        pass
    finally:
        client.stop()


if __name__ == "__main__":
    main()
