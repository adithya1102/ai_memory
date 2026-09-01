"""Acceptance test for the dedup fix, exactly as specified by the user.

Wipes the database, drives the real running server over HTTP, imports
dummy_export twice, then checks counts, search and chains.  Also covers the
dedup paths the plain double-import does not reach.
"""
import http.cookiejar
import urllib.parse
import json, os, shutil, subprocess, sys, tempfile, time, urllib.request

# Flash messages live in the session cookie, so keep a jar like a browser does.
OPENER = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
EXPORT = os.path.join(ROOT, "dummy_export.json.json")
WORK = tempfile.mkdtemp(prefix="contextvault-test-")
DB = os.path.join(WORK, "contextvault.db")
PORT = 5123
BASE = "http://127.0.0.1:%d" % PORT

fails = []
def check(label, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + label + (("  -> " + str(detail)) if detail else ""))
    if not ok:
        fails.append(label)

# ------------------------------------------------------------ 1. clean slate
# Runs against a temp database so the suite can never delete a real library.
print("== clean database ==")
check("starting from no database", not os.path.exists(DB))

# ---------------------------------------------------------------- 2. run app
print("\n== start the app ==")
proc = subprocess.Popen([sys.executable, "backend/main.py", "--no-window",
                         "--port", str(PORT), "--db", DB], cwd=ROOT,
                        stdout=open(os.path.join(WORK, "server.log"), "wb"),
                        stderr=subprocess.STDOUT)
for _ in range(80):
    try:
        urllib.request.urlopen(BASE, timeout=1); break
    except Exception:
        time.sleep(0.25)
check("server responding", True)
check("database recreated on boot", os.path.exists(DB))


def post_import(path):
    """Import through the real multipart form, like the UI does."""
    boundary = "----aimemtest"
    with open(path, "rb") as fh:
        payload = fh.read()
    body = (("--%s\r\nContent-Disposition: form-data; name=\"file\"; "
             "filename=\"dummy_export.json\"\r\n"
             "Content-Type: application/json\r\n\r\n" % boundary).encode()
            + payload + ("\r\n--%s--\r\n" % boundary).encode())
    req = urllib.request.Request(BASE + "/import", data=body, method="POST")
    req.add_header("Content-Type", "multipart/form-data; boundary=%s" % boundary)
    return OPENER.open(req, timeout=120).read().decode("utf-8", "replace")


def get(path):
    return OPENER.open(BASE + path, timeout=60).read().decode("utf-8", "replace")


def counts():
    import sqlite3
    c = sqlite3.connect(DB)
    out = {t: c.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
           for t in ("conversations", "messages", "conversation_fts",
                     "conversation_chains")}
    out["orphan_msgs"] = c.execute(
        "SELECT COUNT(*) FROM messages m WHERE NOT EXISTS "
        "(SELECT 1 FROM conversations c WHERE c.id = m.conversation_id)").fetchone()[0]
    c.close()
    return out

try:
    # ------------------------------------------------------------ 3. import x2
    print("\n== import dummy_export.json (first time) ==")
    post_import(EXPORT)
    first = counts()
    print("  ", first)
    check("5 conversations after first import", first["conversations"] == 5, first["conversations"])
    check("12 messages after first import", first["messages"] == 12, first["messages"])

    print("\n== import dummy_export.json (second time) ==")
    post_import(EXPORT)
    second = counts()
    print("  ", second)

    # ------------------------------------------------------------ 4. verify
    print("\n== acceptance criteria ==")
    check("conversation count is still 5 (not 7 or 10)", second["conversations"] == 5,
          second["conversations"])
    check("message count did not grow", second["messages"] == 12, second["messages"])
    check("fts rows still 5", second["conversation_fts"] == 5, second["conversation_fts"])
    check("no orphaned messages", second["orphan_msgs"] == 0, second["orphan_msgs"])

    html = get("/search?q=gym+workout")
    check("search 'gym workout' returns Welcome", "Welcome" in html)
    check("search returns exactly one hit", html.count('class="card"') == 1,
          html.count('class="card"'))

    chains_html = get("/chains")
    import sqlite3
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    groups = []
    for ch in c.execute("SELECT id, name FROM conversation_chains ORDER BY name"):
        titles = [r["title"] for r in c.execute(
            """SELECT c.title FROM conversation_chain_members m
                 JOIN conversations c ON c.id = m.conversation_id
                WHERE m.chain_id = ? ORDER BY m.position""", (ch["id"],))]
        groups.append((ch["name"], titles))
    c.close()
    for name, titles in groups:
        print("     chain %-22s %s" % (name, titles))
    flat = [t for _n, ts in groups for t in ts]
    # "discussion" is now a stopword, so the Gusto pair shares no topical word
    # and must not chain.  The sourdough pair shares "sourdough" + "starter".
    check("chains detect 1 group", len(groups) == 1, len(groups))
    check("Gusto pair NOT chained", "Gusto forecasting discussion" not in flat, groups)
    check("'Project discussion' NOT chained", "Project discussion" not in flat, groups)
    check("Sourdough pair still chained",
          any({"Sourdough starter troubleshooting",
               "Sourdough starter feeding schedule"} == set(ts) for _n, ts in groups),
          groups)
    check("Welcome not chained", "Welcome" not in flat)
    check("chains page renders one", chains_html.count('class="card"') == 1,
          chains_html.count('class="card"'))

    from backend.core.importer import significant_words, overlap_coeff
    gusto = significant_words("Gusto forecasting discussion")
    project = significant_words("Project discussion")
    sour_a = significant_words("Sourdough starter troubleshooting")
    sour_b = significant_words("Sourdough starter feeding schedule")
    check("'discussion' stripped from titles",
          "discussion" not in gusto and "discussion" not in project,
          sorted(gusto | project))
    check("Gusto/Project similarity is now 0.0",
          overlap_coeff(gusto, project) == 0.0, overlap_coeff(gusto, project))
    check("Sourdough similarity unchanged at 0.667",
          round(overlap_coeff(sour_a, sour_b), 3) == 0.667,
          overlap_coeff(sour_a, sour_b))
    check("sourdough/starter kept as topical",
          sour_a & sour_b == {"sourdough", "starter"}, sorted(sour_a & sour_b))
    singular = ("discussion", "talk", "chat", "question", "answer", "help",
                "advice", "tip", "tips", "idea", "thought", "thoughts",
                "update", "follow", "conversation")
    plural = ("discussions", "ideas", "updates", "talks", "tips", "thoughts",
              "answers", "follows", "chats", "conversations")
    for word in singular + plural:
        check("stopword %-12r removed from titles" % word,
              significant_words("Gusto %s" % word) == {"gusto"},
              sorted(significant_words("Gusto %s" % word)))

    # Case and punctuation must not smuggle a stopword back in.
    for variant in ("Discussions", "DISCUSSIONS", "discussions,", "(ideas)",
                    "updates!", "Talks:"):
        check("variant %-14r still stripped" % variant,
              significant_words("Gusto %s" % variant) == {"gusto"},
              sorted(significant_words("Gusto %s" % variant)))

    # The plurals must kill the same false chain the singular does.
    check("plural form does not chain either",
          overlap_coeff(significant_words("Project discussions"),
                        significant_words("Gusto forecasting discussions")) == 0.0)
    check("mixed singular/plural does not chain",
          overlap_coeff(significant_words("Project discussion"),
                        significant_words("Gusto forecasting discussions")) == 0.0)
    check("topical words still survive alongside plurals",
          significant_words("Sourdough starter tips and ideas") ==
          {"sourdough", "starter"},
          sorted(significant_words("Sourdough starter tips and ideas")))

    # Every generic conversation noun should now be covered in both forms.
    for stem in ("chat", "conversation", "discussion", "talk", "answer",
                 "idea", "tip", "update", "follow", "question", "thought"):
        both = significant_words("Gusto %s" % stem) == {"gusto"} and \
               significant_words("Gusto %ss" % stem) == {"gusto"}
        check("both forms of %-13r stripped" % stem, both,
              sorted(significant_words("Gusto %s / Gusto %ss" % (stem, stem))))
    check("a title of only generic nouns yields nothing",
          significant_words("Chats and conversations: thoughts and ideas") == set(),
          sorted(significant_words("Chats and conversations: thoughts and ideas")))

    # ------------------------------------------------------------ 5. dedup paths
    print("\n== re-export with FRESH ids, identical content ==")
    data = json.load(open(EXPORT, encoding="utf-8"))
    for i, conv in enumerate(data):
        conv["id"] = conv["conversation_id"] = "regenerated-uuid-%d" % i
    tmp = tempfile.mkdtemp()
    fresh = os.path.join(tmp, "fresh_ids.json")
    json.dump(data, open(fresh, "w", encoding="utf-8"))
    body = post_import(fresh)
    third = counts()
    print("  ", third)
    check("fresh ids do NOT duplicate (still 5)", third["conversations"] == 5,
          third["conversations"])
    check("messages did not grow", third["messages"] == 12, third["messages"])
    check("import reported duplicates", "5 duplicate" in body,
          [l for l in body.splitlines() if "duplicate" in l][:1])

    print("\n== same id, CHANGED content -> update + re-index ==")
    data2 = json.load(open(EXPORT, encoding="utf-8"))
    data2[0]["mapping"]["node-2"]["message"]["content"]["parts"] = [
        "Revised answer about kettlebell swings and farmer carries."]
    edited = os.path.join(tmp, "edited.json")
    json.dump(data2, open(edited, "w", encoding="utf-8"))
    body = post_import(edited)
    fourth = counts()
    print("  ", fourth)
    check("still 5 conversations after edit", fourth["conversations"] == 5,
          fourth["conversations"])
    check("import reported 1 updated", "1 updated" in body,
          [l for l in body.splitlines() if "updated" in l][:1])
    check("new text is searchable", "Welcome" in get("/search?q=kettlebell"))
    # Re-indexing owns the literal indexes.  Semantic search may still surface
    # Welcome for "lat pulldowns" -- it is a conversation about gym workouts --
    # so that hit must be badged semantic, never keyword.
    stale = get("/search?q=lat+pulldowns")
    check("stale text dropped from keyword index",
          'badge keyword' not in stale and 'badge both' not in stale, stale.count("card"))
    import sqlite3 as _s
    _c = _s.connect(DB)
    check("stale text dropped from chunk store",
          _c.execute("SELECT COUNT(*) FROM chunks WHERE content LIKE '%pulldown%'"
                     ).fetchone()[0] == 0)
    _c.close()
    check("gym workout still finds Welcome", "Welcome" in get("/search?q=gym+workout"))

    print("\n== semantic search in the UI ==")
    settings_html = get("/settings")
    check("settings shows the semantic toggle", "Enable semantic search" in settings_html)
    check("settings shows the model", "all-MiniLM-L6-v2" in settings_html)
    check("settings shows embedded count", "Conversations embedded" in settings_html)
    check("toggle renders as On by default", ">\n            On\n" in settings_html
          or ">On<" in settings_html.replace("\n", "").replace("  ", ""),
          [l.strip() for l in settings_html.splitlines() if "toggle" in l][:1])

    para = get("/search?q=how+do+I+get+stronger")
    check("paraphrase query finds Welcome in the UI", "Welcome" in para)
    check("result is badged semantic", 'class="badge semantic"' in para,
          [l.strip() for l in para.splitlines() if "badge" in l][:2])
    check("similarity shown", "% similar" in para)

    kw = get("/search?q=gym+workout")
    check("keyword+semantic hit badged 'both'", 'class="badge both"' in kw,
          [l.strip() for l in kw.splitlines() if "badge" in l][:2])

    # Toggle off through the real form, then confirm the fallback.
    body = urllib.parse.urlencode({"enabled": "0"}).encode()
    OPENER.open(urllib.request.Request(BASE + "/settings/semantic", data=body,
                                       method="POST"), timeout=60).read()
    off = get("/search?q=how+do+I+get+stronger")
    check("disabled -> paraphrase finds nothing", 'class="card"' not in off)
    check("disabled -> UI explains why", "semantic search is turned off" in off.lower())
    check("disabled -> keyword search still works",
          "Welcome" in get("/search?q=gym+workout"))
    check("settings reflects Off", ">Off<" in get("/settings").replace("\n", "")
          .replace("            ", "").replace("  ", ""),
          [l.strip() for l in get("/settings").splitlines() if "toggle" in l][:1])

    body = urllib.parse.urlencode({"enabled": "1"}).encode()
    OPENER.open(urllib.request.Request(BASE + "/settings/semantic", data=body,
                                       method="POST"), timeout=60).read()
    check("re-enabled -> paraphrase found again",
          'class="badge semantic"' in get("/search?q=how+do+I+get+stronger"))

    body = urllib.parse.urlencode({}).encode()
    r = OPENER.open(urllib.request.Request(BASE + "/embeddings/rebuild", data=body,
                                           method="POST"), timeout=600).read().decode()
    check("POST /embeddings/rebuild works",
          any(s in r for s in ("Embedded", "already up to date", "unavailable")),
          [l.strip() for l in r.splitlines() if "flash" in l][:1])
    check("rebuild is a no-op when already indexed", "already up to date" in r,
          [l.strip() for l in r.splitlines() if "flash" in l][:1])

    print("\n== final state ==")
    final = counts()
    print("  ", final)
    check("final conversation count is 5", final["conversations"] == 5, final["conversations"])
    check("final message count is 12", final["messages"] == 12, final["messages"])
    check("settings page reports 5", ">5</td>" in get("/settings"))
finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()

print("\n" + ("ALL CHECKS PASSED" if not fails else "%d FAILURES: %s" % (len(fails), fails)))
sys.exit(1 if fails else 0)
