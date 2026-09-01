# Bridge API

The Bridge API is a REST layer over the same SQLite library the desktop UI and
the MCP server use. Anything written through it is searchable from the UI and
from Claude Desktop immediately — there is one database, not three.

It exists for the callers that cannot speak MCP: a browser extension watching
a chat tab, a proxy sitting in front of a provider, a shell script, a cron job,
another process on the same machine.

- **Base URL** — `http://127.0.0.1:5000/api/v1`
- **Content type** — `application/json` on every request with a body
- **Errors** — always `{"error": "<what went wrong>"}` with a 4xx or 5xx status

The server runs as part of the desktop app. Start it with `python backend/main.py`
(add `--no-window` to run headless).

## Authentication

Off by default. The server binds to loopback, so on a single-user desktop a key
adds friction without an attacker to stop.

Turn it on in **Settings → Bridge API**. A key is generated if you do not supply
one. Once enabled, every endpoint except `/health` requires the header:

```
X-API-Key: <your key>
```

| Case | Status |
|---|---|
| Auth off | all requests allowed |
| Valid key | `200` |
| Missing or wrong key | `401` |
| Auth on but no key configured | `503` — fails closed rather than open |

`/health` is deliberately unauthenticated so a monitor can reach it.

The API writes as well as reads. Treat the key as a password for your entire
conversation archive, and do not publish this port beyond loopback without one.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | liveness and library counts |
| `POST` | `/conversations` | store one normalised conversation |
| `GET` | `/conversations/<id>` | full transcript |
| `POST` | `/messages` | append turns to an existing conversation |
| `GET` | `/search?q=&limit=` | hybrid keyword + semantic search |
| `GET` | `/chains/<id>` | one conversation chain |
| `POST` | `/ingest` | accept data in any recognised shape |
| `POST` | `/memories` | save a curated fact |
| `GET` | `/memories` | list curated facts |
| `DELETE` | `/memories/<id>` | delete one |

---

### `GET /health`

```bash
curl http://127.0.0.1:5000/api/v1/health
```

```json
{
  "status": "ok",
  "service": "contextvault-bridge",
  "api_version": "1.0",
  "database": "contextvault.db",
  "conversations": 21,
  "messages": 1462,
  "memories": 3,
  "semantic_search": true,
  "auth_required": false
}
```

---

### `POST /conversations`

Stores one conversation you have already normalised — you know the roles and
the text.

| Field | Required | Notes |
|---|---|---|
| `messages` | yes | array of `{role, content, timestamp?}`; must be non-empty |
| `title` | no | defaults to `"Untitled conversation"` |
| `provider` / `source` | no | defaults to `manual`; creates the provider if new |
| `conversation_id` | no | generated as `<source>:<uuid4>` if omitted |
| `created_at`, `updated_at` | no | ISO 8601; default now |
| `metadata` | no | any JSON object or array |
| `embed` | no | `true` runs an embedding pass before returning |

`role` must be `user`, `assistant` or `system`. `human` is accepted and mapped
to `user`, since that is what Claude exports call it.

```bash
curl -X POST http://127.0.0.1:5000/api/v1/conversations \
  -H 'Content-Type: application/json' \
  -d '{
        "title": "Bridge API design",
        "provider": "manual",
        "messages": [
          {"role": "user", "content": "Should the bridge accept partial conversations?"},
          {"role": "assistant", "content": "Yes — POST /messages appends turn by turn."}
        ]
      }'
```

```json
{
  "conversation_id": "manual:0f3c...",
  "provider": "manual",
  "outcome": "inserted",
  "message_count": 2
}
```

`outcome` is `inserted`, `updated`, `unchanged` or `duplicate` — the same
dedup logic imports use, so posting the same transcript twice does not create
two copies.

**Indexing:** keyword search sees the conversation immediately, because the
FTS triggers fire on insert. Semantic search needs an embedding pass, which is
slow enough to be opt-in — pass `"embed": true`, use `/ingest`, or run a
rebuild from Settings.

---

### `GET /conversations/<id>`

```bash
curl http://127.0.0.1:5000/api/v1/conversations/chatgpt:conv-004
```

Returns the transcript in the same shape the MCP `get_conversation` tool
returns: `conversation_id`, `title`, `provider`, timestamps, `chains`, and
`messages[]` in order. `404` if there is no such id.

---

### `POST /messages`

Appends to a conversation that already exists — the streaming path, for a
client that posts each turn as it happens.

Takes either one message inline or a batch:

```bash
curl -X POST http://127.0.0.1:5000/api/v1/messages \
  -H 'Content-Type: application/json' \
  -d '{
        "conversation_id": "manual:0f3c...",
        "role": "user",
        "content": "One more thought before I forget it."
      }'
```

```json
{"conversation_id": "manual:0f3c...", "appended": 1, "message_count": 3}
```

Ordering continues from the highest existing `message_order`. The
conversation's content hash is recomputed on every append — otherwise a later
re-import would look unchanged and skip re-embedding the new turns. `404` if
the conversation does not exist.

---

### `GET /search`

| Parameter | Default | Notes |
|---|---|---|
| `q` | required | the query |
| `limit` | `10` | integer |

```bash
curl 'http://127.0.0.1:5000/api/v1/search?q=sourdough&limit=5'
```

```json
{
  "query": "sourdough",
  "count": 2,
  "results": [
    {
      "conversation_id": "chatgpt:conv-004",
      "title": "Sourdough starter troubleshooting",
      "provider": "ChatGPT",
      "date": "2024-08-26T02:26:40+00:00",
      "snippet": "My sourdough starter isn't rising...",
      "relevance_score": 0.032787,
      "match_type": "both"
    }
  ]
}
```

`match_type` is `keyword`, `semantic` or `both`. Identical to what MCP returns
for the same query — same ranking, same Reciprocal Rank Fusion.

---

### `GET /chains/<id>`

```bash
curl http://127.0.0.1:5000/api/v1/chains/1
```

Returns `chain_id`, `name`, `size` and the member conversations in order.
`404` if there is no such chain.

---

### `POST /ingest`

The webhook-shaped entry point: hand it what you have and let it work out the
format. Three shapes are recognised.

**1. A raw provider export.** The same JSON as a ChatGPT, Claude or Gemini
download, including a bare top-level array. The shape is sniffed and routed to
that provider's adapter, so tree-walking and content-block flattening are not
reimplemented here. Chains and embeddings both run afterwards.

```bash
curl -X POST http://127.0.0.1:5000/api/v1/ingest \
  -H 'Content-Type: application/json' \
  --data-binary @conversations.json
```

```json
{
  "format": "provider-export",
  "source": "claude",
  "inserted": 16, "updated": 0, "unchanged": 0, "duplicate": 0,
  "messages": 1204, "skipped": 0,
  "chains": 3, "embedded": 16
}
```

**2. A batch of normalised conversations.**

```bash
curl -X POST http://127.0.0.1:5000/api/v1/ingest \
  -H 'Content-Type: application/json' \
  -d '{"source": "extension",
       "conversations": [
         {"title": "A", "messages": [{"role": "user", "content": "alpha"}]},
         {"title": "B", "messages": [{"role": "user", "content": "beta"}]}
       ]}'
```

**3. A single normalised conversation** — the same body `POST /conversations`
takes, with an optional `source`.

`source` may be `chatgpt`, `claude`, `gemini`, `manual` or `extension`. Supply
it to override detection; omit it and the shape decides. Anything else is
rejected with a `400` naming the valid values.

---

### Memories

A memory is a conclusion, not a transcript: *"prefers Postgres over MySQL"*,
not the four messages that established it. This is where an assistant puts
what it decided is worth keeping.

**`POST /memories`**

| Field | Required | Notes |
|---|---|---|
| `content` | yes | the fact |
| `source` | no | who curated it |
| `tags` | no | array of strings |
| `conversation_id` | no | link to where it came from; `404` if unknown |

```bash
curl -X POST http://127.0.0.1:5000/api/v1/memories \
  -H 'Content-Type: application/json' \
  -d '{"content": "Gusto POS is the least certain of the three product lines.",
       "source": "claude",
       "tags": ["gusto", "risk"]}'
```

```json
{
  "id": 1,
  "content": "Gusto POS is the least certain of the three product lines.",
  "source": "claude",
  "tags": ["gusto", "risk"],
  "conversation_id": null,
  "created_at": "2026-09-01T14:02:11+00:00",
  "updated_at": "2026-09-01T14:02:11+00:00"
}
```

**`GET /memories`** — newest first. Optional `limit` (default 50, capped at
500), `source`, `conversation_id`.

```bash
curl 'http://127.0.0.1:5000/api/v1/memories?source=claude&limit=20'
```

**`DELETE /memories/<id>`** — `{"deleted": 1}`, or `404` if already gone.

Deleting a conversation does not delete memories drawn from it; the link is set
to null instead. Losing the transcript should not silently discard the
conclusion.

## Status codes

| Code | Meaning |
|---|---|
| `200` | fine |
| `201` | created — every successful `POST` |
| `400` | your request was malformed; the message says how |
| `401` | auth is on and the key was missing or wrong |
| `404` | no such conversation, chain, memory or route |
| `405` | wrong method for that path |
| `500` | a bug — please report it with the message |
| `503` | auth is on but no key is configured |
