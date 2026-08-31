"""Claude and Gemini adapters, provider sniffing, and cross-provider search."""
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.core import database as db
from backend.core.importer import detect_provider, import_file
from backend.core.search import hybrid_search, search_conversations

DEMO = os.path.join(ROOT, "docs", "demo_data")
WORK = tempfile.mkdtemp(prefix="aimem-providers-")

fails = []


def check(label, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + label + (("  -> " + str(detail)) if detail else ""))
    if not ok:
        fails.append(label)


print("== provider detection ==")
for name, expected in [("chatgpt_export.json", "chatgpt"),
                       ("claude_export.json", "claude"),
                       ("gemini_export.json", "gemini")]:
    got = detect_provider(os.path.join(DEMO, name))
    check("%-22s -> %s" % (name, expected), got == expected, got)
check("existing ChatGPT fixture still detected as chatgpt",
      detect_provider(os.path.join(ROOT, "dummy_export.json.json")) == "chatgpt")
check("edge-case fixture still detected as chatgpt",
      detect_provider(os.path.join(ROOT, "tests", "fixtures",
                                   "edge_cases_export.json")) == "chatgpt")
bad = os.path.join(WORK, "bad.json")
open(bad, "w", encoding="utf-8").write("{not json")
check("unparseable file falls back to chatgpt", detect_provider(bad) == "chatgpt")

print("\n== import all three ==")
conn = db.init_db(os.path.join(WORK, "p.db"))
stats = {}
for name in ("chatgpt_export.json", "claude_export.json", "gemini_export.json"):
    stats[name] = import_file(conn, os.path.join(DEMO, name))
check("chatgpt: 4 conversations", stats["chatgpt_export.json"]["inserted"] == 4)
check("claude: 2 conversations", stats["claude_export.json"]["inserted"] == 2)
check("gemini: 2 conversations", stats["gemini_export.json"]["inserted"] == 2)
check("provider reported in stats",
      [s["provider"] for s in stats.values()] == ["chatgpt", "claude", "gemini"])
check("three providers registered",
      conn.execute("SELECT COUNT(*) FROM providers").fetchone()[0] == 3)
check("8 conversations total",
      conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 8)

print("\n== role and field mapping ==")
check("claude 'human' mapped to 'user'",
      conn.execute("SELECT role FROM messages WHERE conversation_id='claude:cl-001' "
                   "ORDER BY message_order LIMIT 1").fetchone()[0] == "user")
check("gemini 'model' mapped to 'assistant'",
      conn.execute("SELECT role FROM messages WHERE conversation_id='gemini:gm-001' "
                   "ORDER BY message_order").fetchall()[1][0] == "assistant")
check("no unmapped roles survive",
      conn.execute("SELECT COUNT(*) FROM messages WHERE role NOT IN "
                   "('user','assistant')").fetchone()[0] == 0)
check("claude ids namespaced",
      conn.execute("SELECT COUNT(*) FROM conversations WHERE id LIKE 'claude:%'"
                   ).fetchone()[0] == 2)
check("gemini ids namespaced",
      conn.execute("SELECT COUNT(*) FROM conversations WHERE id LIKE 'gemini:%'"
                   ).fetchone()[0] == 2)
check("claude title read from 'name'",
      db.get_conversation(conn, "claude:cl-001")["title"]
      == "Gusto demand model architecture")
check("gemini title read from 'title'",
      db.get_conversation(conn, "gemini:gm-001")["title"]
      == "Weekly gym routine for endurance")

print("\n== cross-provider search ==")
gym = hybrid_search(conn, "gym routine")
providers = {r["provider_name"] for r in gym}
check("'gym routine' spans ChatGPT and Gemini",
      {"ChatGPT", "Gemini"} <= providers, sorted(providers))
check("...and Claude has nothing to offer on it (it was never asked)",
      "Claude" not in providers, sorted(providers))
gusto = hybrid_search(conn, "Gusto forecasting")
gusto_providers = {r["provider_name"] for r in gusto[:2]}
check("'Gusto forecasting' finds ChatGPT and Claude",
      gusto_providers == {"ChatGPT", "Claude"}, sorted(gusto_providers))
check("keyword search works on imported Claude text",
      any(r["title"] == "Gusto demand model architecture"
          for r in search_conversations(conn, "pgbouncer OR covers")) or
      any(r["title"] == "Gusto demand model architecture"
          for r in search_conversations(conn, "covers")),
      [r["title"] for r in search_conversations(conn, "covers")])
check("keyword search works on imported Gemini text",
      any("endurance" in (r["title"] or "").lower()
          for r in search_conversations(conn, "intervals")),
      [r["title"] for r in search_conversations(conn, "intervals")])

print("\n== cross-provider chain ==")
chains = {c["name"]: db.get_chain(conn, c["id"]) for c in db.get_chains(conn)}
gusto_chain = next((c for c in chains.values()
                    if any("Gusto" in m["title"] for m in c["conversations"])), None)
check("a Gusto chain exists", gusto_chain is not None)
check("it spans two providers",
      gusto_chain and {m["provider_name"] for m in gusto_chain["conversations"]}
      == {"ChatGPT", "Claude"},
      gusto_chain and [m["provider_name"] for m in gusto_chain["conversations"]])
check("ordered oldest first",
      gusto_chain and [m["position"] for m in gusto_chain["conversations"]] == [0, 1])
check("chain ids start at 1 after a rebuild",
      min(c["id"] for c in db.get_chains(conn)) == 1,
      [c["id"] for c in db.get_chains(conn)])

print("\n== re-import deduplicates per provider ==")
for name in ("chatgpt_export.json", "claude_export.json", "gemini_export.json"):
    again = import_file(conn, os.path.join(DEMO, name))
    check("%-22s re-import inserts nothing" % name, again["inserted"] == 0, again)
check("still 8 conversations",
      conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 8)
check("chain ids still start at 1",
      min(c["id"] for c in db.get_chains(conn)) == 1,
      [c["id"] for c in db.get_chains(conn)])

print("\n== malformed provider exports ==")
cases = {
    "claude_bad.json": [
        {"uuid": "ok", "name": "Fine", "chat_messages": [
            {"sender": "human", "text": "hello there"}]},
        {"uuid": "empty", "name": "Empty", "chat_messages": []},
        {"name": "No id", "chat_messages": [{"sender": "human", "text": "x"}]},
        "not a dict",
        {"uuid": "blocks", "name": "Block content", "chat_messages": [
            {"sender": "human", "content": [{"type": "text", "text": "block form"}]}]},
    ],
    "gemini_bad.json": [
        {"conversation_id": "ok", "title": "Fine",
         "messages": [{"author": "user", "text": "hello there"}]},
        {"conversation_id": "empty", "title": "Empty", "messages": []},
        {"title": "No id", "messages": [{"author": "user", "text": "x"}]},
        42,
        {"conversation_id": "alt", "title": "Alt keys",
         "turns": [{"role": "user", "content": "alternate field names"}]},
    ],
}
conn2 = db.init_db(os.path.join(WORK, "bad.db"))
for name, payload in cases.items():
    path = os.path.join(WORK, name)
    json.dump(payload, open(path, "w", encoding="utf-8"))
    result = import_file(conn2, path)
    check("%-18s imports the good records" % name, result["inserted"] == 2, result)
    check("%-18s dedup does not reach across providers" % name,
          result["duplicate"] == 0, result)
    check("%-18s skips the bad ones without raising" % name,
          result["skipped"] == 3, result)
check("claude block-form content extracted",
      "block form" in (db.get_messages(conn2, "claude:blocks")[0]["content"]))
check("gemini alternate field names handled",
      "alternate field names" in (db.get_messages(conn2, "gemini:alt")[0]["content"]))
conn2.close()
conn.close()

print("\n" + ("ALL CHECKS PASSED" if not fails
              else "%d FAILURES: %s" % (len(fails), fails)))
sys.exit(1 if fails else 0)
