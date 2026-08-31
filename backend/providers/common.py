"""Helpers shared by provider adapters.

Every adapter does the same three things once it has parsed a conversation:
hash the transcript, upsert the conversation, and replace its messages.  Only
the parsing differs between vendors, so that part lives in the adapter and
this part lives here.
"""

import hashlib

from backend.core import database as db


def new_stats(source=None):
    stats = {"inserted": 0, "updated": 0, "unchanged": 0, "duplicate": 0,
             "messages": 0, "skipped": 0}
    if source is not None:
        stats["source"] = source
    return stats


def content_hash(messages):
    """SHA-256 over the transcript, used to deduplicate re-imports."""
    digest = hashlib.sha256()
    for message in messages:
        digest.update((message["role"] or "").encode("utf-8"))
        digest.update(b"\x00")
        digest.update((message["content"] or "").encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


def ingest(conn, stats, provider_id, conversation_id, title, messages,
           created_at=None, updated_at=None, metadata=None):
    """Upsert one conversation and its messages, recording the outcome.

    Returns the outcome string.  Messages are written only for "inserted" and
    "updated": "unchanged" already has them, and "duplicate" inserted no
    conversation row for them to hang off.
    """
    outcome = db.insert_conversation(
        conn,
        conversation_id=conversation_id,
        provider_id=provider_id,
        title=title,
        created_at=created_at,
        updated_at=updated_at or created_at,
        metadata=metadata,
        content_hash=content_hash(messages),
    )
    stats[outcome] += 1

    if outcome in ("inserted", "updated"):
        db.delete_messages(conn, conversation_id)
        for order, message in enumerate(messages):
            db.insert_message(
                conn,
                conversation_id=conversation_id,
                role=message["role"],
                content=message["content"],
                timestamp=message.get("timestamp"),
                message_order=order,
            )
        stats["messages"] += len(messages)
    return outcome
