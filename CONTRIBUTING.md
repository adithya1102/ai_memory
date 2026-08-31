# Contributing to AI Memory

Thanks for looking. This is a small, deliberately dependency-light project;
the bar for a change is that it works, it is tested, and someone reading the
code in a year can tell *why* it is written that way.

The most valuable contribution right now is **a new provider adapter** — the
architecture was built for it, and there is a worked example below.

## Getting set up

```bash
git clone https://github.com/adithya1102/ai_memory.git
cd ai_memory
pip install -r requirements.txt
python backend/main.py
```

Python 3.10 or newer. `sentence-transformers` and `sqlite-vec` are only needed
for semantic search; without them everything else still works and the app says
so in Settings. If you are not touching embeddings, `pip install flask
pywebview` is enough.

There is a `sample_conversations.json` in the repo — import it and you have a
working library in about ten seconds, without exporting your own history.

## Project structure

```
backend/
├── core/
│   ├── database.py     Schema, FTS5 triggers, all SQL. No business logic.
│   ├── embeddings.py   Chunking, the encoder, vector storage and KNN.
│   ├── importer.py     Import orchestration + chain detection.
│   └── search.py       Query building, ranking, keyword/semantic fusion.
├── mcp/
│   ├── server.py       MCP transports. Protocol only, no tool logic.
│   └── tools.py        The three MCP tools. Calls into core/, never duplicates it.
├── providers/
│   └── chatgpt_importer.py    One file per provider. Parsing lives here.
├── web/
│   ├── app.py          Flask routes. Thin — it calls into core/.
│   ├── templates/      Jinja2. No build step.
│   └── static/         One stylesheet. No framework.
└── main.py             Entry point: Flask thread + pywebview window.

tests/                  Five suites, plain scripts, no framework.
```

The rule that keeps this navigable: **`providers/` is the only place that
knows what a vendor's export looks like.** Everything downstream sees the
universal format. If you find yourself writing `if provider == "chatgpt"`
outside `providers/`, something has gone wrong.

## Adding a provider adapter

An adapter does one job: turn a vendor's export file into `insert_conversation`
and `insert_message` calls. It never touches search, chains, or embeddings —
those come for free once the data is in.

### The contract

One function:

```python
def import_<provider>_export(conn, json_file_path) -> dict
```

It must:

1. Register the provider with `db.insert_provider(conn, name, display_name)`
2. Give every conversation a **stable, deterministic** id, prefixed with the
   provider name (`"claude:" + original_id`). Stability is what makes
   re-import idempotent.
3. Compute a **content hash** over the transcript, for deduplication.
4. Return a stats dict with `inserted`, `updated`, `unchanged`, `duplicate`,
   `messages` and `skipped`.
5. Never raise because of one bad conversation — count it in `skipped` and
   keep going. Exports are messy and a 4,000-conversation import should not
   die on record 12.

### Worked example

`backend/providers/claude_importer.py`:

```python
"""Importer for Claude conversation exports."""

import hashlib
import json
import os

from backend.core import database as db

PROVIDER_NAME = "claude"
PROVIDER_DISPLAY_NAME = "Claude"
ID_PREFIX = "claude:"
KEPT_ROLES = ("human", "assistant")

# The universal format only knows "user" and "assistant".
ROLE_MAP = {"human": "user", "assistant": "assistant"}


def _content_hash(messages):
    digest = hashlib.sha256()
    for message in messages:
        digest.update(message["role"].encode("utf-8"))
        digest.update(b"\x00")
        digest.update(message["content"].encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


def import_claude_export(conn, json_file_path):
    with open(json_file_path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)

    provider_id = db.insert_provider(conn, PROVIDER_NAME, PROVIDER_DISPLAY_NAME)
    conn.commit()

    stats = {"inserted": 0, "updated": 0, "unchanged": 0, "duplicate": 0,
             "messages": 0, "skipped": 0}

    for index, raw in enumerate(payload):
        try:
            raw_id = raw.get("uuid") or raw.get("id")
            if not raw_id:
                stats["skipped"] += 1
                continue

            # Flatten the vendor shape into role/content/timestamp.
            messages = []
            for entry in raw.get("chat_messages", []):
                role = ROLE_MAP.get(entry.get("sender"))
                text = (entry.get("text") or "").strip()
                if role in ("user", "assistant") and text:
                    messages.append({"role": role, "content": text,
                                     "timestamp": entry.get("created_at")})

            if not messages:
                stats["skipped"] += 1     # nothing searchable in it
                continue

            outcome = db.insert_conversation(
                conn,
                conversation_id=ID_PREFIX + str(raw_id),
                provider_id=provider_id,
                title=(raw.get("name") or "").strip() or "Untitled conversation",
                created_at=raw.get("created_at"),
                updated_at=raw.get("updated_at") or raw.get("created_at"),
                metadata={"original_id": str(raw_id),
                          "source_file": os.path.basename(json_file_path)},
                content_hash=_content_hash(messages),
            )
            stats[outcome] += 1

            # Only these two outcomes own a row that needs messages written.
            if outcome in ("inserted", "updated"):
                db.delete_messages(conn, ID_PREFIX + str(raw_id))
                for order, message in enumerate(messages):
                    db.insert_message(
                        conn,
                        conversation_id=ID_PREFIX + str(raw_id),
                        role=message["role"],
                        content=message["content"],
                        timestamp=message["timestamp"],
                        message_order=order,
                    )
                stats["messages"] += len(messages)

            if index % 200 == 0:
                conn.commit()
        except Exception:
            stats["skipped"] += 1   # one bad record must not kill the import

    conn.commit()
    return stats
```

Then register it in `backend/core/importer.py`:

```python
importers = {
    "chatgpt": import_chatgpt_export,
    "claude": import_claude_export,      # <- add this
}
```

That is the whole integration. FTS indexing, embeddings, chain detection and
deduplication all apply automatically, because they operate on the universal
tables rather than on anything provider-shaped.

### What actually makes this hard

The contract is easy; the parsing is not. Things worth checking in any export
format before you trust it:

- **Is the conversation a tree?** ChatGPT's is — every regeneration branches
  it, and naively reading all nodes yields abandoned replies interleaved with
  real ones. See `_node_messages()` for how the active branch is recovered.
- **Which messages are hidden?** Exports often contain system scaffolding,
  tool calls, and messages flagged invisible. They pollute search results.
- **What is `content` really?** It is rarely a plain string. Expect nested
  parts, multimodal payloads with images, and non-text tool output.
- **Are the ids stable between exports?** If a vendor mints new ids each time,
  id matching alone will duplicate everything. The content-hash fallback in
  `insert_conversation()` exists for exactly this case — test it.

Write a fixture that includes the ugly cases, not just the happy path.
`tests/fixtures/edge_cases_export.json` is the ChatGPT one: it has a branched
conversation, a hidden message, multimodal parts, an empty conversation and a
legacy flat format, all deliberately.

## Running tests

```bash
python tests/run_all.py           # everything
python tests/test_core.py         # one suite
```

332 checks across five suites. All of them use temporary databases and never
touch `data/`, so you cannot lose a real library by running them.

| Suite | Covers |
|---|---|
| `test_core.py` | Import edge cases, FTS search, chains, every route |
| `test_dedup_and_chains.py` | Re-import deduplication, chain detection, stopwords, UI |
| `test_semantic.py` | Chunking, embedding, incremental re-embedding, hybrid ranking |
| `test_preload.py` | Background model loading, the loading banner, fallback |
| `test_mcp.py` | MCP tools, the stdio JSON-RPC protocol, TCP transport, degradation |

The semantic suites skip themselves with a note when sentence-transformers is
not installed, so a keyword-only checkout still runs a clean suite.

There is no pytest dependency on purpose: the suites are plain scripts that
print `ok` / `FAIL` lines and exit non-zero on failure. If you add a suite,
follow that shape and add it to the list in `tests/run_all.py`.

### Testing guidance

A few things this project has learned the hard way, all of which came from a
test failing and the *test* turning out to be wrong:

- **Assert on the thing you actually mean.** After editing a message,
  searching for the deleted text still returns the conversation — because
  semantic search legitimately matches on meaning. The right assertion was
  about the keyword index, not about the result list.
- **Test the boundary, not just the middle.** A similarity of exactly 0.5
  against a `> 0.5` threshold is where the interesting bug was.
- **Feed it hostile input.** `test_core.py` throws `"`, `NEAR(`, `((` and
  friends at the search box, because unescaped FTS5 syntax is a crash.

## Code style

No linter is enforced. Match the surrounding code:

- **PEP 8**, 4-space indent, ~79 column soft limit.
- **Docstrings on modules and non-obvious functions.** Say what it does and,
  more importantly, why it is done that way.
- **Comments explain decisions, not mechanics.** `# increment the counter` is
  noise. `# Appending is cheap; rebuilding the whole document per message
  would be quadratic on import.` is the reason someone does not "simplify" it
  next year.
- **Plain SQL in `database.py`**, parameterised. No ORM.
- **`%`-formatting** for strings, matching the existing code.
- **No new runtime dependencies** without a good reason. The dependency list
  is short deliberately, and every addition is something a user has to install
  to read their own chat logs.
- **Degrade, do not crash.** Optional functionality that cannot run should
  report why and let the rest of the app work. Semantic search has four
  distinct unavailable states and every one of them falls back to keyword
  search.

## Pull requests

- One logical change per PR.
- Run `python tests/run_all.py` before pushing and say what it printed.
- Add tests for the behaviour you changed. If you fixed a bug, add the test
  that would have caught it.
- Write a commit message that explains the *why*. The diff shows the what.
- Update the README if behaviour a user can see has changed.

## Reporting bugs

Use the [bug report template](.github/ISSUE_TEMPLATE/bug_report.md). The
single most useful thing you can include is a **minimal export file that
reproduces it** — with anything private removed. Export formats vary in ways
that are hard to guess at from a description.
