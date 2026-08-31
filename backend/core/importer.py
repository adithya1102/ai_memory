"""Import orchestration and chain detection.

``import_file`` picks a provider importer for a file and runs it, then rebuilds
the conversation chains.  Chains are a naive grouping of conversations whose
titles talk about the same thing (see ``detect_chains``).
"""

import os
import re
from collections import Counter, defaultdict

from backend.core import database as db

# Words that carry no topical signal in a chat title.  As well as ordinary
# English stopwords this covers generic nouns for the act of having a
# conversation ("discussion", "advice", "thoughts", ...).  Those describe the
# container rather than the subject, and two titles sharing only one of them
# are not about the same thing: "Project discussion" and "Gusto forecasting
# discussion" would otherwise chain on the strength of "discussion" alone.
#
# Singular and plural are both listed because significant_words() lowercases
# but does not stem, so "discussions" would otherwise survive as a distinct
# token and reintroduce exactly the false chain the singular removes.
STOPWORDS = {
    "a", "about", "advice", "an", "and", "answer", "answers", "any", "are",
    "as", "at", "be", "best", "but", "by", "can", "chat", "chatgpt",
    "conversation", "discussion", "discussions", "do", "does", "explain",
    "follow", "follows", "for", "from", "get", "give", "good", "has",
    "have", "help", "how", "i", "idea", "ideas", "in", "is", "it", "its",
    "just", "know", "like", "make", "me", "my", "need", "new", "not", "of",
    "on", "one", "or", "please", "question", "questions", "should", "some",
    "talk", "talks", "that", "the", "their", "them", "then", "there",
    "these", "this", "thoughts", "tip", "tips", "to", "up", "update",
    "updates", "use", "using", "want", "was", "way", "we", "what", "when",
    "where", "which", "why", "will", "with", "would", "you", "your",
}

_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


def significant_words(title):
    """Lowercase, de-punctuated, stopword-free words of a title."""
    if not title:
        return set()
    return {
        w for w in (m.group(0).lower() for m in _WORD_RE.finditer(title))
        if len(w) > 2 and w not in STOPWORDS
    }


def overlap_coeff(a, b):
    """Overlap coefficient: intersection size / min(set sizes).

    This measures how much of the smaller set is contained in the larger,
    which suits conversation titles better than Jaccard.  A title that is
    a subset of another (e.g. "Sourdough starter" vs "Sourdough starter
    feeding schedule") scores 1.0 rather than being penalised for length
    difference.

    Returns 0.0 if either set is empty.
    """
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


class _UnionFind:
    def __init__(self):
        self.parent = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _chain_name(members, word_sets):
    """Name a chain after the words most of its conversations share."""
    counts = Counter()
    for conv_id, _title, _created in members:
        counts.update(word_sets[conv_id])
    threshold = max(2, (len(members) + 1) // 2)
    # Sort ties alphabetically: set iteration order varies between processes,
    # and a chain that renames itself on every rebuild looks broken.
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    common = [w for w, n in ranked[:4] if n >= threshold]
    if common:
        return " ".join(w.capitalize() for w in common)
    earliest = members[0][1] or "Untitled"
    return earliest[:60]


def detect_chains(conn, threshold=0.5):
    """Group conversations with similar titles into chains.

    Two conversations are linked when the overlap coefficient of their
    significant title words reaches ``threshold``; linked pairs are merged
    transitively.  Existing chains are discarded and rebuilt so the function is
    safe to run after every import.

    The comparison is inclusive: at the default 0.5 a pair that shares exactly
    half of the shorter title's significant words links.  "Project discussion"
    and "Gusto forecasting discussion" score exactly 0.5, and an exclusive
    test would drop them on the boundary.

    Returns the number of chains created.
    """
    rows = conn.execute(
        "SELECT id, title, created_at FROM conversations"
    ).fetchall()

    word_sets = {}
    for row in rows:
        words = significant_words(row["title"])
        if words:
            word_sets[row["id"]] = words

    # Only conversations sharing at least one word can clear the threshold, so
    # compare within an inverted index instead of over every pair.
    by_word = defaultdict(list)
    for conv_id, words in word_sets.items():
        for word in words:
            by_word[word].append(conv_id)

    uf = _UnionFind()
    checked = set()
    for candidates in by_word.values():
        for i, a in enumerate(candidates):
            for b in candidates[i + 1:]:
                pair = (a, b) if a < b else (b, a)
                if pair in checked:
                    continue
                checked.add(pair)
                if overlap_coeff(word_sets[a], word_sets[b]) >= threshold:
                    uf.union(a, b)

    groups = defaultdict(list)
    for conv_id in word_sets:
        groups[uf.find(conv_id)].append(conv_id)

    meta = {r["id"]: (r["title"], r["created_at"]) for r in rows}

    with conn:
        db.clear_chains(conn)
        created = 0
        for members in groups.values():
            if len(members) < 2:
                continue
            # Position within a chain follows chronology.
            ordered = sorted(
                ((cid, meta[cid][0], meta[cid][1]) for cid in members),
                key=lambda t: (t[2] or "", t[1] or ""),
            )
            chain_id = db.create_chain(conn, _chain_name(ordered, word_sets))
            for position, (conv_id, _title, _created) in enumerate(ordered):
                db.add_conversation_to_chain(conn, chain_id, conv_id, position)
            created += 1
    return created


def import_file(conn, file_path, provider="chatgpt"):
    """Import an export file and rebuild chains.

    Returns the provider importer's stats dict with ``chains`` added.
    """
    from backend.providers.chatgpt_importer import import_chatgpt_export

    importers = {"chatgpt": import_chatgpt_export}
    if provider not in importers:
        raise ValueError("Unknown provider: %s" % provider)
    if not os.path.exists(file_path):
        raise FileNotFoundError(file_path)

    stats = importers[provider](conn, file_path)
    stats["chains"] = detect_chains(conn)
    stats.update(embed_new_conversations(conn))
    return stats


def embed_new_conversations(conn):
    """Embed conversations that are new or changed, if semantic search is on.

    Embedding must never break an import: the library is fully usable with
    keyword search alone, so every failure here is reported and swallowed.
    """
    from backend.core.search import SEMANTIC_ENABLED_KEY

    if not db.get_flag(conn, SEMANTIC_ENABLED_KEY, default=True):
        return {"embedded": 0, "embedding_note": "semantic search is turned off"}
    try:
        from backend.core.embeddings import sync_embeddings
        result = sync_embeddings(conn)
        return {"embedded": result["conversations"],
                "embedding_note": result["skipped"]}
    except Exception as exc:  # noqa: BLE001 - surfaced, not raised
        return {"embedded": 0, "embedding_note": str(exc)}
