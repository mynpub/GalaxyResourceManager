"""Fast loopback integration tests with isolated account storage."""

import contextlib
import io
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import database
import main
import tcp_server

PASSWORD = "a long test passphrase"


class TCPServerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        data_patch = patch.object(database, "DATA_DIR", Path(temporary.name))
        data_patch.start()
        self.addCleanup(data_patch.stop)
        database.init_database()
        self.server = tcp_server.TCPGameServer(("127.0.0.1", 0))
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01})
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.assertFalse(self.thread.is_alive())
        self.assertFalse(self.server._clients)

    def connect(self):
        client = socket.create_connection(self.server.server_address, timeout=3)
        self.addCleanup(client.close)
        stream = client.makefile("rb")
        self.addCleanup(stream.close)
        welcome = self.until_prompt(stream)
        self.assertIn("Welcome to Galaxy Resource Manager!", welcome)
        self.assertIn("signup/login", welcome)
        return client, stream

    def until_prompt(self, stream):
        lines = []
        while True:
            line = stream.readline()
            if not line:
                self.fail("Connection closed before prompt")
            lines.append(line.decode("utf-8"))
            if line.startswith(b"Command ("):
                return "".join(lines)

    def send(self, client, text):
        client.sendall((text + "\n").encode("utf-8"))

    def authenticate(self, client, stream, action, username, *credentials):
        password = credentials[0] if credentials else PASSWORD
        self.send(client, action)
        self.assertEqual(stream.readline(), b"Username:\r\n")
        self.send(client, username)
        self.assertEqual(stream.readline(), b"Password:\r\n")
        self.send(client, password)
        if action != "login":
            self.assertEqual(stream.readline(), b"Confirm password:\r\n")
            self.send(client, password)
        output = self.until_prompt(stream)
        self.assertNotIn(password, output)
        return output

    def test_welcome_help_quit_and_crlf(self):
        client, stream = self.connect()
        client.sendall(b"help\r\n")
        self.assertIn("status/profile", self.until_prompt(stream))
        self.send(client, "quit")
        self.assertEqual(stream.readline(), b"Goodbye!\r\n")
        self.assertEqual(stream.readline(), b"")

    def test_signup_login_and_two_client_isolation(self):
        first, first_stream = self.connect()
        second, second_stream = self.connect()
        self.assertIn("Account created successfully",
                      self.authenticate(first, first_stream, "signup", "alice"))
        self.send(second, "profile")
        self.assertIn("Please signup or login first", self.until_prompt(second_stream))
        self.authenticate(second, second_stream, "register", "bobby")
        for client, stream, username, other in (
                (first, first_stream, "alice", "bobby"),
                (second, second_stream, "bobby", "alice")):
            self.send(client, "status")
            output = self.until_prompt(stream)
            self.assertIn(f"Username: {username}", output)
            self.assertIn("Balance: 0.0 fiat, 0.0 bitcoin", output)
            self.assertNotIn(other, output)
        returning, returning_stream = self.connect()
        self.assertIn("Login successful",
                      self.authenticate(returning, returning_stream, "login", "ALICE"))

    def test_invalid_and_oversized_input_disconnect_only_offending_client(self):
        for raw, expected in (
                (b"\xff\n", b"UTF-8"),
                (b"bad\x00command\n", b"Control characters"),
                (b"x" * (tcp_server.MAX_LINE_BYTES + 1), b"Input too long")):
            with self.subTest(raw=raw[:20]):
                client, stream = self.connect()
                client.sendall(raw)
                self.assertIn(expected, stream.readline())
                self.assertEqual(stream.readline(), b"")
        healthy, stream = self.connect()
        self.send(healthy, "help")
        self.assertIn("create a free account", self.until_prompt(stream))

    def test_password_whitespace_is_preserved(self):
        client, stream = self.connect()
        password = "  long passphrase  "
        self.authenticate(client, stream, "signup", "spaces", password)
        returning, returning_stream = self.connect()
        self.assertIn("Login successful",
                      self.authenticate(returning, returning_stream, "login", "spaces", password))

    def test_gameplay_preserves_element_case_and_session_identity(self):
        from economy import World
        client, stream = self.connect()
        self.authenticate(client, stream, "signup", "miner")
        pid = database.authenticate_player("miner", PASSWORD)
        World().seed(pid, resources={"Fe": 100})
        self.send(client, "auction list resource Fe 10 fiat 1")
        self.assertIn("Auction listed:", self.until_prompt(stream))
        self.send(client, "auctions")
        self.assertIn('"asset": "Fe"', self.until_prompt(stream))
        self.send(client, "inventory 999")
        self.assertIn("Unexpected arguments", self.until_prompt(stream))
        self.send(client, "world")
        self.assertIn("Earth", self.until_prompt(stream))

    def test_failed_authentication_attempt_limit(self):
        client, stream = self.connect()
        for attempt in range(tcp_server.MAX_AUTH_FAILURES):
            self.send(client, "login")
            self.assertEqual(stream.readline(), b"Username:\r\n")
            self.send(client, "missing")
            self.assertEqual(stream.readline(), b"Password:\r\n")
            self.send(client, "short")
            self.assertIn(b"Unable to log in", stream.readline())
            if attempt + 1 < tcp_server.MAX_AUTH_FAILURES:
                self.assertTrue(stream.readline().startswith(b"Command ("))
        self.assertIn(b"Too many failed", stream.readline())
        self.assertEqual(stream.readline(), b"")

    def test_unexpected_session_failure_does_not_stop_server(self):
        with patch.object(tcp_server.GameSession, "run_session",
                          side_effect=RuntimeError("private details")), \
                self.assertLogs(tcp_server.LOGGER, level="ERROR") as logs:
            with socket.create_connection(self.server.server_address, timeout=3) as client:
                self.assertEqual(client.recv(1024), b"")
        self.assertNotIn("private details", "".join(logs.output))
        self.connect()

    def test_shutdown_closes_idle_and_partial_line_clients_and_joins_workers(self):
        idle, idle_stream = self.connect()
        partial, partial_stream = self.connect()
        partial.sendall(b"unfinished")
        self.server.shutdown()
        self.server.server_close()
        self.assertEqual(idle_stream.readline(), b"")
        self.assertEqual(partial_stream.readline(), b"")
        self.assertFalse(self.server._clients)
        with self.assertRaises(OSError):
            socket.create_connection(self.server.server_address, timeout=1)


class TCPCLITests(unittest.TestCase):
    def test_server_defaults_and_custom_bind(self):
        for arguments, expected in (
                (["--serve-tcp"], ("0.0.0.0", 7777)),
                (["--serve-tcp", "--host", "127.0.0.1", "--port", "0"],
                 ("127.0.0.1", 0))):
            with patch.object(tcp_server, "serve_tcp") as serve, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main.main(arguments), 0)
                serve.assert_called_once_with(*expected)

    def test_server_cannot_mix_with_local_auth_or_init(self):
        for arguments in (["--serve-tcp", "--login"], ["--serve-tcp", "--register"],
                          ["--serve-tcp", "--init"], ["--serve-tcp", "--username", "alice"],
                          ["--serve-tcp", "--port", "-1"],
                          ["--serve-tcp", "--port", "65536"]):
            with self.subTest(arguments=arguments), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                main.main(arguments)
            self.assertEqual(error.exception.code, 2)

    def test_foreground_keyboard_interrupt_closes_server(self):
        with patch.object(tcp_server, "init_database") as initialize, \
                patch.object(tcp_server, "TCPGameServer") as server_class:
            server = server_class.return_value.__enter__.return_value
            server.server_address = ("127.0.0.1", 7777)
            server.serve_forever.side_effect = KeyboardInterrupt
            tcp_server.serve_tcp("127.0.0.1")
            initialize.assert_called_once_with()
            server_class.return_value.__exit__.assert_called_once()


if __name__ == "__main__":
    unittest.main()
