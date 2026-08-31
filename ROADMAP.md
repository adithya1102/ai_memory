# Roadmap

Where AI Memory is going. Dates are deliberately absent — this is a
spare-time project and guessing them would just be wrong later.

Anything marked **shipped** is working today and covered by the test suite.
Everything below that is intent, not a promise, and the shape of it will
change as the earlier pieces teach us things.

---

## v0.1 — Core import and keyword search ✅ shipped

The minimum that makes a chat archive useful.

- ChatGPT `conversations.json` import, including the branching message tree
- Universal storage format in a single SQLite file
- Full-text search over titles **and** message bodies (FTS5, bm25-ranked)
- Conversation viewer, search results, settings
- Conversation chains from title similarity
- Idempotent re-import, deduplicating on id and on content hash

## v0.2 — Semantic search with local embeddings ✅ shipped

Finding a conversation when you cannot remember a single word you used.

- Local embeddings via sentence-transformers (all-MiniLM-L6-v2, 384d)
- Vectors in sqlite-vec, in the same database file — no second store
- Token-accurate chunking that respects the encoder's window
- Hybrid keyword + semantic ranking with Reciprocal Rank Fusion
- Incremental embedding: only new or changed conversations are re-encoded
- Background model preloading, with graceful keyword-only fallback

---

## v0.3 — MCP server ✅ shipped

Let an AI agent read your memory, so you stop being the one who has to
remember which session a thing was in.

- Search exposed over MCP, so Claude Desktop and other clients can query the
  library
- Three read-only tools: `search_memory`, `get_conversation`,
  `get_conversation_chain`
- stdio transport for clients that spawn a subprocess, plus an optional
  loopback TCP transport behind a Settings toggle
- Works with or without the official `mcp` SDK installed

**Still open.** Access is currently all-or-nothing: a connected client can
search the whole archive. Scoping — exposing only some conversations, and
letting a user revoke that — is not built, and is the next thing to do here.
Also unresolved: what a good result looks like when the consumer is a model
rather than a person. Ten snippets is probably wrong; one well-chosen
conversation with its chain is probably closer.

## v0.4 — Additional providers

The storage format is already provider-agnostic; nothing else is yet.

- Claude export adapter
- Gemini export adapter
- A documented adapter contract so a new provider is one file
  (see [CONTRIBUTING.md](CONTRIBUTING.md))
- Cross-provider chains: the same topic asked of two different assistants
- Per-provider filters in search

The work is mostly in the parsers. Every vendor exports a different shape,
and the shapes change without notice.

## v0.5 — Android client

Your chat history is most useful when you are away from the machine that
holds it.

- Read-only mobile client over a local network connection
- Search and conversation viewing, no import
- Pairing over LAN, no cloud relay — which is the hard part, and the whole
  point

## v1.0 — Automatic sync where provider APIs permit

Today every update means exporting a zip by hand.

- Poll provider APIs for new conversations where the terms allow it
- Fall back to a watched folder for the ones that never will
- Background incremental sync, reusing the existing content-hash dedup
- Conflict handling for conversations edited after import

To be honest about the constraint: most assistants have no supported API for
reading your own history. This milestone will be partial by nature, and the
watched-folder path may end up being the one that matters.

---

## Not planned

Saying no is part of a roadmap.

- **A hosted or cloud version.** The whole point is that the data does not
  leave your machine.
- **Telemetry or analytics.** Not now, not opt-in, not anonymised.
- **An account system.** There is nothing to log in to.
- **Editing conversations.** This is an archive; it should reflect what was
  actually said.

## Contributing to the roadmap

Open an issue with the
[feature request template](.github/ISSUE_TEMPLATE/feature_request.md). The
most useful thing you can bring is the problem, not the solution — a concrete
case where the tool failed you is worth more than a design.
