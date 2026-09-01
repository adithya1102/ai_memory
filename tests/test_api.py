"""Bridge API: the REST layer at /api/v1.

Part A covers conversations and messages, B search and chains, C the ingest
endpoint's three shapes, D memories, E API key auth, F health and error
handling.  Everything runs through Flask's test client against a temporary
database, so no port is bound and no export is touched.
"""
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.core import database as db
from backend.web import api as api_module
from backend.web.app import create_app

WORK = tempfile.mkdtemp(prefix="contextvault-api-")
DB = os.path.join(WORK, "api.db")

fails = []


def check(label, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + label
          + (("  -> " + str(detail)) if detail else ""))
    if not ok:
        fails.append(label)


db.init_db(DB).close()
app = create_app(db_path=DB)
app.config["TESTING"] = True
client = app.test_client()


def post(path, payload, **kwargs):
    return client.post(path, json=payload, **kwargs)


# ======================================================================
print("== A. conversations and messages ==")

created = post("/api/v1/conversations", {
    "title": "Bridge API design",
    "provider": "manual",
    "messages": [
        {"role": "user", "content": "Should the bridge accept partial conversations?"},
        {"role": "assistant", "content": "Yes, POST /messages appends turn by turn."},
    ],
})
check("POST /conversations returns 201", created.status_code == 201,
      created.status_code)
body = created.get_json()
CONV = body.get("conversation_id", "")
check("conversation id is namespaced by source", CONV.startswith("manual:"), CONV)
check("outcome is inserted", body.get("outcome") == "inserted", body)
check("message count echoed", body.get("message_count") == 2, body)

# The id is generated when the caller does not supply one, but an explicit id
# must be honoured -- that is what makes a re-post idempotent.
again = post("/api/v1/conversations", {
    "conversation_id": "manual:fixed-id", "title": "Fixed",
    "messages": [{"role": "user", "content": "pinned identifier"}]})
check("explicit conversation_id honoured",
      again.get_json().get("conversation_id") == "manual:fixed-id",
      again.get_json())
repeat = post("/api/v1/conversations", {
    "conversation_id": "manual:fixed-id", "title": "Fixed",
    "messages": [{"role": "user", "content": "pinned identifier"}]})
check("re-posting the same transcript is unchanged, not a duplicate row",
      repeat.get_json().get("outcome") == "unchanged", repeat.get_json())

conn = db.get_connection(DB)
check("only one row for the pinned id",
      conn.execute("SELECT COUNT(*) FROM conversations WHERE id = ?",
                   ("manual:fixed-id",)).fetchone()[0] == 1)

check("blank conversation_id rejected",
      post("/api/v1/conversations", {"conversation_id": "  ", "messages": [
          {"role": "user", "content": "x"}]}).status_code == 400)
check("empty messages rejected",
      post("/api/v1/conversations", {"messages": []}).status_code == 400)
check("bad role rejected",
      post("/api/v1/conversations", {"messages": [
          {"role": "wizard", "content": "x"}]}).status_code == 400)
check("blank content rejected",
      post("/api/v1/conversations", {"messages": [
          {"role": "user", "content": "   "}]}).status_code == 400)
check("'human' is accepted as 'user'",
      post("/api/v1/conversations", {"title": "Claude wording", "messages": [
          {"role": "human", "content": "claude calls the person human"}]}
           ).status_code == 201)
check("a top-level array is refused here, with a pointer to /ingest",
      "ingest" in post("/api/v1/conversations", [{"x": 1}]).get_json()["error"])

appended = post("/api/v1/messages", {
    "conversation_id": CONV, "role": "user",
    "content": "One more thought about idempotency."})
check("POST /messages returns 201", appended.status_code == 201,
      appended.status_code)
check("append reports the new total",
      appended.get_json().get("message_count") == 3, appended.get_json())

batch = post("/api/v1/messages", {"conversation_id": CONV, "messages": [
    {"role": "assistant", "content": "Ordering continues from the last turn."},
    {"role": "user", "content": "Good."}]})
check("a batch of messages appends in one call",
      batch.get_json().get("appended") == 2, batch.get_json())

orders = [r["message_order"] for r in db.get_messages(conn, CONV)]
check("message_order stays contiguous", orders == [0, 1, 2, 3, 4], orders)

check("appending to an unknown conversation is 404",
      post("/api/v1/messages", {"conversation_id": "nope", "role": "user",
                                "content": "x"}).status_code == 404)
check("append without conversation_id is 400",
      post("/api/v1/messages", {"role": "user", "content": "x"}).status_code == 400)

# Appending changes the transcript, so the hash must move or a later re-import
# would look unchanged and skip re-embedding.
hash_before = conn.execute("SELECT content_hash FROM conversations WHERE id = ?",
                           (CONV,)).fetchone()[0]
post("/api/v1/messages", {"conversation_id": CONV, "role": "user",
                          "content": "and the hash should move"})
hash_after = conn.execute("SELECT content_hash FROM conversations WHERE id = ?",
                          (CONV,)).fetchone()[0]
check("content_hash is recomputed on append", hash_before != hash_after,
      "%s -> %s" % (str(hash_before)[:12], str(hash_after)[:12]))

fetched = client.get("/api/v1/conversations/" + CONV)
check("GET /conversations/<id> returns 200", fetched.status_code == 200)
check("transcript comes back in order",
      [m["order"] for m in fetched.get_json()["messages"]] == [0, 1, 2, 3, 4, 5],
      [m["order"] for m in fetched.get_json()["messages"]])
check("unknown conversation is 404, not 400",
      client.get("/api/v1/conversations/nope").status_code == 404)


# ======================================================================
print("\n== B. search and chains ==")

found = client.get("/api/v1/search?q=idempotency&limit=5")
check("GET /search returns 200", found.status_code == 200)
hits = found.get_json()
check("the posted conversation is searchable immediately",
      any(r["conversation_id"] == CONV for r in hits["results"]),
      [r["title"] for r in hits["results"]])
check("search echoes the query", hits.get("query") == "idempotency", hits.get("query"))
check("results carry a match_type badge",
      all(r["match_type"] in ("keyword", "semantic", "both")
          for r in hits["results"]),
      [r["match_type"] for r in hits["results"]])
check("search without q is 400", client.get("/api/v1/search").status_code == 400)
check("non-integer limit is 400",
      client.get("/api/v1/search?q=x&limit=lots").status_code == 400)

check("unknown chain is 404", client.get("/api/v1/chains/9999").status_code == 404)


# ======================================================================
print("\n== C. ingest ==")

single = post("/api/v1/ingest", {
    "source": "extension", "title": "Captured from a tab",
    "messages": [{"role": "user", "content": "watched in the browser"}]})
check("ingest accepts one normalised conversation", single.status_code == 201,
      single.status_code)
check("ingest labels the normalised shape",
      single.get_json().get("format") == "normalised", single.get_json())

many = post("/api/v1/ingest", {"source": "manual", "conversations": [
    {"title": "Alpha", "messages": [{"role": "user", "content": "alpha note"}]},
    {"title": "Beta", "messages": [{"role": "user", "content": "beta note"}]}]})
check("ingest accepts a normalised batch",
      many.get_json().get("count") == 2, many.get_json())

# A real ChatGPT export is a bare array of records with a message tree.
chatgpt = post("/api/v1/ingest", [{
    "title": "Ingested ChatGPT", "create_time": 1724400000,
    "mapping": {
        "root": {"id": "root", "message": None, "parent": None,
                 "children": ["m1"]},
        "m1": {"id": "m1", "parent": "root", "children": [],
               "message": {"author": {"role": "user"},
                           "content": {"content_type": "text",
                                       "parts": ["posted as a raw export"]},
                           "create_time": 1724400001}}}}])
check("ingest accepts a bare ChatGPT export array",
      chatgpt.status_code == 201, chatgpt.get_json())
check("the ChatGPT adapter was chosen",
      chatgpt.get_json().get("source") == "chatgpt", chatgpt.get_json())
check("ingest labels the provider-export shape",
      chatgpt.get_json().get("format") == "provider-export", chatgpt.get_json())

claude = post("/api/v1/ingest", [{
    "uuid": "bridge-1", "name": "Ingested Claude",
    "created_at": "2026-01-01T00:00:00Z",
    "chat_messages": [{"sender": "human", "text": "posted as a claude export",
                       "created_at": "2026-01-01T00:00:00Z"}]}])
check("the Claude adapter was chosen for a Claude export",
      claude.get_json().get("source") == "claude", claude.get_json())

check("an unknown source is rejected by name",
      post("/api/v1/ingest", {"source": "myspace", "messages": [
          {"role": "user", "content": "x"}]}).status_code == 400)

names = {r["name"] for r in db.get_stats(db.get_connection(DB))["providers"]}
check("providers were created per source",
      {"manual", "extension", "chatgpt", "claude"} <= names, sorted(names))


# ======================================================================
print("\n== D. memories ==")

saved = post("/api/v1/memories", {
    "content": "Gusto POS is the least certain of the three product lines.",
    "source": "claude", "tags": ["gusto", "risk"], "conversation_id": CONV})
check("POST /memories returns 201", saved.status_code == 201, saved.status_code)
memory = saved.get_json()
MEMORY_ID = memory.get("id")
check("tags round-trip as a list", memory.get("tags") == ["gusto", "risk"],
      memory.get("tags"))
check("memory links to its conversation",
      memory.get("conversation_id") == CONV, memory.get("conversation_id"))

post("/api/v1/memories", {"content": "Bridge binds to loopback only.",
                          "source": "manual", "tags": ["api"]})

listed = client.get("/api/v1/memories").get_json()
check("GET /memories lists both", listed.get("count") == 2, listed)
check("newest first",
      listed["memories"][0]["content"].startswith("Bridge binds"),
      [m["content"][:20] for m in listed["memories"]])

filtered = client.get("/api/v1/memories?source=claude").get_json()
check("memories filter by source", filtered.get("count") == 1, filtered)
check("memories filter by conversation_id",
      client.get("/api/v1/memories?conversation_id=" + CONV
                 ).get_json().get("count") == 1)

check("content is required",
      post("/api/v1/memories", {"source": "x"}).status_code == 400)
check("blank content is rejected",
      post("/api/v1/memories", {"content": "  "}).status_code == 400)
check("tags must be strings",
      post("/api/v1/memories", {"content": "x", "tags": [1, 2]}).status_code == 400)
check("linking to an unknown conversation is 404",
      post("/api/v1/memories", {"content": "x",
                                "conversation_id": "nope"}).status_code == 404)

check("DELETE /memories/<id> reports the id",
      client.delete("/api/v1/memories/%d" % MEMORY_ID).get_json()
      == {"deleted": MEMORY_ID})
check("deleting twice is 404",
      client.delete("/api/v1/memories/%d" % MEMORY_ID).status_code == 404)
check("one memory left",
      client.get("/api/v1/memories").get_json().get("count") == 1)

# A memory outlives the transcript it came from: the FK is ON DELETE SET NULL,
# so deleting the conversation must not delete the conclusion.
kept = post("/api/v1/memories", {"content": "survives its source",
                                 "conversation_id": "manual:fixed-id"}).get_json()
conn2 = db.get_connection(DB)
conn2.execute("DELETE FROM conversations WHERE id = ?", ("manual:fixed-id",))
conn2.commit()
row = db.get_memory(conn2, kept["id"])
check("memory survives its conversation being deleted", row is not None)
check("its conversation link is cleared, not left dangling",
      row and row["conversation_id"] is None, row and row["conversation_id"])


# ======================================================================
print("\n== E. API key auth ==")

check("auth is off by default",
      db.get_flag(conn2, api_module.API_AUTH_REQUIRED_KEY, default=False) is False)

db.set_setting(conn2, api_module.API_KEY_KEY, "secret-key-123")
db.set_flag(conn2, api_module.API_AUTH_REQUIRED_KEY, True)

check("no key is 401", client.get("/api/v1/search?q=alpha").status_code == 401)
check("wrong key is 401",
      client.get("/api/v1/search?q=alpha",
                 headers={"X-API-Key": "wrong"}).status_code == 401)
check("right key is 200",
      client.get("/api/v1/search?q=alpha",
                 headers={"X-API-Key": "secret-key-123"}).status_code == 200)
check("writes are guarded too",
      post("/api/v1/memories", {"content": "x"}).status_code == 401)
check("health stays reachable without a key, for monitoring",
      client.get("/api/v1/health").status_code == 200)
check("health reports that auth is on",
      client.get("/api/v1/health").get_json().get("auth_required") is True)

# Enabled with no key configured must fail closed: a misconfiguration that
# read as "auth is off" would silently expose the whole archive.
db.set_setting(conn2, api_module.API_KEY_KEY, "")
check("enabled with no key set fails closed with 503",
      client.get("/api/v1/search?q=alpha",
                 headers={"X-API-Key": "anything"}).status_code == 503)

db.set_flag(conn2, api_module.API_AUTH_REQUIRED_KEY, False)
check("turning auth off restores access",
      client.get("/api/v1/search?q=alpha").status_code == 200)

# The Settings form is the supported way to turn this on.
toggled = client.post("/settings/api", data={"enabled": "1"},
                      follow_redirects=True)
check("Settings can enable auth", toggled.status_code == 200, toggled.status_code)
generated = db.get_setting(db.get_connection(DB), api_module.API_KEY_KEY)
check("a key is generated when none is supplied", bool(generated),
      (generated or "")[:8] + "...")
client.post("/settings/api", data={"enabled": "0"}, follow_redirects=True)
check("toggling off keeps the key for next time",
      db.get_setting(db.get_connection(DB), api_module.API_KEY_KEY) == generated)


# ======================================================================
print("\n== F. health and error handling ==")

health = client.get("/api/v1/health")
check("GET /health returns 200", health.status_code == 200)
payload = health.get_json()
check("health says ok", payload.get("status") == "ok", payload.get("status"))
check("health names the service",
      payload.get("service") == "contextvault-bridge", payload.get("service"))
check("health counts conversations",
      isinstance(payload.get("conversations"), int) and payload["conversations"] > 0,
      payload.get("conversations"))
check("health counts memories", isinstance(payload.get("memories"), int),
      payload.get("memories"))
check("health reports the semantic flag",
      isinstance(payload.get("semantic_search"), bool),
      payload.get("semantic_search"))

check("a non-JSON body is a clear 400",
      client.post("/api/v1/conversations", data="not json",
                  content_type="text/plain").status_code == 400)
missing = client.get("/api/v1/nothing-here")
check("an unknown /api/v1 route is a JSON 404",
      missing.status_code == 404 and missing.is_json, missing.status_code)
wrong_method = client.get("/api/v1/ingest")
check("the wrong method is a JSON 405",
      wrong_method.status_code == 405 and wrong_method.is_json,
      wrong_method.status_code)
check("a missing UI page still gets Flask's HTML 404, not JSON",
      not client.get("/no-such-page").is_json)

# Errors are JSON everywhere, so a client never has to parse an HTML error page.
for path, method in (("/api/v1/search", "get"),
                     ("/api/v1/conversations/nope", "get"),
                     ("/api/v1/memories/999999", "delete")):
    response = getattr(client, method)(path)
    check("error from %s is JSON with an 'error' key" % path,
          response.is_json and "error" in response.get_json(),
          response.status_code)


print("\n" + ("ALL CHECKS PASSED" if not fails
              else "%d FAILURES: %s" % (len(fails), fails)))
sys.exit(1 if fails else 0)
