"""MCP server for ContextVault.

Exposes three read-only tools -- ``search_memory``, ``get_conversation`` and
``get_conversation_chain`` -- over the Model Context Protocol, so an assistant
can search the user's archived conversations.  Tool implementations live in
tools.py; this module is only transports.

Two of them:

* **stdio** (``python mcp_server.py``) -- what Claude Desktop and Cursor use.
  The client spawns this as a subprocess and talks newline-delimited JSON-RPC
  over the pipe.
* **TCP** on 127.0.0.1 -- what the "Start MCP Server" toggle in Settings
  starts, for clients that connect to a socket and for debugging by hand.

If the official ``mcp`` SDK is installed it is used for the stdio transport.
If it is not, the built-in JSON-RPC loop below speaks the same protocol, so a
fresh clone works without installing anything extra.
"""

import json
import os
import socket
import socketserver
import sys
import threading

PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.core import database as db          # noqa: E402
from backend.mcp import tools                    # noqa: E402
from backend.mcp.tools import TOOL_SCHEMAS, ToolError, call_tool  # noqa: E402

SERVER_NAME = "contextvault"
SERVER_VERSION = "0.3.0"

# Echoed back to a client that does not state its own.
DEFAULT_PROTOCOL_VERSION = "2024-11-05"

DEFAULT_TCP_PORT = 8765


# --------------------------------------------------------------------------
# Protocol handling, transport-independent
# --------------------------------------------------------------------------

class Session:
    """Handles JSON-RPC requests for one connected client.

    Each session owns a database connection.  SQLite connections are not safe
    to share between threads, and the TCP transport serves each client on its
    own thread, so they are never shared.
    """

    def __init__(self, db_path):
        self.db_path = db_path
        self._conn = None

    @property
    def conn(self):
        if self._conn is None:
            self._conn = db.get_connection(self.db_path)
        return self._conn

    def close(self):
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    # -- JSON-RPC ---------------------------------------------------------

    def handle(self, message):
        """Return a response dict, or None for notifications."""
        if not isinstance(message, dict):
            return _error(None, -32600, "Invalid Request: expected an object")

        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params") or {}
        is_notification = "id" not in message

        try:
            if method == "initialize":
                result = self._initialize(params)
            elif method in ("notifications/initialized", "initialized"):
                return None
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOL_SCHEMAS}
            elif method == "tools/call":
                result = self._call_tool(params)
            elif method in ("resources/list", "prompts/list"):
                # Declared unsupported, but answer politely: some clients probe
                # for these regardless of the advertised capabilities.
                result = {"resources": []} if method.startswith("resources") \
                    else {"prompts": []}
            elif method in ("shutdown", "exit"):
                result = {}
            else:
                if is_notification:
                    return None
                return _error(request_id, -32601, "Method not found: %s" % method)
        except ToolError as exc:
            # A tool the caller used wrongly is not a protocol error: report it
            # as tool output so the model can read it and correct itself.
            return _ok(request_id, _tool_result(str(exc), is_error=True))
        except Exception as exc:  # noqa: BLE001 - never kill the server
            return _error(request_id, -32603, "Internal error: %s" % exc)

        if is_notification:
            return None
        return _ok(request_id, result)

    def _initialize(self, params):
        requested = params.get("protocolVersion")
        return {
            "protocolVersion": (requested if isinstance(requested, str)
                                else DEFAULT_PROTOCOL_VERSION),
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": (
                "Searches the user's own archive of past AI conversations. "
                "Start with search_memory, then read a result in full with "
                "get_conversation."),
        }

    def _call_tool(self, params):
        name = params.get("name")
        arguments = params.get("arguments") or {}
        payload = call_tool(self.conn, name, arguments)
        return _tool_result(json.dumps(payload, indent=2, ensure_ascii=False))


def _tool_result(text, is_error=False):
    result = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["isError"] = True
    return result


def _ok(request_id, result):
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id,
            "error": {"code": code, "message": message}}


# --------------------------------------------------------------------------
# stdio transport
# --------------------------------------------------------------------------

def serve_stdio(db_path=None):
    """Serve MCP over stdin/stdout until the client closes the pipe."""
    db_path = db_path or db.DB_PATH
    session = Session(db_path)

    # stdout IS the protocol channel here.  Anything else that prints -- a
    # library warning, a stray debug line -- would corrupt the JSON stream and
    # the client would drop the connection.  Keep a private handle to the real
    # stdout and point sys.stdout at stderr so stray output is merely logged.
    out = sys.stdout
    sys.stdout = sys.stderr

    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                _write(out, _error(None, -32700, "Parse error"))
                continue

            # A client may batch requests into a JSON array.
            if isinstance(message, list):
                responses = [r for r in (session.handle(m) for m in message)
                             if r is not None]
                for response in responses:
                    _write(out, response)
                continue

            response = session.handle(message)
            if response is not None:
                _write(out, response)
    except (KeyboardInterrupt, BrokenPipeError):
        pass
    finally:
        session.close()
        sys.stdout = out


def _write(stream, payload):
    stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
    stream.flush()


def serve_stdio_via_sdk(db_path=None):
    """Serve stdio using the official mcp SDK.  Returns False if unavailable.

    The SDK is optional.  The built-in loop speaks the same protocol, so this
    is about following the reference implementation when the user has it, not
    about capability.
    """
    try:
        from mcp.server.mcpserver import MCPServer
    except Exception:
        return False

    db_path = db_path or db.DB_PATH
    session = Session(db_path)
    server = MCPServer(name=SERVER_NAME, version=SERVER_VERSION)

    def _run(name, arguments):
        try:
            return json.dumps(call_tool(session.conn, name, arguments),
                              indent=2, ensure_ascii=False)
        except ToolError as exc:
            return "Error: %s" % exc

    @server.tool(name="search_memory",
                 description=tools.TOOL_SCHEMAS_BY_NAME["search_memory"]["description"])
    def search_memory(query: str, limit: int = tools.DEFAULT_LIMIT) -> str:
        return _run("search_memory", {"query": query, "limit": limit})

    @server.tool(name="get_conversation",
                 description=tools.TOOL_SCHEMAS_BY_NAME["get_conversation"]["description"])
    def get_conversation(conversation_id: str) -> str:
        return _run("get_conversation", {"conversation_id": conversation_id})

    @server.tool(name="get_conversation_chain",
                 description=tools.TOOL_SCHEMAS_BY_NAME["get_conversation_chain"]["description"])
    def get_conversation_chain(chain_id: int) -> str:
        return _run("get_conversation_chain", {"chain_id": chain_id})

    try:
        server.run("stdio")
    finally:
        session.close()
    return True


# --------------------------------------------------------------------------
# TCP transport (the Settings toggle)
# --------------------------------------------------------------------------

class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        session = Session(self.server.db_path)
        try:
            for raw in self.rfile:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    message = json.loads(raw.decode("utf-8"))
                except ValueError:
                    self._send(_error(None, -32700, "Parse error"))
                    continue
                response = session.handle(message)
                if response is not None:
                    self._send(response)
        except (ConnectionError, OSError):
            pass
        finally:
            session.close()

    def _send(self, payload):
        self.wfile.write(
            (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        self.wfile.flush()


class _ThreadedTCPServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


class BackgroundServer:
    """Runs the TCP transport on a daemon thread, for the Settings toggle."""

    def __init__(self):
        self._server = None
        self._thread = None
        self._lock = threading.Lock()

    def start(self, db_path, host="127.0.0.1", port=DEFAULT_TCP_PORT):
        """Start listening.  Returns the status dict; safe to call twice."""
        with self._lock:
            if self._server is not None:
                return self._status()
            # Bound to the loopback interface only, never 0.0.0.0: this
            # exposes the user's entire conversation archive.
            server = _ThreadedTCPServer((host, port), _Handler)
            server.db_path = db_path
            thread = threading.Thread(target=server.serve_forever,
                                      name="mcp-tcp", daemon=True)
            thread.start()
            self._server, self._thread = server, thread
            return self._status()

    def stop(self):
        with self._lock:
            if self._server is None:
                return self._status()
            self._server.shutdown()
            self._server.server_close()
            self._server = None
            self._thread = None
            return self._status()

    def status(self):
        with self._lock:
            return self._status()

    def _status(self):
        if self._server is None:
            return {"running": False, "host": None, "port": None}
        host, port = self._server.server_address[:2]
        return {"running": True, "host": host, "port": port}


BACKGROUND = BackgroundServer()


def find_free_port(preferred=DEFAULT_TCP_PORT, host="127.0.0.1"):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, preferred))
            return preferred
        except OSError:
            pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((host, 0))
        return probe.getsockname()[1]


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(
        description="MCP server for ContextVault (stdio by default).")
    parser.add_argument("--db", default=db.DB_PATH,
                        help="path to the ContextVault SQLite database")
    parser.add_argument("--transport", choices=("stdio", "tcp"), default="stdio")
    parser.add_argument("--port", type=int, default=DEFAULT_TCP_PORT,
                        help="port for --transport tcp")
    parser.add_argument("--no-sdk", action="store_true",
                        help="use the built-in JSON-RPC loop even if the mcp "
                             "SDK is installed")
    args = parser.parse_args(argv)

    # Make sure the schema exists: a client may launch this before the desktop
    # app has ever run.
    db.init_db(args.db).close()

    if args.transport == "tcp":
        status = BACKGROUND.start(args.db, port=args.port)
        print("ContextVault MCP server on tcp://%s:%d (Ctrl+C to stop)"
              % (status["host"], status["port"]), file=sys.stderr)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            BACKGROUND.stop()
        return

    if not args.no_sdk and serve_stdio_via_sdk(args.db):
        return
    serve_stdio(args.db)


if __name__ == "__main__":
    main()
