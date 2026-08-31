"""Import orchestration and chain detection.

``import_file`` picks a provider importer for a file and runs it, then rebuilds
the conversation chains.  Chains are a naive grouping of conversations whose
titles talk about the same thing (see ``detect_chains``).
"""

import os
import re
from collections import Counter, defaultdict

from backend.core import database as db

# Words that carry no topical signal in a chat title.
STOPWORDS = {
    "a", "about", "an", "and", "any", "are", "as", "at", "be", "best", "but",
    "by", "can", "chat", "chatgpt", "conversation", "do", "does", "explain",
    "for", "from", "get", "give", "good", "has", "have", "help", "how", "i",
    "in", "is", "it", "its", "just", "know", "like", "make", "me", "my", "need",
    "new", "not", "of", "on", "one", "or", "please", "question", "questions",
    "should", "some", "that", "the", "their", "them", "then", "there", "these",
    "this", "to", "up", "use", "using", "want", "was", "way", "we", "what",
    "when", "where", "which", "why", "will", "with", "would", "you", "your",
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


def jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


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
    common = [w for w, n in counts.most_common(4) if n >= threshold]
    if common:
        return " ".join(w.capitalize() for w in common)
    earliest = members[0][1] or "Untitled"
    return earliest[:60]


def detect_chains(conn, threshold=0.5):
    """Group conversations with similar titles into chains.

    Two conversations are linked when the Jaccard similarity of their
    significant title words is above ``threshold``; linked pairs are merged
    transitively.  Existing chains are discarded and rebuilt so the function is
    safe to run after every import.

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
                if jaccard(word_sets[a], word_sets[b]) > threshold:
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
    return stats
