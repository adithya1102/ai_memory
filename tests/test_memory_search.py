"""Memories are searchable, by keyword and by meaning, everywhere.

A memory used to be stored and then invisible: the PWA and the API wrote to a
``memories`` table that no search path read, so the one fact a user had
deliberately kept was the one thing search could not find.

Part A covers the FTS index and its triggers, B the backfill for libraries
written before that index existed, C the merged ranking, D the MCP tool's
output, E the Bridge API, F semantic search over memories, G that deleting a
memory leaves nothing behind.
"""
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.core import database as db
from backend.core import embeddings
from backend.core.importer import import_file
from backend.core.search import hybrid_search, search_memories
from backend.mcp.tools import search_memory
from backend.web.app import create_app

WORK = tempfile.mkdtemp(prefix="contextvault-memsearch-")
DB = os.path.join(WORK, "mem.db")
EXPORT = os.path.join(ROOT, "dummy_export.json.json")

fails = []


def check(label, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + label
          + (("  -> " + str(detail)) if detail else ""))
    if not ok:
        fails.append(label)


db.init_db(DB).close()
conn = db.get_connection(DB)
import_file(conn, EXPORT)

GYM = ("Full ChatGPT gym export reviewed: beginner plan is full body three "
       "times a week, progressive overload on squat and bench.")
memory = db.insert_memory(conn, GYM, source="pwa", tags=["gym", "training"])
other = db.insert_memory(conn, "Postgres via Neon is the database for Skip.",
                         source="claude", tags=["infra"])


# ======================================================================
print("== A. memories are indexed on write ==")

indexed = conn.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0]
check("the insert trigger indexed both memories", indexed == 2, indexed)

hits = search_memories(conn, "progressive overload")
check("keyword search finds a memory", len(hits) == 1, len(hits))
check("it is the right one", hits and hits[0]["memory_id"] == memory["id"],
      hits and hits[0]["memory_id"])
check("the snippet comes back", bool(hits and hits[0]["snippet"]),
      hits and hits[0]["snippet"][:40])
check("the source is carried", hits and hits[0]["source"] == "pwa")

check("tags are searchable, not just the body",
      len(search_memories(conn, "training")) >= 1)
check("an unrelated query matches nothing",
      search_memories(conn, "zzzznotathing") == [])

# Editing a memory has to move the index with it, or search keeps answering
# with text that is no longer there.
conn.execute("UPDATE memories SET content = ? WHERE id = ?",
             ("Deadlifts replaced squats in the plan.", memory["id"]))
conn.commit()
check("an edit updates the index",
      len(search_memories(conn, "deadlifts")) == 1,
      len(search_memories(conn, "deadlifts")))
check("and the old text stops matching",
      search_memories(conn, "progressive overload") == [])
conn.execute("UPDATE memories SET content = ? WHERE id = ?",
             (GYM, memory["id"]))
conn.commit()
check("restoring the text restores the match",
      len(search_memories(conn, "progressive overload")) == 1)


# ======================================================================
print("\n== B. libraries written before the index existed ==")

# Simulates an upgrade: rows in memories, nothing in memory_fts.
conn.execute("DELETE FROM memory_fts")
conn.commit()
check("the index is empty to start",
      conn.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0] == 0)
check("and search finds nothing", search_memories(conn, "progressive") == [])

filled = db.backfill_memory_fts(conn)
conn.commit()
check("the backfill indexed the existing memories", filled == 2, filled)
check("search works again", len(search_memories(conn, "progressive")) == 1)
check("running it again is a no-op", db.backfill_memory_fts(conn) == 0)

# init_db runs the backfill, so simply opening an upgraded library repairs it.
conn.execute("DELETE FROM memory_fts")
conn.commit()
conn.close()
db.init_db(DB).close()
conn = db.get_connection(DB)
check("init_db repairs an unindexed library",
      len(search_memories(conn, "progressive")) == 1)


# ======================================================================
print("\n== C. one merged ranking ==")

results = hybrid_search(conn, "gym", limit=10)
kinds = {r["kind"] for r in results}
check("results carry a kind", kinds and kinds <= {"conversation", "memory"},
      kinds)
check("both kinds compete in one list",
      "memory" in kinds and "conversation" in kinds, kinds)

memory_rows = [r for r in results if r["kind"] == "memory"]
check("the memory is present", len(memory_rows) == 1, len(memory_rows))
row = memory_rows[0]
check("it has a memory_id", row.get("memory_id") == memory["id"])
check("it has a title derived from its text", bool(row.get("title")),
      row.get("title"))
check("its title is not the whole fact",
      len(row["title"]) <= 71, len(row["title"]))
check("it carries its full content", row.get("content") == GYM)
check("it has a match label",
      row.get("match_label") in ("keyword", "semantic", "both"),
      row.get("match_label"))
check("its provider shows where it came from",
      row.get("provider_name") == "pwa", row.get("provider_name"))
check("message_count is None, since a memory has no messages",
      row.get("message_count") is None)

conversation_rows = [r for r in results if r["kind"] == "conversation"]
check("conversations still carry a conversation_id",
      all(r.get("conversation_id") for r in conversation_rows))
check("conversations still carry a message count",
      all(r.get("message_count") is not None for r in conversation_rows))

check("memories can be excluded",
      all(r["kind"] == "conversation"
          for r in hybrid_search(conn, "gym", limit=10,
                                 include_memories=False)))

# A memory saved from a conversation keeps the link; one written by hand does
# not, and must not invent one.
check("a standalone memory has no conversation_id",
      row.get("conversation_id") is None, row.get("conversation_id"))
linked = db.insert_memory(conn, "Linked note about the welcome chat.",
                          source="pwa", conversation_id="chatgpt:conv-001")
linked_rows = [r for r in hybrid_search(conn, "welcome chat linked", limit=10)
               if r["kind"] == "memory"]
check("a linked memory keeps its conversation_id",
      any(r.get("conversation_id") == "chatgpt:conv-001" for r in linked_rows),
      [r.get("conversation_id") for r in linked_rows])


# ======================================================================
print("\n== D. the MCP tool ==")

payload = search_memory(conn, "progressive overload squat", limit=10)
mem_results = [r for r in payload["results"] if r["kind"] == "memory"]
check("search_memory returns the memory", len(mem_results) == 1,
      [r["title"] for r in payload["results"]])

hit = mem_results[0]
check("it is labelled as a memory", hit["kind"] == "memory")
check("it carries memory_id", hit.get("memory_id") == memory["id"])
check("it carries the whole fact, not a fragment", hit.get("content") == GYM)
check("it carries its tags", hit.get("tags") == ["gym", "training"],
      hit.get("tags"))
check("it has a relevance score", isinstance(hit.get("relevance_score"), float))
check("it has a match type",
      hit.get("match_type") in ("keyword", "semantic", "both"),
      hit.get("match_type"))
check("a standalone memory reports no conversation_id",
      hit.get("conversation_id") is None)

conv_results = [r for r in search_memory(conn, "gym", limit=10)["results"]
                if r["kind"] == "conversation"]
check("conversation results are labelled too", bool(conv_results))
check("and still carry an id get_conversation accepts",
      all(r.get("conversation_id") for r in conv_results))

# The tool describes itself to the model; if it does not mention memories the
# model has no reason to expect them.
from backend.mcp.tools import TOOL_SCHEMAS_BY_NAME
description = TOOL_SCHEMAS_BY_NAME["search_memory"]["description"]
check("the tool description mentions memories", "memor" in description.lower())
check("it explains the kind field", "kind" in description.lower())


# ======================================================================
print("\n== E. through the Bridge API, as the PWA saves ==")

conn.close()
api_db = os.path.join(WORK, "api.db")
db.init_db(api_db).close()
seed = db.get_connection(api_db)
import_file(seed, EXPORT)
seed.close()

app = create_app(db_path=api_db)
app.config["TESTING"] = True
client = app.test_client()

saved = client.post("/api/v1/memories", json={
    "content": GYM, "source": "pwa", "tags": ["gym", "training"]})
check("the PWA can save a memory", saved.status_code == 201, saved.status_code)
body = saved.get_json()
check("the response reports how it was indexed",
      isinstance(body.get("indexed"), dict), body.get("indexed"))
check("keyword indexing is immediate",
      body["indexed"].get("keyword") is True, body.get("indexed"))

found = client.get("/api/v1/search?q=progressive+overload&limit=10").get_json()
api_memories = [r for r in found["results"] if r.get("kind") == "memory"]
check("the API finds it straight after saving", len(api_memories) == 1,
      [r["title"] for r in found["results"]])
check("with its id", api_memories and api_memories[0]["memory_id"] == body["id"])

# The whole point of the fix: the same query over the tool layer the MCP
# server calls returns the same memory.
mcp_conn = db.get_connection(api_db)
via_mcp = search_memory(mcp_conn, "progressive overload", limit=10)
check("and MCP's search_memory returns it too",
      any(r["kind"] == "memory" and r["memory_id"] == body["id"]
          for r in via_mcp["results"]),
      [(r["kind"], r["title"][:30]) for r in via_mcp["results"]])

# The search page renders both kinds; a memory has no transcript to link to,
# and url_for would have raised on a None id before this was handled.
page = client.get("/search?q=progressive+overload")
check("the web results page renders a memory without erroring",
      page.status_code == 200, page.status_code)
check("and shows it as a memory", b"memory" in page.data)


# ======================================================================
print("\n== F. semantic search over memories ==")

ok, reason = embeddings.availability()
if not ok:
    print("  (semantic stack not installed: %s)" % reason)
else:
    stats = embeddings.sync_memory_embeddings(mcp_conn)
    check("memories embed", stats.get("skipped") is None, stats.get("skipped"))
    # POST /memories embeds on save, so by now there is usually nothing left
    # pending -- which is the point. What matters is that a vector exists.
    check("the memory saved through the API already has a vector",
          embeddings.has_memory_embeddings(mcp_conn))
    check("the API reported it as semantically indexed",
          body["indexed"].get("semantic") is True, body.get("indexed"))
    check("re-running embeds nothing new",
          embeddings.sync_memory_embeddings(mcp_conn).get("memories") == 0)

    # Editing the text must invalidate the vector, or semantic search keeps
    # answering from the old wording.
    mcp_conn.execute("UPDATE memories SET content = ? WHERE id = ?",
                     (GYM + " Rest days matter.", body["id"]))
    mcp_conn.commit()
    check("an edited memory is re-embedded",
          embeddings.sync_memory_embeddings(mcp_conn).get("memories") == 1)

    check("memory vectors exist",
          embeddings.has_memory_embeddings(mcp_conn))

    embeddings.wait_until_ready(180)
    hits = embeddings.semantic_search_memories(mcp_conn, "how often should I "
                                               "lift weights", top_k=5)
    check("a paraphrase with no shared words finds the memory",
          any(h["memory_id"] == body["id"] for h in hits),
          [h.get("content", "")[:40] for h in hits])

    merged = hybrid_search(mcp_conn, "how often should I lift weights",
                           limit=10)
    check("and it surfaces in the merged ranking",
          any(r["kind"] == "memory" for r in merged),
          [(r["kind"], r.get("match_label")) for r in merged])


# ======================================================================
print("\n== G. deleting a memory leaves nothing behind ==")

target = body["id"]
check("it is there first", len(search_memories(mcp_conn, "progressive")) >= 1)
db.delete_memory(mcp_conn, target)

check("the FTS row goes with it",
      mcp_conn.execute("SELECT COUNT(*) FROM memory_fts WHERE rowid = ?",
                       (target,)).fetchone()[0] == 0)
check("it stops appearing in search",
      not any(r["kind"] == "memory"
              for r in hybrid_search(mcp_conn, "progressive overload",
                                     limit=10)))
check("and MCP no longer returns it",
      not any(r["kind"] == "memory" and r.get("memory_id") == target
              for r in search_memory(mcp_conn, "progressive overload",
                                     limit=10)["results"]))

if ok:
    embeddings.sync_memory_embeddings(mcp_conn)
    orphans = mcp_conn.execute(
        """SELECT COUNT(*) FROM memory_vectors v
            WHERE NOT EXISTS (SELECT 1 FROM memories m WHERE m.id = v.rowid)"""
    ).fetchone()[0]
    check("its vector is pruned, not left orphaned", orphans == 0, orphans)

mcp_conn.close()

print("\n" + ("ALL CHECKS PASSED" if not fails
              else "%d FAILURES: %s" % (len(fails), fails)))
sys.exit(1 if fails else 0)
