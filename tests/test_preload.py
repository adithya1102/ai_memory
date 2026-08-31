"""Background model preloading: startup speed, loading banner, degradation.

Part A drives the real server and times it.  Part B swaps in a stub encoder so
the slow-load and failed-load paths can be exercised deterministically instead
of racing a real 15-second load.
"""
import importlib.machinery
import json, os, shutil, subprocess, sys, tempfile, time, types
import urllib.error, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
EXPORT = os.path.join(ROOT, "dummy_export.json.json")
PORT = 5177
BASE = "http://127.0.0.1:%d" % PORT

fails = []
def check(label, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + label + (("  -> " + str(detail)) if detail else ""))
    if not ok:
        fails.append(label)


def get(path, timeout=60):
    t = time.time()
    body = urllib.request.urlopen(BASE + path, timeout=timeout).read().decode("utf-8", "replace")
    return body, time.time() - t


# ======================================================================
# Part A: the real app
# ======================================================================
print("== A. real server ==")
work = tempfile.mkdtemp()
DB = os.path.join(work, "preload.db")

# Seed a database that already has embeddings, so the banner is meaningful.
from backend.core import database as db
from backend.core import embeddings
from backend.core.importer import import_file
conn = db.init_db(DB)
import_file(conn, EXPORT)
seeded = conn.execute("SELECT COUNT(*) FROM chunk_vectors").fetchone()[0]
conn.close()
check("seed database has vectors", seeded == 12, seeded)

launch = time.time()
proc = subprocess.Popen([sys.executable, "backend/main.py", "--no-window",
                         "--port", str(PORT), "--db", DB], cwd=ROOT,
                        stdout=open(os.path.join(work, "server.log"), "wb"),
                        stderr=subprocess.STDOUT)
startup = None
for _ in range(400):
    try:
        urllib.request.urlopen(BASE, timeout=1)
        startup = time.time() - launch
        break
    except Exception:
        time.sleep(0.05)

try:
    check("server answered at all", startup is not None)
    print("     startup: %.2fs" % startup)
    # A cold model load is ~15s; startup must not be waiting on it.
    check("startup is fast (model not loaded inline)", startup < 10, "%.2fs" % startup)

    body, first = get("/search?q=how+do+I+get+stronger")
    print("     first search: %.2fs" % first)
    check("first search does not block on the model", first < 10, "%.2fs" % first)
    loading = "Loading embedding model" in body
    semantic = 'class="badge semantic"' in body
    check("first search either shows the banner or is already semantic",
          loading or semantic, "banner=%s semantic=%s" % (loading, semantic))

    # Wait for the background load to finish, then confirm it took effect.
    ready = False
    for _ in range(120):
        status = json.loads(get("/api/model-status")[0])
        if status["ready"]:
            ready = True
            break
        time.sleep(0.5)
    check("model becomes ready in the background", ready, status)

    body, second = get("/search?q=how+do+I+get+stronger")
    print("     second search: %.2fs" % second)
    check("subsequent search is fast", second < 3, "%.2fs" % second)
    check("subsequent search is semantic", 'class="badge semantic"' in body)
    check("banner gone once ready", "Loading embedding model" not in body)

    body, third = get("/search?q=gym+workout")
    check("a different query is also fast", third < 3, "%.2fs" % third)
finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()


# ======================================================================
# Part B: deterministic stub encoder
# ======================================================================
print("\n== B. stubbed loader ==")
REAL_ST = sys.modules.get("sentence_transformers")


def install_stub(on_load):
    module = types.ModuleType("sentence_transformers")
    # availability() uses find_spec(), which raises ValueError on a module
    # whose __spec__ is None.  A real installed module always has one.
    module.__spec__ = importlib.machinery.ModuleSpec(
        "sentence_transformers", loader=None)

    class StubModel:
        max_seq_length = 256

        def __init__(self, name, *a, **kw):
            on_load()
            self.tokenizer = None

        def encode(self, texts, **kw):
            import numpy as np
            return np.zeros((len(texts), embeddings.EMBEDDING_DIM), dtype="float32")

    module.SentenceTransformer = StubModel
    sys.modules["sentence_transformers"] = module


def reset_model_state():
    embeddings._model = None
    embeddings._model_state = "idle"
    embeddings._model_error = None


from backend.web.app import create_app
app = create_app(db_path=DB, imports_dir=work)
client = app.test_client()

# ---- slow load -------------------------------------------------------
print("\n-- slow load --")
install_stub(lambda: time.sleep(4))
reset_model_state()

t = time.time()
thread = embeddings.start_preload()
spawn = time.time() - t
check("start_preload returns immediately", spawn < 0.5, "%.3fs" % spawn)
check("state is 'loading'", embeddings.model_state()["state"] == "loading",
      embeddings.model_state())
check("get_model_if_ready() returns None while loading",
      embeddings.get_model_if_ready() is None)

t = time.time()
hits = embeddings.semantic_search(db.get_connection(DB), "stronger")
elapsed = time.time() - t
check("semantic_search does not block while loading", elapsed < 0.5, "%.3fs" % elapsed)
check("semantic_search returns [] while loading", hits == [])

t = time.time()
page = client.get("/search?q=how+do+I+get+stronger")
elapsed = time.time() - t
check("/search responds fast while loading", elapsed < 1.0, "%.3fs" % elapsed)
check("/search returns 200 while loading", page.status_code == 200)
check("loading banner is shown", b"Loading embedding model" in page.data)
check("banner has a manual retry link", b"retry now" in page.data)
check("auto-retry script is present", b"model-status" in page.data)
# The paraphrase query has no keyword match by design, so check the
# keyword half of the page with a query that does have one.
kw_page = client.get("/search?q=gym+workout")
check("keyword results still render while loading", b"Welcome" in kw_page.data)
check("those results are badged keyword", b'class="badge keyword"' in kw_page.data)
check("banner shown alongside keyword results",
      b"Loading embedding model" in kw_page.data)
status = json.loads(client.get("/api/model-status").data)
check("/api/model-status says not ready", status["ready"] is False, status)
check("/api/model-status reports loading", status["state"] == "loading", status)

thread.join(timeout=30)
check("state becomes 'ready' after load", embeddings.model_state()["ready"], embeddings.model_state())
check("get_model_if_ready() now returns the model",
      embeddings.get_model_if_ready() is not None)
page = client.get("/search?q=how+do+I+get+stronger")
check("banner gone after load", b"Loading embedding model" not in page.data)
status = json.loads(client.get("/api/model-status").data)
check("/api/model-status says ready", status["ready"] is True, status)

# ---- failed load -----------------------------------------------------
print("\n-- failed load --")
def boom():
    raise RuntimeError("simulated download failure")
install_stub(boom)
reset_model_state()

thread = embeddings.start_preload()
thread.join(timeout=30)
state = embeddings.model_state()
check("state is 'failed'", state["state"] == "failed", state)
check("failure reason is recorded", "simulated download failure" in state["reason"], state)
check("get_model_if_ready() returns None after failure",
      embeddings.get_model_if_ready() is None)
check("semantic_search returns [] after failure",
      embeddings.semantic_search(db.get_connection(DB), "stronger") == [])

page = client.get("/search?q=gym+workout")
check("/search still works after failure", page.status_code == 200)
check("keyword results still returned", b"Welcome" in page.data)
check("results badged keyword only", b'class="badge semantic"' not in page.data)
check("no loading banner after failure", b"Loading embedding model" not in page.data)
check("failure explained in the UI", b"unavailable" in page.data, page.data[:0])
status = json.loads(client.get("/api/model-status").data)
check("/api/model-status reports failed", status["state"] == "failed", status)
check("settings shows the failed state", b"failed" in client.get("/settings").data)

# A failed load must not be retried forever on every search.
check("start_preload retries after a failure (state was reset to failed)",
      embeddings.start_preload() is not None)

# ---- disabled toggle skips preloading --------------------------------
print("\n-- toggle off --")
reset_model_state()
conn = db.get_connection(DB)
from backend.core.search import SEMANTIC_ENABLED_KEY
db.set_flag(conn, SEMANTIC_ENABLED_KEY, False)
conn.close()
from backend.main import preload_model
check("preload skipped when semantic search is off", preload_model(DB) is None)
check("state untouched", embeddings.model_state()["state"] == "idle",
      embeddings.model_state())
conn = db.get_connection(DB)
db.set_flag(conn, SEMANTIC_ENABLED_KEY, True)
conn.close()
check("preload starts when semantic search is on", preload_model(DB) is not None)

if REAL_ST is not None:
    sys.modules["sentence_transformers"] = REAL_ST
else:
    sys.modules.pop("sentence_transformers", None)

print("\n" + ("ALL CHECKS PASSED" if not fails else "%d FAILURES: %s" % (len(fails), fails)))
sys.exit(1 if fails else 0)
