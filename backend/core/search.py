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
