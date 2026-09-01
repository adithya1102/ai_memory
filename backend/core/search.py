"""Full-text search over imported conversations.

Everything goes through the ``conversation_fts`` FTS5 table, which stores the
title and the concatenated message bodies of each conversation.
"""

import html
import re
import sqlite3

# Wrapped around matched terms by FTS5, then swapped for <mark> after the
# surrounding text has been HTML-escaped.
_OPEN = "\x02"
_CLOSE = "\x03"

_TOKEN_RE = re.compile(r'"[^"]+"|[^\W_]+', re.UNICODE)


def build_match_query(query):
    """Turn raw user input into a safe FTS5 MATCH expression.

    Every term is quoted, so FTS5 operators the user did not intend (``-``,
    ``:``, ``*``, ``NEAR``, ``OR``) cannot break the query.  Double-quoted
    input is preserved as a phrase search.
    """
    terms = []
    for raw in _TOKEN_RE.findall(query or ""):
        if raw.startswith('"'):
            phrase = raw.strip('"').strip()
            if phrase:
                terms.append('"%s"' % phrase.replace('"', ""))
        else:
            terms.append('"%s"' % raw)
    return " ".join(terms)


def _to_html(snippet):
    """Escape the snippet, then restore the match markers as <mark> tags."""
    escaped = html.escape(snippet or "")
    return escaped.replace(_OPEN, "<mark>").replace(_CLOSE, "</mark>")


def search_conversations(conn, query, limit=20):
    """Search titles and message bodies.

    Returns a list of dicts with conversation_id, title, provider_name,
    created_at, snippet and match_score (higher is more relevant).
    """
    match = build_match_query(query)
    if not match:
        return []

    sql = """
        SELECT c.id                AS conversation_id,
               c.title             AS title,
               p.display_name      AS provider_name,
               c.created_at        AS created_at,
               c.updated_at        AS updated_at,
               snippet(conversation_fts, -1, ?, ?, ' … ', 20) AS snippet,
               bm25(conversation_fts, 10.0, 5.0, 1.0)         AS rank_score,
               (SELECT COUNT(*) FROM messages m
                 WHERE m.conversation_id = c.id)              AS message_count
          FROM conversation_fts
          JOIN conversations c ON c.rowid = conversation_fts.rowid
          JOIN providers p     ON p.id = c.provider_id
         WHERE conversation_fts MATCH ?
         ORDER BY rank_score
         LIMIT ?
    """
    try:
        rows = conn.execute(sql, (_OPEN, _CLOSE, match, limit)).fetchall()
    except sqlite3.OperationalError:
        # Malformed query that survived sanitising - treat as "no results"
        # rather than crashing the page.
        return []

    results = []
    for row in rows:
        item = dict(row)
        # bm25() is negative, most relevant first; flip it so bigger is better.
        item["match_score"] = round(-item.pop("rank_score"), 4)
        item["snippet_html"] = _to_html(item["snippet"])
        item["snippet"] = (item["snippet"] or "").replace(_OPEN, "").replace(_CLOSE, "")
        results.append(item)
    return results


def search_memories(conn, query, limit=20):
    """Keyword search over saved memories.

    Returns the same shape as ``search_conversations`` where the fields
    overlap, plus memory_id, so the two lists can be merged into one ranking.
    """
    match = build_match_query(query)
    if not match:
        return []

    sql = """
        SELECT m.id                AS memory_id,
               m.content           AS content,
               m.source            AS source,
               m.tags              AS tags,
               m.created_at        AS created_at,
               m.conversation_id   AS conversation_id,
               snippet(memory_fts, 1, ?, ?, ' … ', 20) AS snippet,
               bm25(memory_fts, 1.0, 5.0)             AS rank_score
          FROM memory_fts
          JOIN memories m ON m.rowid = memory_fts.rowid
         WHERE memory_fts MATCH ?
         ORDER BY rank_score
         LIMIT ?
    """
    try:
        rows = conn.execute(sql, (_OPEN, _CLOSE, match, limit)).fetchall()
    except sqlite3.OperationalError:
        # An older library that has not been through init_db yet has no
        # memory_fts table.  Degrade to "no memory hits" rather than breaking
        # conversation search along with it.
        return []

    results = []
    for row in rows:
        item = dict(row)
        item["match_score"] = round(-item.pop("rank_score"), 4)
        item["snippet_html"] = _to_html(item["snippet"])
        item["snippet"] = (item["snippet"] or "").replace(_OPEN, "") \
                                                 .replace(_CLOSE, "")
        results.append(item)
    return results


SEMANTIC_ENABLED_KEY = "semantic_search_enabled"

# Reciprocal Rank Fusion constant.  60 is the value from the original RRF
# paper; it damps the head of each list so a strong hit in one ranking cannot
# alone outrank something both rankings agree on.
RRF_K = 60


def _conversation_rows(conn, conversation_ids):
    """Fetch display metadata for a set of conversations, keyed by id."""
    if not conversation_ids:
        return {}
    placeholders = ",".join("?" * len(conversation_ids))
    rows = conn.execute(
        """SELECT c.id AS conversation_id, c.title, c.created_at, c.updated_at,
                  p.display_name AS provider_name,
                  (SELECT COUNT(*) FROM messages m
                    WHERE m.conversation_id = c.id) AS message_count
             FROM conversations c
             JOIN providers p ON p.id = c.provider_id
            WHERE c.id IN (%s)""" % placeholders,
        list(conversation_ids),
    ).fetchall()
    return {r["conversation_id"]: dict(r) for r in rows}


def _memory_title(content):
    """A memory has no title, but a result list needs one to show."""
    first = re.split(r"[.\n]", (content or "").strip(), 1)[0].strip()
    if not first:
        return "Memory"
    return first if len(first) <= 70 else first[:70].rstrip() + "…"


def _memory_item(row, source_label="Memory"):
    """Shape a memory row like a conversation result.

    The UI, the API and the MCP tool all iterate one list, so a memory has to
    carry the same field names a conversation does.  ``kind`` is what tells
    them apart; ``conversation_id`` stays None unless the memory was saved
    from one, which is what stops a caller trying to open it as a transcript.
    """
    return {
        "kind": "memory",
        "memory_id": row["memory_id"],
        "conversation_id": row.get("conversation_id"),
        "title": _memory_title(row.get("content")),
        "provider_name": row.get("source") or source_label,
        "created_at": row.get("created_at"),
        "updated_at": row.get("created_at"),
        "message_count": None,
        "content": row.get("content"),
        "tags": row.get("tags"),
    }


def hybrid_search(conn, query, limit=20, semantic=None, top_k=10,
                  include_memories=True):
    """Search by keyword and by meaning, then merge into one ranking.

    Results are deduplicated by conversation and carry a ``match_types`` list
    of "keyword" and/or "semantic" so the UI can badge where each came from.
    Ranking is Reciprocal Rank Fusion over the two result lists, which needs no
    score normalisation between a bm25 rank and a cosine similarity.

    ``semantic`` overrides the stored setting; when it is None the setting is
    read from the database.  If semantic search is disabled or unavailable this
    degrades to keyword-only results.
    """
    from backend.core import database as db
    from backend.core import embeddings

    if semantic is None:
        semantic = db.get_flag(conn, SEMANTIC_ENABLED_KEY, default=True)

    keyword_hits = search_conversations(conn, query, limit=limit)
    semantic_hits = embeddings.semantic_search(conn, query, top_k=top_k) \
        if semantic else []

    # Keyed by (kind, id): a conversation and a memory can share neither key
    # space nor identity, and merging them on a bare id would collide.
    merged = {}

    for rank, hit in enumerate(keyword_hits, 1):
        item = dict(hit)
        item["kind"] = "conversation"
        item["match_types"] = ["keyword"]
        item["score"] = 1.0 / (RRF_K + rank)
        item["similarity"] = None
        merged[("conversation", hit["conversation_id"])] = item

    # A conversation can be hit by several chunks; keep only its best one.
    best_chunk = {}
    for rank, hit in enumerate(semantic_hits, 1):
        cid = hit["conversation_id"]
        if cid not in best_chunk:
            best_chunk[cid] = (rank, hit)

    missing = [cid for cid in best_chunk
               if ("conversation", cid) not in merged]
    metadata = _conversation_rows(conn, missing)

    for cid, (rank, hit) in best_chunk.items():
        key = ("conversation", cid)
        contribution = 1.0 / (RRF_K + rank)
        if key in merged:
            item = merged[key]
            item["match_types"].append("semantic")
            item["score"] += contribution
            item["similarity"] = hit["similarity"]
        else:
            row = metadata.get(cid)
            if row is None:  # chunk outlived its conversation
                continue
            item = dict(row)
            item["kind"] = "conversation"
            item["match_types"] = ["semantic"]
            item["score"] = contribution
            item["similarity"] = hit["similarity"]
            # No lexical match to highlight, so show the matching passage.
            item["snippet"] = _shorten(hit["content"])
            item["snippet_html"] = html.escape(item["snippet"])
            item["match_score"] = hit["similarity"]
            merged[key] = item

    # Memories go through the same two engines and the same fusion, so a
    # memory and a conversation compete on one ranking rather than being
    # stapled together at the end.
    if include_memories:
        for rank, hit in enumerate(search_memories(conn, query, limit=limit), 1):
            item = _memory_item(hit)
            item["match_types"] = ["keyword"]
            item["score"] = 1.0 / (RRF_K + rank)
            item["similarity"] = None
            item["snippet"] = hit["snippet"]
            item["snippet_html"] = hit["snippet_html"]
            item["match_score"] = hit["match_score"]
            merged[("memory", hit["memory_id"])] = item

        memory_hits = embeddings.semantic_search_memories(conn, query,
                                                          top_k=top_k) \
            if semantic else []
        for rank, hit in enumerate(memory_hits, 1):
            key = ("memory", hit["memory_id"])
            contribution = 1.0 / (RRF_K + rank)
            if key in merged:
                item = merged[key]
                item["match_types"].append("semantic")
                item["score"] += contribution
                item["similarity"] = hit["similarity"]
            else:
                item = _memory_item(hit)
                item["match_types"] = ["semantic"]
                item["score"] = contribution
                item["similarity"] = hit["similarity"]
                item["snippet"] = _shorten(hit["content"])
                item["snippet_html"] = html.escape(item["snippet"])
                item["match_score"] = hit["similarity"]
                merged[key] = item

    results = sorted(merged.values(), key=lambda r: -r["score"])
    for item in results:
        item["match_label"] = ("both" if len(item["match_types"]) > 1
                               else item["match_types"][0])
    return results[:limit]


def _shorten(text, length=240):
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= length else text[:length].rstrip() + "…"


def count_matches(conn, query):
    match = build_match_query(query)
    if not match:
        return 0
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM conversation_fts WHERE conversation_fts MATCH ?",
            (match,),
        ).fetchone()[0]
    except sqlite3.OperationalError:
        return 0
