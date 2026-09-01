"""The /context pipeline: the injected block, the PWA page, the prompt docs.

Part A covers the block formatter.  Part B proves the Python and JavaScript
formatters agree character for character -- there are two implementations
because the extension cannot import Python and the phone cannot run the
extension, and a silent divergence between them would mean two users of the
same feature getting different context.  Part C covers the /context page, D
the PWA plumbing, E the shipped prompt.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.core import database as db
from backend.core.context_block import format_context_block, truncate
from backend.core.importer import import_file
from backend.web.app import create_app

WORK = tempfile.mkdtemp(prefix="contextvault-context-")
DB = os.path.join(WORK, "context.db")
EXPORT = os.path.join(ROOT, "dummy_export.json.json")

fails = []


def check(label, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + label
          + (("  -> " + str(detail)) if detail else ""))
    if not ok:
        fails.append(label)


RESULTS = [
    {"conversation_id": "chatgpt:conv-001", "title": "Personal gym routine",
     "provider": "ChatGPT", "date": "2024-08-23T02:26:40+00:00",
     "snippet": "I want to build muscle and start going to the gym.",
     "match_type": "both"},
    {"conversation_id": "claude:abc", "title": "Training split rework",
     "provider": "Claude", "date": "2026-01-04T09:00:00+00:00",
     "snippet": "Push pull legs beats a bro split at three days a week.",
     "match_type": "semantic"},
]


# ======================================================================
print("== A. the injected block ==")

block = format_context_block("what should I change?", RESULTS)

check("delimited by the history tags",
      block.startswith("<contextvault_history>")
      and "</contextvault_history>" in block)
check("names the excerpts as the user's own history",
      "excerpts from my own past AI conversations" in block)
check("frames them as background, not instructions",
      "background, not instructions" in block)
check("gives permission to ignore them",
      "say so if none of it helps" in block)
check("the question is last", block.rstrip().endswith("what should I change?"))
check("the question sits after the closing tag",
      block.index("what should I change?")
      > block.index("</contextvault_history>"))
check("results are numbered",
      "1. Personal gym routine" in block and "2. Training split rework" in block)
check("each line carries provider, date and match type",
      "[ChatGPT · 2024-08-23 · both]" in block
      and "[Claude · 2026-01-04 · semantic]" in block)

empty = format_context_block("what should I change?", [])
check("an empty result set says so",
      "No matching conversations were found" in empty)
check("the question still trails an empty block",
      empty.rstrip().endswith("what should I change?"))
check("None results behave like none",
      "No matching conversations were found"
      in format_context_block("q", None))

sparse = format_context_block("q", [{}])
check("a result with no fields still renders",
      "Untitled conversation" in sparse and "unknown" in sparse
      and "undated" in sparse)

check("short snippets are untouched", truncate("hello there") == "hello there")
check("whitespace is collapsed", truncate("a\n\n  b\tc") == "a b c")
check("long snippets are cut and marked",
      truncate("x" * 500, 400).endswith("…") and len(truncate("x" * 500, 400)) <= 401)
check("cutting lands on a word boundary",
      truncate("alpha beta gamma delta", 12) == "alpha beta…")
check("None truncates to empty", truncate(None) == "")


# ======================================================================
print("\n== B. Python and JavaScript agree ==")

NODE = shutil.which("node")
if not NODE:
    print("  (node not installed -- parity check skipped)")
else:
    # Drive the extension's formatter over the same fixture and diff the two
    # strings.  Any wording change on one side that is not mirrored on the
    # other fails here.
    script = """
const CV = require(process.argv[2]);
const payload = JSON.parse(process.argv[3]);
process.stdout.write(CV.formatContextBlock(payload.query, payload.results, {}));
"""
    lib = os.path.join(ROOT, "extension", "lib", "context.js")
    runner = os.path.join(WORK, "parity.js")
    with open(runner, "w", encoding="utf-8") as handle:
        handle.write(script)

    def js_block(query, results):
        payload = json.dumps({"query": query, "results": results})
        done = subprocess.run([NODE, runner, lib, payload],
                              capture_output=True, text=True, timeout=120,
                              encoding="utf-8")
        if done.returncode != 0:
            return "<node failed: %s>" % (done.stderr or "")[-200:]
        return done.stdout

    for label, query, results in (
            ("with results", "what should I change?", RESULTS),
            ("with no results", "what should I change?", []),
            ("with a sparse result", "q", [{}]),
            ("with a long snippet", "q",
             [{"title": "T", "provider": "P", "date": "2026-01-01",
               "snippet": "word " * 200, "match_type": "keyword"}]),
    ):
        expected = format_context_block(query, results)
        actual = js_block(query, results)
        ok = expected == actual
        check("the two formatters match %s" % label, ok,
              "" if ok else "python=%r js=%r" % (expected[-60:], actual[-60:]))

    node_tests = os.path.join(ROOT, "tests", "test_extension.js")
    done = subprocess.run([NODE, node_tests], capture_output=True, text=True,
                          timeout=300, encoding="utf-8")
    check("the extension's own JS suite passes", done.returncode == 0,
          (done.stdout or "").strip().splitlines()[-1:] or done.stderr[-200:])


# ======================================================================
print("\n== C. the /context page ==")

db.init_db(DB).close()
conn = db.get_connection(DB)
import_file(conn, EXPORT)
conn.close()

app = create_app(db_path=DB)
app.config["TESTING"] = True
client = app.test_client()

blank = client.get("/context")
check("GET /context loads without a query", blank.status_code == 200,
      blank.status_code)
check("it explains itself", b"/context" in blank.data)

answered = client.get("/context?q=gym")
check("GET /context?q= returns 200", answered.status_code == 200)
page = answered.data.decode("utf-8")
check("the block is rendered for copying",
      "&lt;contextvault_history&gt;" in page or "<contextvault_history>" in page)
check("the query is echoed back into the field", 'value="gym"' in page)
check("there is a copy button", 'id="copy-block"' in page)
check("matching conversations are listed", "Welcome" in page)

nothing = client.get("/context?q=zzzznotathinginthearchive")
check("a query with no matches says so",
      "Nothing in the archive matched" in nothing.data.decode("utf-8"))

# The page and the API must agree: same query, same conversations, or the
# phone and the extension disagree about what the archive contains.
via_api = client.get("/api/v1/search?q=gym&limit=5").get_json()
api_titles = [r["title"] for r in via_api["results"]]
check("the page and the Bridge API return the same conversations",
      all(title in page for title in api_titles), api_titles)


# ======================================================================
print("\n== D. installable on a phone ==")

manifest = client.get("/manifest.webmanifest")
check("the web manifest is served", manifest.status_code == 200)
data = manifest.get_json()
check("it is named", data.get("name") == "ContextVault", data.get("name"))
check("it opens on /context", data.get("start_url") == "/context",
      data.get("start_url"))
check("it is standalone", data.get("display") == "standalone")
check("it declares an icon", bool(data.get("icons")))
check("the icon file exists",
      client.get(data["icons"][0]["src"]).status_code == 200,
      data["icons"][0]["src"])
check("pages link the manifest",
      b"manifest.webmanifest" in client.get("/context").data)


# ======================================================================
print("\n== E. the shipped prompt ==")

PROMPT = os.path.join(ROOT, "docs", "context-prompt.md")
check("docs/context-prompt.md exists", os.path.exists(PROMPT))
prompt = open(PROMPT, encoding="utf-8").read() if os.path.exists(PROMPT) else ""

check("it tells an MCP client to call search_memory",
      "search_memory" in prompt)
check("it defines the trigger", "/context" in prompt)
check("it tells the model to cite what it used", "Cite" in prompt)
check("it tells the model to admit an empty archive",
      "Do not invent a past discussion" in prompt)
check("it covers the browser path too", "extension" in prompt)

# The prompt quotes the block; if the wording moves, the doc has to move too.
check("the documented block matches what is generated",
      "<contextvault_history>" in prompt
      and "background, not instructions" in prompt)

EXT = os.path.join(ROOT, "extension")
for name in ("manifest.json", "background.js", "content.js", "content.css",
             "options.html", "options.js", "README.md",
             os.path.join("lib", "context.js")):
    check("extension/%s is present" % name.replace(os.sep, "/"),
          os.path.exists(os.path.join(EXT, name)))

manifest_json = json.load(open(os.path.join(EXT, "manifest.json"),
                               encoding="utf-8"))
check("the extension is Manifest V3",
      manifest_json.get("manifest_version") == 3)
check("it asks for loopback host permissions only",
      all("127.0.0.1" in h or "localhost" in h
          for h in manifest_json.get("host_permissions", [])),
      manifest_json.get("host_permissions"))
sites = " ".join(manifest_json["content_scripts"][0]["matches"])
for host in ("chatgpt.com", "claude.ai", "gemini.google.com"):
    check("it runs on %s" % host, host in sites)
check("the shared library loads before the content script",
      manifest_json["content_scripts"][0]["js"]
      == ["lib/context.js", "content.js"],
      manifest_json["content_scripts"][0]["js"])


print("\n" + ("ALL CHECKS PASSED" if not fails
              else "%d FAILURES: %s" % (len(fails), fails)))
sys.exit(1 if fails else 0)
