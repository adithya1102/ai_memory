"""The three tools ContextVault exposes over MCP.

Transport-free on purpose: this module knows nothing about JSON-RPC, stdio or
the MCP SDK, so the same implementations serve every transport in server.py
and can be tested directly.

All retrieval reuses ``backend/core`` -- there is no search logic here.  The
tools translate between the core's return shapes and the MCP payload, and
nothing else.
"""

from backend.core import database as db
from backend.core import embeddings
from backend.core.search import SEMANTIC_ENABLED_KEY, hybrid_search

# How long a tool call will wait for the encoder before answering with keyword
# results alone.  An agent has no UI to show a "still loading" banner to, so it
# waits rather than silently returning a worse answer.
MODEL_WAIT_SECONDS = 60.0

DEFAULT_LIMIT = 10
MAX_LIMIT = 50


class ToolError(Exception):
    """A tool failed in a way the caller should see as text, not a crash."""


TOOL_SCHEMAS = [
    {
        "name": "search_memory",
        "description": (
            "Search the user's archived AI conversations by keyword and by "
            "meaning at once. Use this to find past discussions even when you "
            "do not know the exact words used -- a query like 'how do I get "
            "stronger' will find a conversation about building muscle. "
            "Returns matching conversations with a snippet and the id needed "
            "to read the full transcript with get_conversation."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What to look for. Natural language works "
                                   "as well as keywords.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum conversations to return "
                                   "(default 10, max 50).",
                    "default": DEFAULT_LIMIT,
                    "minimum": 1,
                    "maximum": MAX_LIMIT,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_conversation",
        "description": (
            "Read one archived conversation in full, with every message in "
            "order. Take the conversation_id from a search_memory result."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "conversation_id": {
                    "type": "string",
                    "description": "Conversation id, e.g. 'chatgpt:abc-123'.",
                },
            },
            "required": ["conversation_id"],
        },
    },
    {
        "name": "get_conversation_chain",
        "description": (
            "List the conversations in a chain -- a group of related "
            "conversations the user returned to over time -- oldest first. "
            "Chain ids appear on conversations returned by get_conversation."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "chain_id": {
                    "type": "integer",
                    "description": "Numeric chain id.",
                },
            },
            "required": ["chain_id"],
        },
    },
]


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------

def search_memory(conn, query, limit=DEFAULT_LIMIT):
    """Hybrid keyword + semantic search over every archived conversation."""
    if not isinstance(query, str) or not query.strip():
        raise ToolError("query must be a non-empty string")
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        raise ToolError("limit must be an integer")
    limit = max(1, min(limit, MAX_LIMIT))

    # Give the encoder a chance to finish loading so the agent gets the
    # semantic half of the results rather than a quietly degraded answer.
    if db.get_flag(conn, SEMANTIC_ENABLED_KEY, default=True):
        embeddings.wait_until_ready(MODEL_WAIT_SECONDS)

    results = []
    for row in hybrid_search(conn, query, limit=limit):
        results.append({
            "conversation_id": row["conversation_id"],
            "title": row.get("title") or "Untitled conversation",
            "provider": row.get("provider_name"),
            "date": row.get("created_at"),
            "snippet": row.get("snippet") or "",
            "relevance_score": round(row.get("score") or 0.0, 6),
            "match_type": row.get("match_label"),
        })
    return {"query": query, "count": len(results), "results": results}


def get_conversation(conn, conversation_id):
    """Full transcript of one conversation, messages in order."""
    if not isinstance(conversation_id, str) or not conversation_id.strip():
        raise ToolError("conversation_id must be a non-empty string")

    record = db.get_conversation(conn, conversation_id)
    if record is None:
        raise ToolError(
            "No conversation with id %r. Ids look like 'chatgpt:abc-123' and "
            "come from search_memory results." % conversation_id)

    messages = db.get_messages(conn, conversation_id)
    return {
        "conversation_id": record["id"],
        "title": record.get("title") or "Untitled conversation",
        "provider": record.get("provider_name"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "message_count": len(messages),
        "chains": [{"chain_id": c["id"], "name": c["name"]}
                   for c in db.get_chains_for_conversation(conn, conversation_id)],
        "messages": [{"order": m["message_order"], "role": m["role"],
                      "timestamp": m["timestamp"], "content": m["content"]}
                     for m in messages],
    }


def get_conversation_chain(conn, chain_id):
    """Every conversation in a chain, oldest first."""
    try:
        chain_id = int(chain_id)
    except (TypeError, ValueError):
        raise ToolError("chain_id must be an integer")

    chain = db.get_chain(conn, chain_id)
    if chain is None:
        known = [str(c["id"]) for c in db.get_chains(conn)]
        raise ToolError(
            "No chain with id %d.%s" % (
                chain_id,
                (" Known chain ids: %s." % ", ".join(known)) if known
                else " This library has no chains yet."))

    return {
        "chain_id": chain["id"],
        "name": chain["name"],
        "size": len(chain["conversations"]),
        "conversations": [{
            "position": c["position"],
            "conversation_id": c["id"],
            "title": c["title"] or "Untitled conversation",
            "provider": c["provider_name"],
            "date": c["created_at"],
        } for c in chain["conversations"]],
    }


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------

HANDLERS = {
    "search_memory": search_memory,
    "get_conversation": get_conversation,
    "get_conversation_chain": get_conversation_chain,
}


def call_tool(conn, name, arguments):
    """Run a tool by name.  Raises ToolError for anything the caller did wrong."""
    handler = HANDLERS.get(name)
    if handler is None:
        raise ToolError("Unknown tool %r. Available: %s"
                        % (name, ", ".join(sorted(HANDLERS))))
    arguments = arguments or {}
    if not isinstance(arguments, dict):
        raise ToolError("arguments must be an object")

    allowed = set(TOOL_SCHEMAS_BY_NAME[name]["inputSchema"]["properties"])
    unexpected = set(arguments) - allowed
    if unexpected:
        raise ToolError("Unexpected argument(s) for %s: %s. Expected: %s"
                        % (name, ", ".join(sorted(unexpected)),
                           ", ".join(sorted(allowed))))
    required = TOOL_SCHEMAS_BY_NAME[name]["inputSchema"].get("required", [])
    missing = [key for key in required if key not in arguments]
    if missing:
        raise ToolError("Missing required argument(s) for %s: %s"
                        % (name, ", ".join(missing)))

    return handler(conn, **arguments)


TOOL_SCHEMAS_BY_NAME = {tool["name"]: tool for tool in TOOL_SCHEMAS}
