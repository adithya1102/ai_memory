"""Semantic search tests: chunking, incremental embedding, hybrid ranking."""
import json, os, sys, tempfile, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from backend.core import database as db
from backend.core import embeddings
from backend.core.importer import import_file
from backend.core.search import (SEMANTIC_ENABLED_KEY, hybrid_search,
                                 search_conversations)

EXPORT = os.path.join(ROOT, "dummy_export.json.json")
DB = os.path.join(tempfile.mkdtemp(), "sem.db")

fails = []
def check(label, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + label + (("  -> " + str(detail)) if detail else ""))
    if not ok:
        fails.append(label)

def titles(results):
    return [r["title"] for r in results]

print("== availability ==")
ok, reason = embeddings.availability()
check("semantic stack available", ok, reason)

print("\n== chunking ==")
long_msg = [{"role": "user", "content": " ".join("word%d" % i for i in range(400))}]
chunks = embeddings.chunk_messages(long_msg)
check("long message split into overlapping chunks", len(chunks) > 1, len(chunks))
check("chunk indices sequential",
      [c["chunk_index"] for c in chunks] == list(range(len(chunks))))
check("chunks overlap",
      chunks[0]["content"].split()[-1] in chunks[1]["content"].split(),
      chunks[0]["content"].split()[-5:])
two = embeddings.chunk_messages([{"role": "user", "content": "alpha beta"},
                                 {"role": "assistant", "content": "gamma delta"}])
check("chunks never span messages", len(two) == 2 and "alpha" not in two[1]["content"])
check("roles preserved", [c["role"] for c in two] == ["user", "assistant"])
check("empty messages produce no chunks",
      embeddings.chunk_messages([{"role": "user", "content": "   "}]) == [])

# Every chunk must fit the encoder window or its tail is silently ignored.
model = embeddings.get_model()
tok = model.tokenizer
worst = max(len(tok.encode(c["content"], add_special_tokens=True)) for c in chunks)
check("chunks fit inside the %d-token model window" % model.max_seq_length,
      worst <= model.max_seq_length, "worst chunk = %d tokens" % worst)

print("\n== import + embed ==")
conn = db.init_db(DB)
t = time.time()
stats = import_file(conn, EXPORT)
print("   import: %s  (%.1fs)" % ({k: stats[k] for k in
      ("inserted", "chains", "embedded", "embedding_note")}, time.time() - t))
check("5 conversations imported", stats["inserted"] == 5, stats["inserted"])
check("5 conversations embedded on import", stats["embedded"] == 5, stats["embedded"])
check("no embedding warning", stats["embedding_note"] is None, stats["embedding_note"])
check("chunks stored", conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 12,
      conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
check("vectors match chunks",
      conn.execute("SELECT COUNT(*) FROM chunk_vectors").fetchone()[0] ==
      conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
check("embeddings marked as available", embeddings.has_embeddings(conn))

print("\n== THE acceptance test ==")
res = hybrid_search(conn, "how do I get stronger")
for r in res[:3]:
    print("     %-34s %-9s sim=%s" % (r["title"][:34], r["match_label"], r["similarity"]))
check("'how do I get stronger' finds Welcome", res and res[0]["title"] == "Welcome",
      titles(res))
check("...and it is a semantic match", res and "semantic" in res[0]["match_types"],
      res[0]["match_types"] if res else None)
check("...which keyword search alone cannot find",
      search_conversations(conn, "how do I get stronger") == [],
      titles(search_conversations(conn, "how do I get stronger")))

print("\n== more paraphrase queries ==")
for query, expected in [("bread dough won't rise", "Sourdough starter troubleshooting"),
                        ("predicting restaurant demand", "Project discussion"),
                        ("how much protein for muscle", "Welcome")]:
    res = hybrid_search(conn, query)
    check("%-30r -> %s" % (query, expected),
          res and res[0]["title"] == expected, titles(res)[:3])

print("\n== hybrid merge ==")
res = hybrid_search(conn, "gym workout")
check("keyword hit still found", res and res[0]["title"] == "Welcome", titles(res))
check("hit found by both engines is labelled 'both'",
      res and res[0]["match_label"] == "both", res[0]["match_label"] if res else None)
check("no duplicate conversations in results",
      len({r["conversation_id"] for r in res}) == len(res))
check("every result carries a badge",
      all(r["match_label"] in ("keyword", "semantic", "both") for r in res))
check("badge matches match_types",
      all((r["match_label"] == "both") == (len(r["match_types"]) > 1) for r in res))
check("every result has a snippet", all((r.get("snippet") or "").strip() for r in res))
kw_only = hybrid_search(conn, "sourdough", semantic=False)
check("semantic=False gives keyword-only badges",
      all(r["match_label"] == "keyword" for r in kw_only), [r["match_label"] for r in kw_only])

print("\n== toggle ==")
check("default is on", db.get_flag(conn, SEMANTIC_ENABLED_KEY, default=True))
db.set_flag(conn, SEMANTIC_ENABLED_KEY, False)
check("toggle persists off", db.get_flag(conn, SEMANTIC_ENABLED_KEY) is False)
off = hybrid_search(conn, "how do I get stronger")
check("disabled -> paraphrase no longer found", off == [], titles(off))
db.set_flag(conn, SEMANTIC_ENABLED_KEY, True)
check("toggle persists on", db.get_flag(conn, SEMANTIC_ENABLED_KEY) is True)
check("re-enabled -> paraphrase found again",
      hybrid_search(conn, "how do I get stronger")[0]["title"] == "Welcome")

print("\n== incremental embedding ==")
again = embeddings.sync_embeddings(conn)
check("nothing re-embedded when unchanged", again["conversations"] == 0, again)
check("pending list empty", embeddings.pending_conversations(conn) == [],
      len(embeddings.pending_conversations(conn)))

data = json.load(open(EXPORT, encoding="utf-8"))
data[0]["mapping"]["node-2"]["message"]["content"]["parts"] = [
    "Swim freestyle laps three times a week to build cardiovascular endurance."]
edited = os.path.join(os.path.dirname(DB), "edited.json")
json.dump(data, open(edited, "w", encoding="utf-8"))
before = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
s2 = import_file(conn, edited)
check("only the changed conversation re-embedded", s2["embedded"] == 1, s2["embedded"])
check("chunk count stayed sane",
      abs(conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] - before) <= 2)
check("no orphaned vectors",
      conn.execute("SELECT COUNT(*) FROM chunk_vectors WHERE rowid NOT IN "
                   "(SELECT id FROM chunks)").fetchone()[0] == 0)
check("new content semantically searchable",
      hybrid_search(conn, "swimming for fitness")[0]["title"] == "Welcome",
      titles(hybrid_search(conn, "swimming for fitness"))[:2])
# The replaced *text* must be gone from both indexes.  Semantic search can
# still surface Welcome for a query like "bench press", and should: the
# conversation still opens with "build muscle" and "workout plan".  So assert
# on the literal indexes, which is what re-indexing is responsible for.
check("replaced text gone from keyword index",
      search_conversations(conn, "deadlifts overhead press") == [],
      titles(search_conversations(conn, "deadlifts overhead press")))
check("replaced text gone from chunk store",
      conn.execute("SELECT COUNT(*) FROM chunks WHERE content LIKE '%Deadlifts%'"
                   ).fetchone()[0] == 0)
check("new text present in chunk store",
      conn.execute("SELECT COUNT(*) FROM chunks WHERE content LIKE '%freestyle%'"
                   ).fetchone()[0] == 1)

print("\n== no-embeddings fallback ==")
DB2 = os.path.join(tempfile.mkdtemp(), "noembed.db")
conn2 = db.init_db(DB2)
db.set_flag(conn2, SEMANTIC_ENABLED_KEY, False)   # import without embedding
import_file(conn2, EXPORT)
check("no vectors were created", not embeddings.has_embeddings(conn2))
db.set_flag(conn2, SEMANTIC_ENABLED_KEY, True)    # on, but nothing embedded
res2 = hybrid_search(conn2, "gym workout")
check("keyword search still works with zero embeddings",
      res2 and res2[0]["title"] == "Welcome", titles(res2))
check("results are keyword-labelled", all(r["match_label"] == "keyword" for r in res2))
check("paraphrase finds nothing without embeddings",
      hybrid_search(conn2, "how do I get stronger") == [])
check("semantic_search returns [] not an error",
      embeddings.semantic_search(conn2, "anything") == [])
st = embeddings.embedding_stats(conn2)
check("stats report 0 embedded", st["embedded"] == 0 and st["pending"] == 5, st)
conn2.close()

print("\n== unrelated query returns nothing ==")
junk = hybrid_search(conn, "quantum chromodynamics lattice gauge theory")
check("junk query is not force-matched", junk == [], titles(junk))

conn.close()
print("\n" + ("ALL CHECKS PASSED" if not fails else "%d FAILURES: %s" % (len(fails), fails)))
sys.exit(1 if fails else 0)
