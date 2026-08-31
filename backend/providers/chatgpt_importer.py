"""Importer for ChatGPT ``conversations.json`` exports.

The canonical export is a list of conversation objects, each with a ``mapping``
dict holding the message tree.  Older and hand-rolled exports sometimes use a
flat ``messages`` list instead, so both shapes are handled.
"""

import hashlib
import json
import os
from datetime import datetime, timezone

from backend.core import database as db

PROVIDER_NAME = "chatgpt"
PROVIDER_DISPLAY_NAME = "ChatGPT"
ID_PREFIX = "chatgpt:"

KEPT_ROLES = ("user", "assistant")


def _to_iso(value):
    """Normalise ChatGPT timestamps (epoch seconds, or already a string)."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc) \
                           .replace(microsecond=0).isoformat()
        except (ValueError, OSError, OverflowError):
            return None
    if isinstance(value, str):
        try:  # already ISO-ish, or an epoch in a string
            return datetime.fromtimestamp(float(value), tz=timezone.utc) \
                           .replace(microsecond=0).isoformat()
        except ValueError:
            return value
    return None


def _extract_text(content):
    """Pull plain text out of a message's ``content`` field."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "\n".join(filter(None, (_extract_text(c) for c in content))).strip()
    if not isinstance(content, dict):
        return ""

    content_type = content.get("content_type", "text")
    if content_type not in ("text", "multimodal_text", None):
        # code / execution_output / tool results - not part of the readable
        # transcript for the MVP.
        return ""

    parts = content.get("parts")
    if isinstance(parts, list):
        chunks = []
        for part in parts:
            if isinstance(part, str):
                chunks.append(part)
            elif isinstance(part, dict):
                # multimodal parts: images and friends carry no useful text
                chunks.append(part.get("text") or "")
        return "\n".join(c for c in chunks if c).strip()

    if isinstance(content.get("text"), str):
        return content["text"].strip()
    return ""


def _node_messages(mapping, current_node):
    """Return the message nodes of a ``mapping`` tree in conversation order.

    Prefers the active branch (walk parents up from ``current_node``); falls
    back to every node sorted by creation time.
    """
    ordered = []
    if current_node and current_node in mapping:
        seen = set()
        node_id = current_node
        while node_id and node_id in mapping and node_id not in seen:
            seen.add(node_id)
            node = mapping[node_id] or {}
            if node.get("message"):
                ordered.append(node["message"])
            node_id = node.get("parent")
        ordered.reverse()

    if not ordered:
        nodes = [n.get("message") for n in mapping.values()
                 if isinstance(n, dict) and n.get("message")]
        ordered = sorted(nodes, key=lambda m: (m.get("create_time") or 0))
    return ordered


def _clean_messages(raw_messages):
    """Filter a raw message list down to visible user/assistant text."""
    cleaned = []
    for message in raw_messages:
        if not isinstance(message, dict):
            continue

        author = message.get("author") or {}
        role = author.get("role") if isinstance(author, dict) else None
        role = role or message.get("role")
        if role not in KEPT_ROLES:
            continue

        meta = message.get("metadata") or {}
        if isinstance(meta, dict) and meta.get("is_visually_hidden_from_conversation"):
            continue

        text = _extract_text(message.get("content"))
        if not text:
            continue

        cleaned.append({
            "role": role,
            "content": text,
            "timestamp": _to_iso(message.get("create_time")
                                 or message.get("timestamp")),
        })
    return cleaned


def _content_hash(messages):
    digest = hashlib.sha256()
    for message in messages:
        digest.update(message["role"].encode("utf-8"))
        digest.update(b"\x00")
        digest.update(message["content"].encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


def _iter_conversations(payload):
    """Yield conversation dicts from the various export shapes."""
    if isinstance(payload, dict):
        for key in ("conversations", "data", "items"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
        else:
            payload = [payload]  # a single exported conversation
    if not isinstance(payload, list):
        raise ValueError("Unrecognised ChatGPT export: expected a list of "
                         "conversations")
    for item in payload:
        if isinstance(item, dict):
            yield item


def import_chatgpt_export(conn, json_file_path):
    """Import a ChatGPT export into the database.

    Returns a stats dict: inserted / updated / unchanged / messages / skipped.
    """
    with open(json_file_path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)

    provider_id = db.insert_provider(conn, PROVIDER_NAME, PROVIDER_DISPLAY_NAME)
    conn.commit()

    stats = {"inserted": 0, "updated": 0, "unchanged": 0, "duplicate": 0,
             "messages": 0, "skipped": 0, "source": os.path.basename(json_file_path)}

    for index, raw in enumerate(_iter_conversations(payload)):
        try:
            raw_id = (raw.get("id") or raw.get("conversation_id")
                      or raw.get("uuid") or raw.get("title"))
            if not raw_id:
                stats["skipped"] += 1
                continue
            conversation_id = ID_PREFIX + str(raw_id)

            mapping = raw.get("mapping")
            if isinstance(mapping, dict) and mapping:
                raw_messages = _node_messages(mapping, raw.get("current_node"))
            elif isinstance(raw.get("messages"), list):
                raw_messages = raw["messages"]
            else:
                raw_messages = []

            messages = _clean_messages(raw_messages)
            if not messages:
                stats["skipped"] += 1
                continue

            title = (raw.get("title") or "").strip() or "Untitled conversation"
            created_at = _to_iso(raw.get("create_time") or raw.get("created_at"))
            updated_at = _to_iso(raw.get("update_time")
                                 or raw.get("updated_at")) or created_at
            metadata = {
                "source_file": os.path.basename(json_file_path),
                "original_id": str(raw_id),
                "message_count": len(messages),
            }
            if raw.get("default_model_slug"):
                metadata["model"] = raw["default_model_slug"]

            outcome = db.insert_conversation(
                conn,
                conversation_id=conversation_id,
                provider_id=provider_id,
                title=title,
                created_at=created_at,
                updated_at=updated_at,
                metadata=metadata,
                content_hash=_content_hash(messages),
            )
            stats[outcome] += 1

            # Only these two outcomes own a row that needs messages written.
            # "unchanged" already has them; "duplicate" inserted nothing at
            # all, so writing messages would orphan them against a missing
            # conversation row.
            if outcome in ("inserted", "updated"):
                # Replace the transcript wholesale; the FTS triggers keep the
                # search index in step.
                db.delete_messages(conn, conversation_id)
                for order, message in enumerate(messages):
                    db.insert_message(
                        conn,
                        conversation_id=conversation_id,
                        role=message["role"],
                        content=message["content"],
                        timestamp=message["timestamp"],
                        message_order=order,
                    )
                stats["messages"] += len(messages)

            if index % 200 == 0:
                conn.commit()
        except Exception:  # one broken conversation must not kill the import
            stats["skipped"] += 1

    conn.commit()
    return stats
