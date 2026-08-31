"""Local semantic search: chunking, embedding and vector retrieval.

Everything runs on this machine.  Text is embedded with sentence-transformers
(all-MiniLM-L6-v2, 384 dimensions) and the vectors live in the same SQLite file
as the rest of the library, in a sqlite-vec virtual table.

The whole module is optional.  If sentence-transformers or sqlite-vec is not
installed, or the model has never been downloaded, ``availability()`` reports
why and every entry point degrades to a no-op so search falls back to keyword
matching.
"""

import importlib.util
import os
import threading

MODEL_NAME = "all-MiniLM-L6-v2"
EMBEDDING_DIM = 384

# all-MiniLM-L6-v2 truncates at 256 word-piece tokens, so a 500-token chunk
# would have half of its text silently ignored by the encoder while still
# occupying a row and looking indexed.  Chunks are sized to the model's window
# instead, with room for the [CLS]/[SEP] pair.
#
# Sizing is measured in real tokens rather than words: prose runs about 1.3
# tokens per word, but code, URLs and identifiers can run past 3 (the literal
# "word145" is three word-pieces), so a word budget silently overflows on
# exactly the content people paste into chat.
CHUNK_TOKENS = 220
CHUNK_OVERLAP_TOKENS = 40

# Cosine distance below which a chunk is not worth returning.  Kept low on
# purpose: genuine paraphrase matches score around 0.3 similarity, so a strict
# floor would throw away exactly the hits semantic search exists to find.
MIN_SIMILARITY = 0.15

_model = None
_model_lock = threading.Lock()      # held for the duration of a load
_state_lock = threading.Lock()      # always fast; never held across a load
_model_state = "idle"               # idle | loading | ready | failed
_model_error = None


# --------------------------------------------------------------------------
# Availability
# --------------------------------------------------------------------------

def _installed(module_name):
    """Is a module importable, without actually importing it?

    find_spec() only resolves the module on disk.  Actually importing
    sentence_transformers drags in PyTorch and costs seconds, and this runs on
    request threads and at startup, so it must stay cheap.
    """
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


def availability():
    """Return (ok, reason).  ``reason`` is user-facing when ok is False."""
    if not _installed("sentence_transformers"):
        return False, ("sentence-transformers is not installed "
                       "(pip install -r requirements.txt)")
    if not _installed("sqlite_vec"):
        return False, "sqlite-vec is not installed (pip install -r requirements.txt)"
    return True, "ready"


def load_vec(conn):
    """Load the sqlite-vec extension onto a connection.  Returns True on success.

    Loading is per-connection, so this runs on any path that touches vectors.
    Asking the connection whether vec_version() resolves is the cheapest way to
    tell whether it is already loaded -- sqlite3.Connection is a C type and
    cannot carry a cache attribute.
    """
    try:
        conn.execute("SELECT vec_version()").fetchone()
        return True
    except Exception:
        pass
    try:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        return True
    except Exception:
        return False
    finally:
        try:
            conn.enable_load_extension(False)
        except Exception:
            pass


def ensure_vector_table(conn):
    """Create the vector table if the extension is available."""
    if not load_vec(conn):
        return False
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS chunk_vectors USING vec0("
        "embedding float[%d] distance_metric=cosine)" % EMBEDDING_DIM
    )
    return True


def _quiet_progress_bars():
    """Stop the model loader writing progress bars to stdout.

    This app can be launched with stdout attached to a pipe nobody drains -- a
    GUI launcher, pythonw, a supervising process.  Once that pipe's buffer
    fills, the writing thread blocks forever and takes the whole app with it,
    including request handling.  Best-effort: never fail because a progress bar
    could not be silenced.
    """
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    try:
        from transformers.utils import logging as hf_logging
        hf_logging.disable_progress_bar()
    except Exception:
        pass


def _set_state(state, error=None):
    global _model_state, _model_error
    with _state_lock:
        _model_state = state
        _model_error = error


def model_state():
    """Current encoder state, safe to call from a request handler.

    Returns a dict with ``state`` (unavailable | idle | loading | ready |
    failed), ``ready`` and a human-readable ``reason``.
    """
    ok, reason = availability()
    if not ok:
        return {"state": "unavailable", "ready": False, "reason": reason}
    with _state_lock:
        state, error = _model_state, _model_error
    return {
        "state": state,
        "ready": state == "ready",
        "reason": error or {
            "idle": "not loaded yet",
            "loading": "loading the embedding model",
            "ready": "ready",
        }.get(state, ""),
    }


def get_model():
    """Load the encoder, blocking until it is ready.

    Used by the indexing path, which has to wait.  Query paths should use
    get_model_if_ready() so a request never stalls behind a cold load.
    """
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is None:
            _set_state("loading")
            try:
                _quiet_progress_bars()
                from sentence_transformers import SentenceTransformer
                model = SentenceTransformer(MODEL_NAME)
            except Exception as exc:
                _set_state("failed", str(exc))
                raise
            _model = model
            _set_state("ready")
    return _model


def get_model_if_ready():
    """Return the encoder only if it is already loaded, else None.

    Never blocks: it takes only the fast state lock, so a search issued while
    the preload thread is still working returns immediately with whatever
    keyword results exist.
    """
    with _state_lock:
        return _model if _model_state == "ready" else None


def start_preload():
    """Begin loading the encoder on a background thread.

    Called at startup so the first semantic search does not pay the load cost.
    Returns the thread, or None if loading is unnecessary or impossible.
    """
    global _model_state
    ok, _reason = availability()
    if not ok:
        return None
    with _state_lock:
        if _model_state in ("loading", "ready"):
            return None
        # Claim the slot before releasing the lock so two callers cannot both
        # spawn a loader.  _set_state() would deadlock here: the lock is held.
        _model_state = "loading"

    def _load():
        # Importing sentence_transformers pulls in PyTorch, so it happens here
        # on the background thread rather than in the caller.
        try:
            get_model()
        except Exception:
            pass  # state already records the failure

    thread = threading.Thread(target=_load, name="embedding-preload", daemon=True)
    thread.start()
    return thread


# --------------------------------------------------------------------------
# Chunking
# --------------------------------------------------------------------------

def _token_spans(tokenizer, text):
    """Character offsets of each token, or None if the tokenizer cannot say.

    Slicing the original string by offset keeps the chunk text exactly as the
    user wrote it -- decoding token ids back would lowercase everything, since
    this model's tokenizer is uncased, and that text is shown in snippets.
    """
    try:
        encoded = tokenizer(text, add_special_tokens=False,
                            return_offsets_mapping=True, truncation=False)
        spans = [tuple(o) for o in encoded["offset_mapping"] if o[1] > o[0]]
        return spans or None
    except Exception:
        return None


def _snap_to_words(text, start, end):
    """Widen a character span to whole words.

    A token boundary is not a word boundary -- "word96" is the two pieces
    "word" and "##96", so slicing between them would leave the fragment
    "word9" at the end of one chunk.  Widening costs a couple of tokens and
    keeps both the embedding and the displayed snippet clean.
    """
    while start > 0 and not text[start - 1].isspace():
        start -= 1
    while end < len(text) and not text[end].isspace():
        end += 1
    return start, end


def chunk_messages(messages, tokenizer=None, chunk_tokens=CHUNK_TOKENS,
                   overlap_tokens=CHUNK_OVERLAP_TOKENS):
    """Split a conversation's messages into overlapping chunks.

    Chunks never span two messages: a question and an unrelated later answer
    should not be blended into one vector.  A long message is split into
    overlapping windows so a match near a boundary is not lost.

    Windows are measured in model tokens so no chunk can exceed the encoder's
    input limit.  If the tokenizer cannot report offsets, falls back to a
    conservative word budget.

    Returns a list of dicts with role, content and chunk_index.
    """
    if tokenizer is None:
        tokenizer = get_model().tokenizer

    step = max(1, chunk_tokens - overlap_tokens)
    chunks = []

    for message in messages:
        text = (message.get("content") or "").strip()
        if not text:
            continue

        spans = _token_spans(tokenizer, text)
        pieces = []
        if spans is None:
            # No offsets available: assume the worst plausible ratio of ~3
            # tokens per word so the window still fits the encoder.
            words = text.split()
            budget = max(1, chunk_tokens // 3)
            word_step = max(1, budget - overlap_tokens // 3)
            for start in range(0, len(words), word_step):
                pieces.append(" ".join(words[start:start + budget]))
                if start + budget >= len(words):
                    break
        else:
            start = 0
            while start < len(spans):
                window = spans[start:start + chunk_tokens]
                lo, hi = _snap_to_words(text, window[0][0], window[-1][1])
                pieces.append(text[lo:hi].strip())
                if start + chunk_tokens >= len(spans):
                    break
                start += step

        for piece in pieces:
            if piece:
                chunks.append({
                    "role": message.get("role"),
                    "content": piece,
                    "chunk_index": len(chunks),
                })
    return chunks


# --------------------------------------------------------------------------
# Indexing
# --------------------------------------------------------------------------

def _delete_chunks(conn, conversation_id):
    """Drop a conversation's chunks and their vectors."""
    rows = conn.execute(
        "SELECT id FROM chunks WHERE conversation_id = ?", (conversation_id,)
    ).fetchall()
    for row in rows:
        conn.execute("DELETE FROM chunk_vectors WHERE rowid = ?", (row[0],))
    conn.execute("DELETE FROM chunks WHERE conversation_id = ?", (conversation_id,))


def pending_conversations(conn):
    """Conversations whose transcript is new or has changed since embedding."""
    return conn.execute(
        """SELECT c.id, c.content_hash
             FROM conversations c
             LEFT JOIN embedded_conversations e ON e.conversation_id = c.id
            WHERE e.conversation_id IS NULL
               OR e.content_hash IS NOT c.content_hash
               OR e.model IS NOT ?
            ORDER BY c.created_at""",
        (MODEL_NAME,),
    ).fetchall()


def sync_embeddings(conn, batch_size=64, progress=None):
    """Embed every conversation that is new or changed since the last run.

    Incremental: a conversation is re-embedded only when its content_hash
    differs from the hash recorded at embedding time (or the model changed).
    Returns a stats dict; ``skipped`` carries the reason when nothing ran.
    """
    stats = {"conversations": 0, "chunks": 0, "skipped": None}

    ok, reason = availability()
    if not ok:
        stats["skipped"] = reason
        return stats
    if not ensure_vector_table(conn):
        stats["skipped"] = "sqlite-vec extension could not be loaded"
        return stats

    pending = pending_conversations(conn)
    _prune_orphans(conn)
    if not pending:
        conn.commit()
        return stats

    try:
        model = get_model()
    except Exception as exc:  # model not downloaded, no network, corrupt cache
        stats["skipped"] = "could not load %s: %s" % (MODEL_NAME, exc)
        return stats

    import sqlite_vec
    from backend.core import database as db

    for done, row in enumerate(pending, 1):
        conversation_id = row["id"]
        messages = db.get_messages(conn, conversation_id)
        chunks = chunk_messages(messages, tokenizer=model.tokenizer)

        _delete_chunks(conn, conversation_id)

        if chunks:
            vectors = model.encode([c["content"] for c in chunks],
                                   batch_size=batch_size,
                                   normalize_embeddings=True,
                                   show_progress_bar=False)
            for chunk, vector in zip(chunks, vectors):
                cur = conn.execute(
                    """INSERT INTO chunks
                           (conversation_id, chunk_index, role, content)
                       VALUES (?, ?, ?, ?)""",
                    (conversation_id, chunk["chunk_index"], chunk["role"],
                     chunk["content"]),
                )
                conn.execute(
                    "INSERT INTO chunk_vectors(rowid, embedding) VALUES (?, ?)",
                    (cur.lastrowid, sqlite_vec.serialize_float32(vector)),
                )

        conn.execute(
            """INSERT INTO embedded_conversations
                   (conversation_id, content_hash, model, chunk_count, embedded_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(conversation_id) DO UPDATE SET
                   content_hash = excluded.content_hash,
                   model        = excluded.model,
                   chunk_count  = excluded.chunk_count,
                   embedded_at  = excluded.embedded_at""",
            (conversation_id, row["content_hash"], MODEL_NAME, len(chunks),
             db.utcnow()),
        )
        stats["conversations"] += 1
        stats["chunks"] += len(chunks)
        if progress:
            progress(done, len(pending))
        if done % 25 == 0:
            conn.commit()

    conn.commit()
    return stats


def _prune_orphans(conn):
    """Drop vectors and bookkeeping whose conversation or chunk has gone.

    Conversations cascade into ``chunks``, but a vec0 virtual table cannot
    carry a foreign key, so its rows have to be swept up separately.
    """
    conn.execute(
        """DELETE FROM embedded_conversations
            WHERE conversation_id NOT IN (SELECT id FROM conversations)"""
    )
    orphans = conn.execute(
        "SELECT rowid FROM chunk_vectors WHERE rowid NOT IN (SELECT id FROM chunks)"
    ).fetchall()
    for row in orphans:
        conn.execute("DELETE FROM chunk_vectors WHERE rowid = ?", (row[0],))


# --------------------------------------------------------------------------
# Retrieval
# --------------------------------------------------------------------------

def has_embeddings(conn):
    """True when at least one vector is stored and usable."""
    if not load_vec(conn):
        return False
    try:
        return conn.execute("SELECT COUNT(*) FROM chunk_vectors").fetchone()[0] > 0
    except Exception:
        return False


def embedding_stats(conn):
    """Counts for the settings page."""
    ok, reason = availability()
    total = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
    try:
        embedded = conn.execute(
            "SELECT COUNT(*) FROM embedded_conversations").fetchone()[0]
        chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    except Exception:
        embedded = chunks = 0
    state = model_state()
    return {
        "available": ok,
        "reason": reason,
        "state": state["state"],
        "state_reason": state["reason"],
        "model": MODEL_NAME,
        "dimensions": EMBEDDING_DIM,
        "conversations": total,
        "embedded": embedded,
        "chunks": chunks,
        "pending": max(0, total - embedded),
    }


def semantic_search(conn, query, top_k=10):
    """Return the top_k most similar chunks to the query.

    Each result is a dict with conversation_id, chunk content and similarity
    (1.0 = identical).  Returns [] whenever semantic search cannot run, so
    callers can treat it as "no extra results" rather than an error.
    """
    if not query or not query.strip():
        return []
    ok, _reason = availability()
    if not ok or not load_vec(conn):
        return []
    if not has_embeddings(conn):
        return []

    # Never block a search on a cold model.  If it is still loading, the caller
    # gets keyword results now and the UI retries once loading finishes.
    model = get_model_if_ready()
    if model is None:
        start_preload()
        return []

    try:
        import sqlite_vec
        vector = model.encode([query], normalize_embeddings=True,
                              show_progress_bar=False)[0]
        rows = conn.execute(
            """SELECT v.rowid AS chunk_id, v.distance AS distance,
                      ch.conversation_id, ch.content, ch.role
                 FROM chunk_vectors v
                 JOIN chunks ch ON ch.id = v.rowid
                WHERE v.embedding MATCH ? AND k = ?
                ORDER BY v.distance""",
            (sqlite_vec.serialize_float32(vector), top_k),
        ).fetchall()
    except Exception:
        return []

    results = []
    for row in rows:
        similarity = 1.0 - row["distance"]
        if similarity < MIN_SIMILARITY:
            continue
        results.append({
            "conversation_id": row["conversation_id"],
            "chunk_id": row["chunk_id"],
            "content": row["content"],
            "role": row["role"],
            "similarity": round(similarity, 4),
        })
    return results
