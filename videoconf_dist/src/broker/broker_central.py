import argparse
import zmq
import time


SESSION_TIMEOUT = 30


def main():
    parser = argparse.ArgumentParser(description="Broker central videoconf")
    parser.add_argument("--xsub-port", type=int, default=5555)
    parser.add_argument("--xpub-port", type=int, default=5556)
    parser.add_argument("--auth-port", type=int, default=5557)
    args = parser.parse_args()

    context = zmq.Context()

    print("[BROKER] Iniciando broker ativo...")

    frontend = context.socket(zmq.XSUB)
    frontend.setsockopt(zmq.RCVHWM, 100)
    frontend.bind(f"tcp://*:{args.xsub_port}")

    backend = context.socket(zmq.XPUB)
    backend.setsockopt(zmq.SNDHWM, 100)
    backend.bind(f"tcp://*:{args.xpub_port}")

    auth_socket = context.socket(zmq.REP)
    auth_socket.bind(f"tcp://*:{args.auth_port}")

    print(f"[BROKER] XSUB={args.xsub_port} XPUB={args.xpub_port} AUTH={args.auth_port}")

    poller = zmq.Poller()
    poller.register(frontend, zmq.POLLIN)
    poller.register(backend, zmq.POLLIN)
    poller.register(auth_socket, zmq.POLLIN)

    active_users = {}

    try:
        while True:
            socks = dict(poller.poll(1000))
            current_time = time.time()

            if auth_socket in socks:
                request = auth_socket.recv_string()
                print(request)
                try:
                    acao, sala, usuario = request.split("|")
                    if acao == "LOGIN":
                        if usuario in active_users:
                            auth_socket.send_string("ERRO: Nome já está em uso.")
                            print(f"[AUTH] Acesso negado para '{usuario}' (nome duplicado).")
                        else:
                            active_users[usuario] = {"room": sala, "last_seen": current_time}
                            auth_socket.send_string("OK")
                            print(f"[AUTH] '{usuario}' entrou na sala '{sala}'.")
                            aviso = f"{sala}:TEXTO:SISTEMA:0|O usuário '{usuario}' entrou na sala."
                            backend.send(aviso.encode('utf-8'))
                except Exception as e:
                    auth_socket.send_string(f"ERRO: Formato inválido ({e})")

            if frontend in socks:
                parts = frontend.recv_multipart()
                try:
                    sender_name = None
                    if len(parts) == 5:
                        sender_name = parts[1].decode('utf-8', errors='ignore')
                    elif len(parts) == 1:
                        header_parts = parts[0].split(b"|", 1)
                        if len(header_parts) >= 1:
                            header_str = header_parts[0].decode('utf-8', errors='ignore')
                            header_pieces = header_str.split(":")
                            if len(header_pieces) >= 3:
                                sender_name = header_pieces[2]
                    if sender_name and sender_name in active_users:
                        active_users[sender_name]["last_seen"] = current_time
                except Exception:
                    pass
                backend.send_multipart(parts)

            if backend in socks:
                subscription = backend.recv()
                frontend.send(subscription)

            dead_users = [u for u, info in active_users.items()
                          if current_time - info["last_seen"] > SESSION_TIMEOUT]
            for user in dead_users:
                sala = active_users[user]["room"]
                print(f"[SESSÃO] '{user}' desconectado por inatividade.")
                del active_users[user]
                aviso = f"{sala}:TEXTO:SISTEMA:0|O usuário '{user}' foi desconectado."
                backend.send(aviso.encode('utf-8'))

    except KeyboardInterrupt:
        print("\n[BROKER] Encerrando...")
    finally:
        frontend.close()
        backend.close()
        auth_socket.close()
        context.term()

if __name__ == "__main__":
    main()
