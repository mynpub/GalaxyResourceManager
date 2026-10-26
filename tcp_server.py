"""Bounded, line-oriented TCP sessions backed by the existing game APIs."""

import logging
import socket
import socketserver
import unicodedata
from threading import Lock

from database import init_database
from game_logic import GameLogic

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 7777
MAX_LINE_BYTES = 1024
MAX_AUTH_FAILURES = 3
MAX_CLIENTS = 64
IDLE_TIMEOUT = 120
LOGGER = logging.getLogger(__name__)


class SessionEnded(Exception):
    """The peer disconnected or sent an invalid line."""


class GameSession(socketserver.StreamRequestHandler):
    timeout = IDLE_TIMEOUT

    def send(self, message):
        self.wfile.write((message + "\r\n").encode("utf-8"))

    def read(self, prompt):
        self.send(prompt)
        raw = self.rfile.readline(MAX_LINE_BYTES + 1)
        if not raw:
            raise SessionEnded
        if len(raw) > MAX_LINE_BYTES or not raw.endswith(b"\n"):
            self.send("Input too long or incomplete; disconnecting.")
            raise SessionEnded
        try:
            line = raw[:-1].removesuffix(b"\r").decode("utf-8")
        except UnicodeDecodeError:
            self.send("Input must be UTF-8 text; disconnecting.")
            raise SessionEnded from None
        if any(unicodedata.category(char).startswith("C") for char in line):
            self.send("Control characters are not supported; disconnecting.")
            raise SessionEnded
        return line

    def handle(self):
        LOGGER.info("Client connected: %s", self.client_address)
        try:
            self.run_session()
        except SessionEnded:
            pass
        except (OSError, TimeoutError):
            LOGGER.info("Client connection ended or timed out: %s", self.client_address)
        except Exception:
            # Never log client input, credentials, or exception text.
            LOGGER.error("Session failed: %s", self.client_address)
        finally:
            LOGGER.info("Client disconnected: %s", self.client_address)

    def run_session(self):
        game = GameLogic(output=self.send)
        failures = 0
        self.send("Welcome to Galaxy Resource Manager!")
        self.send("WARNING: Plain TCP sends passwords in cleartext. Your terminal may echo them.")
        self.send("Use signup (or register), login, help, status/profile, or quit.")
        while True:
            line = self.read("Command (signup/login/help/quit):" if game.player_id is None
                             else "Command (help/status/profile/quit):").strip()
            command = line.lower()
            if command == "quit":
                self.send("Goodbye!")
                return
            if command == "help":
                self.send("signup/register: create a free account; login: sign in; "
                          "status/profile: username and balances; quit: disconnect.")
                if game.player_id is not None:
                    self.send(game.command("help"))
            elif command in ("signup", "register", "login"):
                if game.player_id is not None:
                    self.send("Already logged in. Quit to start another session.")
                    continue
                username = self.read("Username:")
                password = self.read("Password:")
                if command != "login" and password != self.read("Confirm password:"):
                    self.send("Passwords do not match.")
                    success = False
                else:
                    success = (game.login(username, password) if command == "login"
                               else game.create_player_account(username, password))
                del password
                if success:
                    self.send(f"Welcome, {game.username}! Use status or help.")
                else:
                    failures += 1
                    if failures >= MAX_AUTH_FAILURES:
                        self.send("Too many failed authentication attempts; disconnecting.")
                        return
            elif command in ("status", "profile"):
                self.send(game.command(command))
            else:
                self.send(game.command(line) if game.player_id is not None
                          else "Unknown command. Use help.")


class TCPGameServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """One isolated game per connection; closing the server joins all workers."""

    allow_reuse_address = True
    daemon_threads = False
    block_on_close = True

    def __init__(self, server_address):
        from simulation import ServerSimulation
        self._simulation = ServerSimulation()
        self._clients = set()
        self._clients_lock = Lock()
        super().__init__(server_address, GameSession)

    def service_actions(self):
        self._simulation.poll()

    def process_request(self, request, client_address):
        with self._clients_lock:
            if len(self._clients) >= MAX_CLIENTS:
                request.close()
                return
            self._clients.add(request)
        try:
            super().process_request(request, client_address)
        except BaseException:
            with self._clients_lock:
                self._clients.discard(request)
            request.close()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._clients_lock:
                self._clients.discard(request)

    def server_close(self):
        socketserver.TCPServer.server_close(self)
        with self._clients_lock:
            for client in self._clients:
                try:
                    client.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                client.close()
        super().server_close()


def serve_tcp(host=DEFAULT_HOST, port=DEFAULT_PORT):
    """Run in the foreground; Ctrl+C closes the listener and active sessions."""
    init_database()
    with TCPGameServer((host, port)) as server:
        LOGGER.info("TCP server listening on %s:%s (plaintext)", *server.server_address)
        try:
            server.serve_forever(poll_interval=0.1)
        except KeyboardInterrupt:
            LOGGER.info("Shutting down TCP server")
    LOGGER.info("TCP server stopped; all client threads joined")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    serve_tcp()
