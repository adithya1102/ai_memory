"""Importer for Claude conversation exports.

Claude exports a flat list of conversations, each with a ``chat_messages``
list.  There is no message tree to walk, so this is considerably simpler than
the ChatGPT adapter -- the work is in the field names and in the message
content, which may be a plain string or a list of typed blocks.
"""

import json
import os

from backend.core import database as db
from backend.providers import common

PROVIDER_NAME = "claude"
PROVIDER_DISPLAY_NAME = "Claude"
ID_PREFIX = "claude:"

# Claude calls the person "human"; the universal format calls them "user".
ROLE_MAP = {"human": "user", "user": "user", "assistant": "assistant"}


def _extract_text(entry):
    """Pull readable text out of one exported message."""
    text = entry.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()

    # Newer exports carry a content array of typed blocks.
    blocks = entry.get("content")
    if isinstance(blocks, list):
        parts = []
        for block in blocks:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") in (None, "text"):
                parts.append(block.get("text") or "")
        return "\n".join(p for p in parts if p).strip()
    if isinstance(blocks, str):
        return blocks.strip()
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
