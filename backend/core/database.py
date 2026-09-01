"""SQLite storage layer for ContextVault.

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

# <project root>/data/contextvault.db
PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
IMPORTS_DIR = os.path.join(DATA_DIR, "imports")
DB_FILENAME = "contextvault.db"
DB_PATH = os.path.join(DATA_DIR, DB_FILENAME)

# The project was called "AI Memory" before it was called ContextVault, and its
# database was named after it.  Kept as a literal so a bulk rename cannot
# quietly turn the migration below into a no-op.
LEGACY_DB_FILENAME = "ai" + "_memory.db"


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

-- Semantic search bookkeeping.  The vectors themselves live in a sqlite-vec
-- virtual table created by backend/core/embeddings.py, keyed by chunks.id;
-- these two tables work with or without the extension installed.
CREATE TABLE IF NOT EXISTS chunks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    chunk_index     INTEGER NOT NULL,
    role            TEXT,
    content         TEXT NOT NULL,
    UNIQUE (conversation_id, chunk_index)
);

-- One row per embedded conversation; content_hash is what makes re-embedding
-- incremental.
CREATE TABLE IF NOT EXISTS embedded_conversations (
    conversation_id TEXT PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
    content_hash    TEXT,
    model           TEXT,
    chunk_count     INTEGER,
    embedded_at     TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Short facts an assistant curated and chose to keep, as opposed to the raw
-- transcripts it read them out of.  Written over the Bridge API.  The
-- conversation reference is ON DELETE SET NULL: losing the source transcript
-- should not silently delete the conclusion drawn from it.
CREATE TABLE IF NOT EXISTS memories (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    content         TEXT NOT NULL,
    source          TEXT,
    tags            TEXT,
    conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,
    created_at      TEXT,
    updated_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_chunks_conversation
    ON chunks(conversation_id);
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
CREATE INDEX IF NOT EXISTS idx_memories_created
    ON memories(created_at DESC);

-- One row per conversation.  rowid is kept equal to conversations.rowid.
CREATE VIRTUAL TABLE IF NOT EXISTS conversation_fts USING fts5(
    conversation_id UNINDEXED,
    title,
    content,
    tokenize='porter unicode61'
);

-- Memories are searched the same way conversations are.  Without this a fact
-- saved through the API or the app is stored but unfindable: the thing the
-- user deliberately kept would be the one thing search could not see.
-- rowid is kept equal to memories.rowid, which for an INTEGER PRIMARY KEY is
-- the id itself.
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    memory_id UNINDEXED,
    content,
    tags,
    tokenize='porter unicode61'
);

-- Bookkeeping for memory embeddings, mirroring embedded_conversations.  A
-- memory is short enough to be one vector, so there is no chunking here.
CREATE TABLE IF NOT EXISTS embedded_memories (
    memory_id   INTEGER PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
    content_hash TEXT,
    model       TEXT,
    embedded_at TEXT
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

-- Memories are small and written one at a time, so the index is maintained
-- inline rather than rebuilt.
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memory_fts(rowid, memory_id, content, tags)
    VALUES (new.rowid, new.id, COALESCE(new.content, ''),
            COALESCE(new.tags, ''));
END;

CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
    UPDATE memory_fts
       SET memory_id = new.id,
           content   = COALESCE(new.content, ''),
           tags      = COALESCE(new.tags, '')
     WHERE rowid = new.rowid;
END;

CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    DELETE FROM memory_fts WHERE rowid = old.rowid;
END;
"""


def utcnow():
    """Current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def migrate_legacy_database(db_path=DB_PATH):
    """Adopt a pre-rename ``ai_memory.db`` under the new name.

    Renaming the project would otherwise orphan every existing library: the
    app would find no database at the new path, create an empty one, and look
    exactly as though the user's conversations had been lost.  Runs only for
    the default filename, so an explicit --db is never second-guessed, and
    only when nothing already exists at the target.

    Returns True if a database was adopted.
    """
    if os.path.basename(db_path) != DB_FILENAME or os.path.exists(db_path):
        return False
    legacy = os.path.join(os.path.dirname(os.path.abspath(db_path)),
                          LEGACY_DB_FILENAME)
    if not os.path.exists(legacy):
        return False
    try:
        os.rename(legacy, db_path)
        # The write-ahead log and shared-memory file sit beside the database
        # and belong to it; leaving them behind would strand recent writes.
        for suffix in ("-wal", "-shm"):
            if os.path.exists(legacy + suffix):
                os.replace(legacy + suffix, db_path + suffix)
        return True
    except OSError:
        return False


def get_connection(db_path=DB_PATH):
    """Open a connection with dict-like rows and foreign keys enabled."""
    directory = os.path.dirname(os.path.abspath(db_path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    migrate_legacy_database(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(db_path=DB_PATH):
    """Create the schema, the FTS5 tables and their sync triggers."""
    conn = get_connection(db_path)
    with conn:
        conn.executescript(SCHEMA)
        conn.executescript(TRIGGERS)
        backfill_memory_fts(conn)
    return conn


def backfill_memory_fts(conn):
    """Index memories that were written before ``memory_fts`` existed.

    The triggers only fire on new writes, so a library that already held
    memories would keep them permanently unsearchable after an upgrade -- the
    facts the user deliberately kept would be the ones search could not find.
    Runs on every init and does nothing once the index has caught up.
    """
    missing = conn.execute(
        """SELECT m.rowid AS rowid, m.id, m.content, m.tags
             FROM memories m
            WHERE NOT EXISTS (SELECT 1 FROM memory_fts f
                               WHERE f.rowid = m.rowid)"""
    ).fetchall()
    for row in missing:
        conn.execute(
            "INSERT INTO memory_fts(rowid, memory_id, content, tags) "
            "VALUES (?, ?, ?, ?)",
            (row["rowid"], row["id"], row["content"] or "", row["tags"] or ""),
        )
    return len(missing)


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


def find_conversation_by_hash(conn, content_hash, provider_id=None):
    """Return the id of a conversation with this transcript, if one exists.

    Scoped to one provider when ``provider_id`` is given, and it always should
    be: identical text said to two different assistants is two conversations,
    not one.  Short exchanges collide easily -- "hello there" to Claude and to
    Gemini hash the same -- and without this scope the second import would
    silently discard the first's twin.
    """
    if not content_hash:
        return None
    if provider_id is None:
        row = conn.execute(
            "SELECT id FROM conversations WHERE content_hash = ? LIMIT 1",
            (content_hash,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT id FROM conversations WHERE content_hash = ? "
            "AND provider_id = ? LIMIT 1",
            (content_hash, provider_id),
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
        twin_id = find_conversation_by_hash(conn, content_hash, provider_id)
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
    """Drop all chains so detection can run again from scratch.

    The AUTOINCREMENT counter is reset too.  Detection rebuilds every chain on
    every import, so without this the ids climb forever and a library with two
    chains ends up numbering them 47 and 48.  Resetting keeps them small and
    makes them a function of the library's contents rather than of how many
    times it has been imported into.
    """
    conn.execute("DELETE FROM conversation_chain_members")
    conn.execute("DELETE FROM conversation_chains")
    try:
        conn.execute("DELETE FROM sqlite_sequence WHERE name = ?",
                     ("conversation_chains",))
    except Exception:
        pass  # no AUTOINCREMENT rows have been written yet


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

def get_setting(conn, key, default=None):
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (key,)
    ).fetchone()
    return row["value"] if row else default


def set_setting(conn, key, value):
    conn.execute(
        """INSERT INTO settings (key, value) VALUES (?, ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
        (key, str(value)),
    )
    conn.commit()


def get_flag(conn, key, default=True):
    """Read a boolean setting."""
    value = get_setting(conn, key)
    if value is None:
        return default
    return value == "1"


def set_flag(conn, key, enabled):
    set_setting(conn, key, "1" if enabled else "0")


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


# --------------------------------------------------------------------------
# Memories
# --------------------------------------------------------------------------

def _memory_row(row):
    """Shape one memories row for JSON, decoding the tags array."""
    record = dict(row)
    try:
        record["tags"] = json.loads(record["tags"]) if record["tags"] else []
    except ValueError:
        record["tags"] = []
    return record


def insert_memory(conn, content, source=None, tags=None, conversation_id=None):
    """Store one curated fact.  Returns the new row."""
    now = utcnow()
    cur = conn.execute(
        """INSERT INTO memories
               (content, source, tags, conversation_id, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (content, source,
         json.dumps(list(tags), ensure_ascii=False) if tags else None,
         conversation_id, now, now),
    )
    conn.commit()
    return get_memory(conn, cur.lastrowid)


def get_memory(conn, memory_id):
    row = conn.execute(
        "SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
    return _memory_row(row) if row else None


def get_memories(conn, limit=50, source=None, conversation_id=None):
    """List memories, newest first, optionally narrowed by source."""
    sql = "SELECT * FROM memories"
    clauses, params = [], []
    if source:
        clauses.append("source = ?")
        params.append(source)
    if conversation_id:
        clauses.append("conversation_id = ?")
        params.append(conversation_id)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY datetime(created_at) DESC, id DESC LIMIT ?"
    params.append(limit)
    return [_memory_row(r) for r in conn.execute(sql, params)]


def delete_memory(conn, memory_id):
    """Delete one memory.  Returns True if a row went away."""
    cur = conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
    conn.commit()
    return cur.rowcount > 0


def count_memories(conn):
    return conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
