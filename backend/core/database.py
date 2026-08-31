"""SQLite storage layer for AI Memory.

The database is a plain local SQLite file.  Full text search is provided by an
FTS5 virtual table (``conversation_fts``) that holds one row per conversation:
the title plus every message of that conversation concatenated together.

That table is kept in sync by triggers, and it is linked to ``conversations``
by rowid so the triggers can find the right FTS row with an indexed lookup
instead of scanning.
"""

import json
import os
import sqlite3
from datetime import datetime, timezone

# <project root>/data/ai_memory.db
PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
IMPORTS_DIR = os.path.join(DATA_DIR, "imports")
DB_PATH = os.path.join(DATA_DIR, "ai_memory.db")


SCHEMA = """
CREATE TABLE IF NOT EXISTS providers (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    id               TEXT PRIMARY KEY,
    provider_id      INTEGER NOT NULL REFERENCES providers(id),
    title            TEXT,
    created_at       TEXT,
    updated_at       TEXT,
    metadata         TEXT,
    content_hash     TEXT,
    last_imported_at TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role            TEXT,
    content         TEXT,
    timestamp       TEXT,
    message_order   INTEGER
);

CREATE TABLE IF NOT EXISTS conversation_chains (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS conversation_chain_members (
    chain_id        INTEGER NOT NULL REFERENCES conversation_chains(id) ON DELETE CASCADE,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    position        INTEGER,
    PRIMARY KEY (chain_id, conversation_id)
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation
    ON messages(conversation_id, message_order);
CREATE INDEX IF NOT EXISTS idx_conversations_provider
    ON conversations(provider_id);
CREATE INDEX IF NOT EXISTS idx_conversations_updated
    ON conversations(updated_at DESC);
-- Deliberately not UNIQUE: dedup is enforced in insert_conversation so that a
-- genuine content collision is skipped rather than aborting the whole import.
CREATE INDEX IF NOT EXISTS idx_conversations_content_hash
    ON conversations(content_hash);
CREATE INDEX IF NOT EXISTS idx_chain_members_conversation
    ON conversation_chain_members(conversation_id);

-- One row per conversation.  rowid is kept equal to conversations.rowid.
CREATE VIRTUAL TABLE IF NOT EXISTS conversation_fts USING fts5(
    conversation_id UNINDEXED,
    title,
    content,
    tokenize='porter unicode61'
);
"""

TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS conversations_ai AFTER INSERT ON conversations BEGIN
    INSERT INTO conversation_fts(rowid, conversation_id, title, content)
    VALUES (new.rowid, new.id, COALESCE(new.title, ''), '');
END;

CREATE TRIGGER IF NOT EXISTS conversations_au AFTER UPDATE ON conversations BEGIN
    UPDATE conversation_fts
       SET conversation_id = new.id,
           title           = COALESCE(new.title, '')
     WHERE rowid = new.rowid;
END;

CREATE TRIGGER IF NOT EXISTS conversations_ad AFTER DELETE ON conversations BEGIN
    DELETE FROM conversation_fts WHERE rowid = old.rowid;
END;

-- Appending is cheap; rebuilding the whole document per message would be
-- quadratic on import.
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    UPDATE conversation_fts
       SET content = CASE WHEN content = '' THEN COALESCE(new.content, '')
                          ELSE content || char(10) || COALESCE(new.content, '') END
     WHERE rowid = (SELECT rowid FROM conversations WHERE id = new.conversation_id);
END;

-- Edits and deletes are rare, so rebuilding the document is fine there.
CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
    UPDATE conversation_fts
       SET content = COALESCE((SELECT group_concat(content, char(10))
                                 FROM messages
                                WHERE conversation_id = new.conversation_id
                                ORDER BY message_order), '')
     WHERE rowid = (SELECT rowid FROM conversations WHERE id = new.conversation_id);
END;

CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    UPDATE conversation_fts
       SET content = COALESCE((SELECT group_concat(content, char(10))
                                 FROM messages
                                WHERE conversation_id = old.conversation_id
                                ORDER BY message_order), '')
     WHERE rowid = (SELECT rowid FROM conversations WHERE id = old.conversation_id);
END;
"""


def utcnow():
    """Current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def get_connection(db_path=DB_PATH):
    """Open a connection with dict-like rows and foreign keys enabled."""
    directory = os.path.dirname(os.path.abspath(db_path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(db_path=DB_PATH):
    """Create the schema, the FTS5 table and its sync triggers."""
    conn = get_connection(db_path)
    with conn:
        conn.executescript(SCHEMA)
        conn.executescript(TRIGGERS)
    return conn


# --------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------

def insert_provider(conn, name, display_name):
    """Insert a provider if missing and return its id."""
    row = conn.execute("SELECT id FROM providers WHERE name = ?", (name,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO providers (name, display_name) VALUES (?, ?)",
        (name, display_name),
    )
    return cur.lastrowid


def find_conversation_by_hash(conn, content_hash):
    """Return the id of a conversation with this transcript, if one exists."""
    if not content_hash:
        return None
    row = conn.execute(
        "SELECT id FROM conversations WHERE content_hash = ? LIMIT 1",
        (content_hash,),
    ).fetchone()
    return row["id"] if row else None


def touch_conversation(conn, conversation_id):
    """Record that an import saw this conversation again."""
    conn.execute(
        "UPDATE conversations SET last_imported_at = ? WHERE id = ?",
        (utcnow(), conversation_id),
    )


def insert_conversation(conn, conversation_id, provider_id, title,
                        created_at=None, updated_at=None, metadata=None,
                        content_hash=None):
    """Insert or update a conversation, deduplicating on id then content_hash.

    Returns one of:

    - "inserted"  - genuinely new, the caller should write its messages.
    - "updated"   - same id, different transcript; the caller should replace
                    its messages so the search index is rebuilt.
    - "unchanged" - same id, same transcript; nothing to do.
    - "duplicate" - a *different* id already holds this exact transcript, so
                    this is the same conversation re-exported under a fresh id.
                    Nothing is inserted.

    Matching on id takes priority: an export that keeps stable ids must still
    be able to update a conversation whose content changed.  The content_hash
    check is the fallback for exports that mint a new id every time, which
    would otherwise accumulate a fresh copy on every import.
    """
    if isinstance(metadata, (dict, list)):
        metadata = json.dumps(metadata, ensure_ascii=False)

    existing = conn.execute(
        "SELECT content_hash FROM conversations WHERE id = ?", (conversation_id,)
    ).fetchone()
    now = utcnow()

    if existing is None:
        twin_id = find_conversation_by_hash(conn, content_hash)
        if twin_id is not None:
            touch_conversation(conn, twin_id)
            return "duplicate"
        conn.execute(
            """INSERT INTO conversations
                   (id, provider_id, title, created_at, updated_at,
                    metadata, content_hash, last_imported_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (conversation_id, provider_id, title, created_at, updated_at,
             metadata, content_hash, now),
        )
        return "inserted"

    unchanged = (
        content_hash is not None and existing["content_hash"] == content_hash
    )
    conn.execute(
        """UPDATE conversations
              SET provider_id = ?, title = ?, created_at = ?, updated_at = ?,
                  metadata = ?, content_hash = ?, last_imported_at = ?
            WHERE id = ?""",
        (provider_id, title, created_at, updated_at, metadata, content_hash,
         now, conversation_id),
    )
    return "unchanged" if unchanged else "updated"


def insert_message(conn, conversation_id, role, content, timestamp=None,
                   message_order=0):
    """Append one message to a conversation."""
    cur = conn.execute(
        """INSERT INTO messages
               (conversation_id, role, content, timestamp, message_order)
           VALUES (?, ?, ?, ?, ?)""",
        (conversation_id, role, content, timestamp, message_order),
    )
    return cur.lastrowid


def delete_messages(conn, conversation_id):
    """Remove every message of a conversation (used when re-importing)."""
    conn.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))


def create_chain(conn, name):
    cur = conn.execute(
        "INSERT INTO conversation_chains (name, created_at) VALUES (?, ?)",
        (name, utcnow()),
    )
    return cur.lastrowid


def add_conversation_to_chain(conn, chain_id, conversation_id, position):
    conn.execute(
        """INSERT OR REPLACE INTO conversation_chain_members
               (chain_id, conversation_id, position)
           VALUES (?, ?, ?)""",
        (chain_id, conversation_id, position),
    )


def clear_chains(conn):
    """Drop all chains so detection can run again from scratch."""
    conn.execute("DELETE FROM conversation_chain_members")
    conn.execute("DELETE FROM conversation_chains")


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------

def get_recent_conversations(conn, limit=10):
    rows = conn.execute(
        """SELECT c.id, c.title, c.created_at, c.updated_at,
                  p.display_name AS provider_name,
                  (SELECT COUNT(*) FROM messages m
                    WHERE m.conversation_id = c.id) AS message_count
             FROM conversations c
             JOIN providers p ON p.id = c.provider_id
            ORDER BY COALESCE(c.updated_at, c.created_at) DESC
            LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_conversation(conn, conversation_id):
    row = conn.execute(
        """SELECT c.*, p.display_name AS provider_name, p.name AS provider_slug
             FROM conversations c
             JOIN providers p ON p.id = c.provider_id
            WHERE c.id = ?""",
        (conversation_id,),
    ).fetchone()
    return dict(row) if row else None


def get_messages(conn, conversation_id):
    rows = conn.execute(
        """SELECT id, role, content, timestamp, message_order
             FROM messages
            WHERE conversation_id = ?
            ORDER BY message_order, id""",
        (conversation_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_chains(conn):
    rows = conn.execute(
        """SELECT ch.id, ch.name, ch.created_at,
                  COUNT(m.conversation_id) AS size
             FROM conversation_chains ch
             LEFT JOIN conversation_chain_members m ON m.chain_id = ch.id
            GROUP BY ch.id
            ORDER BY size DESC, ch.name""",
    ).fetchall()
    return [dict(r) for r in rows]


def get_chain(conn, chain_id):
    row = conn.execute(
        "SELECT id, name, created_at FROM conversation_chains WHERE id = ?",
        (chain_id,),
    ).fetchone()
    if not row:
        return None
    chain = dict(row)
    members = conn.execute(
        """SELECT c.id, c.title, c.created_at, m.position,
                  p.display_name AS provider_name
             FROM conversation_chain_members m
             JOIN conversations c ON c.id = m.conversation_id
             JOIN providers p ON p.id = c.provider_id
            WHERE m.chain_id = ?
            ORDER BY m.position""",
        (chain_id,),
    ).fetchall()
    chain["conversations"] = [dict(r) for r in members]
    return chain


def get_chains_for_conversation(conn, conversation_id):
    rows = conn.execute(
        """SELECT ch.id, ch.name
             FROM conversation_chain_members m
             JOIN conversation_chains ch ON ch.id = m.chain_id
            WHERE m.conversation_id = ?""",
        (conversation_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_stats(conn):
    """Counts shown on the settings page."""
    def scalar(sql):
        return conn.execute(sql).fetchone()[0]

    providers = conn.execute(
        """SELECT p.name, p.display_name,
                  COUNT(c.id) AS conversation_count
             FROM providers p
             LEFT JOIN conversations c ON c.provider_id = p.id
            GROUP BY p.id
            ORDER BY conversation_count DESC""",
    ).fetchall()

    return {
        "conversations": scalar("SELECT COUNT(*) FROM conversations"),
        "messages": scalar("SELECT COUNT(*) FROM messages"),
        "chains": scalar("SELECT COUNT(*) FROM conversation_chains"),
        "providers": [dict(r) for r in providers],
        "last_import": conn.execute(
            "SELECT MAX(last_imported_at) FROM conversations"
        ).fetchone()[0],
    }
