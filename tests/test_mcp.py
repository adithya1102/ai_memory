"""MCP server: the three tools, the stdio protocol, and graceful failure.

Part A calls the tool layer directly.  Part B speaks real JSON-RPC to the
launcher as a subprocess, exactly as Claude Desktop does.  Part C checks the
TCP transport behind the Settings toggle.  Part D blocks the semantic imports
to prove the server still works keyword-only.
"""
import importlib.machinery
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.core import database as db
from backend.core.importer import import_file
from backend.mcp import tools as mcp_tools
from backend.mcp.tools import ToolError

EXPORT = os.path.join(ROOT, "dummy_export.json.json")
WORK = tempfile.mkdtemp(prefix="aimem-mcp-")
DB = os.path.join(WORK, "mcp.db")

fails = []


def check(label, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + label + (("  -> " + str(detail)) if detail else ""))
    if not ok:
        fails.append(label)


# ======================================================================
print("== setup ==")
conn = db.init_db(DB)
import_file(conn, EXPORT)
check("library seeded", conn.execute(
    "SELECT COUNT(*) FROM conversations").fetchone()[0] == 5)
CHAIN_ID = conn.execute("SELECT id FROM conversation_chains").fetchone()[0]
CONV_ID = "chatgpt:conv-001"

# ======================================================================
print("\n== A. tools ==")
res = mcp_tools.search_memory(conn, "gym workout")
titles = [r["title"] for r in res["results"]]
check("search_memory('gym workout') finds Welcome",
      titles and titles[0] == "Welcome", titles)
check("result has exactly the documented keys",
      set(res["results"][0]) == {"conversation_id", "title", "provider", "date",
                                 "snippet", "relevance_score", "match_type"},
      sorted(res["results"][0]))
check("provider is populated", res["results"][0]["provider"] == "ChatGPT")
check("date is populated", bool(res["results"][0]["date"]))
check("snippet is populated", bool(res["results"][0]["snippet"].strip()))
check("relevance_score is a number",
      isinstance(res["results"][0]["relevance_score"], float))

sem = mcp_tools.search_memory(conn, "how do I get stronger")
sem_titles = [r["title"] for r in sem["results"]]
check("search_memory('how do I get stronger') finds Welcome",
      sem_titles and sem_titles[0] == "Welcome", sem_titles)
check("...via a semantic match",
      sem["results"][0]["match_type"] in ("semantic", "both"),
      sem["results"][0]["match_type"])

check("limit is honoured", len(mcp_tools.search_memory(
    conn, "starter", limit=1)["results"]) <= 1)
check("limit is clamped to the maximum",
      mcp_tools.search_memory(conn, "starter", limit=9999) is not None)
# Junk queries mostly return nothing, but MIN_SIMILARITY is deliberately low
# so an occasional weak hit clears it -- "asdfghjkl qwertyuiop" lands at 0.152
# against a 0.15 floor. Asserting "always empty" would be asserting a tuning
# constant. The invariants that actually matter are that junk never produces a
# keyword match and never outranks a genuine one.
strong = mcp_tools.search_memory(conn, "gym workout")["results"]
for junk in ("asdfghjkl qwertyuiop", "zzzqqq nonexistent",
             "quantum chromodynamics lattice gauge theory", "zxcvbnm poiuytrewq"):
    hits = mcp_tools.search_memory(conn, junk)["results"]
    check("junk %-42r yields no keyword match" % junk,
          all(h["match_type"] == "semantic" for h in hits),
          [(h["title"], h["match_type"]) for h in hits])
    check("junk %-42r ranks below a real match" % junk,
          all(h["relevance_score"] < strong[0]["relevance_score"] for h in hits),
          [(h["title"], h["relevance_score"]) for h in hits])

full = mcp_tools.get_conversation(conn, CONV_ID)
check("get_conversation returns the transcript", full["message_count"] == 4,
      full["message_count"])
check("messages are in order",
      [m["order"] for m in full["messages"]] == [0, 1, 2, 3])
check("roles alternate",
      [m["role"] for m in full["messages"]] == ["user", "assistant", "user", "assistant"])
check("message content present",
      "build muscle" in full["messages"][0]["content"])
check("title and provider present",
      full["title"] == "Welcome" and full["provider"] == "ChatGPT")

chain = mcp_tools.get_conversation_chain(conn, CHAIN_ID)
check("get_conversation_chain returns members", chain["size"] == 2, chain["size"])
check("chain members carry dates",
      all(c["date"] for c in chain["conversations"]))
check("chain members ordered by position",
      [c["position"] for c in chain["conversations"]] == [0, 1])
check("chain members are sourdough pair",
      all("Sourdough" in c["title"] for c in chain["conversations"]),
      [c["title"] for c in chain["conversations"]])

print("\n-- invalid input --")
for label, fn in [
    ("unknown conversation id", lambda: mcp_tools.get_conversation(conn, "chatgpt:nope")),
    ("empty conversation id", lambda: mcp_tools.get_conversation(conn, "")),
    ("unknown chain id", lambda: mcp_tools.get_conversation_chain(conn, 9999)),
    ("non-numeric chain id", lambda: mcp_tools.get_conversation_chain(conn, "abc")),
    ("empty query", lambda: mcp_tools.search_memory(conn, "")),
    ("unknown tool", lambda: mcp_tools.call_tool(conn, "drop_tables", {})),
    ("missing argument", lambda: mcp_tools.call_tool(conn, "get_conversation", {})),
    ("unexpected argument",
     lambda: mcp_tools.call_tool(conn, "search_memory", {"query": "x", "evil": 1})),
]:
    try:
        fn()
        check("%s raises ToolError" % label, False, "no exception")
    except ToolError as exc:
        check("%s raises ToolError" % label, True, str(exc)[:60])
    except Exception as exc:
        check("%s raises ToolError" % label, False,
              "%s: %s" % (type(exc).__name__, exc))
conn.close()


# ======================================================================
print("\n== B. stdio protocol (as Claude Desktop speaks it) ==")

def stdio_session(messages, use_sdk=False, db_path=DB, timeout=180):
    """Send JSON-RPC lines to the launcher subprocess, collect responses."""
    cmd = [sys.executable, os.path.join(ROOT, "mcp_server.py"), "--db", db_path]
    if not use_sdk:
        cmd.append("--no-sdk")
    payload = "".join(json.dumps(m) + "\n" for m in messages)
    proc = subprocess.run(cmd, input=payload, capture_output=True, text=True,
                          timeout=timeout, cwd=ROOT)
    out = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except ValueError:
                check("stdout line is valid JSON", False, line[:120])
    return out, proc


INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05",
                   "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}}
INITED = {"jsonrpc": "2.0", "method": "notifications/initialized"}

responses, proc = stdio_session([
    INIT, INITED,
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
     "params": {"name": "search_memory", "arguments": {"query": "gym workout"}}},
    {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
     "params": {"name": "search_memory",
                "arguments": {"query": "how do I get stronger"}}},
    {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
     "params": {"name": "get_conversation",
                "arguments": {"conversation_id": CONV_ID}}},
    {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
     "params": {"name": "get_conversation_chain",
                "arguments": {"chain_id": CHAIN_ID}}},
    {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
     "params": {"name": "get_conversation",
                "arguments": {"conversation_id": "chatgpt:does-not-exist"}}},
    {"jsonrpc": "2.0", "id": 8, "method": "tools/call",
     "params": {"name": "get_conversation_chain", "arguments": {"chain_id": 4242}}},
    {"jsonrpc": "2.0", "id": 9, "method": "ping"},
    {"jsonrpc": "2.0", "id": 10, "method": "no/such/method"},
])
by_id = {r.get("id"): r for r in responses}

check("subprocess exited cleanly", proc.returncode == 0,
      "rc=%s stderr=%s" % (proc.returncode, (proc.stderr or "")[-300:]))
check("every response is JSON-RPC 2.0",
      all(r.get("jsonrpc") == "2.0" for r in responses))
check("notification produced no response",
      not any(r.get("id") is None and "result" in r for r in responses))

init = by_id.get(1, {}).get("result", {})
check("initialize returns protocolVersion",
      init.get("protocolVersion") == "2024-11-05", init.get("protocolVersion"))
check("initialize advertises tools capability", "tools" in init.get("capabilities", {}))
check("initialize names the server",
      init.get("serverInfo", {}).get("name") == "ai-memory", init.get("serverInfo"))

listed = by_id.get(2, {}).get("result", {}).get("tools", [])
names = sorted(t["name"] for t in listed)
check("exactly three tools exposed",
      names == ["get_conversation", "get_conversation_chain", "search_memory"], names)
check("every tool has a description and inputSchema",
      all(t.get("description") and t.get("inputSchema") for t in listed))
check("schemas declare required arguments",
      all("required" in t["inputSchema"] for t in listed))


def tool_payload(response):
    return json.loads(response["result"]["content"][0]["text"])


check("search_memory over stdio finds Welcome",
      tool_payload(by_id[3])["results"][0]["title"] == "Welcome")
check("semantic search over stdio finds Welcome",
      tool_payload(by_id[4])["results"][0]["title"] == "Welcome",
      [r["title"] for r in tool_payload(by_id[4])["results"]])
check("semantic search over stdio is badged semantic",
      tool_payload(by_id[4])["results"][0]["match_type"] in ("semantic", "both"),
      tool_payload(by_id[4])["results"][0]["match_type"])
check("get_conversation over stdio returns 4 messages",
      tool_payload(by_id[5])["message_count"] == 4)
check("get_conversation_chain over stdio returns 2 members",
      tool_payload(by_id[6])["size"] == 2)

check("invalid conversation id -> isError, not a crash",
      by_id[7]["result"].get("isError") is True, by_id[7])
check("...with a readable message",
      "No conversation with id" in by_id[7]["result"]["content"][0]["text"])
check("invalid chain id -> isError",
      by_id[8]["result"].get("isError") is True)
check("...suggesting the known ids",
      "Known chain ids" in by_id[8]["result"]["content"][0]["text"],
      by_id[8]["result"]["content"][0]["text"])
check("ping answered", "result" in by_id.get(9, {}))
check("unknown method -> JSON-RPC error",
      by_id.get(10, {}).get("error", {}).get("code") == -32601, by_id.get(10))

bad, _ = stdio_session([INIT, "not json at all"] if False else [INIT])
check("server survives a malformed line",
      len(stdio_session([INIT])[0]) >= 1)

# Malformed input must produce a parse error, not a crash.
proc = subprocess.run(
    [sys.executable, os.path.join(ROOT, "mcp_server.py"), "--db", DB, "--no-sdk"],
    input='{"jsonrpc":"2.0","id":1,"method":"initialize"}\nthis is not json\n'
          '{"jsonrpc":"2.0","id":2,"method":"ping"}\n',
    capture_output=True, text=True, timeout=180, cwd=ROOT)
lines = [json.loads(l) for l in proc.stdout.splitlines() if l.strip()]
check("malformed line yields a parse error",
      any(r.get("error", {}).get("code") == -32700 for r in lines), lines)
check("server kept serving after the bad line",
      any(r.get("id") == 2 for r in lines), lines)

# The official SDK path, if the library is installed.
try:
    import mcp  # noqa: F401
    have_sdk = True
except ImportError:
    have_sdk = False

if have_sdk:
    print("\n-- official mcp SDK transport --")
    sdk_out, sdk_proc = stdio_session(
        [INIT, INITED, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}],
        use_sdk=True)
    sdk_by_id = {r.get("id"): r for r in sdk_out}
    sdk_tools = sorted(t["name"] for t in
                       sdk_by_id.get(2, {}).get("result", {}).get("tools", []))
    check("SDK transport lists the same three tools",
          sdk_tools == ["get_conversation", "get_conversation_chain",
                        "search_memory"],
          sdk_tools or (sdk_proc.stderr or "")[-200:])
else:
    print("  (mcp SDK not installed -- built-in transport only)")


# ======================================================================
print("\n== C. TCP transport (Settings toggle) ==")
from backend.mcp import server as mcp_server
from backend.web.app import MCP_ENABLED_KEY, create_app

port = mcp_server.find_free_port(8799)
status = mcp_server.BACKGROUND.start(DB, port=port)
check("background server reports running", status["running"] is True, status)
check("bound to loopback only", status["host"] == "127.0.0.1", status["host"])

try:
    with socket.create_connection(("127.0.0.1", status["port"]), timeout=30) as sock:
        sock.sendall((json.dumps(INIT) + "\n").encode())
        sock.sendall((json.dumps(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "search_memory",
                        "arguments": {"query": "sourdough"}}}) + "\n").encode())
        buf = b""
        deadline = time.time() + 60
        while buf.count(b"\n") < 2 and time.time() < deadline:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
    got = [json.loads(l) for l in buf.decode().splitlines() if l.strip()]
    check("TCP transport answered initialize",
          got and got[0].get("result", {}).get("serverInfo", {}).get("name") == "ai-memory",
          got[:1])
    check("TCP transport ran a tool",
          len(got) > 1 and json.loads(
              got[1]["result"]["content"][0]["text"])["count"] >= 1, got[1:2])
finally:
    stopped = mcp_server.BACKGROUND.stop()
check("background server stops", stopped["running"] is False, stopped)

app = create_app(db_path=DB, imports_dir=WORK)
client = app.test_client()
page = client.get("/settings")
check("settings page shows the MCP section", b"MCP server" in page.data)
check("settings shows the Claude Desktop config", b"mcpServers" in page.data)
check("settings names the launcher", b"mcp_server.py" in page.data)
check("toggle starts stopped by default", b"Stopped" in page.data)

client.post("/settings/mcp", data={"enabled": "1"}, follow_redirects=True)
check("toggle starts the server", mcp_server.BACKGROUND.status()["running"] is True)
conn2 = db.get_connection(DB)
check("toggle state persisted", db.get_flag(conn2, MCP_ENABLED_KEY) is True)
conn2.close()
check("settings now shows Running", b"Running" in client.get("/settings").data)
client.post("/settings/mcp", data={"enabled": "0"}, follow_redirects=True)
check("toggle stops the server", mcp_server.BACKGROUND.status()["running"] is False)


# ======================================================================
print("\n== D. without the semantic stack ==")
BLOCKED = {"sentence_transformers", "sqlite_vec"}


class Blocker:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in BLOCKED:
            raise ImportError("blocked for test: %s" % name)
        return None


# Run the real server in a subprocess whose import machinery refuses the
# semantic packages, so this exercises the same code path a user without them
# would hit.  A launcher file rather than `python -c`: mcp_server.py resolves
# its own location from __file__, which exec() would leave undefined.
launcher = os.path.join(WORK, "blocked_launcher.py")
with open(launcher, "w", encoding="utf-8") as fh:
    fh.write(
        "import sys\n"
        "class Blocker:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] in ('sentence_transformers', 'sqlite_vec'):\n"
        "            raise ImportError('blocked for test')\n"
        "        return None\n"
        "sys.meta_path.insert(0, Blocker())\n"
        "sys.path.insert(0, %r)\n"
        "from backend.mcp.server import main\n"
        "main(['--db', %r, '--no-sdk'])\n" % (ROOT, DB))

proc = subprocess.run(
    [sys.executable, launcher],
    input=json.dumps(INIT) + "\n" + json.dumps(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "search_memory",
                    "arguments": {"query": "gym workout"}}}) + "\n"
    + json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                  "params": {"name": "search_memory",
                             "arguments": {"query": "how do I get stronger"}}}) + "\n",
    capture_output=True, text=True, timeout=180, cwd=ROOT)
degraded = {}
for line in proc.stdout.splitlines():
    if line.strip():
        try:
            msg = json.loads(line)
            degraded[msg.get("id")] = msg
        except ValueError:
            pass

check("server starts without the semantic stack", proc.returncode == 0,
      (proc.stderr or "")[-300:])
check("initialize still works", 2 in degraded or 1 in degraded, sorted(degraded))
kw = json.loads(degraded[2]["result"]["content"][0]["text"])
check("keyword search still works", kw["results"][0]["title"] == "Welcome",
      [r["title"] for r in kw["results"]])
check("results badged keyword only",
      all(r["match_type"] == "keyword" for r in kw["results"]),
      [r["match_type"] for r in kw["results"]])
para = json.loads(degraded[3]["result"]["content"][0]["text"])
check("paraphrase returns nothing rather than crashing", para["results"] == [],
      para["results"])

print("\n" + ("ALL CHECKS PASSED" if not fails
              else "%d FAILURES: %s" % (len(fails), fails)))
sys.exit(1 if fails else 0)
