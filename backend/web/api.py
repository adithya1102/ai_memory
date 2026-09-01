"""The Bridge API: a REST layer over the same library the UI and MCP use.

The desktop UI is for a person and the MCP server is for an assistant that can
speak MCP.  This is for everything else -- a browser extension watching a chat
tab, a proxy in front of a provider, a shell script, another machine on the
loopback interface.  It reads and writes the same SQLite file, so a
conversation posted here is searchable from Claude Desktop a second later.

Two ways in, and the difference matters:

* ``POST /conversations`` and ``POST /messages`` take *already normalised*
  data.  The caller knows the roles and the text.
* ``POST /ingest`` takes whatever a provider exports.  It sniffs the shape,
  picks the adapter, and normalises for you.

Reads reuse the MCP tool layer rather than reimplementing it, so a search over
HTTP and a search over MCP return the same JSON for the same query.
"""

import json
import os
import tempfile
import uuid

from flask import Blueprint, current_app, g, jsonify, request

from backend.core import database as db
from backend.core.importer import detect_chains, detect_provider, import_file
from backend.mcp import tools as mcp_tools
from backend.mcp.tools import ToolError
from backend.providers import common

API_PREFIX = "/api/v1"
API_VERSION = "1.0"

# Off by default.  The server listens on loopback, so on a single-user desktop
# the key is friction with no attacker to stop.  It exists for the cases that
# are not that: a shared machine, a tunnel, a container with a published port.
API_AUTH_REQUIRED_KEY = "api_auth_required"
API_KEY_KEY = "api_key"

# Sources the bridge understands.  Anything else is stored as given, so an
# unrecognised extension still works.
KNOWN_SOURCES = ("chatgpt", "claude", "gemini", "manual", "extension")

VALID_ROLES = ("user", "assistant", "system")

api = Blueprint("api", __name__, url_prefix=API_PREFIX)


# --------------------------------------------------------------------------
# Plumbing
# --------------------------------------------------------------------------

def connection():
    """Per-request connection, shared with the UI's teardown handler."""
    if "db" not in g:
        g.db = db.get_connection(current_app.config["DB_PATH"])
    return g.db


class ApiError(Exception):
    """A problem the caller can fix, carrying the status to report it with."""

    def __init__(self, message, status=400, **extra):
        super().__init__(message)
        self.message = message
        self.status = status
        self.extra = extra


@api.errorhandler(ApiError)
def _handle_api_error(exc):
    payload = {"error": exc.message}
    payload.update(exc.extra)
    return jsonify(payload), exc.status


@api.errorhandler(ToolError)
def _handle_tool_error(exc):
    # The tool layer raises this for a bad id or a bad argument.  Both are the
    # caller's mistake, and its message already says which.
    return jsonify({"error": str(exc)}), 400


def register_error_handlers(app):
    """Answer 404 and 405 in JSON for API paths.

    A URL that matches no rule is rejected before any blueprint owns the
    request, so a blueprint-level handler never runs for it.  These have to be
    registered on the app, and they defer to Flask's HTML pages for every path
    outside /api/v1 so the UI is unaffected.
    """

    @app.errorhandler(404)
    def _not_found(exc):
        if request.path.startswith(API_PREFIX):
            return jsonify({"error": "No such endpoint: %s. See %s/health."
                                     % (request.path, API_PREFIX)}), 404
        return exc

    @app.errorhandler(405)
    def _method_not_allowed(exc):
        if request.path.startswith(API_PREFIX):
            return jsonify({"error": "%s is not allowed on %s."
                                     % (request.method, request.path)}), 405
        return exc


@api.errorhandler(Exception)
def _handle_unexpected(exc):
    current_app.logger.exception("Bridge API error")
    return jsonify({"error": "Internal error: %s" % exc}), 500


def require_key():
    """Reject the request unless it carries the configured key.

    Enforced for every route except /health, which a monitor must be able to
    reach without credentials.
    """
    if request.endpoint == "api.health":
        return None
    conn = connection()
    if not db.get_flag(conn, API_AUTH_REQUIRED_KEY, default=False):
        return None

    expected = db.get_setting(conn, API_KEY_KEY)
    if not expected:
        # Configured to require a key but no key set: fail closed rather than
        # letting a misconfiguration read as "auth is off".
        return jsonify({"error": "API key auth is enabled but no key is set. "
                                 "Set one in Settings."}), 503

    import hmac
    presented = request.headers.get("X-API-Key", "")
    if not hmac.compare_digest(str(presented), str(expected)):
        return jsonify({"error": "Invalid or missing X-API-Key header."}), 401
    return None


api.before_request(require_key)


def body(allow_list=False):
    """The request's JSON, or a clear error saying why it is not usable.

    ``allow_list`` is for /ingest: a real ChatGPT or Claude export is a
    top-level JSON array, so rejecting arrays would reject the format the
    endpoint exists to accept.
    """
    payload = request.get_json(silent=True)
    if payload is None:
        raise ApiError("Request body must be JSON, and Content-Type must be "
                       "application/json.")
    if isinstance(payload, list):
        if allow_list:
            return payload
        raise ApiError("Request body must be a JSON object. To post a whole "
                       "provider export, use POST /api/v1/ingest.")
    if not isinstance(payload, dict):
        raise ApiError("Request body must be a JSON object.")
    return payload


def normalise_messages(raw, where="messages"):
    """Validate a list of {role, content} into what common.ingest expects."""
    if not isinstance(raw, list) or not raw:
        raise ApiError("%s must be a non-empty array." % where)

    messages = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ApiError("%s[%d] must be an object." % (where, index))
        role = (entry.get("role") or "").strip().lower()
        # "human" is what Claude exports call the person.
        if role == "human":
            role = "user"
        if role not in VALID_ROLES:
            raise ApiError("%s[%d].role must be one of %s (got %r)."
                           % (where, index, ", ".join(VALID_ROLES),
                              entry.get("role")))
        content = entry.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ApiError("%s[%d].content must be a non-empty string."
                           % (where, index))
        messages.append({"role": role, "content": content,
                         "timestamp": entry.get("timestamp")})
    return messages


def provider_for(source):
    """Resolve a source name to a provider row id, creating it if needed."""
    name = (source or "manual").strip().lower() or "manual"
    return name, db.insert_provider(connection(), name, name.title())


def store_conversation(payload, default_source="manual"):
    """Validate and upsert one conversation.  Returns the response body."""
    source, provider_id = provider_for(payload.get("provider")
                                       or payload.get("source")
                                       or default_source)
    messages = normalise_messages(payload.get("messages"))

    conversation_id = payload.get("conversation_id") or payload.get("id")
    if conversation_id is not None and not str(conversation_id).strip():
        raise ApiError("conversation_id may not be blank.")
    if conversation_id is None:
        conversation_id = "%s:%s" % (source, uuid.uuid4())
    conversation_id = str(conversation_id)

    metadata = payload.get("metadata")
    if metadata is not None and not isinstance(metadata, (dict, list)):
        raise ApiError("metadata must be an object or an array.")

    conn = connection()
    stats = common.new_stats(source)
    outcome = common.ingest(
        conn, stats, provider_id,
        conversation_id=conversation_id,
        title=(payload.get("title") or "").strip() or "Untitled conversation",
        messages=messages,
        created_at=payload.get("created_at") or db.utcnow(),
        updated_at=payload.get("updated_at") or payload.get("created_at")
                   or db.utcnow(),
        metadata=metadata,
    )
    conn.commit()
    return {"conversation_id": conversation_id, "provider": source,
            "outcome": outcome, "message_count": len(messages)}


def rehash(conn, conversation_id):
    """Recompute a conversation's content hash from the messages it now has.

    Appending a message changes the transcript, and the hash is what dedup and
    incremental embedding both key off.  Leaving it stale would make a
    re-import look "unchanged" and skip re-embedding the new turn.
    """
    messages = [{"role": m["role"], "content": m["content"]}
                for m in db.get_messages(conn, conversation_id)]
    conn.execute(
        "UPDATE conversations SET content_hash = ?, updated_at = ? WHERE id = ?",
        (common.content_hash(messages), db.utcnow(), conversation_id))
    conn.commit()
    return len(messages)


# --------------------------------------------------------------------------
# Health
# --------------------------------------------------------------------------

@api.route("/health")
def health():
    """Liveness plus enough state to diagnose a misconfigured client."""
    conn = connection()
    from backend.core.search import SEMANTIC_ENABLED_KEY

    stats = db.get_stats(conn)
    return jsonify({
        "status": "ok",
        "service": "contextvault-bridge",
        "api_version": API_VERSION,
        "database": os.path.basename(current_app.config["DB_PATH"]),
        "conversations": stats.get("conversations", 0),
        "messages": stats.get("messages", 0),
        "memories": db.count_memories(conn),
        "semantic_search": db.get_flag(conn, SEMANTIC_ENABLED_KEY, default=True),
        "auth_required": db.get_flag(conn, API_AUTH_REQUIRED_KEY, default=False),
    })


# --------------------------------------------------------------------------
# Conversations and messages
# --------------------------------------------------------------------------

@api.route("/conversations", methods=["POST"])
def create_conversation():
    """Accept one already-normalised conversation.

    Keyword search sees it immediately: the FTS triggers fire on insert.
    Semantic search needs an embedding pass, which is slow enough that it is
    opt-in per call -- pass "embed": true, or run a rebuild from Settings.
    """
    payload = body()
    result = store_conversation(payload)

    if payload.get("embed"):
        from backend.core.importer import embed_new_conversations
        result.update(embed_new_conversations(connection()))
    return jsonify(result), 201


@api.route("/conversations/<path:conversation_id>")
def read_conversation(conversation_id):
    """Full transcript, in the same shape the MCP tool returns."""
    conn = connection()
    # The tool layer raises ToolError for a missing id, which maps to 400.
    # Over HTTP a GET for something that is not there is a 404.
    if db.get_conversation(conn, conversation_id) is None:
        raise ApiError("No conversation with id %r." % conversation_id,
                       status=404)
    return jsonify(mcp_tools.get_conversation(conn, conversation_id))


@api.route("/messages", methods=["POST"])
def append_message():
    """Append one message to an existing conversation.

    This is the streaming path: an extension posts each turn as it happens
    rather than waiting for the conversation to end.
    """
    payload = body()
    conversation_id = payload.get("conversation_id")
    if not isinstance(conversation_id, str) or not conversation_id.strip():
        raise ApiError("conversation_id is required.")

    conn = connection()
    if db.get_conversation(conn, conversation_id) is None:
        raise ApiError("No conversation with id %r. POST /api/v1/conversations "
                       "to create it first." % conversation_id, status=404)

    # Accept one message inline or a batch under "messages".
    raw = payload.get("messages")
    if raw is None:
        raw = [{"role": payload.get("role"), "content": payload.get("content"),
                "timestamp": payload.get("timestamp")}]
    messages = normalise_messages(raw)

    start = conn.execute(
        "SELECT COALESCE(MAX(message_order), -1) + 1 FROM messages "
        "WHERE conversation_id = ?", (conversation_id,)).fetchone()[0]
    for offset, message in enumerate(messages):
        db.insert_message(conn, conversation_id=conversation_id,
                          role=message["role"], content=message["content"],
                          timestamp=message.get("timestamp"),
                          message_order=start + offset)
    total = rehash(conn, conversation_id)

    return jsonify({"conversation_id": conversation_id,
                    "appended": len(messages),
                    "message_count": total}), 201


# --------------------------------------------------------------------------
# Search and chains
# --------------------------------------------------------------------------

@api.route("/search")
def search():
    """Hybrid keyword + semantic search, same ranking as the UI."""
    query = (request.args.get("q") or "").strip()
    if not query:
        raise ApiError("Pass a query as ?q=")
    raw_limit = request.args.get("limit", "10")
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        raise ApiError("limit must be an integer (got %r)." % raw_limit)

    return jsonify(mcp_tools.search_memory(connection(), query, limit=limit))


@api.route("/chains/<int:chain_id>")
def read_chain(chain_id):
    conn = connection()
    if db.get_chain(conn, chain_id) is None:
        raise ApiError("No chain with id %d." % chain_id, status=404)
    return jsonify(mcp_tools.get_conversation_chain(conn, chain_id))


# --------------------------------------------------------------------------
# Ingest: the webhook-shaped entry point
# --------------------------------------------------------------------------

@api.route("/ingest", methods=["POST"])
def ingest():
    """Accept conversation data in whatever shape the caller has.

    Three shapes are recognised:

    1. A raw provider export -- the same JSON as a ChatGPT, Claude or Gemini
       download.  Detected by shape and run through that provider's adapter,
       so the tree-walking and block-flattening logic is not duplicated here.
    2. ``{"conversations": [ ... ]}`` in the normalised shape.
    3. A single normalised conversation object.

    ``source`` in the body overrides detection when the caller knows better.
    """
    payload = body(allow_list=True)
    # A bare array is always a provider export; only an object can declare a
    # source or carry the normalised shape.
    is_array = isinstance(payload, list)
    declared = None
    if not is_array:
        declared = (payload.get("source") or "").strip().lower() or None
        if declared and declared not in KNOWN_SOURCES:
            raise ApiError("Unknown source %r. Known: %s."
                           % (declared, ", ".join(KNOWN_SOURCES)))

    conn = connection()

    # Shape 2 and 3: normalised data, which carries "messages" per record.
    records = None
    if not is_array:
        if isinstance(payload.get("conversations"), list):
            candidates = payload["conversations"]
            if candidates and all(isinstance(c, dict)
                                  and isinstance(c.get("messages"), list)
                                  for c in candidates):
                records = candidates
        elif isinstance(payload.get("messages"), list):
            records = [payload]

    if records is not None:
        results = [store_conversation(record, default_source=declared
                                      or "extension")
                   for record in records]
        chains = detect_chains(conn)
        return jsonify({"source": declared or "extension",
                        "format": "normalised",
                        "conversations": results,
                        "count": len(results),
                        "chains": chains}), 201

    # Shape 1: a provider export.  Round-trip it through a temp file so the
    # existing adapters and their sniffing run unchanged.
    handle, path = tempfile.mkstemp(suffix=".json", prefix="bridge-ingest-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(payload, out, ensure_ascii=False)
        provider = declared if declared in ("chatgpt", "claude", "gemini") \
            else detect_provider(path)
        stats = import_file(conn, path, provider=provider)
    finally:
        os.unlink(path)

    stats["format"] = "provider-export"
    stats["source"] = provider
    return jsonify(stats), 201


# --------------------------------------------------------------------------
# Memories
# --------------------------------------------------------------------------

@api.route("/memories", methods=["POST"])
def create_memory():
    """Save one curated fact.

    A memory is a conclusion, not a transcript: "prefers Postgres over MySQL",
    not the four messages that established it.
    """
    payload = body()
    content = payload.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ApiError("content must be a non-empty string.")

    tags = payload.get("tags")
    if tags is not None:
        if not isinstance(tags, list) or \
                not all(isinstance(t, str) for t in tags):
            raise ApiError("tags must be an array of strings.")

    conversation_id = payload.get("conversation_id")
    conn = connection()
    if conversation_id is not None:
        if not isinstance(conversation_id, str):
            raise ApiError("conversation_id must be a string.")
        if db.get_conversation(conn, conversation_id) is None:
            raise ApiError("No conversation with id %r." % conversation_id,
                           status=404)

    memory = db.insert_memory(conn, content=content.strip(),
                              source=payload.get("source"), tags=tags,
                              conversation_id=conversation_id)
    return jsonify(memory), 201


@api.route("/memories")
def list_memories():
    raw_limit = request.args.get("limit", "50")
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        raise ApiError("limit must be an integer (got %r)." % raw_limit)
    limit = max(1, min(limit, 500))

    memories = db.get_memories(
        connection(), limit=limit,
        source=request.args.get("source"),
        conversation_id=request.args.get("conversation_id"))
    return jsonify({"count": len(memories), "memories": memories})


@api.route("/memories/<int:memory_id>", methods=["DELETE"])
def remove_memory(memory_id):
    if not db.delete_memory(connection(), memory_id):
        raise ApiError("No memory with id %d." % memory_id, status=404)
    return jsonify({"deleted": memory_id})
