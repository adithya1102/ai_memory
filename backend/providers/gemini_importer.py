"""Importer for Gemini conversation exports (Google Takeout).

Caveat worth reading before trusting this: Google Takeout's Gemini export has
changed shape more than once and is not publicly specified.  This adapter
targets the common form -- a list of conversations each holding a flat
``messages`` list whose author is "user" or "model" -- and accepts several
field-name variants seen in the wild.  If your export does not import
cleanly, the parsing here is what needs adjusting, not the rest of the app;
see CONTRIBUTING.md.
"""

import json
import os

from backend.core import database as db
from backend.providers import common

PROVIDER_NAME = "gemini"
PROVIDER_DISPLAY_NAME = "Gemini"
ID_PREFIX = "gemini:"

# Gemini calls the assistant "model".
ROLE_MAP = {"user": "user", "human": "user",
            "model": "assistant", "assistant": "assistant", "gemini": "assistant"}

_MESSAGE_KEYS = ("messages", "turns", "conversation_messages")
_TEXT_KEYS = ("text", "content", "message")
_AUTHOR_KEYS = ("author", "role", "sender", "speaker")


def _first(mapping, keys):
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return None


def _extract_text(entry):
    value = _first(entry, _TEXT_KEYS)
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts = []
        for part in value:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                parts.append(part.get("text") or "")
        return "\n".join(p for p in parts if p).strip()
    if isinstance(value, dict):
        return (value.get("text") or "").strip()
    return ""


def _messages_of(raw):
    for key in _MESSAGE_KEYS:
        value = raw.get(key)
        if isinstance(value, list):
            return value
    return []


def looks_like_gemini_export(payload):
    """True if this payload has Gemini's shape.

    Gemini's flat shape is close to ChatGPT's legacy flat export, so this
    looks for markers ChatGPT never uses rather than for a message list, which
    both have:

    * an author of "model" -- the strongest signal, and present in any export
      containing a reply
    * an ``author`` key on messages, where ChatGPT uses ``role``
    * a ``turns`` list, which ChatGPT never emits

    An export with none of these is genuinely ambiguous and is left to the
    ChatGPT adapter, which is the more tolerant of the two.
    """
    records = payload if isinstance(payload, list) else [payload]
    for record in records:
        if not isinstance(record, dict):
            continue
        if isinstance(record.get("turns"), list) and record["turns"]:
            return True
        entries = _messages_of(record)
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if "author" in entry:
                return True
            author = _first(entry, _AUTHOR_KEYS)
            if isinstance(author, str) and author.lower() == "model":
                return True
    return False


def import_gemini_export(conn, json_file_path):
    """Import a Gemini export.  Returns the standard stats dict."""
    with open(json_file_path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)

    if isinstance(payload, dict):
        for key in ("conversations", "data", "items"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
        else:
            payload = [payload]
    if not isinstance(payload, list):
        raise ValueError("Unrecognised Gemini export: expected a list of "
                         "conversations")

    provider_id = db.insert_provider(conn, PROVIDER_NAME, PROVIDER_DISPLAY_NAME)
    conn.commit()
    stats = common.new_stats(os.path.basename(json_file_path))

    for index, raw in enumerate(payload):
        try:
            if not isinstance(raw, dict):
                stats["skipped"] += 1
                continue

            raw_id = _first(raw, ("conversation_id", "id", "uuid", "name"))
            if not raw_id:
                stats["skipped"] += 1
                continue

            messages = []
            for entry in _messages_of(raw):
                if not isinstance(entry, dict):
                    continue
                author = _first(entry, _AUTHOR_KEYS)
                role = ROLE_MAP.get(str(author).lower()) if author else None
                text = _extract_text(entry)
                if role and text:
                    messages.append({
                        "role": role, "content": text,
                        "timestamp": _first(entry, ("create_time", "created_at",
                                                    "timestamp")),
                    })

            if not messages:
                stats["skipped"] += 1
                continue

            created = _first(raw, ("create_time", "created_at", "start_time"))
            common.ingest(
                conn, stats, provider_id,
                conversation_id=ID_PREFIX + str(raw_id),
                title=str(_first(raw, ("title", "name", "subject")) or "").strip()
                      or "Untitled conversation",
                messages=messages,
                created_at=created,
                updated_at=_first(raw, ("update_time", "updated_at")) or created,
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
