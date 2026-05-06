"""Testes dos RFs 01–03 (login, presença, salas).

Rodar:
    python -m unittest test_presenca.py -v
"""
import socket
import threading
import time
import unittest

import zmq

import broker
import presenca
from presenca import EstadoPresenca, handle_cmd, parse_list, parse_list_sala, ClientePresenca


def _porta_livre() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# =============================================================================
# Unit: EstadoPresenca (lógica pura, sem ZMQ)
# =============================================================================
class TestEstadoPresenca(unittest.TestCase):
    def test_login_id_unico(self):
        e = EstadoPresenca()
        ok, _ = e.login("alice")
        self.assertTrue(ok)
        ok, msg = e.login("alice")
        self.assertFalse(ok, "segundo LOGIN com mesmo id deve falhar")
        self.assertIn("ja em uso", msg)

    def test_login_id_vazio(self):
        e = EstadoPresenca()
        ok, _ = e.login("")
        self.assertFalse(ok)

    def test_logout_remove_e_retorna_salas(self):
        e = EstadoPresenca()
        e.login("bob")
        e.join("bob", "A")
        e.join("bob", "B")
        ok, _, salas = e.logout("bob")
        self.assertTrue(ok)
        self.assertEqual(sorted(salas), ["A", "B"])
        self.assertEqual(e.list_all(), {})

    def test_logout_inexistente(self):
        ok, *_ = EstadoPresenca().logout("fantasma")
        self.assertFalse(ok)

    def test_join_requer_login(self):
        ok, _ = EstadoPresenca().join("ghost", "A")
        self.assertFalse(ok)

    def test_join_duplicado(self):
        e = EstadoPresenca()
        e.login("a")
        e.join("a", "A")
        ok, _ = e.join("a", "A")
        self.assertFalse(ok, "join duplicado na mesma sala deve falhar")

    def test_leave_nao_esta_na_sala(self):
        e = EstadoPresenca()
        e.login("a")
        ok, _ = e.leave("a", "Z")
        self.assertFalse(ok)

    def test_list_all_e_list_sala(self):
        e = EstadoPresenca()
        for uid in ("alice", "bob", "carol"):
            e.login(uid)
        e.join("alice", "A"); e.join("alice", "B")
        e.join("bob", "A")
        self.assertEqual(e.list_sala("A"), ["alice", "bob"])
        self.assertEqual(e.list_sala("B"), ["alice"])
        self.assertEqual(e.list_sala("C"), [])
        todos = e.list_all()
        self.assertEqual(set(todos.keys()), {"alice", "bob", "carol"})
        self.assertEqual(todos["carol"], [])


# =============================================================================
# Unit: handle_cmd (parser + eventos)
# =============================================================================
class TestHandleCmd(unittest.TestCase):
    def test_login_gera_evento_online(self):
        e = EstadoPresenca()
        resp, ev = handle_cmd(e, "LOGIN alice")
        self.assertTrue(resp.startswith("OK"))
        self.assertEqual(ev, ["PRESENCE ONLINE alice"])

    def test_login_falhou_nao_gera_evento(self):
        e = EstadoPresenca(); handle_cmd(e, "LOGIN alice")
        resp, ev = handle_cmd(e, "LOGIN alice")
        self.assertTrue(resp.startswith("ERR"))
        self.assertEqual(ev, [])

    def test_logout_gera_leave_de_cada_sala_e_offline(self):
        e = EstadoPresenca()
        handle_cmd(e, "LOGIN alice")
        handle_cmd(e, "JOIN alice A")
        handle_cmd(e, "JOIN alice B")
        _, ev = handle_cmd(e, "LOGOUT alice")
        self.assertIn("SALA A LEAVE alice", ev)
        self.assertIn("SALA B LEAVE alice", ev)
        self.assertEqual(ev[-1], "PRESENCE OFFLINE alice")

    def test_join_leave_eventos(self):
        e = EstadoPresenca(); handle_cmd(e, "LOGIN a")
        _, ev = handle_cmd(e, "JOIN a A")
        self.assertEqual(ev, ["SALA A JOIN a"])
        _, ev = handle_cmd(e, "LEAVE a A")
        self.assertEqual(ev, ["SALA A LEAVE a"])

    def test_list_formato_parseavel(self):
        e = EstadoPresenca()
        handle_cmd(e, "LOGIN alice"); handle_cmd(e, "JOIN alice A")
        handle_cmd(e, "LOGIN bob")
        resp, _ = handle_cmd(e, "LIST")
        parsed = parse_list(resp)
        self.assertEqual(parsed, {"alice": ["A"], "bob": []})

    def test_list_sala_formato_parseavel(self):
        e = EstadoPresenca()
        handle_cmd(e, "LOGIN a"); handle_cmd(e, "JOIN a A")
        handle_cmd(e, "LOGIN b"); handle_cmd(e, "JOIN b A")
        resp, _ = handle_cmd(e, "LIST_SALA A")
        self.assertEqual(sorted(parse_list_sala(resp)), ["a", "b"])

    def test_comando_invalido(self):
        resp, ev = handle_cmd(EstadoPresenca(), "FOO bar")
        self.assertTrue(resp.startswith("ERR"))
        self.assertEqual(ev, [])

    def test_comando_vazio(self):
        resp, _ = handle_cmd(EstadoPresenca(), "   ")
        self.assertTrue(resp.startswith("ERR"))


# =============================================================================
# Integração: broker.controle_presenca ↔ ClientePresenca sobre TCP
# =============================================================================
class TestIntegracaoPresenca(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = zmq.Context()
        cls.ctrl_port = _porta_livre()
        cls.pres_port = _porta_livre()
        cls.parar = threading.Event()
        cls.estado = EstadoPresenca()
        cls.thread = threading.Thread(
            target=broker.controle_presenca,
            args=(cls.ctx, cls.parar, cls.ctrl_port, cls.pres_port, cls.estado),
            daemon=True,
        )
        cls.thread.start()
        time.sleep(0.2)  # dá tempo do bind

    @classmethod
    def tearDownClass(cls):
        cls.parar.set()
        cls.thread.join(timeout=2)
        cls.ctx.term()

    def _cliente(self) -> ClientePresenca:
        return ClientePresenca(
            self.ctx,
            f"tcp://127.0.0.1:{self.ctrl_port}",
            f"tcp://127.0.0.1:{self.pres_port}",
            timeout_ms=1500,
        )

    def test_login_unico_dois_clientes(self):
        c1 = self._cliente(); c2 = self._cliente()
        try:
            self.assertTrue(c1.login("dup").startswith("OK"))
            self.assertTrue(c2.login("dup").startswith("ERR"))
        finally:
            c1.logout(); c1.close(); c2.close()

    def test_join_leave_e_list_sala(self):
        c1 = self._cliente(); c2 = self._cliente()
        try:
            c1.login("u1"); c2.login("u2")
            self.assertTrue(c1.join("A").startswith("OK"))
            self.assertTrue(c2.join("A").startswith("OK"))
            self.assertEqual(sorted(c1.list_sala("A")), ["u1", "u2"])
            self.assertTrue(c2.leave("A").startswith("OK"))
            self.assertEqual(c1.list_sala("A"), ["u1"])
        finally:
            c1.logout(); c2.logout(); c1.close(); c2.close()

    def test_eventos_presence_chegam_ao_sub(self):
        observador = self._cliente()
        ator = self._cliente()
        try:
            observador.login("obs")
            time.sleep(0.2)  # SUB precisa conectar antes do evento
            ator.login("novo")

            deadline = time.time() + 2
            while time.time() < deadline:
                if "novo" in observador.online:
                    break
                time.sleep(0.05)
            self.assertIn("novo", observador.online, "evento ONLINE não propagou")

            ator.logout()
            deadline = time.time() + 2
            while time.time() < deadline:
                if "novo" not in observador.online:
                    break
                time.sleep(0.05)
            self.assertNotIn("novo", observador.online, "evento OFFLINE não propagou")
        finally:
            observador.logout(); observador.close(); ator.close()

    def test_eventos_sala_join_leave(self):
        observador = self._cliente(); ator = self._cliente()
        try:
            observador.login("obs2")
            time.sleep(0.2)
            ator.login("ator"); ator.join("B")
            deadline = time.time() + 2
            while time.time() < deadline:
                if "ator" in observador.salas_membros.get("B", set()):
                    break
                time.sleep(0.05)
            self.assertIn("ator", observador.salas_membros.get("B", set()))

            ator.leave("B")
            deadline = time.time() + 2
            while time.time() < deadline:
                if "ator" not in observador.salas_membros.get("B", set()):
                    break
                time.sleep(0.05)
            self.assertNotIn("ator", observador.salas_membros.get("B", set()))
        finally:
            observador.logout(); observador.close()
            ator.logout(); ator.close()

    def test_semente_inicial_via_list(self):
        # Garante que um cliente que chega tarde enxerga quem já estava online.
        pioneiro = self._cliente(); recem = self._cliente()
        try:
            pioneiro.login("pioneiro"); pioneiro.join("C")
            time.sleep(0.1)
            recem.login("recem")
            deadline = time.time() + 2
            while time.time() < deadline:
                if "pioneiro" in recem.online:
                    break
                time.sleep(0.05)
            self.assertIn("pioneiro", recem.online)
            self.assertIn("pioneiro", recem.salas_membros.get("C", set()))
        finally:
            pioneiro.logout(); recem.logout()
            pioneiro.close(); recem.close()


if __name__ == "__main__":
    unittest.main()
