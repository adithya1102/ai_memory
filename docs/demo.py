"""Runs the walkthrough in docs/demo.md and prints its output.

    python docs/demo.py

Uses a temporary database, so it never touches your real library. Every block
of output quoted in demo.md is produced by this script -- if the two ever
disagree, this one is right.
"""

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.core import database as db
from backend.core.importer import detect_provider, import_file
from backend.core.search import hybrid_search
from backend.mcp import tools as mcp_tools

DEMO_DATA = os.path.join(ROOT, "docs", "demo_data")
EXPORTS = ["chatgpt_export.json", "claude_export.json", "gemini_export.json"]


def rule(title):
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


def show_results(conn, query, limit=5):
    print("\n$ search %r" % query)
    results = hybrid_search(conn, query, limit=limit)
    if not results:
        print("  (no results)")
        return results
    for row in results:
        print("\n  %-38s [%s]%s" % (
            (row["title"] or "Untitled")[:38],
            row["match_label"],
            ("  %d%% similar" % round(row["similarity"] * 100))
            if row.get("similarity") else ""))
        print("  %s · %s" % (row["provider_name"], (row["created_at"] or "")[:10]))
        snippet = " ".join((row["snippet"] or "").split())[:150]
        print("  \"%s\"" % snippet)
    return results


def main():
    work = tempfile.mkdtemp(prefix="aimem-demo-")
    db_path = os.path.join(work, "demo.db")
    conn = db.init_db(db_path)

    rule("1. THE PROBLEM — three assistants, three separate memories")
    for name in EXPORTS:
        path = os.path.join(DEMO_DATA, name)
        print("  %-22s detected as %s" % (name, detect_provider(path)))

    rule("2. THE IMPORT")
    for name in EXPORTS:
        stats = import_file(conn, os.path.join(DEMO_DATA, name))
        print("  %-22s provider=%-8s new=%d messages=%d chains=%d"
              % (name, stats["provider"], stats["inserted"],
                 stats["messages"], stats["chains"]))

    total = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
    print("\n  Library now holds %d conversations from %d providers:" % (
        total, conn.execute("SELECT COUNT(*) FROM providers").fetchone()[0]))
    for row in conn.execute(
            "SELECT p.display_name, COUNT(c.id) n FROM providers p "
            "LEFT JOIN conversations c ON c.provider_id = p.id "
            "GROUP BY p.id ORDER BY p.display_name"):
        print("    %-10s %d" % (row[0], row[1]))

    rule("3. THE SEARCH")
    show_results(conn, "gym routine")
    show_results(conn, "how do I get stronger", limit=2)
    show_results(conn, "Gusto forecasting", limit=3)

    rule("4. THE MCP TOOLS — what an assistant receives")
    payload = mcp_tools.search_memory(conn, "gym routine", limit=3)
    print("\n  search_memory(query='gym routine', limit=3)")
    for row in payload["results"]:
        print("    %-38s %-9s %s"
              % (row["title"][:38], row["match_type"], row["provider"]))

    rule("5. THE CHAINS")
    for chain in db.get_chains(conn):
        detail = db.get_chain(conn, chain["id"])
        print("\n  Chain %d: %s (%d conversations)"
              % (chain["id"], chain["name"], chain["size"]))
        for member in detail["conversations"]:
            print("    %d. %-38s %s · %s"
                  % (member["position"] + 1, member["title"][:38],
                     member["provider_name"], (member["created_at"] or "")[:10]))

    print("\n" + "=" * 70)
    print("Demo database: %s" % db_path)
    print("Your own library was not touched.")
    conn.close()


if __name__ == "__main__":
    main()
