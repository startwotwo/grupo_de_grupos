import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from client import Client
import time


def main():
    if len(sys.argv) > 1:
        client_id = sys.argv[1]
    else:
        client_id = None

    client = Client(
        client_id=client_id,
        discovery_host='localhost',
        enable_failover=True
    )

    try:
        client.start()

        print(f"\nClient {client.client_id} connected with failover enabled!")
        print("\nCommands:")
        print("  /join <group>     - Join a group (A-K)")
        print("  /leave            - Leave current group")
        print("  /users            - List online users")
        print("  /groups           - List active groups")
        print("  /dm <user> <msg>  - Send direct message")
        print("  /quit             - Exit")
        print("  <message>         - Send message to current group")
        print()

        while client.running:
            try:
                user_input = input("> ")

                if not user_input:
                    continue

                if user_input.startswith("/join "):
                    group = user_input.split()[1].upper()
                    client.join_group(group)

                elif user_input == "/leave":
                    client.leave_group()

                elif user_input == "/users":
                    client.list_users()

                elif user_input == "/groups":
                    client.list_groups()

                elif user_input.startswith("/dm "):
                    parts = user_input.split(maxsplit=2)
                    if len(parts) >= 3:
                        recipient = parts[1]
                        message = parts[2]
                        client.send_text(message, recipient=recipient)
                    else:
                        print("Usage: /dm <user> <message>")

                elif user_input == "/quit":
                    break

                else:
                    if client.current_group:
                        client.send_text(user_input)
                    else:
                        print("You must join a group first. Use /join <group>")

            except EOFError:
                break

    except KeyboardInterrupt:
        print("\nStopping client...")

    finally:
        client.stop()


if __name__ == "__main__":
    main()
