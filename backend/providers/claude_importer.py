"""Importer for Claude conversation exports.

Claude exports a flat list of conversations, each with a ``chat_messages``
list.  There is no message tree to walk, so this is considerably simpler than
the ChatGPT adapter -- the work is in the field names and in the message
content, which may be a plain string or a list of typed blocks.
"""

import json
import os
import re

from backend.core import database as db
from backend.providers import common

PROVIDER_NAME = "claude"
PROVIDER_DISPLAY_NAME = "Claude"
ID_PREFIX = "claude:"

# Claude calls the person "human"; the universal format calls them "user".
ROLE_MAP = {"human": "user", "user": "user", "assistant": "assistant"}

# Block types that carry conversation.  Everything else in a real export is
# machinery rather than transcript: "thinking" is the model's scratchpad,
# "tool_use"/"tool_result" are the mechanics of a tool call, and
# "injected_prompt_block" is system plumbing spliced in at request time.  None
# of it is something the person said or was shown as the reply, so indexing it
# would surface hits the user cannot recognise in their own history.
TEXT_BLOCK_TYPES = (None, "text")

# Every message in a real export carries BOTH a structured ``content`` list and
# a flattened ``text`` rendering of it, and the two disagree.  ``text`` inlines
# the model's thinking as though it were prose and replaces each tool block
# with the placeholder below, so reading it first would index hundreds of
# copies of a sentence nobody wrote and attribute the scratchpad to the reply.
# The structured blocks are the source of truth and are read first; ``text``
# remains the fallback for older exports that carry no ``content``, scrubbed of
# the placeholder when it is used.
UNSUPPORTED_BLOCK_RE = re.compile(
    r"```\s*This block is not supported on your current device yet\.\s*```")


def _clean(text):
    """Drop placeholder chrome and collapse the blank runs it leaves behind."""
    text = UNSUPPORTED_BLOCK_RE.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _extract_text(entry):
    """Pull readable text out of one exported message.

    Structured blocks win over the flattened ``text`` field; see
    ``UNSUPPORTED_BLOCK_RE`` for why.
    """
    blocks = entry.get("content")
    if isinstance(blocks, list):
        parts = []
        for block in blocks:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") in TEXT_BLOCK_TYPES:
                parts.append(block.get("text") or "")
        joined = _clean("\n".join(p for p in parts if p))
        if joined:
            return joined
    elif isinstance(blocks, str) and blocks.strip():
        return _clean(blocks)

    # No usable blocks: fall back to the flattened rendering.
    text = entry.get("text")
    if isinstance(text, str) and text.strip():
        return _clean(text)
    return ""


def looks_like_claude_export(payload):
    """True if this payload has Claude's shape."""
    records = payload if isinstance(payload, list) else [payload]
    for record in records:
        if isinstance(record, dict) and isinstance(record.get("chat_messages"), list):
            return True
    return False


def import_claude_export(conn, json_file_path):
    """Import a Claude export.  Returns the standard stats dict."""
    with open(json_file_path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)

    if isinstance(payload, dict):
        payload = payload.get("conversations", [payload])
    if not isinstance(payload, list):
        raise ValueError("Unrecognised Claude export: expected a list of "
                         "conversations")

    provider_id = db.insert_provider(conn, PROVIDER_NAME, PROVIDER_DISPLAY_NAME)
    conn.commit()
    stats = common.new_stats(os.path.basename(json_file_path))

    for index, raw in enumerate(payload):
        try:
            if not isinstance(raw, dict):
                stats["skipped"] += 1
                continue

            raw_id = raw.get("uuid") or raw.get("id") or raw.get("conversation_id")
            if not raw_id:
                stats["skipped"] += 1
                continue

            messages = []
            for entry in raw.get("chat_messages") or []:
                if not isinstance(entry, dict):
                    continue
                role = ROLE_MAP.get(entry.get("sender") or entry.get("role"))
                text = _extract_text(entry)
                if role and text:
                    messages.append({"role": role, "content": text,
                                     "timestamp": entry.get("created_at")})

            if not messages:
                stats["skipped"] += 1
                continue

            common.ingest(
                conn, stats, provider_id,
                conversation_id=ID_PREFIX + str(raw_id),
                title=(raw.get("name") or raw.get("title") or "").strip()
                      or "Untitled conversation",
                messages=messages,
                created_at=raw.get("created_at"),
                updated_at=raw.get("updated_at"),
                metadata={"original_id": str(raw_id),
                          "source_file": os.path.basename(json_file_path),
                          "message_count": len(messages)},
            )

            if index % 200 == 0:
                conn.commit()
        except Exception:  # one bad record must not kill the import
            stats["skipped"] += 1

    conn.commit()
    return stats
