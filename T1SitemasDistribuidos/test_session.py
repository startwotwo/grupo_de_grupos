# test_session.py
import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

from identity.session import Session

s1 = Session("Alice", sala="A")
s2 = Session("Bob",   sala="B")

print("\n--- Login ---")
print(s1.login())
print(s2.login())

print("\n--- Ping ---")
print("broker vivo?", s1.ping())

print("\n--- Clientes online ---")
print(s1.listar_clientes())

print("\n--- Logout ---")
s1.logout()
print("Clientes após logout de Alice:", s2.listar_clientes())
s2.logout()