"""End-to-end check: import -> search -> chains -> every Flask route."""
import os, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.core import database as db
from backend.core.importer import import_file, detect_chains
from backend.core.search import search_conversations
from backend.web.app import create_app

SAMPLE = (sys.argv[1] if len(sys.argv) > 1 else
          os.path.join(ROOT, "tests", "fixtures", "edge_cases_export.json"))
DB = os.path.join(tempfile.mkdtemp(), "test.db")

fails = []
def check(label, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + label + (("  -> " + str(detail)) if detail else ""))
    if not ok:
        fails.append(label)

print("\n== import ==")
conn = db.init_db(DB)
stats = import_file(conn, SAMPLE, provider="chatgpt")
print(" ", stats)
check("8 conversations inserted", stats["inserted"] == 8, stats["inserted"])
check("1 empty conversation skipped", stats["skipped"] == 1, stats["skipped"])

rows = conn.execute("SELECT id, title FROM conversations ORDER BY id").fetchall()
check("ids are prefixed", all(r["id"].startswith("chatgpt:") for r in rows))
check("created_at is ISO 8601",
      conn.execute("SELECT created_at FROM conversations WHERE id='chatgpt:aaa-111'"
                   ).fetchone()[0].startswith("2025-01-01T"),
      conn.execute("SELECT created_at FROM conversations WHERE id='chatgpt:aaa-111'").fetchone()[0])

print("\n== message extraction ==")
def msgs(cid):
    return db.get_messages(conn, "chatgpt:" + cid)

check("system messages dropped",
      conn.execute("SELECT COUNT(*) FROM messages WHERE role='system'").fetchone()[0] == 0)
check("linear conversation keeps 4 turns", len(msgs("aaa-111")) == 4, len(msgs("aaa-111")))
check("message_order is sequential",
      [m["message_order"] for m in msgs("aaa-111")] == [0, 1, 2, 3])
check("roles alternate",
      [m["role"] for m in msgs("aaa-111")] == ["user", "assistant", "user", "assistant"])
branch = " ".join(m["content"] for m in msgs("fff-666"))
check("abandoned branch excluded", "DISCARDED" not in branch, branch[:60])
check("active branch kept", "systemd" in branch)
edge = msgs("ggg-777")
check("hidden message excluded", not any("HIDDEN" in m["content"] for m in edge))
check("multimodal text part kept",
      any("What does this chart show?" in m["content"] for m in edge))
check("flat-format conversation imported", len(msgs("iii-999")) == 2, len(msgs("iii-999")))

print("\n== search ==")
def ids(q):
    return [r["conversation_id"] for r in search_conversations(conn, q)]

r = search_conversations(conn, "borrow checker")
check("body-only term found", r and r[0]["conversation_id"] == "chatgpt:aaa-111", ids("borrow checker"))
check("snippet is populated", bool(r and r[0]["snippet"].strip()), r[0]["snippet"] if r else "")
check("snippet marks the hit", "<mark>" in r[0]["snippet_html"], r[0]["snippet_html"][:80] if r else "")
check("result has all required fields",
      set(["conversation_id", "title", "provider_name", "created_at", "snippet",
           "match_score"]).issubset(r[0].keys()))
check("provider name resolved", r[0]["provider_name"] == "ChatGPT", r[0]["provider_name"])
check("title-only term found", "chatgpt:eee-555" in ids("SQLite"), ids("SQLite"))
check("multi-term is AND", ids("sourdough acetone") == ["chatgpt:ccc-333"], ids("sourdough acetone"))
check("two conversations match sourdough", len(ids("sourdough")) == 2, ids("sourdough"))
check("stemming works (annotations->annotate)", "chatgpt:aaa-111" in ids("annotations"), ids("annotations"))
check("no match returns empty", ids("zzzznotpresent") == [])
check("hidden text not searchable", ids("HIDDEN SCAFFOLD") == [])
check("discarded branch not searchable", ids("DISCARDED REGENERATION") == [])
for bad in ['"', 'foo"bar', 'a AND OR b', 'NEAR(', 'x*', '-term', 'a:b', '((']:
    check("hostile query %-12r survives" % bad, isinstance(search_conversations(conn, bad), list))

print("\n== chains ==")
chains = db.get_chains(conn)
print(" ", [(c["name"], c["size"]) for c in chains])
# Overlap coefficient, inclusive at 0.5: the rust pair scores 1.0 (one title's
# words are a subset of the other's) and the sourdough pair 2/3.
check("two chains detected", len(chains) == 2, len(chains))
members = {}
for c in chains:
    full = db.get_chain(conn, c["id"])
    members[c["name"]] = [m["id"] for m in full["conversations"]]
rust = [m for m in members.values() if "chatgpt:aaa-111" in m]
sour = [m for m in members.values() if "chatgpt:ccc-333" in m]
check("rust pair chained", rust and set(rust[0]) == {"chatgpt:aaa-111", "chatgpt:bbb-222"}, rust)
check("sourdough pair chained", sour and set(sour[0]) == {"chatgpt:ccc-333", "chatgpt:ddd-444"}, sour)
check("unrelated conversation not chained",
      all("chatgpt:eee-555" not in m for m in members.values()))
check("positions ordered by created_at", rust and rust[0][0] == "chatgpt:aaa-111", rust)
check("chain names are meaningful", all(c["name"] for c in chains), [c["name"] for c in chains])
check("detect_chains is idempotent", detect_chains(conn) == 2 and len(db.get_chains(conn)) == 2)
check("chain names are stable across rebuilds",
      [c["name"] for c in db.get_chains(conn)] ==
      [detect_chains(conn) and c["name"] for c in db.get_chains(conn)],
      [c["name"] for c in db.get_chains(conn)])

# A stricter threshold drops the weaker pair but keeps the subset match.
check("stricter threshold keeps only the rust pair",
      detect_chains(conn, threshold=0.9) == 1,
      [(c["name"], c["size"]) for c in db.get_chains(conn)])
detect_chains(conn)  # restore the default grouping

print("\n== re-import (dedup) ==")
again = import_file(conn, SAMPLE, provider="chatgpt")
print(" ", again)
check("nothing re-inserted", again["inserted"] == 0, again["inserted"])
check("all unchanged by hash", again["unchanged"] == 8, again["unchanged"])
check("no duplicate messages",
      conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] ==
      sum(len(msgs(c)) for c in ["aaa-111","bbb-222","ccc-333","ddd-444","eee-555","fff-666","ggg-777","iii-999"]))
check("no duplicate FTS rows",
      conn.execute("SELECT COUNT(*) FROM conversation_fts").fetchone()[0] == 8,
      conn.execute("SELECT COUNT(*) FROM conversation_fts").fetchone()[0])
check("search still returns one hit", len(ids("borrow checker")) == 1, ids("borrow checker"))

print("\n== content change re-import ==")
import json
data = json.load(open(SAMPLE, encoding="utf-8"))
data[0]["mapping"]["aaa-111-n3"]["message"]["content"]["parts"] = ["Totally rewritten answer about xylophones."]
edited = os.path.join(os.path.dirname(DB), "edited.json")
json.dump(data, open(edited, "w", encoding="utf-8"), indent=1)
upd = import_file(conn, edited, provider="chatgpt")
check("changed conversation updated", upd["updated"] == 1, upd["updated"])
check("new text searchable", ids("xylophones") == ["chatgpt:aaa-111"], ids("xylophones"))
check("stale text gone from index", ids("elision") == [], ids("elision"))
check("message count unchanged after update", len(msgs("aaa-111")) == 4, len(msgs("aaa-111")))
conn.close()

print("\n== legacy database adoption (AI Memory -> ContextVault) ==")
_work = tempfile.mkdtemp()
_legacy = os.path.join(_work, "ai_memory.db")
_target = os.path.join(_work, db.DB_FILENAME)
check("new filename is contextvault.db",
      db.DB_FILENAME == "contextvault.db", db.DB_FILENAME)
check("legacy filename constant is intact",
      db.LEGACY_DB_FILENAME == "ai_memory.db", db.LEGACY_DB_FILENAME)

_c = db.init_db(_legacy)
_pid = db.insert_provider(_c, "chatgpt", "ChatGPT")
db.insert_conversation(_c, "chatgpt:legacy", _pid, "From the old name",
                       content_hash="legacy-hash")
db.insert_message(_c, "chatgpt:legacy", "user", "carried over", message_order=0)
_c.commit()
_c.close()

_c = db.get_connection(_target)
check("legacy database adopted at the new path", os.path.exists(_target))
check("old file no longer left behind", not os.path.exists(_legacy))
check("conversations survived the rename",
      _c.execute("SELECT title FROM conversations").fetchone()[0]
      == "From the old name")
check("messages survived the rename",
      _c.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1)
check("search index survived the rename",
      [r["conversation_id"] for r in search_conversations(_c, "carried")]
      == ["chatgpt:legacy"])
_c.close()

_c = db.get_connection(_target)
check("adoption is idempotent",
      _c.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 1)
_c.close()

# An explicit --db must never adopt a stray ai_memory.db sitting beside it.
_decoy = os.path.join(_work, "ai_memory.db")
open(_decoy, "wb").write(b"not a database")
_explicit = os.path.join(_work, "explicit.db")
db.get_connection(_explicit).close()
check("an explicit --db path is never hijacked", os.path.exists(_decoy))
check("...and the explicit database was still created", os.path.exists(_explicit))

print("\n== flask routes ==")
app = create_app(db_path=DB, imports_dir=os.path.dirname(DB))
c = app.test_client()

r = c.get("/")
check("GET /", r.status_code == 200 and b"Rust ownership" in r.data, r.status_code)
r = c.get("/search?q=sourdough")
check("GET /search", r.status_code == 200 and b"sourdough" in r.data.lower(), r.status_code)
check("search page highlights", b"<mark>" in r.data)
r = c.get("/search?q=")
check("GET /search empty redirects", r.status_code == 302, r.status_code)
r = c.get("/conversation/chatgpt:aaa-111")
check("GET /conversation (colon id)", r.status_code == 200 and b"borrow checker" in r.data, r.status_code)
check("conversation shows chain link", b"/chain/" in r.data)
r = c.get("/conversation/chatgpt:nope")
check("missing conversation 404s", r.status_code == 404, r.status_code)
r = c.get("/chains")
check("GET /chains", r.status_code == 200, r.status_code)
cid = db.get_chains(db.get_connection(DB))[0]["id"]
r = c.get("/chain/%d" % cid)
check("GET /chain/<id>", r.status_code == 200, r.status_code)
check("chain/999 404s", c.get("/chain/999").status_code == 404)
r = c.get("/settings")
check("GET /settings", r.status_code == 200 and b"ChatGPT" in r.data, r.status_code)
check("settings shows db path", DB.split(os.sep)[-1].encode() in r.data)
r = c.get("/import")
check("GET /import", r.status_code == 200 and b"multipart/form-data" in r.data, r.status_code)

with open(SAMPLE, "rb") as fh:
    r = c.post("/import", data={"file": (fh, "conversations.json")},
               content_type="multipart/form-data", follow_redirects=True)
check("POST /import upload", r.status_code == 200 and b"unchanged" in r.data, r.status_code)
r = c.post("/import", data={"path": SAMPLE}, follow_redirects=True)
check("POST /import by path", r.status_code == 200 and b"Imported" in r.data, r.status_code)
r = c.post("/import", data={"path": "C:/nope/missing.json"}, follow_redirects=True)
check("POST /import bad path is handled", r.status_code == 200 and b"No file found" in r.data)
r = c.post("/import", data={}, follow_redirects=True)
check("POST /import with nothing is handled", r.status_code == 200 and b"Choose a" in r.data)
r = c.post("/rebuild-chains", follow_redirects=True)
check("POST /rebuild-chains", r.status_code == 200 and b"Rebuilt chains" in r.data)

print("\n" + ("ALL %d CHECKS PASSED" % 0 if not fails else "%d FAILURES: %s" % (len(fails), fails)))
sys.exit(1 if fails else 0)
