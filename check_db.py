import sqlite3

conn = sqlite3.connect('data/ai_memory.db')
conn.row_factory = sqlite3.Row

print("=== Conversations ===")
for row in conn.execute('SELECT id, title, content_hash, created_at FROM conversations ORDER BY created_at'):
    hash_val = row['content_hash'][:12] if row['content_hash'] else 'NONE'
    print(f"{row['id']} | {row['title']} | hash={hash_val}... | {row['created_at']}")

print()
print("=== Messages per conversation ===")
for row in conn.execute('SELECT conversation_id, COUNT(*) as cnt FROM messages GROUP BY conversation_id'):
    print(f"{row['conversation_id']}: {row['cnt']} messages")

print()
print("=== Chains ===")
for row in conn.execute('SELECT id, name FROM conversation_chains'):
    print(f"Chain {row['id']}: {row['name']}")
    for member in conn.execute('SELECT conversation_id, position FROM conversation_chain_members WHERE chain_id = ? ORDER BY position', (row['id'],)):
        print(f"  - {member['conversation_id']} (pos {member['position']})")

conn.close()